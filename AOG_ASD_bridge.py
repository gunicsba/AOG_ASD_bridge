"""
AOG-ASD Bridge: AgOpenGPS <-> ASD section-control terminal bridge.

Reads section and speed PGNs from AgIO over UDP, drives the ASD serial
protocol at 19200 8-N-1, and feeds section state back into AgIO so the
map UI mirrors reality.
"""

import serial
import serial.tools.list_ports
import socket
import threading
import time
import msvcrt
import logging
import os
import sys
from configparser import ConfigParser
from enum import Enum, auto
from typing import Optional

from asd_protocol import (
    BAUD,
    DEFAULT_TOOL_ID,
    ASDFrame,
    ASDStreamParser,
    OBJ_ACTUAL_RATE,
    OBJ_SPEED,
    OBJ_TARGET_RATE,
    OBJ_WIDTH,
    REPLY_REJECT,
    SIDE_LEFT,
    SIDE_RIGHT,
    build_read,
    build_write_float,
    decode_frame,
    parse_float_reply,
    build_init_request,
    build_init_config,
    build_section_request,
    build_section_submit,
    parse_init_response,
    parse_section_response,
    RESP_INIT,
    RESP_SECTION,
)

# ---------------------------------------------------------------------------
#  Timing / protocol constants
# ---------------------------------------------------------------------------
DEFAULT_SECTION_COUNT = 8       # Quantron A = 4 or 8
INIT_PROBE_S = 1.0              # Init request interval (DISCONNECTED)
SECTION_POLL_S = 0.5            # Section request interval (RUNNING)
MACHINE_TIMEOUT_S = 10.0        # No response -> DISCONNECTED

UDP_PORT = 8888
AOG_PORT = 9999
UDP_TIMEOUT_S = 3

AOG_MACHINE_SRC = 0x7B          # 123 = machine module
TICK_S = 0.05                   # Periodic loop tick (50 ms)

# ---------------------------------------------------------------------------
#  State machine
# ---------------------------------------------------------------------------
class MachineState(Enum):
    DISCONNECTED = auto()
    READY = auto()
    RUNNING = auto()


# ---------------------------------------------------------------------------
#  Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = logging.INFO

# File-based logging
def get_app_directory() -> str:
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = get_app_directory()
exe_name = os.path.splitext(os.path.basename(sys.executable if getattr(sys, 'frozen', False)
                                              else __file__))[0]
LOG_PATH = os.path.join(APP_DIR, f"{exe_name}_{time.strftime('%Y%m%d_%H%M%S')}.log")

# Console stays at INFO; the file gets DEBUG (raw RX bytes, AgIO values)
_console = logging.StreamHandler()
_console.setLevel(LOG_LEVEL)
_console.setFormatter(logging.Formatter(
    "[%(asctime)s.%(msecs)03d] %(levelname)s %(message)s", datefmt="%H:%M:%S"))
_file = logging.FileHandler(LOG_PATH, mode='w', encoding='utf-8')
_file.setLevel(logging.DEBUG)
_file.setFormatter(logging.Formatter(
    "[%(asctime)s.%(msecs)03d] %(levelname)s [%(threadName)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"))
logging.basicConfig(level=logging.DEBUG, handlers=[_console, _file])
logger = logging.getLogger("asd")
logger.info(f"Logging to file: {LOG_PATH}")


# ---------------------------------------------------------------------------
#  Config
# ---------------------------------------------------------------------------
CONFIG_PATH = os.path.join(APP_DIR, "config.ini")


def load_config() -> ConfigParser:
    config = ConfigParser()
    if not os.path.exists(CONFIG_PATH):
        config["main"] = {
            "com": "0",
            "comms_lost_zero": "1",
            "sections": str(DEFAULT_SECTION_COUNT),
            "machine": "quantron",
            "base_rate": "0",
            "sct_hz": "2",
            "subnet": "255.255.255.255",
        }
        with open(CONFIG_PATH, "w") as f:
            config.write(f)
    else:
        config.read(CONFIG_PATH)
    return config


def save_config(config: ConfigParser):
    with open(CONFIG_PATH, "w") as f:
        config.write(f)


