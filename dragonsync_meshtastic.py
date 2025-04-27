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
import re
import time
import asyncio

import zmq.asyncio
from meshtastic.protobuf import atak_pb2
import meshtastic.serial_interface

# --- Argument Parsing ---
parser = argparse.ArgumentParser(
    description="DragonSync-Meshtastic: ZMQ→Meshtastic with asyncio, full throttling & cleanup"
)
parser.add_argument('--port', help='Meshtastic serial port')
parser.add_argument('--zmq-host', default='127.0.0.1', help='ZMQ server hostname or IP')
parser.add_argument('--zmq-drone-port', type=int, default=4224, help='ZMQ port for drone telemetry')
parser.add_argument('--zmq-system-port', type=int, default=4225, help='ZMQ port for system status')
parser.add_argument('-d', '--debug', action='store_true', help='Show PLI/GeoChat dumps')
parser.add_argument('--meshtastic-debug', action='store_true', help='Enable Meshtastic logs')
args = parser.parse_args()

# --- Logging Setup ---
level = logging.DEBUG if args.debug else logging.INFO
logging.basicConfig(level=level, format='%(asctime)s %(levelname)s %(message)s')
logging.getLogger('meshtastic').setLevel(logging.DEBUG if args.meshtastic_debug else logging.CRITICAL)
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

# --- Throttle & Threshold Settings ---
DRONE_PLI_INTERVAL      = 5    # secs between drone PLIs
PILOT_HOME_PLI_INTERVAL = 60
DRONE_GEO_HEARTBEAT     = 60   # secs between GeoChats if no event
SYSTEM_GEO_HEARTBEAT    = 60
PILOT_HOME_GEO_INTERVAL = 30
STALE_TIMEOUT           = 300  # drop after 5 min silent

DRONE_RSSI_THRESH       = 5.0   # dB change to trigger GeoChat
SYSTEM_CPU_THRESH       = 10.0  # % change to trigger GeoChat
SYSTEM_TEMP_THRESH      = 2.0   # °C change to trigger GeoChat

# --- Global State ---
last_sent_pli   = {}   # uid -> timestamp
last_sent_geo   = {}   # uid -> timestamp
last_metrics    = {}   # uid -> dict of previous numeric metrics
last_seen       = {}   # uid -> timestamp
latest_updates  = {}   # uid -> msg dict
mac_to_serial   = {}
pending_caa     = {}
tx_lock         = asyncio.Lock()

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
    sc = safe_str(shorten_callsign(msg['callsign']), 120)
    pkt.contact.callsign = pkt.contact.device_callsign = sc

    # keep icon alive on map
    pkt.pli.latitude_i  = int(msg['lat'] * 1e7)
    pkt.pli.longitude_i = int(msg['lon'] * 1e7)
    pkt.pli.altitude    = int(msg['alt'])

    text = msg.get('remarks', '')
    pkt.chat.message     = safe_str(text, 256)
    pkt.chat.to          = pkt.chat.to_callsign = "All Chat Rooms"
    pkt.group.role       = atak_pb2.MemberRole.TeamMember
    pkt.group.team       = atak_pb2.Team.Cyan
    return pkt.SerializeToString()

# --- ZMQ Parsers ---
def parse_zmq_drone(raw):
    info = {'lat':0.0,'lon':0.0,'alt':0.0,'speed':0.0,'course':0.0,'rssi':0.0,'mac':None,'id':None}
    pilot = {}
    items = raw if isinstance(raw, list) else [raw]
    for itm in items:
        if not isinstance(itm, dict):
            continue

        if 'Basic ID' in itm:
            b = itm['Basic ID']
            mac = b.get('MAC'); rssi = parse_float(b.get('RSSI'))
            info['mac']  = mac
            info['rssi'] = rssi
            idt = b.get('id_type')
            if idt == 'Serial Number (ANSI/CTA-2063-A)':
                serial = b.get('id','unknown')
                info['id'] = serial
                mac_to_serial[mac] = serial
                if mac in pending_caa:
                    info['caa'] = pending_caa.pop(mac)
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
                'lat':    parse_float(v.get('latitude')),
                'lon':    parse_float(v.get('longitude')),
                'alt':    parse_float(v.get('geodetic_altitude')),
                'speed':  parse_float(v.get('speed')),
                'course': parse_float(v.get('direction')),
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
        cid = f'drone-{cid}'

    rssi = info['rssi']
    mac  = info.get('mac','N/A')
    remark = f"RSSI:{rssi:.0f}dBm MAC:{mac}"

    entry = {
        'callsign': cid,
        'type':     'drone',
        'lat':      info['lat'],
        'lon':      info['lon'],
        'alt':      info['alt'],
        'speed':    info['speed'],
        'course':   info['course'],
        'rssi':     rssi,
        'mac':      mac,
        'remarks':  remark,
    }
    if 'caa' in info:
        entry['caa'] = info['caa']

    msgs = [entry]

    if pilot.get('pilot_lat') or pilot.get('pilot_lon'):
        pid = cid.split('-',1)[1]
        msgs.append({
            'callsign': f'pilot-{pid}',
            'type':     'pilot',
            'lat':      pilot['pilot_lat'],
            'lon':      pilot['pilot_lon'],
            'alt':      0,
            'speed':    0,
            'remarks':  f"refs {cid}",
        })
    if pilot.get('home_lat') or pilot.get('home_lon'):
        pid = cid.split('-',1)[1]
        msgs.append({
            'callsign': f'home-{pid}',
            'type':     'home',
            'lat':      pilot['home_lat'],
            'lon':      pilot['home_lon'],
            'alt':      0,
            'speed':    0,
            'remarks':  f"refs {cid}",
        })

    return msgs

