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
import struct
import sys
from configparser import ConfigParser
from enum import Enum, auto
from typing import Callable, Optional

import log_archive
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
LOG_DIR = os.path.join(APP_DIR, "logs")
LOG_PATH = os.path.join(LOG_DIR, f"{exe_name}_{time.strftime('%Y%m%d_%H%M%S')}.log")
# Logs of older versions were written next to the exe
LEGACY_LOG_PREFIXES = ("AOG-ASD_", "AOG_ASD_bridge_")
DEFAULT_LOG_KEEP_DAYS = 7

logger = logging.getLogger("asd")


def setup_logging(console: bool):
    """Console (if any) stays at INFO; the file gets DEBUG (raw RX bytes,
    AgIO values)."""
    os.makedirs(LOG_DIR, exist_ok=True)
    handlers = []
    if console and sys.stderr is not None:
        h = logging.StreamHandler()
        h.setLevel(LOG_LEVEL)
        h.setFormatter(logging.Formatter(
            "[%(asctime)s.%(msecs)03d] %(levelname)s %(message)s", datefmt="%H:%M:%S"))
        handlers.append(h)
    f = logging.FileHandler(LOG_PATH, mode='w', encoding='utf-8')
    f.setLevel(logging.DEBUG)
    f.setFormatter(logging.Formatter(
        "[%(asctime)s.%(msecs)03d] %(levelname)s [%(threadName)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"))
    handlers.append(f)
    logging.basicConfig(level=logging.DEBUG, handlers=handlers)
    logger.info(f"Logging to file: {LOG_PATH}")


def archive_logs(config: ConfigParser):
    keep_days = config.getint("main", "log_keep_days", fallback=DEFAULT_LOG_KEEP_DAYS)
    try:
        archived, pruned = log_archive.archive_old_logs(
            LOG_DIR, LOG_PATH, keep_days,
            legacy_dir=APP_DIR, legacy_prefixes=LEGACY_LOG_PREFIXES)
    except Exception as e:
        logger.warning(f"Log archiving failed: {e}")
        return
    if archived or pruned:
        logger.info(f"Logs: {archived} older run(s) zipped into "
                    f"{os.path.join(LOG_DIR, log_archive.ARCHIVE_SUBDIR)}"
                    + (f", {pruned} archive(s) older than {keep_days} days removed"
                       if pruned else ""))


# ---------------------------------------------------------------------------
#  Config
# ---------------------------------------------------------------------------
CONFIG_PATH = os.path.join(APP_DIR, "config.ini")


def load_config() -> ConfigParser:
    config = ConfigParser()
    if not os.path.exists(CONFIG_PATH):
        config["main"] = {
            "com": "0",
            "sections": str(DEFAULT_SECTION_COUNT),
            "machine": "auto",
            "startup_scan": "1",
            "sct_hz": "2",
            "subnet": "255.255.255.255",
            "log_keep_days": str(DEFAULT_LOG_KEEP_DAYS),
        }
        with open(CONFIG_PATH, "w") as f:
            config.write(f)
    else:
        config.read(CONFIG_PATH)
    if not config.has_section("main"):
        config.add_section("main")

    # base_rate used to pin the Amados rate and ignore the terminal. The rate
    # now always comes from the terminal; keep the value as the remembered
    # fallback for when the terminal reports 0.
    legacy = config.getfloat("main", "base_rate", fallback=0.0)
    if config.has_option("main", "base_rate"):
        if legacy > 0 and config.getfloat("main", "last_base_rate", fallback=0.0) <= 0:
            config.set("main", "last_base_rate", f"{legacy:g}")
        config.remove_option("main", "base_rate")
        try:
            save_config(config)
        except OSError as e:
            logger.warning(f"Could not update config.ini: {e}")
        if legacy > 0:
            logger.info(f"config.ini: base_rate = {legacy:g} no longer overrides the "
                        f"terminal; the rate is read from the terminal and set in "
                        f"the bridge window")
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

    def on_agio_lost(self):
        # Hand the machine back fully open: all sections on, GPS auto flag
        # off (agio_connected is cleared first). periodic_loop sends it.
        self.agio_connected = False
        all_on = (1 << min(self.section_count, 16)) - 1
        logger.info("AgIO lost: all sections on")
        self.update_sections_from_aog(all_on & 0xFF, all_on >> 8)
        self.update_speed_from_aog(0.0)
        if self.state == MachineState.RUNNING:
            self.enter_ready("AgIO timeout")

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

            # --- READY / RUNNING: send section commands on change (READY:
            #     the all-on hand-back after AgIO was lost) ---
            if self.state in (MachineState.READY, MachineState.RUNNING) \
                    and self.section_change:
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
AOG_SECTIONS_TIMEOUT_S = 3.0    # no section PGN this long -> hand back to terminal


