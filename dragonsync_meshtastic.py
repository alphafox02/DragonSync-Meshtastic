#!/usr/bin/env python3
"""
MIT License

Copyright (c) 2025 Cemaxecuter LLC

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
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

import argparse
import json
import logging
import re
import socket
import struct
import xmltodict
import time
import math
import asyncio
import zmq
import serial

from gps3 import gps3

# Import ATAK protobuf definitions
from meshtastic.protobuf import atak_pb2


# --- Helper Functions ---
def safe_str(val, max_size):
    """Convert a value to a string and truncate to max_size characters."""
    s = str(val) if val is not None else ""
    return s[:max_size]


def clamp_int(val, bits):
    """Clamp an integer to the maximum value allowed for 'bits' bits (unsigned)."""
    max_val = (1 << bits) - 1
    return max(0, min(int(val), max_val))


def shorten_callsign(callsign):
    """
    Return a shortened version of the callsign.
    For callsigns starting with 'wardragon-' or 'drone-', return the prefix plus the last 4 characters.
    For example, 'wardragon-00e04c3618a3' becomes 'wardragon-18a3'.
    """
    if callsign.startswith("wardragon-"):
        return "wardragon-" + callsign[-4:]
    elif callsign.startswith("drone-"):
        return "drone-" + callsign[-4:]
    else:
        return callsign[-4:] if len(callsign) >= 4 else callsign


# --- GPS Functions ---
def init_gps_connection():
    """Initialize and return a GPSDSocket and DataStream instance."""
    gps_socket = gps3.GPSDSocket()
    data_stream = gps3.DataStream()
    try:
        gps_socket.connect(host="127.0.0.1", port=2947)
        gps_socket.watch()
        logging.info("GPS connection established.")
    except Exception as e:
        logging.error("Failed to initialize GPS connection: %s", e)
        gps_socket, data_stream = None, None
    return gps_socket, data_stream


def get_gps_location(gps_socket, data_stream):
    """
    Attempt to get GPS location from the gpsd connection.
    Returns a tuple (lat, lon) or (0.0, 0.0) on failure.
    """
    try:
        new_data = next(gps_socket)
        if new_data:
            data_stream.unpack(new_data)
            lat = data_stream.TPV.get("lat", 0.0)
            lon = data_stream.TPV.get("lon", 0.0)
            return lat, lon
    except Exception as e:
        logging.error("Error getting GPS location: %s", e)
    return 0.0, 0.0


# --- Global Throttling & State ---
GEO_CHAT_INTERVAL = 10  # seconds
last_geo_chat_sent = {}
latest_updates = {}  # Latest update for each unique (shortened) callsign
tx_lock = asyncio.Lock()

# Global GPS state variables
gps_socket = None
data_stream = None
static_lat = 0.0
static_lon = 0.0

# --- Command-Line Argument Parsing ---
parser = argparse.ArgumentParser(
    description=(
        "Meshtastic Duplex: Async CoT listener (for TX) and radio FPV receiver with GPS "
        "enrichment and ZMQ publishing. Use --fpv to enable FPV features."
    )
)
parser.add_argument("--port", type=str, default=None,
                    help="Serial device for Meshtastic (e.g., /dev/ttyACM0).")
parser.add_argument("--mcast", type=str, default="239.2.3.1",
                    help="Multicast group IP (default: 239.2.3.1).")
parser.add_argument("--mcast-port", type=int, default=6969,
                    help="Multicast port (default: 6969).")
parser.add_argument("--zmq-port", type=int, default=4020,
                    help="ZMQ port to publish FPV messages (default: 4020).")
parser.add_argument("--stationary", action="store_true",
                    help="If set, use static GPS coordinates obtained at startup.")
parser.add_argument("--fpv", action="store_true",
                    help="Enable FPV reception features (radio receive, GPS enrichment, ZMQ publishing).")
parser.add_argument("--fpv-port", type=str, default=None,
                    help="Serial port for FPV messages (e.g., /dev/ttyUSB0).")
parser.add_argument("--fpv-baud", type=int, default=115200,
                    help="Baud rate for FPV serial messages (default: 115200).")
args = parser.parse_args()

logging.basicConfig(level=logging.INFO)

# --- Meshtastic Interface ---
import meshtastic.serial_interface
if args.port:
    logging.info(f"Using Meshtastic device (devPath): {args.port}")
    interface = meshtastic.serial_interface.SerialInterface(devPath=args.port)
else:
    logging.info("No Meshtastic device specified; using auto-detection.")
    interface = meshtastic.serial_interface.SerialInterface()
logging.info("Meshtastic interface created successfully.")

# --- UDP Multicast Setup (for CoT messages) ---
MCAST_GRP = args.mcast
MCAST_PORT = args.mcast_port
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind((MCAST_GRP, MCAST_PORT))
mreq = struct.pack("4sl", socket.inet_aton(MCAST_GRP), socket.INADDR_ANY)
sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
logging.info("Multicast socket bound on %s:%d", MCAST_GRP, MCAST_PORT)

# --- ZMQ Publisher Setup (for FPV messages) ---
zmq_context = zmq.Context()
zmq_socket = zmq_context.socket(zmq.PUB)
zmq_socket.setsockopt(zmq.SNDHWM, 1000)
zmq_socket.setsockopt(zmq.LINGER, 0)
zmq_endpoint = f"tcp://0.0.0.0:{args.zmq_port}"
try:
    zmq_socket.bind(zmq_endpoint)
    logging.info("ZMQ PUB socket bound to %s", zmq_endpoint)
except zmq.ZMQError as e:
    logging.error("Failed to bind ZMQ socket: %s", e)
    exit(1)

# --- Initialize GPS for FPV Enrichment ---
if args.stationary:
    gps_socket, data_stream = init_gps_connection()
    static_lat, static_lon = get_gps_location(gps_socket, data_stream)
    if gps_socket:
        gps_socket.close()
        gps_socket, data_stream = None, None
    logging.info("Using static GPS coordinates: lat=%s, lon=%s", static_lat, static_lon)
else:
    gps_socket, data_stream = init_gps_connection()


# --- CoT Parsing ---
def parse_cot(xml_data):
    """
    Parse a CoT (Cursor-on-Target) XML message and return a list of message dicts.
    For 'drone-' events, build a drone message.
    For 'wardragon-' events, build a system message.
    """
    cot_dict = xmltodict.parse(xml_data)
    event = cot_dict.get("event", {})
    uid = event.get("@uid", "unknown")
    point = event.get("point", {})
    detail = event.get("detail", {})
    callsign = detail.get("contact", {}).get("@callsign", uid)
    lat = float(point.get("@lat", 0))
    lon = float(point.get("@lon", 0))
    alt = float(point.get("@hae", 0))
    remarks = detail.get("remarks", "")
    messages = []
    if uid.startswith("drone-"):
        msg = {
            "callsign": callsign,
            "lat": lat,
            "lon": lon,
            "alt": alt,
            "remarks": remarks,
            "type": "drone"
        }
        messages.append(msg)
    elif uid.startswith("wardragon-"):
        msg = {
            "callsign": callsign,
            "lat": lat,
            "lon": lon,
            "alt": alt,
            "remarks": remarks,
            "type": "system"
        }
        messages.append(msg)
    else:
        msg = {
            "callsign": callsign,
            "lat": lat,
            "lon": lon,
            "alt": alt,
            "remarks": remarks,
            "type": "unknown"
        }
        messages.append(msg)
        logging.warning("Received unknown type; processing as generic PLI.")
    for msg in messages:
        logging.info("Parsed CoT message: %s", msg)
    return messages


# --- ATAK Packet Builders ---
def build_atak_pli_packet(msg):
    """
    Build a TAKPacket with a PLI payload from a CoT message.
    Uses the shortened callsign.
    """
    if msg["type"] == "unknown":
        logging.warning("Unknown message type; skipping TAKPacket construction.")
        return None
    packet = atak_pb2.TAKPacket()
    packet.is_compressed = False
    short_callsign = shorten_callsign(msg["callsign"])
    packet.contact.callsign = safe_str(short_callsign, 120)
    packet.contact.device_callsign = safe_str(short_callsign, 120)
    packet.pli.latitude_i = int(msg["lat"] * 1e7)
    packet.pli.longitude_i = int(msg["lon"] * 1e7)
    packet.pli.altitude = int(msg["alt"])
    packet.pli.speed = clamp_int(msg.get("speed", 0), 16)
    packet.pli.course = clamp_int(msg.get("course", 0), 16)
    packet.group.role = atak_pb2.MemberRole.TeamMember
    packet.group.team = atak_pb2.Team.Cyan
    logging.info("Constructed PLI: lat=%d, lon=%d, alt=%d, course=%d",
                 packet.pli.latitude_i, packet.pli.longitude_i,
                 packet.pli.altitude, packet.pli.course)
    serialized = packet.SerializeToString()
    logging.info("Serialized TAKPacket (PLI), length: %d bytes", len(serialized))
    return serialized


def build_atak_geochat_packet(msg):
    """
    Build a TAKPacket with a GeoChat payload for system messages.
    Extracts key metrics (CPU, Temperature, AD936X, Zynq) from remarks.
    """
    packet = atak_pb2.TAKPacket()
    packet.is_compressed = False
    short_callsign = shorten_callsign(msg["callsign"])
    packet.contact.callsign = safe_str(short_callsign, 120)
    packet.contact.device_callsign = safe_str(short_callsign, 120)
    packet.pli.latitude_i = int(msg["lat"] * 1e7)
    packet.pli.longitude_i = int(msg["lon"] * 1e7)
    packet.pli.altitude = int(msg["alt"])
    remarks = msg.get("remarks", "")
    cpu_match = re.search(r"CPU Usage:\s*([\d\.]+)%", remarks)
    temp_match = re.search(r"Temperature:\s*([\d\.]+)°C", remarks)
    ad936x_match = re.search(r"(?:Pluto|AD936X)\s*Temp:\s*([\w./]+)", remarks)
    zynq_match = re.search(r"Zynq Temp:\s*([\w./]+)", remarks)
    cpu_val = cpu_match.group(1) if cpu_match else "N/A"
    temp_val = temp_match.group(1) if temp_match else "N/A"
    ad936x_val = ad936x_match.group(1) if ad936x_match else "N/A"
    zynq_val = zynq_match.group(1) if zynq_match else "N/A"
    detailed_message = (
        f"{short_callsign} | CPU: {cpu_val}% | Temp: {temp_val}°C | "
        f"AD936X: {ad936x_val} | Zynq: {zynq_val}"
    )
    packet.chat.message = safe_str(detailed_message, 256)
    packet.chat.to = safe_str("All Chat Rooms", 120)
    packet.chat.to_callsign = safe_str("All Chat Rooms", 120)
    packet.group.role = atak_pb2.MemberRole.TeamMember
    packet.group.team = atak_pb2.Team.Cyan
    logging.info("Constructed GeoChat: %s", packet.chat.message)
    serialized = packet.SerializeToString()
    logging.info("Serialized TAKPacket (GeoChat), length: %d bytes", len(serialized))
    return serialized


# --- FPV Serial Reading (for --fpv mode) ---
RECONNECT_DELAY = 5  # seconds for FPV serial reconnection


def read_fpv_serial(fpv_port, baud):
    """A generator that continuously reads lines from the FPV serial port."""
    while True:
        try:
            with serial.Serial(fpv_port, baud, timeout=1) as ser:
                logging.info("Connected to FPV serial %s at %d baud.", fpv_port, baud)
                while True:
                    line = ser.readline().decode("utf-8", errors="replace").strip()
                    if line:
                        logging.debug("FPV raw data: %s", line)
                        yield line
        except serial.SerialException as e:
            logging.error("FPV serial error: %s. Reconnecting in %d seconds...", e, RECONNECT_DELAY)
            time.sleep(RECONNECT_DELAY)
        except Exception as e:
            logging.exception("Unexpected FPV error: %s", e)
            time.sleep(RECONNECT_DELAY)


# --- Asynchronous FPV Receiver ---
async def fpv_receiver(fpv_port, baud):
    """
    Asynchronously read FPV messages from the specified serial port using pyserial,
    enrich with GPS data, and publish via ZMQ.
    """
    loop = asyncio.get_running_loop()
    fpv_gen = read_fpv_serial(fpv_port, baud)
    while True:
        # Run the generator in an executor so as not to block the event loop.
        try:
            line = await loop.run_in_executor(None, next, fpv_gen)
        except StopIteration:
            continue
        logging.info(f"FPV received: {line}")
        process_rx_data(line)


def process_rx_data(rx_data):
    """
    Process received FPV data.
    Attempt to parse JSON; if successful, enrich it with GPS coordinates
    (either live or static) and publish it via ZMQ.
    """
    try:
        msg = json.loads(rx_data)
    except json.JSONDecodeError:
        logging.warning("Failed to parse FPV JSON: %s", rx_data)
        return
    if not args.stationary and gps_socket and data_stream:
        lat, lon = get_gps_location(gps_socket, data_stream)
    else:
        lat, lon = static_lat, static_lon
    msg["gps_lat"] = lat
    msg["gps_lon"] = lon
    try:
        zmq_socket.send_string(json.dumps(msg))
        logging.info("Published FPV message via ZMQ.")
    except Exception as e:
        logging.error("Error publishing FPV message via ZMQ: %s", e)


# --- Asynchronous Receiver for CoT (UDP) ---
async def cot_receiver():
    """Continuously receive CoT messages from UDP multicast and update latest_updates."""
    loop = asyncio.get_running_loop()
    while True:
        data, addr = await loop.run_in_executor(None, sock.recvfrom, 8192)
        logging.debug(f"Received CoT from {addr}")
        messages = parse_cot(data.decode("utf-8"))
        for msg in messages:
            unique_id = shorten_callsign(msg["callsign"])
            latest_updates[unique_id] = msg


# --- Periodic Flusher for CoT Updates ---
async def flush_updates(interval=1):
    """Every interval seconds, send out the latest update for each unique transmitter."""
    while True:
        if latest_updates:
            keys = list(latest_updates.keys())
            for key in keys:
                msg = latest_updates.pop(key, None)
                if msg is not None:
                    await send_packets_async(msg)
        await asyncio.sleep(interval)


# --- Asynchronous Sender for CoT Updates ---
async def send_packets_async(msg):
    """
    Asynchronously send a PLI packet from a CoT message and, for system messages,
    send a throttled GeoChat packet.
    """
    pli_packet = build_atak_pli_packet(msg)
    if pli_packet is not None:
        async with tx_lock:
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: interface.sendData(pli_packet, portNum=72, wantAck=False)
                )
                logging.info("Sent ATAK PLI packet.")
            except Exception as e:
                logging.error("Error sending PLI packet: %s", e)
    if msg.get("type") != "system":
        logging.debug(f"GeoChat skipped for {shorten_callsign(msg['callsign'])} (not system).")
        return
    unique_id = shorten_callsign(msg["callsign"])
    now = time.time()
    last_time = last_geo_chat_sent.get(unique_id, 0)
    if now - last_time >= GEO_CHAT_INTERVAL:
        last_geo_chat_sent[unique_id] = now
        geochat_packet = build_atak_geochat_packet(msg)
        if geochat_packet is not None:
            async with tx_lock:
                try:
                    await asyncio.get_running_loop().run_in_executor(
                        None,
                        lambda: interface.sendData(geochat_packet, portNum=72, wantAck=False)
                    )
                    logging.info("Sent ATAK GeoChat packet.")
                except Exception as e:
                    logging.error("Error sending GeoChat packet: %s", e)
    else:
        logging.debug(f"GeoChat throttled for {unique_id}.")


# --- Main Async Function ---
async def main_async():
    tasks = [
        asyncio.create_task(cot_receiver()),
        asyncio.create_task(flush_updates(interval=1))
    ]
    # If FPV mode is enabled and a FPV port is provided, start the FPV receiver.
    if args.fpv and args.fpv_port:
        tasks.append(asyncio.create_task(fpv_receiver(args.fpv_port, args.fpv_baud)))
    else:
        logging.info("FPV receiver disabled (use --fpv and --fpv-port to enable).")
    await asyncio.gather(*tasks)


# --- Run the Async Loop ---
if __name__ == "__main__":
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logging.info("KeyboardInterrupt received. Stopping...")
    finally:
        interface.close()
        zmq_socket.close()
        zmq_context.term()
        logging.info("Clean shutdown complete.")