# ---------------------------------------------------------------------------
#  AgOpenGPS message helpers
# ---------------------------------------------------------------------------

def aog_checksum(msg: bytes) -> int:
    """Sum bytes 2..n-1 (everything between preamble and CRC slot)."""
    return sum(msg[2:]) & 0xFF


def build_hello_reply(relay_lo: int, relay_hi: int) -> bytes:
    """Build the Hello reply that makes the machine icon go green in AOG."""
    msg = bytearray([
        0x80, 0x81,
        AOG_MACHINE_SRC,
        AOG_MACHINE_SRC,
        5,
        relay_lo & 0xFF,
        relay_hi & 0xFF,
        0, 0, 0,
    ])
    msg.append(aog_checksum(msg))
    return bytes(msg)


def build_from_machine(relay_lo: int, relay_hi: int) -> bytes:
    """Build the 'From Machine' PGN 0xED."""
    msg = bytearray([
        0x80, 0x81,
        AOG_MACHINE_SRC,
        0xED,
        8,
        relay_lo & 0xFF,
        relay_hi & 0xFF,
        0, 0,
        0, 0, 0, 0,
    ])
    msg.append(aog_checksum(msg))
    return bytes(msg)


def build_section_data(relay_lo: int, relay_hi: int,
                       off_lo: int = 0, off_hi: int = 0) -> bytes:
    """Build PGN 0xEA (234) -- Section Control Data to AOG."""
    msg = bytearray([
        0x80, 0x81,
        AOG_MACHINE_SRC,
        0xEA,
        8,
        0x00,                  # byte 5: main switch bits
        0, 0, 0,               # bytes 6-8: reserved
        relay_lo & 0xFF,       # byte 9:  sections ON  1-8
        off_lo & 0xFF,         # byte 10: sections OFF 1-8
        relay_hi & 0xFF,       # byte 11: sections ON  9-16
        off_hi & 0xFF,         # byte 12: sections OFF 9-16
    ])
    msg.append(aog_checksum(msg))
    return bytes(msg)


# ---------------------------------------------------------------------------
#  COM port selection
# ---------------------------------------------------------------------------

def list_ports():
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        print("No COM ports available.")
        return []
    print("Available COM ports:")
    for i, p in enumerate(ports):
        print(f"  [{i}] {p.device}  ({p.description})")
    return ports


def select_port() -> Optional[str]:
    ports = list_ports()
    if not ports:
        return None
    while True:
        choice = input("Select port index or COM name: ").strip()
        if choice.isdigit():
            idx = int(choice)
            if 0 <= idx < len(ports):
                return ports[idx].device
        if choice.upper().startswith("COM"):
            return choice.upper()
        print("Invalid choice.")


# ---------------------------------------------------------------------------
#  ASDRequester -- manages ASD terminal serial communication
# ---------------------------------------------------------------------------