class AmadosRequester(ASDRequester):
    """The Amados has no section object. Instead each side (index 1 = left,
    index 2 = right) gets its own rate setpoint: every closed AgOpenGPS
    section removes 1/per_side of the base rate from its side.

    Index 0 (the machine-wide setpoint) is never written. The base rate
    always comes from the terminal: it is read before the first write, and
    afterwards, if the terminal's target no longer matches the average of
    our side rates, the operator changed it on the terminal and it becomes
    the new base. The remembered rate (last_base_rate) is only used when
    the terminal reports 0 before we wrote anything (left closed by a run
    that could not restore). A 0 we caused ourselves (all sections closed)
    is ignored. The operator can also set the base from the bridge window.
    """

    POLL_OBJECTS = (OBJ_TARGET_RATE, OBJ_ACTUAL_RATE, OBJ_WIDTH, OBJ_SPEED)

    # Where base_rate came from (shown in the window)
    SRC_TERMINAL = "terminal"
    SRC_REMEMBERED = "remembered"
    SRC_PC = "pc"

    def __init__(self, ser: serial.Serial, section_count: int,
                 sct_hz: int, config: ConfigParser):
        super().__init__(ser, section_count, sct_hz, config)
        self.per_side = max(1, section_count // 2)
        self.section_mask = 0               # AOG sections, bit 0 = section 1
        self.have_sections = False
        self.last_sections_time = 0.0
        self.controlling = False            # True while AgOpenGPS drives us

        self.base_rate: Optional[float] = None
        self.base_source: Optional[str] = None
        self.terminal_zero = False          # terminal read 0 before our first write
        self.pending_base: Optional[float] = None   # set from the window
        self.config_error = ""
        self.target_avg: Optional[float] = None
        self.actual_rate: Optional[float] = None
        self.width: Optional[float] = None
        self.speed: Optional[float] = None

        self.cmd = {SIDE_LEFT: None, SIDE_RIGHT: None}
        self.resend = False
        self.last_write_time = 0.0
        self.last_poll_time = 0.0
        self.poll_idx = 0
        self.restored = False
        logger.info(f"Amados mode: {self.per_side * 2} sections, "
                    f"1-{self.per_side} = left, "
                    f"{self.per_side + 1}-{self.per_side * 2} = right, "
                    f"{100 / self.per_side:.0f}% per section, base rate from terminal")

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
        refresh = (now - self.last_write_time) >= AMADOS_REFRESH_S or self.resend
        self.resend = False
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

                sections_age = now - self.last_sections_time
                if self.controlling and sections_age > AOG_SECTIONS_TIMEOUT_S:
                    self.release(f"no section data from AgOpenGPS for "
                                 f"{sections_age:.1f}s")
                elif (not self.controlling and self.have_sections
                      and self.state == MachineState.RUNNING
                      and sections_age <= AOG_SECTIONS_TIMEOUT_S):
                    logger.info("AgOpenGPS now controls the side rates")
                    self.controlling = True

                if self.pending_base is not None:
                    self._apply_pc_base(now)

                if self.controlling and self.base_rate:
                    self.apply(now)

            time.sleep(TICK_S)

    def request_base(self, rate: float):
        """Called from the window: use this base rate and send it to the
        terminal (written by periodic_loop)."""
        self.pending_base = rate

    def _apply_pc_base(self, now: float):
        rate, self.pending_base = self.pending_base, None
        logger.info(f"Base rate set in the bridge window: {rate:g} kg/ha")
        self._set_base(rate, self.SRC_PC)
        self.terminal_zero = False
        if self.controlling:
            self.apply(now, force=True)
        else:
            # Not driven by AgOpenGPS: both sides at the new base, so the
            # terminal shows the new rate right away
            for side in (SIDE_LEFT, SIDE_RIGHT):
                self.write_side(side, rate, "(set in window)")
                time.sleep(0.03)

    def release(self, reason: str):
        """Hand the machine back to the terminal: both sides to the base
        rate, stop controlling until AgOpenGPS sends sections again."""
        if self.base_rate and any(v is not None for v in self.cmd.values()):
            logger.info(f"{reason}: restoring both sides to base rate "
                        f"{self.base_rate:.1f}")
            for side in (SIDE_LEFT, SIDE_RIGHT):
                self.write_side(side, self.base_rate, "(restore)")
                time.sleep(0.05)
        else:
            logger.info(f"{reason}: releasing control")
        self.controlling = False
        self.have_sections = False
        # Forget our side values so the terminal's target is read as the
        # base again (picks up changes made on the terminal meanwhile).
        self.cmd = {SIDE_LEFT: None, SIDE_RIGHT: None}

    def shutdown(self):
        """Leave the machine spreading at the base rate on both sides."""
        if self.restored:
            return
        self.restored = True
        if self.controlling:
            self.release("Bridge closing")

    def on_agio_lost(self):
        # Don't touch the sections: periodic_loop hands the machine back to
        # the terminal (both sides at the base rate = all sections on) once
        # section data stops arriving.
        self.agio_connected = False
        if self.state == MachineState.RUNNING:
            self.enter_ready("AgIO timeout")

    # ---- AgOpenGPS input ----

    def update_sections_from_aog(self, relay_lo: int, relay_hi: int):
        mask = ((relay_hi & 0xFF) << 8 | (relay_lo & 0xFF)) & ((1 << (self.per_side * 2)) - 1)
        with self.sections_lock:
            self.section_mask = mask
            self.have_sections = True
            self.last_sections_time = time.time()
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
        # A read that crossed one of our writes still shows the old value
        # (e.g. 0 right after release() restored a fully closed machine)
        if time.time() - self.last_write_time < AMADOS_SETTLE_S:
            return

        if self.cmd[SIDE_LEFT] is None or self.cmd[SIDE_RIGHT] is None:
            # Nothing of ours on the terminal: its target is the base rate
            if target > 0:
                self.terminal_zero = False
                if target != self.base_rate or self.base_source != self.SRC_TERMINAL:
                    logger.info(f"Base rate from terminal: {target:.1f} kg/ha")
                    self._set_base(target, self.SRC_TERMINAL)
                return
            # Terminal at 0: most likely left closed by a run that was killed
            # or lost power before it could restore. Keep a base we already
            # have, else fall back to the remembered one.
            first = not self.terminal_zero
            self.terminal_zero = True
            if self.base_rate is not None:
                return
            last = self.config.getfloat("main", "last_base_rate", fallback=0.0)
            if last > 0:
                logger.warning(f"Terminal target is 0 (left closed by a previous "
                               f"run?): using last known base rate {last:.1f} kg/ha")
                self._set_base(last, self.SRC_REMEMBERED)
            elif first:
                logger.warning("Terminal target is 0 and no rate is remembered: "
                               "set the rate on the terminal or in the bridge window")
            return

        expected = (self.cmd[SIDE_LEFT] + self.cmd[SIDE_RIGHT]) / 2
        if target > 0:
            self.terminal_zero = False
            if abs(target - expected) > AMADOS_REBASE_TOL:
                logger.info(f"Target changed on terminal (expected {expected:.1f}, "
                            f"now {target:.1f}): new base rate {target:.1f} kg/ha")
                self._set_base(target, self.SRC_TERMINAL)
                self.cmd = {SIDE_LEFT: None, SIDE_RIGHT: None}   # force re-apply
        elif expected > 0 and self.controlling:
            # We sent rates but the terminal reads 0: keep the base, send again.
            # (0 while all sections are closed is our own doing: ignored.)
            if not self.terminal_zero:
                logger.warning(f"Terminal target reads 0 although {expected:.1f} was "
                               f"sent: keeping base {self.base_rate:.1f}, sending again")
            self.terminal_zero = True
            self.resend = True

    def _set_base(self, rate: float, source: str):
        """Adopt a base rate and remember it for the next start."""
        self.base_rate = rate
        self.base_source = source
        if source == self.SRC_REMEMBERED:
            return
        try:
            if self.config.get("main", "last_base_rate", fallback="") != f"{rate:g}":
                self.config.set("main", "last_base_rate", f"{rate:g}")
                save_config(self.config)
            self.config_error = ""
        except Exception as e:
            if not self.config_error:
                logger.warning(f"Could not save last_base_rate: {e}")
            self.config_error = str(e)


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
            if not req.running:         # port closed by Bridge.stop()
                break
            logger.warning(f"RX error: {e}")
            time.sleep(0.2)


def open_udp_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    if hasattr(socket, "SIO_UDP_CONNRESET"):
        # Windows: an ICMP "port unreachable" for one of our replies would
        # make the next recvfrom() fail with WinError 10054
        sock.ioctl(socket.SIO_UDP_CONNRESET, False)
    sock.bind(("", UDP_PORT))
    sock.settimeout(UDP_TIMEOUT_S)
    logger.info(f"UDP listening on port {UDP_PORT}")
    return sock


def udp_listener_loop(sock: socket.socket, req: ASDRequester, subnet: str):
    """Thread: receives AgOpenGPS PGNs via UDP, updates shared state,
    and sends Hello reply + From Machine PGN back to AgIO."""
    broadcast = (subnet, AOG_PORT)
    got_e5 = False
    logger.info(f"UDP broadcast -> {broadcast}")

    while req.running:
        try:
            data, addr = sock.recvfrom(1024)
        except socket.timeout:
            if req.agio_connected:
                logger.info("AgIO timeout -- connection lost")
                req.on_agio_lost()
            continue
        except ConnectionResetError:
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
#  Startup object scan + machine auto-detection
# ---------------------------------------------------------------------------

SCAN_REPLY_S = 0.25


def _ask(ser: serial.Serial, parser: ASDStreamParser, frame: bytes,
         timeout_s: float = SCAN_REPLY_S):
    """Send one frame, return the first decoded reply (or None)."""
    ser.reset_input_buffer()
    ser.write(frame)
    ser.flush()
    end = time.time() + timeout_s
    while time.time() < end:
        for fr in parser.feed(ser.read(ser.in_waiting or 1)):
            f = decode_frame(fr)
            if f is not None:
                return f
    return None


def _describe_value(data: bytes) -> str:
    v = data[2:]                                # after the tool ID
    out = f"raw=[{v.hex(' ')}]"
    if len(v) >= 4:
        four = v[-4:]
        out += (f"  float={struct.unpack('<f', four)[0]:.6g}"
                f"  u32={struct.unpack('<I', four)[0]}")
    return out


def scan_objects(ser: serial.Serial, wait_s: float = 15.0,
                 progress: Optional[Callable[[float], None]] = None,
                 stop: Optional[Callable[[], bool]] = None) -> set:
    """Read every object 0x00-0xFF once (reads only, nothing is written) and
    log what the terminal supports. Returns the objects that answered with
    data. Waits up to wait_s for the terminal to answer the init first.
    progress(fraction) is called per object; stop() aborts the scan."""
    stop = stop or (lambda: False)
    parser = ASDStreamParser()
    end = time.time() + wait_s
    while True:
        f = _ask(ser, parser, build_init_request(), 0.5)
        if f is not None and f.obj == 0x00 and f.typ == 0x03:
            break
        if time.time() > end:
            logger.warning("Startup scan skipped: terminal not answering init")
            return set()
        if stop():
            return set()
        time.sleep(0.5)

    logger.info("Startup scan: reading objects 0x00-0xFF (read only, ~10 s) ...")
    answered, rejected, silent = set(), {}, []
    for obj in range(0x100):
        if stop():
            logger.info("Startup scan aborted")
            return answered
        if progress:
            progress(obj / 0x100)
        if obj == 0x01:                         # init object
            continue
        f = _ask(ser, parser, build_read(obj))
        if f is None:
            silent.append(obj)
        elif f.obj == 0x00 and f.typ == REPLY_REJECT:
            code = f.data[2] if len(f.data) > 2 else -1
            rejected.setdefault(code, []).append(obj)
        elif f.typ == 0x01 and f.obj == obj:
            answered.add(obj)
            logger.info(f"  object 0x{obj:02X}: {_describe_value(f.data)}")
        else:
            logger.info(f"  object 0x{obj:02X}: unexpected reply {f}")

    fmt = lambda objs: " ".join(f"{o:02X}" for o in objs)
    logger.info(f"Startup scan: {len(answered)} objects answer: {fmt(sorted(answered))}")
    for code, objs in sorted(rejected.items()):
        logger.info(f"Startup scan: {len(objs)} rejected (code 0x{code:02X})")
        logger.debug(f"  rejected code 0x{code:02X}: {fmt(objs)}")
    if silent:
        logger.info(f"Startup scan: no reply from {fmt(silent)}")
    return answered


def pick_machine(answered: set) -> str:
    if OBJ_TARGET_RATE in answered and RESP_SECTION not in answered:
        logger.info("Detected Amados-type terminal (rate object 0x00, no section object)")
        return "amados"
    if RESP_SECTION in answered:
        logger.warning("Detected section object 0x55: using section-bitmask mode "
                       "(EXPERIMENTAL, untested on real hardware)")
        return "quantron"
    logger.warning("Terminal type not detected, using amados mode. Set machine = "
                   "amados or quantron in config.ini to choose explicitly")
    return "amados"


# ---------------------------------------------------------------------------
#  Bridge -- serial port, requester and worker threads
# ---------------------------------------------------------------------------

class Bridge:
    """Opens the port, runs the startup scan, picks the machine mode and
    starts the worker threads. start() blocks for the scan (~10 s), so the
    window calls it from a thread; stop() hands the machine back (restores
    the base rate) and closes everything. Used by the window and --console."""

    IDLE, OPENING, SCANNING, RUNNING, ERROR = (
        "idle", "opening", "scanning", "running", "error")

    def __init__(self, config: ConfigParser):
        self.config = config
        self.port = ""
        self.machine = ""
        self.phase = self.IDLE
        self.error = ""
        self.scan_progress = 0.0
        self.ser: Optional[serial.Serial] = None
        self.sock: Optional[socket.socket] = None
        self.requester: Optional[ASDRequester] = None
        self.threads = []
        self._stop = threading.Event()
        self._lock = threading.Lock()       # serializes start / stop

    def start(self, port: str) -> bool:
        self._stop.clear()
        with self._lock:
            self.port, self.error, self.scan_progress = port, "", 0.0
            self.requester = None
            try:
                return self._start(port)
            except Exception as e:
                logger.error(f"Could not start on {port}: {e}")
                self._close()
                self.error = str(e)
                self.phase = self.ERROR
                return False

    def _start(self, port: str) -> bool:
        cfg = self.config
        section_count = cfg.getint("main", "sections", fallback=DEFAULT_SECTION_COUNT)
        sct_hz = cfg.getint("main", "sct_hz", fallback=2)
        subnet = cfg.get("main", "subnet", fallback="255.255.255.255")
        machine = cfg.get("main", "machine", fallback="auto").strip().lower()
        startup_scan = cfg.getboolean("main", "startup_scan", fallback=True)
        logger.info(f"Config: machine={machine}  sections={section_count}  "
                    f"SCT={sct_hz}Hz  subnet={subnet}")

        self.phase = self.OPENING
        logger.info(f"Opening {port} @ {BAUD} baud")
        self.ser = serial.Serial(
            port=port,
            baudrate=BAUD,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.05,
        )
        # Bind UDP before the scan, so a second instance fails right away
        self.sock = open_udp_socket()

        answered = set()
        if startup_scan or machine not in ("amados", "quantron"):
            self.phase = self.SCANNING
            answered = scan_objects(
                self.ser, progress=lambda f: setattr(self, "scan_progress", f),
                stop=self._stop.is_set)
        if self._stop.is_set():
            self._close()
            self.phase = self.IDLE
            return False
        if machine not in ("amados", "quantron"):
            machine = pick_machine(answered)
        elif machine == "quantron":
            logger.warning("machine = quantron: section-bitmask mode is EXPERIMENTAL, "
                           "untested on real hardware")
        self.machine = machine

        if machine == "amados":
            req = AmadosRequester(self.ser, section_count, sct_hz, cfg)
        else:
            req = ASDRequester(self.ser, section_count, sct_hz, cfg)

        self.threads = [
            threading.Thread(target=udp_listener_loop, name="udp",
                             args=(self.sock, req, subnet), daemon=True),
            threading.Thread(target=receiver_loop, name="serial-rx",
                             args=(self.ser, ASDStreamParser(), req), daemon=True),
            threading.Thread(target=req.periodic_loop, name="periodic", daemon=True),
        ]
        for t in self.threads:
            t.start()
        self.requester = req
        self.phase = self.RUNNING
        return True

    def stop(self):
        """Hand the machine back to the terminal and close the port."""
        self._stop.set()
        with self._lock:
            req = self.requester
            if req is not None:
                req.running = False
                time.sleep(0.3)
                try:
                    req.shutdown()
                except Exception as e:
                    logger.warning(f"Shutdown failed: {e}")
            was_open = self.ser is not None
            self._close()
            if was_open:
                logger.info("Serial port closed")
            if self.phase != self.ERROR:
                self.phase = self.IDLE

    def _close(self):
        for res in (self.sock, self.ser):
            if res is not None:
                try:
                    res.close()
                except Exception:
                    pass
        self.sock = self.ser = None
        for t in self.threads:
            t.join(timeout=1.0)
        self.threads = []


# ---------------------------------------------------------------------------
#  Console mode (--console): close handler (window X, logoff, shutdown)
# ---------------------------------------------------------------------------

_console_handler = None     # keep a reference, or ctypes frees the callback


def install_console_close_handler(bridge: Bridge):
    """Run the normal shutdown (restore rates) when the console window is
    closed. Windows gives the process ~5 s after CTRL_CLOSE_EVENT."""
    global _console_handler
    import ctypes
    from ctypes import wintypes

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    def handler(event):
        if event in (2, 5, 6):      # CLOSE, LOGOFF, SHUTDOWN
            logger.info("Console closing -- shutting down")
            try:
                bridge.stop()
            except Exception as e:
                logger.warning(f"Shutdown on close failed: {e}")
            logging.shutdown()
            return True
        return False                # Ctrl+C: default -> KeyboardInterrupt

    _console_handler = handler
    if not ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, True):
        logger.warning("Could not install console close handler")


def attach_console():
    """The exe is built without a console; give --console one."""
    import ctypes
    if ctypes.windll.kernel32.AllocConsole():
        sys.stdout = sys.stderr = open("CONOUT$", "w", buffering=1)
        sys.stdin = open("CONIN$", "r")


def run_console(config: ConfigParser):
    print("AOG-ASD Bridge  (AgOpenGPS -> ASD section control terminal)")
    print()

    # --- COM port selection ---
    saved_com = config.get("main", "com", fallback="0")
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

    bridge = Bridge(config)
    if not bridge.start(port):
        input("Press Enter to exit")
        return
    req = bridge.requester
    threading.Thread(target=keyboard_loop, args=(req,), daemon=True).start()
    install_console_close_handler(bridge)

    try:
        while req.running:
            time.sleep(0.2)
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt")
    finally:
        bridge.stop()


# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------

def main():
    console = "--console" in sys.argv[1:]
    if console and sys.stdout is None:
        attach_console()
    setup_logging(console)
    config = load_config()
    archive_logs(config)

    if console:
        run_console(config)
    else:
        import bridge_gui
        bridge_gui.run(sys.modules[__name__], config)


if __name__ == "__main__":
    main()
