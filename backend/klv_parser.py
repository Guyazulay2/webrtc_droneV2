"""
KLV Parser - MISB ST 0601 (UAV/Drone telemetry standard)
Decodes KLV (Key-Length-Value) metadata embedded in MPEG-2 TS streams.

Keys reference: https://www.gwg.nga.mil/misb/docs/standards/ST0601.17.pdf
"""

import struct
import math
from dataclasses import dataclass, field
from typing import Optional, Dict, Any


# MISB ST 0601 Key tags we care about
KLV_TAGS = {
    2:  "unix_timestamp",
    3:  "mission_id",
    4:  "platform_tail_number",
    5:  "platform_heading_angle",
    6:  "platform_pitch_angle",
    7:  "platform_roll_angle",
    9:  "platform_designation",
    10: "image_source_sensor",
    13: "sensor_latitude",
    14: "sensor_longitude",
    15: "sensor_true_altitude",
    16: "sensor_horizontal_fov",
    17: "sensor_vertical_fov",
    18: "sensor_relative_azimuth_angle",
    19: "sensor_relative_elevation_angle",
    20: "sensor_relative_roll_angle",
    21: "slant_range",
    23: "frame_center_latitude",
    24: "frame_center_longitude",
    25: "frame_center_elevation",
    26: "offset_corner_lat_1",
    27: "offset_corner_lon_1",
    28: "offset_corner_lat_2",
    29: "offset_corner_lon_2",
    30: "offset_corner_lat_3",
    31: "offset_corner_lon_3",
    32: "offset_corner_lat_4",
    33: "offset_corner_lon_4",
    40: "target_width",
    65: "uas_lds_version",
    82: "airspeed",
    94: "magnetic_heading",
}

# MISB ST 0601 Universal Key (16 bytes) — the "outer" KLV packet marker
MISB_UL_KEY = bytes([
    0x06, 0x0E, 0x2B, 0x34, 0x02, 0x0B, 0x01, 0x01,
    0x0E, 0x01, 0x03, 0x01, 0x01, 0x00, 0x00, 0x00
])


@dataclass
class KLVData:
    """Parsed telemetry data from KLV stream."""
    # Platform position
    sensor_lat: Optional[float] = None
    sensor_lon: Optional[float] = None
    sensor_alt: Optional[float] = None

    # Frame center (where camera is pointing)
    frame_lat: Optional[float] = None
    frame_lon: Optional[float] = None
    frame_alt: Optional[float] = None

    # Platform attitude
    heading: Optional[float] = None
    pitch: Optional[float] = None
    roll: Optional[float] = None

    # Other fields
    unix_timestamp: Optional[int] = None
    mission_id: Optional[str] = None
    platform_designation: Optional[str] = None
    airspeed: Optional[float] = None
    slant_range: Optional[float] = None
    hfov: Optional[float] = None
    vfov: Optional[float] = None

    # Raw all-fields dict for forwarding to UI
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "lat": self.sensor_lat or self.frame_lat,
            "lon": self.sensor_lon or self.frame_lon,
            "alt": self.sensor_alt,
            "heading": self.heading,
            "pitch": self.pitch,
            "roll": self.roll,
            "airspeed": self.airspeed,
            "slant_range": self.slant_range,
            "mission_id": self.mission_id,
            "platform": self.platform_designation,
            "timestamp": self.unix_timestamp,
            "frame_lat": self.frame_lat,
            "frame_lon": self.frame_lon,
            "hfov": self.hfov,
            "vfov": self.vfov,
        }


def decode_ber_length(data: bytes, offset: int):
    """Decode BER-OID length encoding. Returns (length, bytes_consumed)."""
    first = data[offset]
    if first < 0x80:
        return first, 1
    elif first == 0x81:
        return data[offset + 1], 2
    elif first == 0x82:
        return struct.unpack(">H", data[offset+1:offset+3])[0], 3
    elif first == 0x83:
        return struct.unpack(">I", b'\x00' + data[offset+1:offset+4])[0], 4
    else:
        # BER-OID multi-byte (used in some implementations)
        length = 0
        num_bytes = first & 0x7F
        for i in range(num_bytes):
            length = (length << 8) | data[offset + 1 + i]
        return length, 1 + num_bytes