class ASDRequester:
    def __init__(self, ser: serial.Serial, section_count: int,
                 sct_hz: int, config: ConfigParser):
        self.ser = ser
        self.config = config
        self.lock = threading.Lock()            # serial write lock
        self.sections_lock = threading.Lock()   # protects target_sections
        self.running = True

        # Machine connection state
        self.state = MachineState.DISCONNECTED
        self.last_valid_machine_time = 0.0
        self.got_init = False

        # Section configuration
        self.section_count = section_count
        self.tool_id = DEFAULT_TOOL_ID

        # Section state (written by UDP thread, read by TX thread)
        self.target_sections = bytes(4)         # 4 bytes bitmask
        self.machine_sections = bytes(4)        # reported by ASD terminal
        self.section_change = False

        # Speed (from AgOpenGPS, for future use / speed pulse)
        self.current_speed_kmh = 0.0

        # AgIO connection flag
        self.agio_connected = False

        # Current relay bytes (for AOG feedback PGNs)
        self.relay_lo = 0
        self.relay_hi = 0

        # Configurable rates
        self.sct_hz = max(1, sct_hz)

        # Timer tracking
        self.last_init_time = 0.0
        self.last_sct_time = 0.0
        self.init_config_sent = False
        self.rejects_seen = set()

    # ---- serial helpers ----

    def send_frame(self, frame: bytes, desc: str = ""):
        with self.lock:
            self.ser.write(frame)
            self.ser.flush()
        logger.debug(f"TX >> {desc} [{frame.hex()}]")

    def shutdown(self):
        """Called before the serial port closes."""

    # ---- state transitions ----

    def enter_disconnected(self, reason: str):
        if self.state != MachineState.DISCONNECTED:
            logger.info(f"STATE -> DISCONNECTED ({reason})")
        self.state = MachineState.DISCONNECTED
        self.got_init = False
        self.init_config_sent = False

    def enter_ready(self, reason: str):
        if self.state != MachineState.READY:
            logger.info(f"STATE -> READY ({reason})")
        self.state = MachineState.READY

    def enter_running(self, reason: str):
        if self.state != MachineState.RUNNING:
            logger.info(f"STATE -> RUNNING ({reason})")
        self.state = MachineState.RUNNING
        self.last_sct_time = 0.0

    # ---- periodic TX loop ----

    def periodic_loop(self):
        while self.running:
            now = time.time()

            # --- timeout checks ---
            if self.state != MachineState.DISCONNECTED:
                if self.last_valid_machine_time > 0 and \
                   (now - self.last_valid_machine_time) > MACHINE_TIMEOUT_S:
                    self.enter_disconnected(
                        f"machine timeout {now - self.last_valid_machine_time:.1f}s")

            # READY -> RUNNING when AgIO connects
            if self.state == MachineState.READY and self.agio_connected:
                self.enter_running("AgIO connected")

            # --- DISCONNECTED: send init probes ---
            if self.state == MachineState.DISCONNECTED:
                if (now - self.last_init_time) >= INIT_PROBE_S:
                    self.send_frame(build_init_request(), "INIT_REQ")
                    self.last_init_time = now

            # --- READY / RUNNING: periodic section request ---
            if self.state in (MachineState.READY, MachineState.RUNNING):
                # Keep-alive init probe every 3.6s
                if (now - self.last_init_time) >= 3.6:
                    self.send_frame(build_init_request(), "INIT_REQ (keepalive)")
                    self.last_init_time = now

                # Section poll at configured rate
                if (now - self.last_sct_time) >= (1.0 / self.sct_hz):
                    self.send_frame(
                        build_section_request(self.tool_id), "SECT_REQ")
                    self.last_sct_time = now

            # --- RUNNING: send section commands on change ---
            if self.state == MachineState.RUNNING and self.section_change:
                with self.sections_lock:
                    sects = self.target_sections
                    self.section_change = False
                self.send_frame(
                    build_section_submit(sects, self.tool_id),
                    f"SECT_SUBMIT {sects.hex()}")
                # Request back to confirm
                time.sleep(0.05)
                self.send_frame(
                    build_section_request(self.tool_id), "SECT_REQ (verify)")

            time.sleep(TICK_S)

    # ---- section / speed update from AgOpenGPS ----

    def update_sections_from_aog(self, relay_lo: int, relay_hi: int):
        """Map AgOpenGPS relay bytes to 4-byte ASD section mask."""
        # ASD uses 4 bytes for up to 32 sections.
        # byte[3] bit 7 = GPS auto mode flag (0x80)
        new_sects = bytearray(4)
        new_sects[0] = relay_lo & 0xFF
        new_sects[1] = relay_hi & 0xFF
        new_sects[2] = 0x00
        # Set GPS auto mode flag if connected
        new_sects[3] = 0x80 if self.agio_connected else 0x00

        with self.sections_lock:
            if new_sects != self.target_sections:
                self.section_change = True
            self.target_sections = bytes(new_sects)

    def update_speed_from_aog(self, speed_kmh: float):
        self.current_speed_kmh = speed_kmh

    # ---- machine response parsing ----

    def handle_frame(self, buf: bytes):
        """Process a complete frame received from the ASD terminal."""
        self.last_valid_machine_time = time.time()

        if len(buf) < 3:
            logger.debug(f"RX SHORT frame: {buf.hex()}")
            return

        cmd = buf[1]  # Command/response type (byte after STX)

        f = decode_frame(buf)
        if f is not None and not f.crc_ok:
            logger.warning(f"RX BAD CRC: {f}")
        if f is not None and f.obj == 0x00 and f.typ == REPLY_REJECT:
            self._handle_reject(f)
            return

        if cmd == RESP_INIT:
            self._handle_init_response(buf)
        elif cmd == RESP_SECTION:
            self._handle_section_response(buf)
        else:
            logger.info(f"RX UNKNOWN cmd=0x{cmd:02X}: {buf.hex()}")

    def _handle_reject(self, f: ASDFrame):
        """Terminal replied 00 04 03 <obj> <type> <code>: request refused."""
        key = f.data[:3]
        if key not in self.rejects_seen:
            self.rejects_seen.add(key)
            obj, typ, code = (list(key) + [0, 0, 0])[:3]
            logger.warning(f"ASD REJECTED obj=0x{obj:02X} type=0x{typ:02X} "
                           f"code=0x{code:02X} (first occurrence)")
        else:
            logger.debug(f"ASD rejected {f}")

    def _handle_init_response(self, buf: bytes):
        """Handle Init Response from ASD terminal."""
        if parse_init_response(buf):
            if not self.got_init:
                f = decode_frame(buf)
                logger.info(f"ASD terminal acknowledged (INIT OK) "
                            f"data=[{f.data.hex(' ') if f else '?'}]")
                self.got_init = True

                # Send init config sequence
                if not self.init_config_sent:
                    config_frames = build_init_config(self.tool_id)
                    for frame in config_frames:
                        self.send_frame(frame, "INIT_CONFIG")
                        time.sleep(0.05)
                    self.init_config_sent = True

                    # Initial section request
                    time.sleep(0.1)
                    self.send_frame(
                        build_section_request(self.tool_id), "SECT_REQ (init)")

                # Transition to READY
                if self.state == MachineState.DISCONNECTED:
                    self.enter_ready("ASD init confirmed")
        else:
            logger.debug(f"INIT response but not ACK: {buf.hex()}")

    def _handle_section_response(self, buf: bytes):
        """Handle Section Response from ASD terminal."""
        sections = parse_section_response(buf)
        if sections is None:
            logger.debug(f"SECT response parse failed: {buf.hex()}")
            return

        sect_bytes = bytes(sections)
        if sect_bytes != self.machine_sections:
            logger.info(f"ASD sections = [{' '.join(f'{b:02X}' for b in sect_bytes)}]")
            self.machine_sections = sect_bytes

        # Update relay bytes for AOG feedback
        self.relay_lo = sect_bytes[0] if len(sect_bytes) > 0 else 0
        self.relay_hi = sect_bytes[1] if len(sect_bytes) > 1 else 0


