"""
AOG-ASD Probe: active discovery against an ASD terminal.

Sends INIT, INIT_CONFIG and *read* requests only (type 0x02) -- never a
section write -- and logs every reply, so we can see which objects / tool
IDs the terminal accepts. Stop AOG-ASD.exe first; the port must be free.

Usage:
    AOG-ASD-Probe.exe            port from config.ini (else COM18)
    AOG-ASD-Probe.exe COM18
    AOG-ASD-Probe.exe COM18 --full   also scan every object 0x00-0xFF
    AOG-ASD-Probe.exe COM18 --watch 180   poll known objects, log changes
    AOG-ASD-Probe.exe COM18 --set-rate 0  WRITE rate setpoint (moves the machine!)
    AOG-ASD-Probe.exe COM18 --ramp 250 --seconds 10 --obj 0x00 --index 1 --loops 3 --hold 5
                                         WRITE 250 -> 0 -> 250 (index 1 = left, 2 = right)
"""

import argparse
import msvcrt
import os
import struct
import sys
import time
from collections import Counter
from configparser import ConfigParser
from datetime import datetime

import serial

from asd_protocol import (BAUD, REPLY_REJECT, ASDStreamParser, build_frame,
                          decode_frame)

REPLY_TIMEOUT_S = 0.25


def get_app_directory() -> str:
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = get_app_directory()


class Probe:
    def __init__(self, ser: serial.Serial, log_path: str):
        self.ser = ser
        self.parser = ASDStreamParser()
        self.f = open(log_path, "w", encoding="utf-8", buffering=1)
        self.results = []    # (label, request, reply-or-None)

    def log(self, msg: str):
        now = datetime.now()
        line = f"{now:%Y-%m-%d %H:%M:%S}.{now.microsecond // 1000:03d} {msg}"
        print(line)
        self.f.write(line + "\n")

    def ask(self, label: str, obj: int, typ: int, data: bytes = b"",
            quiet: bool = False):
        """Send one request and wait for the first reply frame."""
        frame = build_frame(obj, typ, data)
        self.ser.reset_input_buffer()
        self.ser.write(frame)
        self.ser.flush()
        replies = []
        raw = bytearray()
        deadline = time.perf_counter() + REPLY_TIMEOUT_S
        while time.perf_counter() < deadline and not replies:
            chunk = self.ser.read(self.ser.in_waiting or 1)
            raw += chunk
            replies += self.parser.feed(chunk)
        # let any trailing frames arrive so they don't pollute the next ask
        time.sleep(0.02)
        tail = self.ser.read(self.ser.in_waiting)
        raw += tail
        replies += self.parser.feed(tail)

        req = f"{obj:02X} {typ:02X} {len(data):02X} {data.hex(' ')}".strip()
        if not replies:
            if not quiet:
                self.log(f"{label:28s} TX [{req}]  ->  NO REPLY  raw=[{raw.hex(' ')}]")
            self.results.append((label, req, None))
            return None
        for r in replies:
            d = decode_frame(r)
            tag = "REJECT" if d and d.obj == 0 and d.typ == REPLY_REJECT else "REPLY "
            if not quiet:
                self.log(f"{label:28s} TX [{req}]  ->  {tag} {d}   wire=[{r.hex(' ')} 04]")
            self.results.append((label, req, d))
        return decode_frame(replies[0])

    def summary(self):
        self.log("=" * 70)
        self.log("SUMMARY (reply kind -> count)")
        kinds = Counter()
        for _, _, d in self.results:
            if d is None:
                kinds["no reply"] += 1
            elif d.obj == 0 and d.typ == REPLY_REJECT:
                kinds[f"reject code 0x{d.data[2]:02X}" if len(d.data) > 2 else "reject ?"] += 1
            else:
                kinds[f"reply obj=0x{d.obj:02X} type=0x{d.typ:02X}"] += 1
        for k, n in kinds.most_common():
            self.log(f"  {k:40s} {n}")
        self.log("NON-REJECT REPLIES:")
        for label, req, d in self.results:
            if d is not None and not (d.obj == 0 and d.typ == REPLY_REJECT):
                self.log(f"  {label:28s} TX [{req}] -> {d}")


