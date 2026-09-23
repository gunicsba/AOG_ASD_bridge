"""
AOG-ASD Sniffer: passive RS232 logger for the ASD serial line.

Opens one or more COM ports read-only (never transmits) and writes every
received byte to a timestamped log file, together with ASCII and a
best-effort ASD frame decode. Use it to capture traffic between a working
ASD host and the terminal, or to see what the terminal sends back to us.

Usage:
    AOG-ASD-Sniffer.exe                      prompt (defaults from sniffer.ini)
    AOG-ASD-Sniffer.exe COM18                sniff COM18 @ 19200
    AOG-ASD-Sniffer.exe COM18 COM19 -b 9600  sniff two ports (TX + RX taps)

Press X to stop.
"""

import argparse
import msvcrt
import os
import sys
import threading
import time
from configparser import ConfigParser
from datetime import datetime

import serial
import serial.tools.list_ports

from asd_protocol import BAUD, ASDStreamParser, decode_frame, unescape

DEFAULT_PORT = "COM18"
GAP_S = 0.010          # silence longer than this starts a new "burst" line


def get_app_directory() -> str:
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = get_app_directory()
CONFIG_PATH = os.path.join(APP_DIR, "sniffer.ini")


# ---------------------------------------------------------------------------
#  Output
# ---------------------------------------------------------------------------

class Log:
    """Thread-safe log to console + file with wall clock and relative time."""

    def __init__(self, path: str):
        self.f = open(path, "w", encoding="utf-8", buffering=1)
        self.lock = threading.Lock()
        self.t0 = time.perf_counter()
        self.path = path

    def write(self, msg: str, console: bool = True):
        now = datetime.now()
        rel = time.perf_counter() - self.t0
        line = f"{now:%Y-%m-%d %H:%M:%S}.{now.microsecond // 1000:03d} +{rel:10.3f}s {msg}"
        with self.lock:
            self.f.write(line + "\n")
            if console:
                print(line)

    def close(self):
        with self.lock:
            self.f.flush()
            self.f.close()


def ascii_view(data: bytes) -> str:
    return "".join(chr(b) if 32 <= b < 127 else "." for b in data)


def describe(frame: bytes) -> str:
    """Best-effort label for a known ASD frame (frame starts with STX)."""
    p = unescape(frame)
    if not p:
        return "EMPTY"
    cmd = p[0]
    if cmd == 0x01 and p[:5] == bytes([0x01, 0x03, 0x02, 0x08, 0x01]):
        return "INIT_REQ (host)"
    if cmd == 0x00:
        f = decode_frame(frame)
        if f and f.typ == 0x04:
            return f"REJECT (terminal) {f}"
        return f"REPLY (terminal) {f}" if f else "REPLY?"
    if cmd in (0x25, 0x35):
        return f"INIT_CONFIG 0x{cmd:02X} (host)"
    if cmd == 0x55 and len(p) > 2:
        if p[1] == 0x02 and p[2] == 0x02:
            return "SECT_REQ (host)"
        if p[1] == 0x01:
            sect = p[5:9]
            return (f"SECT_SUBMIT/RESP len=0x{p[2]:02X} tool={p[3:5].hex()} "
                    f"sect=[{' '.join(f'{b:02X}' for b in sect)}]")
    f = decode_frame(frame)
    return f"UNKNOWN {f}" if f else f"UNKNOWN cmd=0x{cmd:02X}"


# ---------------------------------------------------------------------------
#  Port reader
# ---------------------------------------------------------------------------

def sniff_port(ser: serial.Serial, label: str, log: Log, stop: threading.Event,
               stats: dict):
    parser = ASDStreamParser()
    burst = bytearray()
    last_rx = 0.0

    def flush_burst():
        if burst:
            log.write(f"{label} RAW ({len(burst):3d}) {burst.hex(' ')}  |{ascii_view(burst)}|")
            burst.clear()

    while not stop.is_set():
        try:
            data = ser.read(ser.in_waiting or 1)
        except Exception as e:
            log.write(f"{label} READ ERROR: {e}")
            time.sleep(0.5)
            continue

        now = time.perf_counter()
        if not data:
            if burst and now - last_rx > GAP_S:
                flush_burst()
            continue

        if burst and now - last_rx > GAP_S:
            flush_burst()
        burst.extend(data)
        last_rx = now
        stats[label]["bytes"] += len(data)

        for frame in parser.feed(data):
            stats[label]["frames"] += 1
            log.write(f"{label} FRAME {frame.hex(' ')} 04  -> {describe(frame)}")

    flush_burst()


