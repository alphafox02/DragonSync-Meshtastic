#!/usr/bin/env python3
"""
Meshtastic Duplex

A unified script to run a Meshtastic node in duplex mode:
 - Listens for CoT messages via UDP multicast, processes and flushes the latest updates,
   and transmits TAK PLI packets (with GeoChat for system messages) via the Meshtastic serial interface.

Usage example:
    ./meshtastic_duplex.py --port /dev/ttyACM0 --mcast 239.2.3.1 --mcast-port 6969

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

__version__ = "1.0.0"

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

# Import ATAK protobuf definitions and Meshtastic serial interface
from meshtastic.protobuf import atak_pb2
import meshtastic.serial_interface


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

    For callsigns starting with 'wardragon-', 'drone-', 'pilot-', or 'home-',
    return the prefix plus the last 4 characters. Otherwise, return the last 4 characters.
    """
    prefixes = ["wardragon-", "drone-", "pilot-", "home-"]
    for prefix in prefixes:
        if callsign.startswith(prefix):
            return prefix + callsign[-4:]
    return callsign[-4:] if len(callsign) >= 4 else callsign


# --- Global Throttling & State Setup ---
GEO_CHAT_INTERVAL = 10  # seconds between GeoChat transmissions per unique callsign
last_geo_chat_sent = {}
latest_updates = {}  # Latest update per unique (shortened) callsign
tx_lock = asyncio.Lock()  # Async lock for serializing radio transmissions


# --- Command-Line Argument Parsing ---
parser = argparse.ArgumentParser(
    description="Meshtastic Duplex: Async CoT listener and radio transmitter for the Meshtastic ATAK plugin."
)
parser.add_argument("--port", type=str, default=None,
                    help="Serial device for Meshtastic (e.g., /dev/ttyACM0).")
parser.add_argument("--mcast", type=str, default="239.2.3.1",
                    help="Multicast group IP (default: 239.2.3.1).")
parser.add_argument("--mcast-port", type=int, default=6969,
                    help="Multicast port (default: 6969).")
args = parser.parse_args()

logging.basicConfig(level=logging.INFO)

# --- Meshtastic Interface Initialization ---
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


# --- CoT Parsing ---
def parse_cot(xml_data):
    """
    Parse a CoT (Cursor-on-Target) XML message and return a list of message dicts.
    """
    try:
        cot_dict = xmltodict.parse(xml_data)
    except Exception as e:
        logging.error("Error parsing CoT XML: %s", e)
        return []

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
        msg = {"callsign": callsign,
               "lat": lat,
               "lon": lon,
               "alt": alt,
               "remarks": remarks,
               "type": "drone"}
        messages.append(msg)
    elif uid.startswith("wardragon-"):
        msg = {"callsign": callsign,
               "lat": lat,
               "lon": lon,
               "alt": alt,
               "remarks": remarks,
               "type": "system"}
        messages.append(msg)
    elif uid.startswith("pilot-"):
        msg = {"callsign": callsign,
               "lat": lat,
               "lon": lon,
               "alt": alt,
               "remarks": remarks,
               "type": "pilot"}
        messages.append(msg)
    elif uid.startswith("home-"):
        msg = {"callsign": callsign,
               "lat": lat,
               "lon": lon,
               "alt": alt,
               "remarks": remarks,
               "type": "home"}
        messages.append(msg)
    else:
        msg = {"callsign": callsign,
               "lat": lat,
               "lon": lon,
               "alt": alt,
               "remarks": remarks,
               "type": "unknown"}
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
    Extracts key metrics from remarks.
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
    # For system, pilot, and home messages, send as GeoChat (if throttling permits)
    if msg.get("type") in ["system", "pilot", "home"]:
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
            logging.debug("GeoChat throttled for %s.", unique_id)
    else:
        logging.debug("GeoChat skipped for %s (not system/pilot/home).", shorten_callsign(msg["callsign"]))


# --- Asynchronous Receiver for CoT (UDP) ---
async def cot_receiver():
    """Continuously receive CoT messages from UDP multicast and update latest_updates."""
    loop = asyncio.get_running_loop()
    while True:
        data, addr = await loop.run_in_executor(None, sock.recvfrom, 8192)
        logging.debug("Received CoT from %s", addr)
        messages = parse_cot(data.decode("utf-8"))
        for msg in messages:
            unique_id = shorten_callsign(msg["callsign"])
            latest_updates[unique_id] = msg


# --- Periodic Flusher for CoT Updates ---
async def flush_updates(interval=1):
    """Every 'interval' seconds, send out the latest update for each unique transmitter."""
    while True:
        if latest_updates:
            keys = list(latest_updates.keys())
            for key in keys:
                msg = latest_updates.pop(key, None)
                if msg is not None:
                    await send_packets_async(msg)
        await asyncio.sleep(interval)


# --- Main Async Function ---
async def main_async():
    tasks = [
        asyncio.create_task(cot_receiver()),
        asyncio.create_task(flush_updates(interval=1))
    ]
    await asyncio.gather(*tasks)


# --- Run the Async Loop ---
if __name__ == "__main__":
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logging.info("KeyboardInterrupt received. Stopping...")
    finally:
        interface.close()
        logging.info("Clean shutdown complete.")

