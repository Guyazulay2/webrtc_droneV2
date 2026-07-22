"""Unit tests for KLV (MISB ST 0601) parser — no GStreamer required."""
import struct
import pytest
from klv_parser import (
    MISB_UL_KEY,
    KLVData,
    decode_ber_length,
    parse_klv_packet,
    _map_range,
)


def encode_ber(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    elif n < 0x100:
        return bytes([0x81, n])
    else:
        return bytes([0x82]) + n.to_bytes(2, "big")


def tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + encode_ber(len(value)) + value


def make_packet(**fields) -> bytes:
    inner = b""
    if "lat" in fields:
        v = int(_map_range(fields["lat"], -90, 90, -(2**31), 2**31 - 1))
        inner += tlv(13, struct.pack(">i", v))
    if "lon" in fields:
        v = int(_map_range(fields["lon"], -180, 180, -(2**31), 2**31 - 1))
        inner += tlv(14, struct.pack(">i", v))
    if "alt" in fields:
        v = int(_map_range(fields["alt"], -900, 19000, 0, 65535))
        inner += tlv(15, struct.pack(">H", v))
    if "heading" in fields:
        v = int(_map_range(fields["heading"], 0, 360, 0, 65535))
        inner += tlv(5, struct.pack(">H", v))
    if "mission_id" in fields:
        inner += tlv(3, fields["mission_id"].encode())
    if "timestamp" in fields:
        inner += tlv(2, struct.pack(">Q", fields["timestamp"]))
    return MISB_UL_KEY + encode_ber(len(inner)) + inner


class TestDecodeBerLength:
    def test_short_form(self):
        assert decode_ber_length(bytes([0x10]), 0) == (16, 1)

    def test_short_max(self):
        assert decode_ber_length(bytes([0x7F]), 0) == (127, 1)

    def test_one_byte_long_form(self):
        assert decode_ber_length(bytes([0x81, 0xC8]), 0) == (200, 2)

    def test_two_byte_long_form(self):
        assert decode_ber_length(bytes([0x82, 0x01, 0x00]), 0) == (256, 3)


class TestParseKlvPacket:
    def test_returns_none_on_short_input(self):
        assert parse_klv_packet(b"\x00" * 10) is None

    def test_returns_none_when_key_missing(self):
        assert parse_klv_packet(b"\x00" * 50) is None

    def test_basic_lat_lon(self):
        pkt = make_packet(lat=32.0853, lon=34.7818)
        result = parse_klv_packet(pkt)
        assert result is not None
        assert result.sensor_lat == pytest.approx(32.0853, abs=0.001)
        assert result.sensor_lon == pytest.approx(34.7818, abs=0.001)

    def test_altitude(self):
        pkt = make_packet(lat=0.0, lon=0.0, alt=500.0)
        result = parse_klv_packet(pkt)
        assert result.sensor_alt == pytest.approx(500.0, abs=1.0)

    def test_heading(self):
        pkt = make_packet(lat=0.0, lon=0.0, heading=270.0)
        result = parse_klv_packet(pkt)
        assert result.heading == pytest.approx(270.0, abs=0.1)

    def test_mission_id(self):
        pkt = make_packet(lat=0.0, lon=0.0, mission_id="ALPHA-1")
        result = parse_klv_packet(pkt)
        assert result.mission_id == "ALPHA-1"

    def test_timestamp(self):
        ts = 1_700_000_000_000_000
        pkt = make_packet(lat=0.0, lon=0.0, timestamp=ts)
        result = parse_klv_packet(pkt)
        assert result.unix_timestamp == ts

    def test_to_dict_keys(self):
        pkt = make_packet(lat=1.0, lon=2.0, alt=100.0, heading=45.0)
        d = parse_klv_packet(pkt).to_dict()
        assert "lat" in d
        assert "lon" in d
        assert "alt" in d
        assert "heading" in d

    def test_packet_with_prefix_garbage(self):
        pkt = b"\xFF" * 20 + make_packet(lat=10.0, lon=20.0)
        result = parse_klv_packet(pkt)
        assert result is not None
        assert result.sensor_lat == pytest.approx(10.0, abs=0.001)


class TestKLVData:
    def test_to_dict_uses_sensor_lat_over_frame(self):
        k = KLVData(sensor_lat=10.0, sensor_lon=20.0, frame_lat=99.0)
        d = k.to_dict()
        assert d["lat"] == 10.0

    def test_to_dict_falls_back_to_frame_lat(self):
        k = KLVData(frame_lat=55.0, frame_lon=37.0)
        d = k.to_dict()
        assert d["lat"] == 55.0

    def test_empty_to_dict_has_none_values(self):
        k = KLVData()
        d = k.to_dict()
        assert d["lat"] is None
        assert d["heading"] is None