# ---------------------------------------------------------------------------
#  Setup
# ---------------------------------------------------------------------------

def load_config() -> ConfigParser:
    cfg = ConfigParser()
    cfg.read(CONFIG_PATH)
    if "sniff" not in cfg:
        cfg["sniff"] = {"ports": DEFAULT_PORT, "baud": str(BAUD)}
    return cfg


def prompt_setup(cfg: ConfigParser):
    ports = list(serial.tools.list_ports.comports())
    print("Available COM ports:")
    for p in ports:
        print(f"  {p.device:8s} {p.description}")
    if not ports:
        print("  (none found)")
    print()

    default_ports = cfg.get("sniff", "ports")
    default_baud = cfg.get("sniff", "baud")
    s = input(f"Port(s) to sniff, space separated [{default_ports}]: ").strip()
    port_list = s.upper().split() if s else default_ports.split()
    s = input(f"Baud [{default_baud}]: ").strip()
    baud = int(s) if s else int(default_baud)

    cfg.set("sniff", "ports", " ".join(port_list))
    cfg.set("sniff", "baud", str(baud))
    with open(CONFIG_PATH, "w") as f:
        cfg.write(f)
    return port_list, baud


def main():
    ap = argparse.ArgumentParser(description="Passive ASD RS232 sniffer")
    ap.add_argument("ports", nargs="*", help="COM port(s), e.g. COM18")
    ap.add_argument("-b", "--baud", type=int, default=None)
    args = ap.parse_args()

    print("AOG-ASD Sniffer  (passive, receive only)")
    print()

    cfg = load_config()
    if args.ports:
        port_list = [p.upper() for p in args.ports]
        baud = args.baud or cfg.getint("sniff", "baud")
    else:
        port_list, baud = prompt_setup(cfg)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log = Log(os.path.join(APP_DIR, f"AOG-ASD-Sniffer_{stamp}.log"))
    print(f"Logging to {log.path}")
    log.write(f"Sniffer start: ports={port_list} baud={baud} 8N1")

    stop = threading.Event()
    stats = {}
    threads = []
    opened = []
    for port in port_list:
        try:
            ser = serial.Serial(port=port, baudrate=baud,
                                bytesize=serial.EIGHTBITS,
                                parity=serial.PARITY_NONE,
                                stopbits=serial.STOPBITS_ONE,
                                timeout=0.005)
        except Exception as e:
            log.write(f"{port} OPEN FAILED: {e}")
            continue
        opened.append(ser)
        stats[port] = {"bytes": 0, "frames": 0}
        t = threading.Thread(target=sniff_port,
                             args=(ser, port, log, stop, stats), daemon=True)
        t.start()
        threads.append(t)
        log.write(f"{port} opened")

    if not opened:
        log.write("No ports opened, exiting")
        log.close()
        input("Press Enter to exit...")
        return

    print("Press X to stop.")
    last_stat = time.time()
    try:
        while True:
            if msvcrt.kbhit() and msvcrt.getch() in (b"x", b"X"):
                break
            if time.time() - last_stat >= 10:
                last_stat = time.time()
                summary = "  ".join(f"{k}: {v['bytes']} B / {v['frames']} frames"
                                    for k, v in stats.items())
                log.write(f"STATS {summary}")
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass

    stop.set()
    for t in threads:
        t.join(timeout=1)
    for ser in opened:
        ser.close()
    summary = "  ".join(f"{k}: {v['bytes']} B / {v['frames']} frames"
                        for k, v in stats.items())
    log.write(f"Sniffer stop. {summary}")
    log.close()


if __name__ == "__main__":
    main()
