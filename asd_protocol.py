"""
ASD serial protocol: framing, escaping, CRC, packet builders, and stream parser.

Wire format
-----------
    STX  <payload with escape markers>  ETX

    STX = 0x02   (start of frame)
    ETX = 0x04   (end of frame)
    ESC = 0x10   (escape marker)

STX/ETX are only recognised as frame delimiters when NOT preceded by ESC.
Data bytes equal to STX, ETX or ESC are preceded by an ESC byte on the wire.
ESC markers remain in the receive buffer and must be skipped during field
extraction.

Baud rate: 19200, 8-N-1, no flow control.
"""

import struct
from typing import List, NamedTuple, Optional

# ---------------------------------------------------------------------------
#  Protocol constants
# ---------------------------------------------------------------------------
STX = 0x02
ETX = 0x04
ESC = 0x10

BAUD = 19200

# Default tool identifier (Quantron)
DEFAULT_TOOL_ID = bytes([0x05, 0x00])


# ---------------------------------------------------------------------------
#  Low-level helpers
# ---------------------------------------------------------------------------

def _esc_byte(b: int) -> bytes:
    """Return ESC + byte if byte needs escaping, else just the byte."""
    if b in (STX, ETX, ESC):
        return bytes([ESC, b])
    return bytes([b])


# ---------------------------------------------------------------------------
#  Generic frame layout (deduced from captures, both directions):
#
#      <obj> <type> <len> <data x len> <crc>     crc = -(sum of all) & 0xFF
#
#  Host requests seen: type 0x01 = write, 0x02 = read, 0x03 = init.
#  Terminal replies use obj 0x00; type 0x04 = reject, data = obj type code.
# ---------------------------------------------------------------------------

REPLY_REJECT = 0x04


class ASDFrame(NamedTuple):
    obj: int
    typ: int
    data: bytes
    crc_ok: bool

    def __str__(self):
        return (f"obj=0x{self.obj:02X} type=0x{self.typ:02X} "
                f"data=[{self.data.hex(' ')}]" + ("" if self.crc_ok else " BAD-CRC"))


def build_frame(obj: int, typ: int, data: bytes = b"") -> bytes:
    """Build a frame from its logical fields, with CRC and escaping."""
    body = bytes([obj, typ, len(data)]) + bytes(data)
    body += bytes([(-sum(body)) & 0xFF])
    frame = bytearray([STX])
    for b in body:
        frame.extend(_esc_byte(b))
    frame.append(ETX)
    return bytes(frame)


def unescape(frame: bytes) -> bytes:
    """Strip leading STX and ESC markers -> logical payload."""
    out = bytearray()
    i = 1 if frame[:1] == bytes([STX]) else 0
    while i < len(frame):
        if frame[i] == ESC and i + 1 < len(frame):
            i += 1
        out.append(frame[i])
        i += 1
    return bytes(out)


def decode_frame(frame: bytes) -> Optional["ASDFrame"]:
    """Decode a received frame (STX included, ETX excluded)."""
    p = unescape(frame)
    if len(p) < 4:
        return None
    n = p[2]
    data = p[3:3 + n]
    crc_ok = len(p) == 4 + n and (sum(p) & 0xFF) == 0
    return ASDFrame(p[0], p[1], bytes(data), crc_ok)


# ---------------------------------------------------------------------------
#  Amazone Amados objects (found by probing; data = tool(2) + index + float LE)
#
#  Reads ignore the index and return the machine-wide value. Writing
#  OBJ_TARGET_RATE with index 1 / 2 sets the left / right side rate; the
#  terminal accepts writes silently (no reply). Index 0 = whole machine.
# ---------------------------------------------------------------------------
OBJ_TARGET_RATE = 0x00      # kg/ha setpoint (read: average of both sides)
OBJ_DISTANCE = 0x10         # uint32 counter, ~1 m per count
OBJ_ACTUAL_RATE = 0x20      # kg/ha actual, averaged over width (read only)
OBJ_AREA = 0x30             # uint32 counter, ~10 m^2 per count
OBJ_WIDTH = 0x40            # active working width in m (36 / 18 / 0)
OBJ_SPEED = 0x50            # km/h

SIDE_LEFT = 1
SIDE_RIGHT = 2


def build_read(obj: int, tool_id: bytes = DEFAULT_TOOL_ID, index: int = 0) -> bytes:
    return build_frame(obj, 0x02, bytes(tool_id[:2]) + bytes([index]))


def build_write_float(obj: int, value: float, tool_id: bytes = DEFAULT_TOOL_ID,
                      index: int = 0) -> bytes:
    return build_frame(obj, 0x01, bytes(tool_id[:2]) + bytes([index])
                       + struct.pack("<f", value))


def parse_float_reply(f: "ASDFrame") -> Optional[float]:
    """Value of a type-0x01 data reply: tool + [index] + float LE (the speed
    reply 0x50 has no index byte)."""
    if f.typ != 0x01 or len(f.data) < 6:
        return None
    return struct.unpack("<f", f.data[-4:])[0]


# ---------------------------------------------------------------------------
#  Packet builders  (Host -> ASD Client)
# ---------------------------------------------------------------------------

def build_init_request() -> bytes:
    """Build Init Request frame.

    Logical payload: [0x01, 0x03, 0x02, 0x08, 0x01]
    CRC = 0xFF - payload[1] - payload[2] - payload[3] - payload[4]
    Escaping applied to each payload byte individually.
    """
    payload = [0x01, 0x03, 0x02, 0x08, 0x01]
    crc = (0xFF - payload[1] - payload[2] - payload[3] - payload[4]) & 0xFF

    frame = bytearray([STX])
    for b in payload:
        frame.extend(_esc_byte(b))
    frame.extend(_esc_byte(crc))
    frame.append(ETX)
    return bytes(frame)