def _map_range(val: int, from_min: int, from_max: int,
               to_min: float, to_max: float) -> float:
    """Linear mapping from integer range to float range."""
    return to_min + (val - from_min) * (to_max - to_min) / (from_max - from_min)


def _decode_value(tag: int, raw_bytes: bytes) -> Any:
    """Decode a KLV tag value according to MISB ST 0601 spec."""
    n = len(raw_bytes)

    if tag == 2:  # unix timestamp (uint64 microseconds)
        if n >= 8:
            return struct.unpack(">Q", raw_bytes[:8])[0]

    elif tag in (3, 4, 9, 10):  # string fields
        return raw_bytes.decode("utf-8", errors="replace").strip()

    elif tag in (5, 94):  # platform heading / magnetic heading (0..360)
        if n == 2:
            val = struct.unpack(">H", raw_bytes)[0]
            return round(_map_range(val, 0, 65535, 0.0, 360.0), 4)

    elif tag in (6, 7):  # pitch, roll (-20..+20 degrees)
        if n == 2:
            val = struct.unpack(">h", raw_bytes)[0]
            return round(_map_range(val, -32768, 32767, -20.0, 20.0), 4)

    elif tag == 13:  # sensor latitude
        if n == 4:
            val = struct.unpack(">i", raw_bytes)[0]
            return round(_map_range(val, -(2**31), 2**31 - 1, -90.0, 90.0), 8)

    elif tag == 14:  # sensor longitude
        if n == 4:
            val = struct.unpack(">i", raw_bytes)[0]
            return round(_map_range(val, -(2**31), 2**31 - 1, -180.0, 180.0), 8)

    elif tag == 15:  # sensor altitude (meters, -900..19000)
        if n == 2:
            val = struct.unpack(">H", raw_bytes)[0]
            return round(_map_range(val, 0, 65535, -900.0, 19000.0), 2)

    elif tag in (16, 17):  # HFOV/VFOV (0..180 degrees)
        if n == 2:
            val = struct.unpack(">H", raw_bytes)[0]
            return round(_map_range(val, 0, 65535, 0.0, 180.0), 4)

    elif tag == 21:  # slant range (meters)
        if n == 4:
            val = struct.unpack(">I", raw_bytes)[0]
            return round(_map_range(val, 0, 2**32-1, 0.0, 5_000_000.0), 2)

    elif tag == 23:  # frame center latitude
        if n == 4:
            val = struct.unpack(">i", raw_bytes)[0]
            return round(_map_range(val, -(2**31), 2**31-1, -90.0, 90.0), 8)

    elif tag == 24:  # frame center longitude
        if n == 4:
            val = struct.unpack(">i", raw_bytes)[0]
            return round(_map_range(val, -(2**31), 2**31-1, -180.0, 180.0), 8)

    elif tag == 25:  # frame center elevation
        if n == 2:
            val = struct.unpack(">H", raw_bytes)[0]
            return round(_map_range(val, 0, 65535, -900.0, 19000.0), 2)

    elif tag == 82:  # airspeed (m/s)
        if n == 2:
            val = struct.unpack(">H", raw_bytes)[0]
            return round(_map_range(val, 0, 65535, 0.0, 100.0), 2)

    elif tag == 65:  # UAS LDS version
        return raw_bytes[0] if n >= 1 else None

    return raw_bytes.hex()  # fallback: return hex string


