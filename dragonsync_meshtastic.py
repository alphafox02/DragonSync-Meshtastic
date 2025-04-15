#!/usr/bin/env python3
"""
MIT License

Copyright (c) 2025 CEMAXECUTER LLC

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

# Import ATAK protobuf definitions
from meshtastic.protobuf import atak_pb2

# --- Helper Functions for Field Constraints ---
def safe_str(val, max_size):
    """Convert value to string and truncate to max_size characters."""
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
    For example, 'wardragon-00e04c3618a3' -> 'wardragon-18a3'
    """
    if callsign.startswith("wardragon-"):
        return "wardragon-" + callsign[-4:]
    elif callsign.startswith("drone-"):
        return "drone-" + callsign[-4:]
    else:
        return callsign[-4:] if len(callsign) >= 4 else callsign

# --- Global Throttling & State Setup ---
# GEO_CHAT_INTERVAL defines how often (in seconds) a GeoChat packet is sent per unique callsign.
GEO_CHAT_INTERVAL = 10  
last_geo_chat_sent = {}

# For asynchronous processing, we maintain a dictionary that holds the latest update per unique callsign.
latest_updates = {}

# Create an asyncio Lock for serializing access to the radio.
tx_lock = asyncio.Lock()

# --- Command-line Argument Parsing ---
parser = argparse.ArgumentParser(
    description="Meshtastic CoT Multicast Listener (Async: Latest Update + ATAK PLI/GeoChat)"
)
parser.add_argument(
    "--port",
    type=str,
    default=None,
    help="Serial device to use (e.g., /dev/ttyACM0)."
)
parser.add_argument(
    "--mcast",
    type=str,
    default="239.2.3.1",
    help="Multicast Group IP address (default: 239.2.3.1)."
)
parser.add_argument(
    "--mcast-port",
    type=int,
    default=6969,
    help="Multicast port (default: 6969)."
)
args = parser.parse_args()

logging.basicConfig(level=logging.INFO)

# --- Create the Meshtastic Interface using devPath if provided ---
import meshtastic.serial_interface
if args.port:
    logging.info(f"Using specified device (devPath): {args.port}")
    interface = meshtastic.serial_interface.SerialInterface(devPath=args.port)
else:
    logging.info("No device specified; using auto-detection.")
    interface = meshtastic.serial_interface.SerialInterface()

logging.info("Meshtastic interface created successfully.")

# --- Multicast UDP Setup ---
MCAST_GRP = args.mcast
MCAST_PORT = args.mcast_port

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind((MCAST_GRP, MCAST_PORT))

mreq = struct.pack("4sl", socket.inet_aton(MCAST_GRP), socket.INADDR_ANY)
sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
logging.info(f"Multicast socket setup complete on {MCAST_GRP}:{MCAST_PORT}")

