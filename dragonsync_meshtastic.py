#!/usr/bin/env python3
"""
DragonSync‑Meshtastic (ZMQ→Meshtastic with asyncio, smart throttling & stale cleanup)

MIT License

Copyright (c) 2025 Cemaxecuter LLC

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software are
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

__version__ = "1.0.0"

import argparse
import logging
import math
import re
import time
import asyncio

import zmq.asyncio
from meshtastic.protobuf import atak_pb2
import meshtastic.serial_interface

# --- Argument Parsing ---
parser = argparse.ArgumentParser(
    description="DragonSync‑Meshtastic: ZMQ→Meshtastic with asyncio, smart throttling & cleanup"
)
parser.add_argument('--port', help='Meshtastic serial port')
parser.add_argument('--zmq-host', default='127.0.0.1',
                    help='ZMQ server hostname or IP')
parser.add_argument('--zmq-drone-port', type=int, default=4224,
                    help='ZMQ port for drone telemetry')
parser.add_argument('--zmq-system-port', type=int, default=4225,
                    help='ZMQ port for system status')
parser.add_argument('-d', '--debug', action='store_true',
                    help='Show PLI/GeoChat protobuf dumps')
parser.add_argument('--meshtastic-debug', action='store_true',
                    help='Enable Meshtastic library logs')
args = parser.parse_args()

# --- Logging Setup ---
level = logging.DEBUG if args.debug else logging.INFO
logging.basicConfig(level=level,
                    format='%(asctime)s %(levelname)s %(message)s')
meshtastic_level = logging.DEBUG if args.meshtastic_debug else logging.CRITICAL
logging.getLogger('meshtastic').setLevel(meshtastic_level)
logger = logging.getLogger(__name__)

# --- Helpers ---
def safe_str(val, max_size):
    s = str(val) if val is not None else ''
    return s[:max_size]

def clamp_int(val, bits):
    max_val = (1 << bits) - 1
    try:
        ival = int(val)
    except (TypeError, ValueError):
        ival = 0
    return max(0, min(ival, max_val))

def parse_float(v, default=0.0):
    m = re.search(r'[-+]?\d*\.?\d+', str(v))
    if not m:
        return default
    try:
        return float(m.group(0))
    except ValueError:
        return default

def shorten_callsign(cs: str) -> str:
    prefixes = ['wardragon-', 'drone-', 'pilot-', 'home-']
    for p in prefixes:
        if cs.startswith(p):
            return p + cs[-4:]
    return cs[-4:] if len(cs) >= 4 else cs

def haversine(lat1, lon1, lat2, lon2):
    # return distance in meters between two lat/lon
    R = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

# --- Throttle & Cleanup Settings ---
DRONE_PLI_INTERVAL      = 5    # seconds between drone PLIs
PILOT_HOME_PLI_INTERVAL = 60   # seconds between pilot/home PLIs
DRONE_GEO_INTERVAL      = 10   # seconds between drone GeoChats
PILOT_HOME_GEO_INTERVAL = 30   # seconds between pilot/home GeoChats
SYSTEM_GEO_INTERVAL     = 10   # seconds between system GeoChats
STALE_TIMEOUT           = 300  # seconds before dropping stale
# ATAK default stale-out and margin for keep-alive
ATAK_STALE_TIMEOUT      = 300
STALE_MARGIN            = 30

# --- Global State ---
last_sent_pli     = {}        # uid -> timestamp
last_sent_geochat = {}        # uid -> timestamp
last_seen         = {}        # uid -> timestamp
latest_updates    = {}        # uid -> msg dict
mac_to_serial     = {}        # mac -> serial id
pending_caa       = {}        # mac -> caa id buffer
last_position     = {}        # uid -> (lat, lon)
tx_lock           = asyncio.Lock()

# --- Packet Builders ---
def build_atak_pli_packet(msg):
    pkt = atak_pb2.TAKPacket(is_compressed=False)
    sc = safe_str(shorten_callsign(msg['callsign']), 120)
    pkt.contact.callsign = pkt.contact.device_callsign = sc
    pkt.pli.latitude_i  = int(msg['lat'] * 1e7)
    pkt.pli.longitude_i = int(msg['lon'] * 1e7)
    pkt.pli.altitude    = int(msg['alt'])
    pkt.pli.speed       = clamp_int(msg.get('speed', 0), 16)
    pkt.pli.course      = clamp_int(msg.get('course', 0), 16)
    pkt.group.role      = atak_pb2.MemberRole.TeamMember
    pkt.group.team      = atak_pb2.Team.Cyan
    return pkt.SerializeToString()

def build_atak_geochat_packet(msg):
    pkt = atak_pb2.TAKPacket(is_compressed=False)
    full_cs = msg['callsign']
    header_cs = shorten_callsign(full_cs) if msg['type']=='system' else full_cs
    pkt.contact.callsign = pkt.contact.device_callsign = safe_str(header_cs, 120)
    pkt.pli.latitude_i  = int(msg['lat'] * 1e7)
    pkt.pli.longitude_i = int(msg['lon'] * 1e7)
    pkt.pli.altitude    = int(msg['alt'])

    if msg['type']=='system':
        remarks     = msg.get('remarks','')
        cpu_m       = re.search(r"CPU Usage:\s*([\d\.]+)%", remarks)
        tmp_m       = re.search(r"Temp:\s*([\d\.]+)°C",   remarks)
        pluto_m     = re.search(r"Pluto:\s*([\w\./]+)",   remarks)
        zynq_m      = re.search(r"Zynq:\s*([\w\./]+)",    remarks)
        text = (
            f"{shorten_callsign(full_cs)} | "
            f"CPU: {cpu_m.group(1) if cpu_m else 'N/A'}% | "
            f"Temp: {tmp_m.group(1) if tmp_m else 'N/A'}°C | "
            f"Pluto: {pluto_m.group(1) if pluto_m else 'N/A'} | "
            f"Zynq: {zynq_m.group(1) if zynq_m else 'N/A'}"
        )
    elif msg['type']=='drone':
        text = (
            f"{full_cs} | RSSI: {msg.get('rssi','N/A')} dBm | "
            f"MAC: {msg.get('mac','N/A')}"
        )
    else:  # pilot or home
        text = full_cs

    pkt.chat.message     = safe_str(text, 256)
    pkt.chat.to          = pkt.chat.to_callsign = "All Chat Rooms"
    pkt.group.role       = atak_pb2.MemberRole.TeamMember
    pkt.group.team       = atak_pb2.Team.Cyan
    return pkt.SerializeToString()

def debug_proto(label, data):
    pkt = atak_pb2.TAKPacket()
    pkt.ParseFromString(data)
    logger.debug(f"--- {label} ---\n{pkt}")

# --- ZMQ Parsers ---
def parse_zmq_drone(raw):
    info = {'lat':0.0,'lon':0.0,'alt':0.0,'speed':0.0,'rssi':None,'mac':None,'id':None}
    pilot = {}
    items = raw if isinstance(raw, list) else [raw]
    for itm in items:
        if not isinstance(itm, dict): continue
        if 'Basic ID' in itm:
            b = itm['Basic ID']
            mac, idt = b.get('MAC'), b.get('id_type')
            info['mac'], info['rssi'] = mac, b.get('RSSI')
            if idt == 'Serial Number (ANSI/CTA-2063-A)':
                serial = b.get('id','unknown')
                mac_to_serial[mac] = serial
                if mac in pending_caa:
                    info['caa'] = pending_caa.pop(mac)
                info['id'] = serial
            elif idt == 'CAA Assigned Registration ID':
                caa = b.get('id','unknown')
                if mac in mac_to_serial:
                    info['caa'] = caa
                else:
                    pending_caa[mac] = caa
                    return []
        if 'Location/Vector Message' in itm:
            v = itm['Location/Vector Message']
            info.update({
                'lat':   parse_float(v.get('latitude')),
                'lon':   parse_float(v.get('longitude')),
                'alt':   parse_float(v.get('geodetic_altitude')),
                'speed': parse_float(v.get('speed')),
            })
        if 'System Message' in itm:
            sm = itm['System Message']
            pilot = {
                'pilot_lat': parse_float(sm.get('latitude') or sm.get('operator_lat')),
                'pilot_lon': parse_float(sm.get('longitude') or sm.get('operator_lon')),
                'home_lat':  parse_float(sm.get('home_lat')),
                'home_lon':  parse_float(sm.get('home_lon')),
            }
    if not info.get('id'):
        return []
    cid = info['id']
    if not cid.startswith('drone-'):
        cid = f"drone-{cid}"
    msgs = []
    remark = f"MAC:{info['mac']} RSSI:{info['rssi']}dBm"
    entry = {
        'callsign': cid, 'type':'drone',
        'lat':info['lat'],'lon':info['lon'],'alt':info['alt'],
        'speed':info['speed'],'remarks':remark,
        'mac':info['mac'],'rssi':info['rssi'],
    }
    if 'caa' in info:
        entry['caa'] = info['caa']
    msgs.append(entry)
    if pilot.get('pilot_lat') or pilot.get('pilot_lon'):
        pid = cid.split('-',1)[1]
        msgs.append({
            'callsign': f'pilot-{pid}','type':'pilot',
            'lat':pilot['pilot_lat'],'lon':pilot['pilot_lon'],
            'alt':0,'speed':0,'remarks':f"refs {cid}"
        })
    if pilot.get('home_lat') or pilot.get('home_lon'):
        pid = cid.split('-',1)[1]
        msgs.append({
            'callsign': f'home-{pid}','type':'home',
            'lat':pilot['home_lat'],'lon':pilot['home_lon'],
            'alt':0,'speed':0,'remarks':f"refs {cid}"
        })
    return msgs

def parse_zmq_system(raw):
    serial = raw.get('serial_number','unknown')
    uid    = f'wardragon-{serial}'
    gps    = raw.get('gps_data',{})
    stats  = raw.get('system_stats',{})
    temps  = raw.get('ant_sdr_temps',{})
    remarks= (
        f"CPU Usage: {parse_float(stats.get('cpu_usage'))}% "
        f"Temp: {parse_float(stats.get('temperature'))}°C "
        f"Pluto: {temps.get('pluto_temp','N/A')} "
        f"Zynq: {temps.get('zynq_temp','N/A')}"
    )
    return [{
        'callsign': uid,'type':'system',
        'lat':parse_float(gps.get('latitude')),
        'lon':parse_float(gps.get('longitude')),
        'alt':parse_float(gps.get('altitude')),
        'speed':0,'remarks':remarks
    }]

# --- Async Send Logic ---
async def send_packets_async(msg, iface, debug=False):
    uid = shorten_callsign(msg['callsign'])
    now = time.time()

    # movement detection
    prev = last_position.get(uid)
    if msg['type']=='drone':
        moving = msg.get('speed',0) > 0.5
    else:
        if prev:
            moving = haversine(prev[0], prev[1], msg['lat'], msg['lon']) > 5
        else:
            moving = True
    last_position[uid] = (msg['lat'], msg['lon'])

    # PLI throttle with keep-alive near stale
    pli_int = DRONE_PLI_INTERVAL if msg['type']=='drone' else PILOT_HOME_PLI_INTERVAL
    last_pli = last_sent_pli.get(uid, 0)
    t_since = now - last_pli
    force_keepalive = (
        not moving
        and last_pli > 0
        and t_since >= (ATAK_STALE_TIMEOUT - STALE_MARGIN)
    )
    if (moving or force_keepalive) and t_since >= pli_int:
        data = build_atak_pli_packet(msg)
        if debug:
            debug_proto(f"PLI {uid}", data)
        async with tx_lock:
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: iface.sendData(data, portNum=72, wantAck=False)
            )
        last_sent_pli[uid] = now
        logger.info(f"Sent PLI for {uid}" + (" (keepalive)" if force_keepalive else ""))

    # GeoChat throttle
    if msg['type']=='drone':
        geo_int = DRONE_GEO_INTERVAL
    elif msg['type']=='system':
        geo_int = SYSTEM_GEO_INTERVAL
    else:
        geo_int = PILOT_HOME_GEO_INTERVAL

    if now - last_sent_geochat.get(uid,0) >= geo_int:
        data = build_atak_geochat_packet(msg)
        if debug:
            debug_proto(f"GeoChat {uid}", data)
        async with tx_lock:
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: iface.sendData(data, portNum=72, wantAck=False)
            )
        last_sent_geochat[uid] = now
        logger.info(f"Sent GeoChat for {uid}")

# --- ZMQ Listeners & Flusher ---
async def drone_listener(host, port):
    ctx = zmq.asyncio.Context()
    sock = ctx.socket(zmq.SUB)
    sock.connect(f"tcp://{host}:{port}")
    sock.setsockopt_string(zmq.SUBSCRIBE,'')
    logger.info(f"Subscribed to drone ZMQ at {host}:{port}")
    while True:
        raw = await sock.recv_json()
        for m in parse_zmq_drone(raw):
            uid = shorten_callsign(m['callsign'])
            latest_updates[uid] = m
            last_seen[uid] = time.time()

async def system_listener(host, port):
    ctx = zmq.asyncio.Context()
    sock = ctx.socket(zmq.SUB)
    sock.connect(f"tcp://{host}:{port}")
    sock.setsockopt_string(zmq.SUBSCRIBE,'')
    logger.info(f"Subscribed to system ZMQ at {host}:{port}")
    while True:
        raw = await sock.recv_json()
        for m in parse_zmq_system(raw):
            uid = shorten_callsign(m['callsign'])
            latest_updates[uid] = m
            last_seen[uid] = time.time()
        await asyncio.sleep(0)

async def flush_updates(iface, debug=False):
    while True:
        now = time.time()
        for uid in list(last_seen):
            if now - last_seen[uid] > STALE_TIMEOUT:
                last_seen.pop(uid, None)
                latest_updates.pop(uid, None)
                logger.info(f"Dropped stale {uid}")
        for uid, msg in list(latest_updates.items()):
            latest_updates.pop(uid, None)
            await send_packets_async(msg, iface, debug)
        await asyncio.sleep(1)

async def main():
    iface = (meshtastic.serial_interface.SerialInterface(devPath=args.port)
             if args.port else
             meshtastic.serial_interface.SerialInterface())
    logger.info('Meshtastic interface ready')
    await asyncio.gather(
        drone_listener(args.zmq_host,      args.zmq_drone_port),
        system_listener(args.zmq_host,     args.zmq_system_port),
        flush_updates(iface, args.debug)
    )

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info('Stopping…')