# ---------------------------------------------------------------------------
#  AmadosRequester -- Amazone Amados: sections via per-side rate setpoints
# ---------------------------------------------------------------------------

AMADOS_POLL_S = 0.25            # one object read per tick-group (4 objects -> 1 s)
AMADOS_REFRESH_S = 10.0         # re-send side rates even if unchanged
AMADOS_SETTLE_S = 2.0           # ignore target reads this long after a write
AMADOS_REBASE_TOL = 1.5         # kg/ha; terminal rounds setpoints to integers


class AmadosRequester(ASDRequester):
    """The Amados has no section object. Instead each side (index 1 = left,
    index 2 = right) gets its own rate setpoint: every closed AgOpenGPS
    section removes 1/per_side of the base rate from its side.

    Index 0 (the machine-wide setpoint) is never written. The base rate is
    read from the terminal before the first write; afterwards, if the
    terminal's target no longer matches the average of our side rates, the
    operator changed it on the terminal and it becomes the new base.
    """

    POLL_OBJECTS = (OBJ_TARGET_RATE, OBJ_ACTUAL_RATE, OBJ_WIDTH, OBJ_SPEED)

    def __init__(self, ser: serial.Serial, section_count: int,
                 sct_hz: int, config: ConfigParser, base_rate: float = 0.0):
        super().__init__(ser, section_count, sct_hz, config)
        self.per_side = max(1, section_count // 2)
        self.section_mask = 0               # AOG sections, bit 0 = section 1
        self.have_sections = False
        self.controlling = False            # set once AgIO drives us

        self.base_rate: Optional[float] = base_rate if base_rate > 0 else None
        self.fixed_base = base_rate > 0
        self.target_avg: Optional[float] = None
        self.actual_rate: Optional[float] = None
        self.width: Optional[float] = None
        self.speed: Optional[float] = None

        self.cmd = {SIDE_LEFT: None, SIDE_RIGHT: None}
        self.last_write_time = 0.0
        self.last_poll_time = 0.0
        self.poll_idx = 0
        self.restored = False
        logger.info(f"Amados mode: {self.per_side * 2} sections, "
                    f"1-{self.per_side} = left, "
                    f"{self.per_side + 1}-{self.per_side * 2} = right, "
                    f"{100 / self.per_side:.0f}% per section, base rate "
                    + (f"{base_rate} (config)" if self.fixed_base else "from terminal"))

    # ---- side rate calculation ----

    def side_rates(self) -> dict:
        side_bits = (1 << self.per_side) - 1
        left_open = bin(self.section_mask & side_bits).count("1")
        right_open = bin((self.section_mask >> self.per_side) & side_bits).count("1")
        return {
            SIDE_LEFT: round(self.base_rate * left_open / self.per_side, 1),
            SIDE_RIGHT: round(self.base_rate * right_open / self.per_side, 1),
        }

    def write_side(self, side: int, rate: float, why: str = ""):
        name = "LEFT" if side == SIDE_LEFT else "RIGHT"
        self.send_frame(build_write_float(OBJ_TARGET_RATE, rate, self.tool_id, side),
                        f"RATE {name} {rate:.1f} kg/ha {why}".rstrip())
        self.cmd[side] = rate
        self.last_write_time = time.time()

    def apply(self, now: float, force: bool = False):
        want = self.side_rates()
        changed = [s for s in want if want[s] != self.cmd[s]]
        refresh = (now - self.last_write_time) >= AMADOS_REFRESH_S
        if changed:
            logger.info(f"Sections {self.section_mask:0{self.per_side * 2}b} -> "
                        f"left {want[SIDE_LEFT]:.1f} / right {want[SIDE_RIGHT]:.1f} kg/ha "
                        f"(base {self.base_rate:.1f})")
        for side in (want if (force or refresh) else changed):
            self.write_side(side, want[side], "(refresh)" if side not in changed else "")
            time.sleep(0.03)

    # ---- periodic loop ----

    def periodic_loop(self):
        while self.running:
            now = time.time()

            if self.state != MachineState.DISCONNECTED and \
               self.last_valid_machine_time > 0 and \
               (now - self.last_valid_machine_time) > MACHINE_TIMEOUT_S:
                self.enter_disconnected(
                    f"machine timeout {now - self.last_valid_machine_time:.1f}s")

            if self.state == MachineState.READY and self.agio_connected:
                self.enter_running("AgIO connected")

            if self.state == MachineState.DISCONNECTED:
                if (now - self.last_init_time) >= INIT_PROBE_S:
                    self.send_frame(build_init_request(), "INIT_REQ")
                    self.last_init_time = now
            else:
                if (now - self.last_init_time) >= 3.6:
                    self.send_frame(build_init_request(), "INIT_REQ (keepalive)")
                    self.last_init_time = now

                if (now - self.last_poll_time) >= AMADOS_POLL_S:
                    obj = self.POLL_OBJECTS[self.poll_idx % len(self.POLL_OBJECTS)]
                    self.poll_idx += 1
                    with self.lock:
                        self.ser.write(build_read(obj, self.tool_id))
                        self.ser.flush()
                    logger.debug(f"TX >> READ 0x{obj:02X}")
                    self.last_poll_time = now

                if self.state == MachineState.RUNNING and self.have_sections:
                    if not self.controlling:
                        logger.info("AgOpenGPS now controls the side rates")
                    self.controlling = True

                if self.controlling and self.base_rate:
                    self.apply(now)

            time.sleep(TICK_S)

    def shutdown(self):
        """Leave the machine spreading at the base rate on both sides."""
        if self.restored:
            return
        self.restored = True
        if self.controlling and self.base_rate:
            logger.info(f"Restoring both sides to base rate {self.base_rate:.1f}")
            for side in (SIDE_LEFT, SIDE_RIGHT):
                self.write_side(side, self.base_rate, "(restore)")
                time.sleep(0.05)

    # ---- AgOpenGPS input ----

    def update_sections_from_aog(self, relay_lo: int, relay_hi: int):
        mask = ((relay_hi & 0xFF) << 8 | (relay_lo & 0xFF)) & ((1 << (self.per_side * 2)) - 1)
        with self.sections_lock:
            self.section_mask = mask
            self.have_sections = True
        self._update_feedback()

    def _update_feedback(self):
        # Per-side state can't be read back, so report what we commanded,
        # but nothing while the terminal says it isn't spreading (width 0).
        mask = self.section_mask if (self.width is None or self.width > 0) else 0
        self.relay_lo = mask & 0xFF
        self.relay_hi = (mask >> 8) & 0xFF

    # ---- terminal replies ----

    def handle_frame(self, buf: bytes):
        self.last_valid_machine_time = time.time()
        f = decode_frame(buf)
        if f is None:
            logger.debug(f"RX SHORT frame: {buf.hex()}")
            return
        if not f.crc_ok:
            logger.warning(f"RX BAD CRC: {f}")
        if f.obj == 0x00 and f.typ == REPLY_REJECT:
            self._handle_reject(f)
            return
        if f.obj == 0x00 and f.typ == 0x03:
            self._handle_init_response(buf)
            return
        value = parse_float_reply(f)
        if value is None:
            logger.info(f"RX UNHANDLED {f}")
            return

        if f.obj == OBJ_TARGET_RATE:
            self.target_avg = value
            self._check_base(value)
        elif f.obj == OBJ_ACTUAL_RATE:
            self.actual_rate = value
        elif f.obj == OBJ_WIDTH:
            if value != self.width:
                logger.info(f"ASD working width = {value:.1f} m")
            self.width = value
            self._update_feedback()
        elif f.obj == OBJ_SPEED:
            self.speed = value
        logger.debug(f"RX 0x{f.obj:02X} = {value:.2f}")

    def _handle_init_response(self, buf: bytes):
        if not self.got_init:
            f = decode_frame(buf)
            logger.info(f"ASD terminal acknowledged (INIT OK) "
                        f"data=[{f.data.hex(' ') if f else '?'}]")
            self.got_init = True
        if self.state == MachineState.DISCONNECTED:
            self.enter_ready("ASD init confirmed")

    def _check_base(self, target: float):
        if self.fixed_base:
            return
        if self.cmd[SIDE_LEFT] is None or self.cmd[SIDE_RIGHT] is None:
            # Nothing written yet: the terminal's target is the base rate
            if target <= 0:
                # Most likely left closed by a run that was killed before it
                # could restore; fall back to the last base we learned.
                last = self.config.getfloat("main", "last_base_rate", fallback=0.0)
                if last > 0 and self.base_rate != last:
                    logger.warning(f"Terminal target is 0 (left closed by a previous "
                                   f"run?): using last known base rate {last:.1f} kg/ha")
                    self.base_rate = last
                elif last <= 0 and self.base_rate is not None:
                    logger.warning("Terminal target is 0 and no last_base_rate in "
                                   "config.ini: set the rate on the terminal")
                    self.base_rate = None
                return
            if target != self.base_rate:
                logger.info(f"Base rate from terminal: {target:.1f} kg/ha")
                self._set_base(target)
            return
        if time.time() - self.last_write_time < AMADOS_SETTLE_S:
            return
        expected = (self.cmd[SIDE_LEFT] + self.cmd[SIDE_RIGHT]) / 2
        if abs(target - expected) > AMADOS_REBASE_TOL and target > 0:
            logger.info(f"Target changed on terminal (expected {expected:.1f}, "
                        f"now {target:.1f}): new base rate {target:.1f} kg/ha")
            self._set_base(target)
            self.cmd = {SIDE_LEFT: None, SIDE_RIGHT: None}   # force re-apply

    def _set_base(self, rate: float):
        """Adopt a learned base rate and remember it for the next start."""
        self.base_rate = rate
        try:
            if self.config.get("main", "last_base_rate", fallback="") != f"{rate:g}":
                self.config.set("main", "last_base_rate", f"{rate:g}")
                save_config(self.config)
        except Exception as e:
            logger.warning(f"Could not save last_base_rate: {e}")


# ---------------------------------------------------------------------------
#  Thread functions
# ---------------------------------------------------------------------------

def receiver_loop(ser: serial.Serial, parser: ASDStreamParser,
                  req: ASDRequester):
    """Thread: reads serial data from ASD terminal, parses frames."""
    while req.running:
        try:
            data = ser.read(256)
            if not data:
                continue

            logger.debug(f"RX raw ({len(data)}): {data.hex(' ')}")

            for frame in parser.feed(data):
                logger.debug(f"RX << [{frame.hex()}]")
                req.handle_frame(frame)

        except Exception as e:
            logger.warning(f"RX error: {e}")
            time.sleep(0.2)


def udp_listener_loop(req: ASDRequester, comms_lost_zero: bool,
                      subnet: str):
    """Thread: receives AgOpenGPS PGNs via UDP, updates shared state,
    and sends Hello reply + From Machine PGN back to AgIO."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("", UDP_PORT))
    sock.settimeout(UDP_TIMEOUT_S)
    logger.info(f"UDP listening on port {UDP_PORT}")

    broadcast = (subnet, AOG_PORT)
    got_e5 = False
    logger.info(f"UDP broadcast -> {broadcast}")

    while req.running:
        try:
            data, addr = sock.recvfrom(1024)
        except socket.timeout:
            if req.agio_connected:
                logger.info("AgIO timeout -- connection lost")
                req.agio_connected = False
                if comms_lost_zero:
                    req.update_sections_from_aog(0x00, 0x00)
                    req.update_speed_from_aog(0.0)
                if req.state == MachineState.RUNNING:
                    req.enter_ready("AgIO timeout")
            continue
        except OSError:
            if not req.running:
                break
            raise

        if len(data) < 5:
            continue
        if data[0] != 0x80 or data[1] != 0x81:
            continue

        pgn = data[3]

        if pgn == 0xC8:  # AgIO Hello
            if not req.agio_connected:
                version = data[5] if len(data) > 5 else 0
                logger.info(f"AgIO connected (version {version / 10:.1f})")
            req.agio_connected = True

            # Reply when machine is at least READY
            if req.state in (MachineState.READY, MachineState.RUNNING):
                reply = build_hello_reply(req.relay_lo, req.relay_hi)
                sock.sendto(reply, broadcast)

        elif pgn == 0xE5:  # 64-section state (AgIO -> machine)
            # 8 bytes, byte0 = sections 1-8 ... byte7 = sections 57-64.
            # Authoritative in current AgIO; see the 0xEF note below.
            if len(data) >= 5 + 8:
                if not got_e5:
                    logger.info("AgIO sends PGN 0xE5, using it for section state")
                got_e5 = True
                req.update_sections_from_aog(data[5], data[6])
                logger.debug(f"AgIO 0xE5 sections lo=0x{data[5]:02X} hi=0x{data[6]:02X}")

        elif pgn == 0xEF:  # Machine Data -- section bits
            if len(data) > 12:
                # The 0xEF section bytes are stale in current AgIO and fight
                # with 0xE5 (same finding as the TUVR bridge): only use them
                # when AgIO doesn't send 0xE5.
                if not got_e5:
                    req.update_sections_from_aog(data[11], data[12])
                    logger.debug(f"AgIO 0xEF sections lo=0x{data[11]:02X} "
                                 f"hi=0x{data[12]:02X}")

                # Feedback with no section bits, as AOG drives the sections
                # (auto mode). AOG reads ON/OFF bits in 0xEA as physical
                # section switches and puts those sections into manual mode;
                # relay bits in 0xED make it revert its own commands.
                if req.state == MachineState.RUNNING:
                    sock.sendto(build_section_data(0, 0, 0, 0), broadcast)
                    sock.sendto(build_from_machine(0, 0), broadcast)

        elif pgn == 0xFE:  # Steer Data -- speed
            if len(data) > 6:
                spd = int.from_bytes(data[5:7], "little", signed=False) * 0.1
                req.update_speed_from_aog(spd)
                logger.debug(f"AgIO speed={spd:.1f} km/h")
            if len(data) > 12 and not got_e5:
                req.update_sections_from_aog(data[11], data[12])


def keyboard_loop(req: ASDRequester):
    """Thread: keyboard input. X = exit."""
    logger.info("Keyboard: X = exit")
    while req.running:
        if msvcrt.kbhit():
            key = msvcrt.getch()
            if key in (b"x", b"X"):
                req.running = False
                logger.info("Exit requested")
                break
        time.sleep(0.05)


# ---------------------------------------------------------------------------
#  Console close (window X, logoff, shutdown)
# ---------------------------------------------------------------------------

_console_handler = None     # keep a reference, or ctypes frees the callback


def install_console_close_handler(req: ASDRequester, ser: serial.Serial):
    """Run the normal shutdown (restore rates) when the console window is
    closed. Windows gives the process ~5 s after CTRL_CLOSE_EVENT."""
    global _console_handler
    import ctypes
    from ctypes import wintypes

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    def handler(event):
        if event in (2, 5, 6):      # CLOSE, LOGOFF, SHUTDOWN
            logger.info("Console closing -- shutting down")
            req.running = False
            time.sleep(0.3)
            try:
                req.shutdown()
                time.sleep(0.2)
                ser.close()
            except Exception as e:
                logger.warning(f"Shutdown on close failed: {e}")
            logging.shutdown()
            return True
        return False                # Ctrl+C: default -> KeyboardInterrupt

    _console_handler = handler
    if not ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, True):
        logger.warning("Could not install console close handler")


# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------

def main():
    print("AOG-ASD Bridge  (AgOpenGPS -> ASD section control terminal)")
    print()

    # --- config ---
    config = load_config()
    saved_com = config.get("main", "com", fallback="0")
    comms_lost_zero = config.getboolean("main", "comms_lost_zero", fallback=True)
    section_count = config.getint("main", "sections", fallback=DEFAULT_SECTION_COUNT)
    sct_hz = config.getint("main", "sct_hz", fallback=2)
    subnet = config.get("main", "subnet", fallback="255.255.255.255")
    machine = config.get("main", "machine", fallback="quantron").strip().lower()
    base_rate = config.getfloat("main", "base_rate", fallback=0.0)

    print(f"Config: machine={machine}  sections={section_count}  SCT={sct_hz}Hz  "
          f"comms_lost_zero={comms_lost_zero}  subnet={subnet}")
    print()

    # --- COM port selection ---
    available = {p.device for p in serial.tools.list_ports.comports()}
    if saved_com != "0" and saved_com in available:
        print(f"Using saved COM port: {saved_com}")
        port = saved_com
    else:
        if saved_com != "0":
            print(f"Saved port {saved_com} not found.")
        port = select_port()
        if not port:
            return
        config.set("main", "com", port)
        save_config(config)

    logger.info(f"Opening {port} @ {BAUD} baud")

    ser = serial.Serial(
        port=port,
        baudrate=BAUD,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=0.05,
    )

    parser = ASDStreamParser()
    if machine == "amados":
        requester = AmadosRequester(ser, section_count, sct_hz, config, base_rate)
    else:
        requester = ASDRequester(ser, section_count, sct_hz, config)

    # --- start threads ---
    threads = [
        threading.Thread(target=udp_listener_loop,
                         args=(requester, comms_lost_zero, subnet), daemon=True),
        threading.Thread(target=receiver_loop,
                         args=(ser, parser, requester), daemon=True),
        threading.Thread(target=requester.periodic_loop, daemon=True),
        threading.Thread(target=keyboard_loop,
                         args=(requester,), daemon=True),
    ]
    for t in threads:
        t.start()

    install_console_close_handler(requester, ser)

    try:
        while requester.running:
            time.sleep(0.2)
    except KeyboardInterrupt:
        requester.running = False
        logger.info("KeyboardInterrupt")
    finally:
        requester.running = False
        time.sleep(0.3)
        try:
            requester.shutdown()
        except Exception as e:
            logger.warning(f"Shutdown failed: {e}")
        ser.close()
        logger.info("Serial port closed")


if __name__ == "__main__":
    main()
