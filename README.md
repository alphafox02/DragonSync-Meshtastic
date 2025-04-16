# DragonSync-Meshtastic

DragonSync-Meshtastic is a Python-based application designed to bridge multicast network messages and Meshtastic devices. The application runs in duplex mode—listening for Cursor-on-Target (CoT) messages via a multicast group and relaying them as TAK packets (PLI and GeoChat) over a Meshtastic serial connection. FPV (First Person View) message processing has been removed from the main application and may be developed as a separate module in the future.

## Table of Contents

- [Overview](#overview)
- [Features](#features)
- [How It Works](#how-it-works)
- [Installation](#installation)
- [Usage](#usage)
- [Configuration](#configuration)
- [License](#license)
- [Contributing](#contributing)
- [Contact](#contact)

## Overview

DragonSync-Meshtastic acts as a middleware solution that integrates multicast CoT data into a unified communication stream for Meshtastic devices. The application:
- Listens for multicast CoT messages containing system and drone telemetry.
- Parses these messages and transmits them as compact TAK PLI packets (with additional GeoChat packets) via the Meshtastic serial interface.
- **Note:** FPV functionality has been removed from the main module. A dedicated module for FPV message processing may be developed in the future.

## Features

- **Duplex Communication:**
  - **Transmitting:**  
    Receives CoT messages via UDP multicast, parses them, and sends TAK PLI packets (with throttled GeoChat packets) via the Meshtastic serial interface.
- **Asynchronous Processing:**  
  Uses asyncio to decouple message reception and periodic update flushing.
- **Configurable Settings:**  
  Command-line arguments allow you to specify:
  - Meshtastic serial port.
  - Multicast IP address and port.
- **Extensible & Modular:**  
  The clear, documented structure allows for future expansion, including the addition of a separate module to process incoming FPV messages.

## How It Works

1. **Initialization:**  
   The application parses command-line arguments for Meshtastic device settings and multicast configurations. It then initializes the Meshtastic interface (or auto-detects it).

2. **CoT Reception & Processing:**  
   A UDP multicast socket listens for CoT messages. Incoming XML messages are parsed into dictionaries, categorized by type (e.g., "drone", "wardragon", "pilot", or "home") based on the UID prefix, and the most recent update for each unique transmitter (identified by a shortened callsign) is maintained.

3. **Message Transmission:**  
   A periodic flusher task sends the latest update for each transmitter as a TAK PLI packet via the Meshtastic interface. For system (and optionally pilot/home) messages, an additional GeoChat packet carrying key telemetry details is sent in a throttled manner.

4. **Concurrency & Locking:**  
   All tasks run concurrently in the asyncio event loop. An asyncio lock ensures that the Meshtastic interface is accessed in a thread-safe manner.

## Installation

1. **Clone the Repository:**

   ~~~bash
   git clone https://github.com/yourusername/DragonSync-Meshtastic.git
   cd DragonSync-Meshtastic
   ~~~

2. **Install Dependencies:**

   Ensure you have Python 3 installed. Then, install the required packages:

   ~~~bash
   pip install -r requirements.txt
   ~~~

   *Dependencies include modules such as `xmltodict` and libraries required by Meshtastic.*

## Usage

Run the application from the command line. For example:

~~~bash
./dragonsync_meshtastic.py --port /dev/ttyACM0 --mcast 239.2.3.1 --mcast-port 6969
~~~

**Command-line options:**

- **--port:** Serial device for Meshtastic (e.g., `/dev/ttyACM0`).
- **--mcast:** Multicast group IP address (default: `239.2.3.1`).
- **--mcast-port:** Multicast port (default: `6969`).

*FPV and ZMQ options have been removed from this module.*

## Configuration

The application is fully configurable via command-line arguments, enabling you to:
- Adjust multicast settings to match your network environment.
- Specify the serial port for the Meshtastic device.
- Set other parameters related to CoT message processing and ATAK packet construction.

## License

This project is licensed under the MIT License. See the [LICENSE](./LICENSE) file for full details.

## Contributing

Contributions are welcome! Fork this repository, make your improvements, and submit a pull request. Please follow PEP8 guidelines and include proper documentation and tests with your code.

## Contact

For more information or inquiries, visit [cemaxecuter.com](https://www.cemaxecuter.com).

---

*DragonSync-Meshtastic – Bridging multicast networks with Meshtastic devices for real-time situational awareness.*
