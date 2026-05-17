[![Build AOG-ASD](https://github.com/gunicsba/AOG_ASD_bridge/actions/workflows/build.yml/badge.svg)](https://github.com/gunicsba/AOG_ASD_bridge/actions/workflows/build.yml)

# AOG-ASD Bridge

Bridge between **AgOpenGPS** and **ASD-compatible** sprayer/spreader terminals (Quantron, Amatron, etc.) via the ASD serial protocol.

AgOpenGPS sends section states via UDP. This bridge translates them into
ASD serial commands so the terminal opens and closes sections in real time.
Section feedback from the terminal is reported back to AgOpenGPS.

## Download

Grab the latest `AOG-ASD.exe` from [Releases](../../releases).

## Requirements

- Serial connection to the ASD terminal (USB-to-Serial adapter)
- AgOpenGPS / AgIO broadcasting on UDP port 8888

For development:
- Python 3.8+
- [pyserial](https://pypi.org/project/pyserial/) (`pip install pyserial`)

## Connection

Connect the PC to the ASD terminal's serial port using a USB-to-RS232
adapter. The ASD interface uses a standard serial connection (no null-modem
crossover needed -- the terminal has a DCE-style port).

- **Baud:** 19200
- **Data bits:** 8, Parity: None, Stop bits: 1
- **Flow control:** None

## Usage

Run `AOG-ASD.exe`. On first run you will be prompted to select a COM port.
The choice is saved to `config.ini` so subsequent runs connect automatically.

Press **X** to exit.

## Features

| Feature | Description |
|---------|-------------|
| Section control | Sends section ON/OFF commands to ASD terminal |
| Section feedback | Reports actual terminal section state back to AgOpenGPS |
| GPS auto mode | Sets GPS-auto flag (byte 3 bit 7) on the ASD bus |
| Comms-lost safety | Sections zeroed when AgIO connection is lost |
| Auto-reconnect | 3-state machine handles connection loss and recovery |
| Configurable section count | Supports 4 or 8 section Quantron variants |

## How It Works

```
 AgOpenGPS / AgIO                    Bridge                  ASD Terminal
 +---------------------+      +------------------+      +-----------------+
 | Section control     |----->| UDP :8888        |      |                 |
 | Speed data          |      |                  |----->| SECT_SUBMIT     |
 | Hello heartbeat     |      |  Serial TX       |      | SECT_REQUEST    |
 |                     |      |  19200 8N1       |      | INIT_REQUEST    |
 | PGN 0xEA (sect data)|<----| UDP :9999        |      |                 |
 | PGN 0xED (from mach)|<----|                  |<-----| SECT_RESPONSE   |
 | Hello reply         |<----|  Serial RX       |<-----| INIT_RESPONSE   |
 +---------------------+      +------------------+      +-----------------+
```

## ASD Serial Protocol

- **Baud:** 19200, 8N1
- **Frame:** `STX <escaped payload> ETX`
- **STX** = `0x02`, **ETX** = `0x04`, **ESC** = `0x10`
- **Escaping:** Any payload byte equal to STX, ETX, or ESC is preceded by ESC on the wire
- **CRC:** `0xFF - sum(specific payload bytes)` (varies per message)

### Commands (Bridge -> Terminal)

| Command | ID | Purpose | Rate |
|---------|----|---------|------|
| Init Request | `0x01` | Connection probe / handshake | 1 Hz (disconnected) |
| Init Config | `0x25`/`0x35` | Tool configuration | Once after init |
| Section Request | `0x55` | Poll current section state | Configurable Hz |
| Section Submit | `0x55` (sub `0x01`) | Set section states | On change |

### Responses (Terminal -> Bridge)

| Response | ID | Purpose |
|----------|----|---------|
| Init ACK | `0x00` (sub `0x03`) | Confirms terminal is alive |
| Section State | `0x55` (sub `0x01`) | Current section bitmask (4 bytes) |

### Section Byte Layout

The ASD protocol carries sections in 4 bytes (32 bits total):

| Byte | Sections |
|------|----------|
| `sect[0]` | Sections 1-8 (bit 0 = section 1) |
| `sect[1]` | Sections 9-16 |
| `sect[2]` | Reserved |
| `sect[3]` | Bit 7 = GPS Auto mode flag (`0x80`) |

## State Machine

```
DISCONNECTED ──[Init ACK]──> READY ──[AgIO connected]──> RUNNING
     ^                          |                            |
     └───────── [10s timeout] ──┴────────────────────────────┘
```

| State | Activity |
|-------|----------|
| DISCONNECTED | Init Request probe at 1 Hz |
| READY | Machine connected, waiting for AgIO; section polling active |
| RUNNING | Sending section commands, speed; full AOG feedback |

## AgOpenGPS PGNs

### Incoming from AgIO (port 8888)

| PGN | Name | What the bridge does |
|-----|------|----------------------|
| `0xC8` | AgIO Hello | Replies with Hello Machine PGN (icon turns green) |
| `0xEF` | Machine Data | Bytes 11-12 = section mask, forwarded to terminal |
| `0xFE` | Steer Data | Speed + section bits extracted |

### Outgoing to AgIO (port 9999)

| PGN | Description |
|-----|-------------|
| `0x7B` (123) | Hello Reply (machine alive) |
| `0xEA` (234) | Section Control Data (relay ON/OFF bytes) |
| `0xED` (237) | From Machine (current relay state) |

## config.ini

Created automatically on first run.

```ini
[main]
com = COM3
comms_lost_zero = 1
sections = 8
sct_hz = 2
subnet = 255.255.255.255
```

| Parameter | Default | Description |
|-----------|---------|-------------|
| `com` | `0` (prompt) | Serial port. Set to `0` to prompt on startup |
| `comms_lost_zero` | `1` | Zero all sections when AgIO connection is lost |
| `sections` | `8` | Number of sections (4 or 8 for Quantron) |
| `sct_hz` | `2` | Section request polling rate in Hz |
| `subnet` | `255.255.255.255` | UDP broadcast address |

## Files

| File | Purpose |
|------|---------|
| [AOG_ASD_bridge.py](AOG_ASD_bridge.py) | Main bridge. Threads: UDP, serial RX, periodic TX, keyboard |
| [asd_protocol.py](asd_protocol.py) | ASD framing, escaping, CRC, packet builders, stream parser |
| [build.bat](build.bat) | PyInstaller one-file build -> `AOG-ASD.exe` |
| [startup.bat](startup.bat) | Convenience launcher |
| [config.ini](config.ini) | Auto-created on first run (see above) |

## Building

```bat
build.bat
```

Produces `AOG-ASD.exe` via PyInstaller.

## Credits

Based on the ASD host protocol by Coffeetrac (W.Eder, 2019) and
Daniel Desmartins (2025). Rewritten as a PC-based serial-UDP bridge
for direct AgOpenGPS integration without ESP32 hardware.

## License / legal

This bridge is a clean-room reimplementation for AgOpenGPS integration.