WATCH_OBJECTS = (0x00, 0x10, 0x20, 0x30, 0x40, 0x50, 0x60, 0x70, 0x80, 0x90)


def data_views(data: bytes) -> str:
    """Show the payload after the 2-byte tool ID as float / uint32 guesses."""
    v = data[2:]
    out = [f"raw=[{v.hex(' ')}]"]
    for off in range(0, max(1, len(v) - 3)):
        four = v[off:off + 4]
        if len(four) == 4:
            f = struct.unpack("<f", four)[0]
            u = struct.unpack("<I", four)[0]
            out.append(f"@{off}: f={f:.6g} u={u}")
    return "  ".join(out)


def watch(p: "Probe", tool: bytes, period: float, duration: float):
    """Poll the known objects and log every value change."""
    p.log(f"--- WATCH objects {[hex(o) for o in WATCH_OBJECTS]} every {period}s "
          f"for {duration:.0f}s. Operate the terminal now (sections, rate, ...)")
    last = {}
    end = time.time() + duration
    while time.time() < end and not (msvcrt.kbhit() and msvcrt.getch() in (b"x", b"X")):
        t0 = time.time()
        for obj in WATCH_OBJECTS:
            d = p.ask(f"W 0x{obj:02X}", obj, 0x02, tool, quiet=True)
            key = None if d is None else (d.obj, d.typ, d.data)
            if key != last.get(obj, "unset"):
                if d is None:
                    p.log(f"CHANGE 0x{obj:02X}: no reply")
                elif d.obj == 0 and d.typ == REPLY_REJECT:
                    p.log(f"CHANGE 0x{obj:02X}: rejected {d}")
                else:
                    p.log(f"CHANGE 0x{obj:02X}: {data_views(d.data)}")
                last[obj] = key
        time.sleep(max(0.0, period - (time.time() - t0)))


def float_of(d) -> str:
    if d is None:
        return "no reply"
    if d.obj == 0 and d.typ == REPLY_REJECT:
        return f"REJECT[{d.data.hex(' ')}]"
    if len(d.data) >= 6:
        return f"{struct.unpack('<f', d.data[-4:])[0]:.2f}"
    return f"[{d.data.hex(' ')}]"


def set_rate(p: "Probe", tool: bytes, rate: float, index: int, seconds: float,
             obj: int = 0x20):
    """Write a rate setpoint (Coffeetrac format: 20 01 07 tool idx float)."""
    idx = bytes([index & 0xFF])

    def snapshot(seconds: float):
        end = time.time() + seconds
        while time.time() < end:
            v = [float_of(p.ask("", o, 0x02, tool + b"\x00", quiet=True))
                 for o in (0x00, 0x20, 0x40)]
            p.log(f"   target={v[0]}  actual={v[1]}  width={v[2]}")
            time.sleep(0.8)

    p.ask("INIT 08 01", 0x01, 0x03, bytes([0x08, 0x01]))
    p.log("--- index reads (0x00 / 0x20 / 0x40, index 0-3)")
    for obj in (0x00, 0x20, 0x40):
        for i in range(4):
            p.log(f"R 0x{obj:02X} idx {i}: "
                  f"{float_of(p.ask('', obj, 0x02, tool + bytes([i]), quiet=True))}")
    p.log("--- before write")
    snapshot(2)
    p.log(f"--- WRITE rate {rate} to 0x{obj:02X} index {index}")
    d = p.ask(f"WRITE 0x{obj:02X} rate {rate}", obj, 0x01,
              tool + idx + struct.pack("<f", rate))
    p.log(f"write reply: {d}")
    snapshot(seconds)