def parse_klv_packet(data: bytes) -> Optional[KLVData]:
    """
    Parse a MISB ST 0601 KLV packet.
    data should start with the 16-byte universal key.
    Returns KLVData or None if not a valid packet.
    """
    if len(data) < 20:
        return None

    # Find the MISB universal key
    idx = data.find(MISB_UL_KEY)
    if idx < 0:
        return None

    offset = idx + 16  # skip the universal key

    # Decode outer length
    if offset >= len(data):
        return None

    outer_len, len_bytes = decode_ber_length(data, offset)
    offset += len_bytes

    if offset + outer_len > len(data):
        # Truncated packet — try to parse what we have
        outer_len = len(data) - offset

    klv = KLVData()
    end = offset + outer_len

    # Parse inner tag-length-value triplets
    while offset < end - 1:
        tag = data[offset]
        offset += 1

        if offset >= end:
            break

        try:
            val_len, len_bytes = decode_ber_length(data, offset)
            offset += len_bytes

            if offset + val_len > end:
                break

            raw_val = data[offset: offset + val_len]
            offset += val_len

            decoded = _decode_value(tag, raw_val)
            if decoded is None:
                continue

            tag_name = KLV_TAGS.get(tag, f"tag_{tag}")
            klv.raw[tag_name] = decoded

            # Assign to well-known fields
            if tag == 2:   klv.unix_timestamp = decoded
            elif tag == 3:  klv.mission_id = decoded
            elif tag == 5:  klv.heading = decoded
            elif tag == 6:  klv.pitch = decoded
            elif tag == 7:  klv.roll = decoded
            elif tag == 9:  klv.platform_designation = decoded
            elif tag == 13: klv.sensor_lat = decoded
            elif tag == 14: klv.sensor_lon = decoded
            elif tag == 15: klv.sensor_alt = decoded
            elif tag == 16: klv.hfov = decoded
            elif tag == 17: klv.vfov = decoded
            elif tag == 21: klv.slant_range = decoded
            elif tag == 23: klv.frame_lat = decoded
            elif tag == 24: klv.frame_lon = decoded
            elif tag == 25: klv.frame_alt = decoded
            elif tag == 82: klv.airspeed = decoded
            elif tag == 94: klv.heading = klv.heading or decoded

        except Exception:
            break  # malformed — stop parsing this packet

    # Only return if we got at least a position
    if klv.sensor_lat is not None or klv.frame_lat is not None:
        return klv
    return klv  # return even without position so UI shows partial data


def parse_klv_from_buffer(buf: bytes) -> Optional[KLVData]:
    """
    Scan a raw buffer (e.g. from GStreamer meta or MPEG-TS PES)
    for any embedded KLV packet and return the first one found.
    """
    return parse_klv_packet(buf)


# ── Test / demo ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Synthetic minimal KLV packet for testing
    def encode_ber(n: int) -> bytes:
        if n < 0x80:
            return bytes([n])
        elif n < 0x100:
            return bytes([0x81, n])
        else:
            return bytes([0x82]) + n.to_bytes(2, "big")

    def make_test_packet(lat: float, lon: float, alt: float, heading: float) -> bytes:
        def enc_lat(v):  return struct.pack(">i", int(_map_range(v, -90, 90, -(2**31), 2**31-1)))
        def enc_lon(v):  return struct.pack(">i", int(_map_range(v, -180, 180, -(2**31), 2**31-1)))
        def enc_alt(v):  return struct.pack(">H", int(_map_range(v, -900, 19000, 0, 65535)))
        def enc_hdg(v):  return struct.pack(">H", int(_map_range(v, 0, 360, 0, 65535)))

        def tlv(tag, val):
            return bytes([tag]) + encode_ber(len(val)) + val

        inner = (
            tlv(2, struct.pack(">Q", 1_700_000_000_000_000)) +
            tlv(13, enc_lat(lat)) +
            tlv(14, enc_lon(lon)) +
            tlv(15, enc_alt(alt)) +
            tlv(5,  enc_hdg(heading)) +
            tlv(9,  b"TEST-UAV-1")
        )

        return MISB_UL_KEY + encode_ber(len(inner)) + inner

    pkt = make_test_packet(32.0853, 34.7818, 500, 270)
    result = parse_klv_packet(pkt)
    if result:
        import json
        print("Parsed KLV data:")
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print("Failed to parse test packet")