# --- CoT Parsing Function ---
def parse_cot(xml_data):
    """
    Parse a CoT (Cursor-on-Target) XML message and return a list of message dicts.
    For events starting with 'drone-', a base drone message is built and additional
    pilot and home messages are appended if found in remarks.
    For events starting with 'wardragon-', a system message is built.
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
        drone_msg = {
            "callsign": callsign,
            "lat": lat,
            "lon": lon,
            "alt": alt,
            "remarks": remarks,
            "type": "drone"
        }
        messages.append(drone_msg)
    elif uid.startswith("wardragon-"):
        system_msg = {
            "callsign": callsign,
            "lat": lat,
            "lon": lon,
            "alt": alt,
            "remarks": remarks,
            "type": "system"
        }
        messages.append(system_msg)
    else:
        unknown_msg = {
            "callsign": callsign,
            "lat": lat,
            "lon": lon,
            "alt": alt,
            "remarks": remarks,
            "type": "unknown"
        }
        messages.append(unknown_msg)
        logging.warning("Received message with unknown type; processing as generic PLI.")

    for msg in messages:
        logging.info("Parsed message: %s", msg)
    return messages

# --- ATAK Packet Builder Function for PLI ---
def build_atak_pli_packet(msg):
    """
    Build an ATAK TAKPacket with a PLI payload for a parsed CoT message.
    The contact callsign is shortened to include only the prefix and last 4 characters.
    """
    if msg["type"] == "unknown":
        logging.warning("Unknown message type received; skipping TAKPacket construction.")
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

    logging.info("Constructed PLI payload: lat=%d, lon=%d, alt=%d, course=%d",
                 packet.pli.latitude_i, packet.pli.longitude_i,
                 packet.pli.altitude, packet.pli.course)
    serialized = packet.SerializeToString()
    logging.info("Serialized TAKPacket (PLI) (length: %d bytes)", len(serialized))
    return serialized

# --- ATAK Packet Builder Function for GeoChat (System messages only) ---
def build_atak_geochat_packet(msg):
    """
    Build an ATAK TAKPacket with a GeoChat payload carrying extra details.
    This function is designed for 'system' messages only and creates a concise summary
    extracting key metrics (CPU usage, Temperature, AD936X, Zynq) from remarks.
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
    # Extract metrics using regex.
    cpu_match = re.search(r'CPU Usage:\s*([\d\.]+)%', remarks)
    temp_match = re.search(r'Temperature:\s*([\d\.]+)°C', remarks)
    ad936x_match = re.search(r'(?:Pluto|AD936X)\s*Temp:\s*([\w./]+)', remarks)
    zynq_match = re.search(r'Zynq Temp:\s*([\w./]+)', remarks)
    cpu_val = cpu_match.group(1) if cpu_match else "N/A"
    temp_val = temp_match.group(1) if temp_match else "N/A"
    ad936x_val = ad936x_match.group(1) if ad936x_match else "N/A"
    zynq_val = zynq_match.group(1) if zynq_match else "N/A"
    detailed_message = f"{short_callsign} | CPU: {cpu_val}% | Temp: {temp_val}°C | AD936X: {ad936x_val} | Zynq: {zynq_val}"
    packet.chat.message = safe_str(detailed_message, 256)
    packet.chat.to = safe_str("All Chat Rooms", 120)
    packet.chat.to_callsign = safe_str("All Chat Rooms", 120)
    packet.group.role = atak_pb2.MemberRole.TeamMember
    packet.group.team = atak_pb2.Team.Cyan

    logging.info("Constructed GeoChat payload: %s", packet.chat.message)
    serialized = packet.SerializeToString()
    logging.info("Serialized TAKPacket (GeoChat) (length: %d bytes)", len(serialized))
    return serialized

# --- Asynchronous Sender ---
async def send_packets_async(msg):
    """
    Asynchronously send packets for a given message.
    This sends a PLI packet and, for system messages, sends a throttled GeoChat packet.
    """
    # Send the PLI packet.
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
                logging.error("Error sending ATAK PLI packet: %s", e)
                
    # Only send GeoChat for system messages.
    if msg.get("type") != "system":
        logging.debug(f"GeoChat not sent for {shorten_callsign(msg['callsign'])} (not system)")
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
                    logging.error("Error sending ATAK GeoChat packet: %s", e)
    else:
        logging.debug(f"GeoChat throttled for {unique_id}.")

# --- Asynchronous Receiver ---
async def receiver():
    loop = asyncio.get_running_loop()
    while True:
        # Use run_in_executor for blocking socket.recvfrom.
        data, addr = await loop.run_in_executor(None, sock.recvfrom, 8192)
        logging.debug(f"Received packet from {addr}")
        messages = parse_cot(data.decode("utf-8"))
        for msg in messages:
            # Update the latest_updates dictionary with the most recent update per unique callsign.
            unique_id = shorten_callsign(msg["callsign"])
            latest_updates[unique_id] = msg

# --- Periodic Flusher ---
async def flush_updates(interval=1):
    """
    Every 'interval' seconds, send out the latest update from each unique drone.
    """
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
    receiver_task = asyncio.create_task(receiver())
    flush_task = asyncio.create_task(flush_updates(interval=1))
    await asyncio.gather(receiver_task, flush_task)

# --- Run the Async Loop ---
if __name__ == "__main__":
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logging.info("Stopping listener.")
    finally:
        interface.close()
        logging.info("Interface closed. Exiting.")
