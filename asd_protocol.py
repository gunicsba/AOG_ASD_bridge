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

from typing import List, Tuple, Optional

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
    """
    frames = []
    for cmd in (0x25, 0x35):
        crc = (0xFF - cmd - 0x02 - 0x02 - tool_id[0] - tool_id[1]) & 0xFF

        frame = bytearray([STX])
        frame.extend(_esc_byte(cmd))
        frame.extend([ESC, 0x02])   # structural field (always escaped)
        frame.extend([ESC, 0x02])   # structural field (always escaped)
        frame.extend(_esc_byte(tool_id[0]))
        frame.extend(_esc_byte(tool_id[1]))
        frame.extend(_esc_byte(crc))
        frame.append(ETX)
        frames.append(bytes(frame))
    return frames


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
        self.prev_byte: Optional[int] = None

    def feed(self, chunk: bytes) -> List[bytes]:
        """Feed raw serial bytes.

        Returns a list of complete frame buffers (including STX, excluding ETX).
        Each returned buffer starts with STX at index 0.
        """
        frames = []

        for b in chunk:
            if b == STX and self.prev_byte != ESC:
                # Start new frame
                self.in_frame = True
                self.buf = bytearray([STX])
            elif b == ETX and self.prev_byte != ESC:
                # End of frame
                if self.in_frame and len(self.buf) > 1:
                    frames.append(bytes(self.buf))
                self.in_frame = False
                self.buf = bytearray()
            elif self.in_frame:
                self.buf.append(b)

            self.prev_byte = b

            # Safety: prevent buffer overflow
            if len(self.buf) > 256:
                self.in_frame = False
                self.buf = bytearray()

        return frames