def parse_zmq_system(raw):
    serial = raw.get('serial_number','unknown')
    uid    = f'wardragon-{serial}'
    gps    = raw.get('gps_data',{})
    stats  = raw.get('system_stats',{})
    temps  = raw.get('ant_sdr_temps',{})

    lat    = parse_float(gps.get('latitude'))
    lon    = parse_float(gps.get('longitude'))
    alt    = parse_float(gps.get('altitude'))
    speed  = parse_float(gps.get('speed'))
    course = parse_float(gps.get('direction'))

    cpu  = parse_float(stats.get('cpu_usage'))
    temp = parse_float(stats.get('temperature'))
    remark = f"CPU:{cpu:.0f}% Temp:{temp:.0f}°C Pluto:{temps.get('pluto_temp','N/A')} Zynq:{temps.get('zynq_temp','N/A')}"

    entry = {
        'callsign': uid,
        'type':     'system',
        'lat':      lat,
        'lon':      lon,
        'alt':      alt,
        'speed':    speed,
        'course':   course,
        'cpu':      cpu,
        'temp':     temp,
        'remarks':  remark,
    }
    return [entry]

# --- Async Send Logic ---
async def send_packets_async(msg, iface, debug=False):
    uid = shorten_callsign(msg['callsign'])
    now = time.time()

    # PLI throttle
    pli_int = DRONE_PLI_INTERVAL if msg['type']=='drone' else PILOT_HOME_PLI_INTERVAL
    if now - last_sent_pli.get(uid,0) >= pli_int:
        data = build_atak_pli_packet(msg)
        if debug:
            pkt = atak_pb2.TAKPacket(); pkt.ParseFromString(data)
            logger.debug(f"PLI {uid} → {pkt}")
        async with tx_lock:
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: iface.sendData(data, portNum=72, wantAck=False)
            )
        last_sent_pli[uid] = now

    # GeoChat event-driven + heartbeat
    if msg['type']=='drone':
        hb = DRONE_GEO_HEARTBEAT
        # check RSSI change
        prev = last_metrics.get(uid,{}).get('rssi')
        curr = msg.get('rssi',0.0)
        event = (prev is None) or (abs(curr - prev) >= DRONE_RSSI_THRESH)
        last_metrics.setdefault(uid, {})['rssi'] = curr

    elif msg['type']=='system':
        hb = SYSTEM_GEO_HEARTBEAT
        prev_cpu  = last_metrics.get(uid,{}).get('cpu')
        prev_temp = last_metrics.get(uid,{}).get('temp')
        curr_cpu  = msg.get('cpu',0.0)
        curr_temp = msg.get('temp',0.0)
        event = (prev_cpu is None or abs(curr_cpu - prev_cpu) >= SYSTEM_CPU_THRESH) or \
                (prev_temp is None or abs(curr_temp - prev_temp) >= SYSTEM_TEMP_THRESH)
        last_metrics.setdefault(uid, {}).update(cpu=curr_cpu, temp=curr_temp)

    else:
        hb = PILOT_HOME_GEO_INTERVAL
        event = False

    # heartbeat fallback
    if now - last_sent_geo.get(uid,0) >= hb:
        event = True

    if event:
        data = build_atak_geochat_packet(msg)
        if debug:
            pkt = atak_pb2.TAKPacket(); pkt.ParseFromString(data)
            logger.debug(f"GeoChat {uid} → {pkt}")
        async with tx_lock:
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: iface.sendData(data, portNum=72, wantAck=False)
            )
        last_sent_geo[uid] = now

# --- ZMQ Listeners & Flusher ---
async def drone_listener(host, port):
    ctx  = zmq.asyncio.Context()
    sock = ctx.socket(zmq.SUB)
    sock.connect(f"tcp://{host}:{port}")
    sock.setsockopt_string(zmq.SUBSCRIBE,'')
    logger.info(f"Subscribed to drone ZMQ at {host}:{port}")
    while True:
        raw = await sock.recv_json()
        for m in parse_zmq_drone(raw):
            uid = shorten_callsign(m['callsign'])
            latest_updates[uid] = m
            last_seen[uid]      = time.time()

async def system_listener(host, port):
    ctx  = zmq.asyncio.Context()
    sock = ctx.socket(zmq.SUB)
    sock.connect(f"tcp://{host}:{port}")
    sock.setsockopt_string(zmq.SUBSCRIBE,'')
    logger.info(f"Subscribed to system ZMQ at {host}:{port}")
    while True:
        raw = await sock.recv_json()
        for m in parse_zmq_system(raw):
            uid = shorten_callsign(m['callsign'])
            latest_updates[uid] = m
            last_seen[uid]      = time.time()

async def flush_updates(iface, debug=False):
    while True:
        now = time.time()
        # drop stale
        for uid in list(last_seen):
            if now - last_seen[uid] > STALE_TIMEOUT:
                last_seen.pop(uid,None)
                latest_updates.pop(uid,None)
                last_metrics.pop(uid,None)
                last_sent_pli.pop(uid,None)
                last_sent_geo.pop(uid,None)
                logger.info(f"Dropped stale {uid}")
        # send pending
        for uid, msg in list(latest_updates.items()):
            latest_updates.pop(uid,None)
            await send_packets_async(msg, iface, debug)
        await asyncio.sleep(1)

async def main():
    iface = (meshtastic.serial_interface.SerialInterface(devPath=args.port)
             if args.port else
             meshtastic.serial_interface.SerialInterface())
    logger.info("Meshtastic interface ready")
    await asyncio.gather(
        drone_listener(args.zmq_host, args.zmq_drone_port),
        system_listener(args.zmq_host, args.zmq_system_port),
        flush_updates(iface, args.debug)
    )

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Stopping…")