def rate_ramp(p: "Probe", tool: bytes, high: float, index: int, seconds: float,
              obj: int = 0x20, delay: float = 0, hold: float = 0, loops: int = 1):
    """Ramp the rate setpoint high -> 0 over `seconds`, then 0 -> high.

    Writes about once per second and logs target/actual/width after each
    write. X / Ctrl+C aborts and immediately restores `high`.
    """
    idx = bytes([index & 0xFF])

    def write(rate: float):
        d = p.ask("", obj, 0x01, tool + idx + struct.pack("<f", rate), quiet=True)
        v = [float_of(p.ask("", o, 0x02, tool + b"\x00", quiet=True))
             for o in (0x00, 0x20, 0x40)]
        p.log(f"WRITE {rate:7.2f} -> reply {float_of(d) if d else 'none':>14s}   "
              f"target={v[0]}  actual={v[1]}  width={v[2]}")

    p.ask("INIT 08 01", 0x01, 0x03, bytes([0x08, 0x01]))
    for left in range(int(delay), 0, -1):
        if left % 5 == 0 or left <= 3:
            p.log(f"--- starting in {left}s")
        time.sleep(1)
    p.log(f"--- RAMP obj 0x{obj:02X}: {high} -> 0 -> {high}, {seconds:.0f}s each way, index {index}. "
          f"Press X to abort (restores {high}).")
    try:
        for loop in range(1, loops + 1):
            p.log(f"--- loop {loop}/{loops}")
            for down in (True, False):
                t0 = time.time()
                while True:
                    if msvcrt.kbhit() and msvcrt.getch() in (b"x", b"X"):
                        raise KeyboardInterrupt
                    frac = min(1.0, (time.time() - t0) / seconds)
                    rate = high * (1 - frac) if down else high * frac
                    write(round(rate, 1))
                    if frac >= 1.0:
                        break
                    time.sleep(max(0.0, 1.0 - 0.3))   # ~1 write/s incl. 3 reads
                if down and hold > 0:
                    p.log(f"--- reached 0, holding {hold:.0f}s")
                    t_hold = time.time()
                    while time.time() - t_hold < hold:
                        if msvcrt.kbhit() and msvcrt.getch() in (b"x", b"X"):
                            raise KeyboardInterrupt
                        write(0.0)
                        time.sleep(1.0)
                p.log("--- ramping back up" if down else f"--- back at {high}")
    except KeyboardInterrupt:
        p.log(f"--- ABORTED, restoring {high}")
        write(high)