def build_init_config(tool_id: bytes = DEFAULT_TOOL_ID) -> List[bytes]:
    """Build the two Init Config frames (cmd 0x25 and 0x35).

    Returns a list of two frames to send sequentially with ~50 ms gap.

    The original CRC here (0xFF - sum) was one too low; the Amados rejected
    both frames with reply 00 04 03 <cmd> 02 01. Use the generic -sum CRC.
    """
    return [build_frame(cmd, 0x02, tool_id[:2]) for cmd in (0x25, 0x35)]


def build_section_request(tool_id: bytes = DEFAULT_TOOL_ID) -> bytes:
    """Build Section Request frame.

    CRC = 0xFF - 0x58 - toolID[0] - toolID[1]
    """
    crc = (0xFF - 0x58 - tool_id[0] - tool_id[1]) & 0xFF

    frame = bytearray([STX])
    frame.extend(_esc_byte(0x55))
    frame.extend([ESC, 0x02])   # structural field
    frame.extend([ESC, 0x02])   # structural field
    frame.extend(_esc_byte(tool_id[0]))
    frame.extend(_esc_byte(tool_id[1]))
    frame.extend(_esc_byte(crc))
    frame.append(ETX)
    return bytes(frame)


def build_section_submit(sections: bytes, tool_id: bytes = DEFAULT_TOOL_ID) -> bytes:
    """Build Section Submit frame.

    sections: 4 bytes of section bitmask (sections[0] = bits 0-7, etc.)
    CRC = 0xFF - 0x5B - toolID[0] - toolID[1] - sect[0] - sect[1] - sect[2] - sect[3]
    """
    s = sections[:4].ljust(4, b'\x00')
    crc = (0xFF - 0x5B - tool_id[0] - tool_id[1]
           - s[0] - s[1] - s[2] - s[3]) & 0xFF

    frame = bytearray([STX])
    frame.extend(_esc_byte(0x55))
    frame.extend(_esc_byte(0x01))
    frame.extend(_esc_byte(0x06))
    frame.extend(_esc_byte(tool_id[0]))
    frame.extend(_esc_byte(tool_id[1]))
    for b in s:
        frame.extend(_esc_byte(b))
    frame.extend(_esc_byte(crc))
    frame.append(ETX)
    return bytes(frame)


# ---------------------------------------------------------------------------
#  Response parsing helpers
# ---------------------------------------------------------------------------

# Response command IDs (at buffer index 1, where index 0 = STX)
RESP_INIT = 0x00
RESP_SECTION = 0x55


def _skip_esc(buf: bytes, pos: int) -> int:
    """Advance pos past an ESC byte if present."""
    if pos < len(buf) and buf[pos] == ESC:
        return pos + 1
    return pos


def parse_init_response(buf: bytes) -> bool:
    """Check if buffer is a valid Init Response (cmd=0x00, sub=0x03).

    buf includes STX at [0].
    Returns True if init acknowledged.
    """
    if len(buf) < 3:
        return False
    # buf[1] = cmd, buf[2] = status
    return buf[1] == RESP_INIT and buf[2] == 0x03


def parse_section_response(buf: bytes, section_count: int = 4) -> Optional[List[int]]:
    """Parse Section Response and extract section bytes.

    buf includes STX at [0].
    Returns list of section bytes (up to 4) or None if not a section response.
    Format: buf[1]=0x55, buf[2]=0x01, sections start at position 6.
    """
    if len(buf) < 8:
        return None
    if buf[1] != RESP_SECTION or buf[2] != 0x01:
        return None

    sections = []
    pos = 6
    for _ in range(min(section_count, 4)):
        if pos >= len(buf):
            sections.append(0)
            continue
        pos = _skip_esc(buf, pos)
        if pos >= len(buf):
            sections.append(0)
            continue
        sections.append(buf[pos])
        pos += 1

    return sections


# ---------------------------------------------------------------------------
#  Stream parser
# ---------------------------------------------------------------------------

class ASDStreamParser:
    """Buffers serial bytes and yields complete ASD frames.

    Handles the ESC-aware STX/ETX framing.
    """

    def __init__(self):
        self.buf = bytearray()
        self.in_frame = False
        self.escaped = False     # previous byte was an ESC *marker*

    def feed(self, chunk: bytes) -> List[bytes]:
        """Feed raw serial bytes.

        Returns a list of complete frame buffers (including STX, excluding ETX).
        Each returned buffer starts with STX at index 0; ESC markers are kept.

        Tracks escape state rather than the previous byte, so an escaped ESC
        data byte (10 10) followed by ETX still closes the frame.
        """
        frames = []

        for b in chunk:
            if self.escaped:
                self.escaped = False
                if self.in_frame:
                    self.buf.append(b)
            elif b == ESC:
                self.escaped = True
                if self.in_frame:
                    self.buf.append(b)
            elif b == STX:
                # Start new frame
                self.in_frame = True
                self.buf = bytearray([STX])
            elif b == ETX:
                # End of frame
                if self.in_frame and len(self.buf) > 1:
                    frames.append(bytes(self.buf))
                self.in_frame = False
                self.buf = bytearray()
            elif self.in_frame:
                self.buf.append(b)

            # Safety: prevent buffer overflow
            if len(self.buf) > 256:
                self.in_frame = False
                self.buf = bytearray()

        return frames