def main():
    ap = argparse.ArgumentParser(description="ASD terminal probe (read-only requests)")
    ap.add_argument("port", nargs="?")
    ap.add_argument("--full", action="store_true", help="scan all objects 0x00-0xFF")
    ap.add_argument("--watch", type=float, metavar="SECONDS",
                    help="only poll the known objects and log changes")
    ap.add_argument("--set-rate", type=float, metavar="KG_HA",
                    help="WRITE rate setpoint to object 0x20 (moves the machine!), "
                         "then watch target/actual/width (see --seconds)")
    ap.add_argument("--index", type=int, default=0,
                    help="index byte after the tool ID for --set-rate (default 0)")
    ap.add_argument("--ramp", type=float, metavar="KG_HA",
                    help="WRITE rate ramp KG_HA -> 0 -> KG_HA (moves the machine!)")
    ap.add_argument("--obj", type=lambda v: int(v, 0), default=0x20,
                    help="object to write for --ramp (default 0x20; try 0x00)")
    ap.add_argument("--delay", type=float, default=0,
                    help="--ramp: countdown before starting (seconds)")
    ap.add_argument("--loops", type=int, default=1,
                    help="--ramp: number of down/up cycles")
    ap.add_argument("--hold", type=float, default=0,
                    help="--ramp: hold at 0 for this many seconds before ramping back")
    ap.add_argument("--seconds", type=float, default=90,
                    help="--set-rate: watch time; --ramp: time per direction (default 90)")
    args = ap.parse_args()

    port = args.port
    if not port:
        cfg = ConfigParser()
        cfg.read(os.path.join(APP_DIR, "config.ini"))
        port = cfg.get("main", "com", fallback="COM18")
        if port == "0":
            port = "COM18"
    port = port.upper()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(APP_DIR, f"AOG-ASD-Probe_{stamp}.log")
    print(f"AOG-ASD Probe on {port} @ {BAUD}  (make sure AOG-ASD.exe is closed)")
    print(f"Logging to {log_path}\n")

    try:
        ser = serial.Serial(port, BAUD, timeout=0.01)
    except Exception as e:
        print(f"Cannot open {port}: {e}")
        input("Press Enter to exit...")
        return

    p = Probe(ser, log_path)
    tool = bytes([0x05, 0x00])

    if args.ramp is not None:
        rate_ramp(p, tool, args.ramp, args.index, args.seconds, args.obj,
                  args.delay, args.hold, args.loops)
        ser.close()
        p.f.close()
        print(f"\nDone. Log: {log_path}")
        input("Press Enter to exit...")
        return

    if args.set_rate is not None:
        set_rate(p, tool, args.set_rate, args.index, args.seconds, args.obj)
        ser.close()
        p.f.close()
        print(f"\nDone. Log: {log_path}")
        input("Press Enter to exit...")
        return

    if args.watch:
        p.ask("INIT 08 01", 0x01, 0x03, bytes([0x08, 0x01]))
        watch(p, tool, 0.5, args.watch)
        ser.close()
        p.f.close()
        print(f"\nDone. Log: {log_path}")
        return

    p.log("--- 1. Init handshake (as the bridge does it, CRC-fixed config)")
    p.ask("INIT 08 01", 0x01, 0x03, bytes([0x08, 0x01]))
    p.ask("CONFIG 0x25 tool 05 00", 0x25, 0x02, tool)
    p.ask("CONFIG 0x35 tool 05 00", 0x35, 0x02, tool)
    p.ask("SECT_REQ tool 05 00", 0x55, 0x02, tool)

    p.log("--- 2. Init variants (2-section machine)")
    for d in ([0x02, 0x01], [0x01, 0x01], [0x08, 0x01]):
        p.ask(f"INIT {bytes(d).hex(' ')}", 0x01, 0x03, bytes(d))
        p.ask("  CONFIG 0x25", 0x25, 0x02, tool)
        p.ask("  CONFIG 0x35", 0x35, 0x02, tool)
        p.ask("  SECT_REQ", 0x55, 0x02, tool)

    p.log("--- 3. Read with different data lengths")
    for obj in (0x25, 0x35, 0x55):
        p.ask(f"READ 0x{obj:02X} no data", obj, 0x02)
        p.ask(f"READ 0x{obj:02X} 1 byte 05", obj, 0x02, bytes([0x05]))

    p.log("--- 4. Tool-ID scan on 0x25 / 0x55")
    candidates = [bytes([i, 0]) for i in range(0x10)] + \
                 [bytes([0, i]) for i in range(1, 0x10)] + [b"\xff\xff"]
    for obj in (0x25, 0x55):
        for t in candidates:
            p.ask(f"READ 0x{obj:02X} tool {t.hex(' ')}", obj, 0x02, t)

    if args.full:
        p.log("--- 5. Object scan 0x00-0xFF (read, tool 05 00)")
        for obj in range(0x100):
            if obj == 0x01:
                continue
            p.ask(f"READ 0x{obj:02X}", obj, 0x02, tool)

    p.summary()
    ser.close()
    p.f.close()
    print(f"\nDone. Log: {log_path}")
    input("Press Enter to exit...")


if __name__ == "__main__":
    main()
