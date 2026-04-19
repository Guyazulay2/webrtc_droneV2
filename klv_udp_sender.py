"""
UDP Stream Sender with KLV Telemetry (MISB ST 0601)
====================================================
שולח UDP RTP/H.264 stream עם KLV מוטמע לבדיקת המערכת.

דרישות:
    pip install numpy
    apt install gstreamer1.0-tools gstreamer1.0-plugins-good \
                gstreamer1.0-plugins-bad gstreamer1.0-x264  (או: gstreamer1.0-libav)

הפעלה:
    python klv_udp_sender.py                          # וידאו + KLV
    python klv_udp_sender.py --klv-only               # KLV בלבד
    python klv_udp_sender.py --verify                 # בדיקת encoding
"""

import socket
import struct
import time
import math
import argparse
import subprocess
import threading
import sys
import os
from typing import Optional

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


# ══════════════════════════════════════════════════════════════════════════════
#  KLV ENCODING  (MISB ST 0601)
# ══════════════════════════════════════════════════════════════════════════════

MISB_UL_KEY = bytes([
    0x06, 0x0E, 0x2B, 0x34, 0x02, 0x0B, 0x01, 0x01,
    0x0E, 0x01, 0x03, 0x01, 0x01, 0x00, 0x00, 0x00
])


def _encode_ber(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    elif n < 0x100:
        return bytes([0x81, n])
    else:
        return bytes([0x82]) + n.to_bytes(2, "big")


def _map_to_int(val: float, v_min: float, v_max: float,
                i_min: int, i_max: int) -> int:
    ratio = (val - v_min) / (v_max - v_min)
    return int(i_min + ratio * (i_max - i_min))


def _tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _encode_ber(len(value)) + value


def encode_klv(lat: float, lon: float, alt: float,
               heading: float, pitch: float = 0.0, roll: float = 0.0,
               airspeed: float = 15.0, mission_id: str = "TEST-MISSION-01",
               platform: str = "TEST-UAV-1") -> bytes:
    """
    מקודד packet KLV לפי תקן MISB ST 0601.
    מחזיר bytes מוכנים לשליחה.
    """
    ts_us = int(time.time() * 1_000_000)

    inner = b""

    # Tag 2: Unix timestamp (uint64, microseconds)
    inner += _tlv(2, struct.pack(">Q", ts_us))

    # Tag 3: Mission ID (string)
    inner += _tlv(3, mission_id.encode())

    # Tag 9: Platform designation (string)
    inner += _tlv(9, platform.encode())

    # Tag 5: Platform heading (0..360 → uint16)
    hdg_raw = _map_to_int(heading % 360, 0, 360, 0, 65535)
    inner += _tlv(5, struct.pack(">H", hdg_raw))

    # Tag 6: Platform pitch (-20..+20 → int16)
    pitch_clamped = max(-20.0, min(20.0, pitch))
    pitch_raw = _map_to_int(pitch_clamped, -20, 20, -32768, 32767)
    inner += _tlv(6, struct.pack(">h", pitch_raw))

    # Tag 7: Platform roll (-20..+20 → int16)
    roll_clamped = max(-20.0, min(20.0, roll))
    roll_raw = _map_to_int(roll_clamped, -20, 20, -32768, 32767)
    inner += _tlv(7, struct.pack(">h", roll_raw))

    # Tag 13: Sensor latitude (-90..+90 → int32)
    lat_raw = _map_to_int(lat, -90, 90, -(2**31), 2**31 - 1)
    inner += _tlv(13, struct.pack(">i", lat_raw))

    # Tag 14: Sensor longitude (-180..+180 → int32)
    lon_raw = _map_to_int(lon, -180, 180, -(2**31), 2**31 - 1)
    inner += _tlv(14, struct.pack(">i", lon_raw))

    # Tag 15: Sensor altitude (-900..19000 → uint16)
    alt_clamped = max(-900.0, min(19000.0, alt))
    alt_raw = _map_to_int(alt_clamped, -900, 19000, 0, 65535)
    inner += _tlv(15, struct.pack(">H", alt_raw))

    # Tag 82: Airspeed (0..100 m/s → uint16)
    spd_clamped = max(0.0, min(100.0, airspeed))
    spd_raw = _map_to_int(spd_clamped, 0, 100, 0, 65535)
    inner += _tlv(82, struct.pack(">H", spd_raw))

    # Tag 23: Frame center latitude (= sensor lat for simplicity)
    inner += _tlv(23, struct.pack(">i", lat_raw))

    # Tag 24: Frame center longitude
    inner += _tlv(24, struct.pack(">i", lon_raw))

    # Tag 65: UAS LDS version = 13
    inner += _tlv(65, bytes([13]))

    # Assemble full packet
    return MISB_UL_KEY + _encode_ber(len(inner)) + inner


# ══════════════════════════════════════════════════════════════════════════════
#  DRONE SIMULATION
# ══════════════════════════════════════════════════════════════════════════════

class DroneSimulator:
    """
    מדמה טיסת רחפן במסלול מעגלי מעל תל אביב.
    """
    def __init__(self):
        self.center_lat = 32.0853   # תל אביב
        self.center_lon = 34.7818
        self.radius_deg = 0.008     # ~900 מטר
        self.alt = 300.0            # מטרים
        self.airspeed = 20.0        # m/s
        self.angle = 0.0            # radians
        self.angular_speed = 0.02   # rad/frame

    def step(self) -> dict:
        self.angle += self.angular_speed
        lat = self.center_lat + self.radius_deg * math.sin(self.angle)
        lon = self.center_lon + self.radius_deg * math.cos(self.angle)

        # Heading = tangent direction
        heading = (math.degrees(self.angle) + 90) % 360

        # גובה מתנדנד קלות
        alt = self.alt + 20 * math.sin(self.angle * 3)

        # Pitch/roll בפניות
        roll  = 10 * math.sin(self.angular_speed * 5)
        pitch = -2.0

        return {
            "lat": lat, "lon": lon, "alt": alt,
            "heading": heading, "pitch": pitch, "roll": roll,
            "airspeed": self.airspeed,
        }


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN SENDER
# ══════════════════════════════════════════════════════════════════════════════

def check_gstreamer() -> tuple[bool, str]:
    """בדוק אם GStreamer זמין ואיזה encoder H.264 יש."""
    try:
        r = subprocess.run(["gst-launch-1.0", "--version"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return False, ""
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False, ""

    # בדוק encoders זמינים (x264 > openh264 > avenc_h264)
    for enc in ["x264enc", "openh264enc", "avenc_h264"]:
        r = subprocess.run(["gst-inspect-1.0", enc],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return True, enc

    return False, ""


def build_gst_pipeline(host: str, port: int, fps: int, encoder: str) -> str:
    """
    בונה pipeline GStreamer ששולח H.264 RTP ל-UDP.
    videotestsrc → encoder → RTP → UDP
    """
    enc_opts = {
        "x264enc":     "x264enc tune=zerolatency bitrate=1500 speed-preset=ultrafast",
        "openh264enc": "openh264enc",
        "avenc_h264":  "avenc_h264 bitrate=1500000",
    }
    enc_str = enc_opts.get(encoder, "x264enc tune=zerolatency bitrate=1500 speed-preset=ultrafast")

    return (
        f"gst-launch-1.0 -v "
        f"videotestsrc pattern=smpte is-live=true "
        f"! video/x-raw,width=1280,height=720,framerate={fps}/1 "
        f"! videoconvert "
        f"! {enc_str} "
        f"! h264parse "
        f"! rtph264pay config-interval=1 pt=96 "
        f"! udpsink host={host} port={port} sync=false async=false"
    )


def run_sender(host: str, port: int, fps: int, klv_only: bool):
    """
    מריץ שני threads במקביל:
      1. GStreamer subprocess → H.264 RTP → UDP (וידאו)
      2. Python loop → KLV UDP packets (טלמטריה)
    """
    drone  = DroneSimulator()
    stop_event = threading.Event()

    # ── Thread 1: GStreamer video ───────────────────────────────────────────
    gst_proc = None
    if not klv_only:
        has_gst, encoder = check_gstreamer()
        if has_gst:
            gst_cmd = build_gst_pipeline(host, port, fps, encoder)
            print(f"[✓] GStreamer encoder: {encoder}")
            print(f"    pipeline: {gst_cmd}\n")
            gst_proc = subprocess.Popen(
                gst_cmd, shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )

            def watch_gst():
                while not stop_event.is_set():
                    line = gst_proc.stderr.readline()
                    if not line:
                        break
                    txt = line.decode(errors="replace").strip()
                    # הדפס רק שגיאות — לא spam
                    if "ERROR" in txt or "WARNING" in txt:
                        print(f"  [GST] {txt}")

            threading.Thread(target=watch_gst, daemon=True).start()
        else:
            print("[!] GStreamer או H.264 encoder לא נמצאו")
            print("    התקן: apt install gstreamer1.0-tools gstreamer1.0-plugins-ugly")
            print("    ממשיך עם KLV בלבד...\n")
            klv_only = True

    # ── Thread 2: KLV sender ────────────────────────────────────────────────
    klv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # KLV נשלח לפורט נפרד כדי לא להתנגש עם ה-RTP
    klv_port = port + 1 if not klv_only else port

    print(f"[→] שולח UDP stream ל-{host}:{port}")
    if not klv_only:
        print(f"    וידאו: RTP H.264  port={port}")
        print(f"    KLV:   raw UDP    port={klv_port}")
    else:
        print(f"    KLV:   raw UDP    port={klv_port}")
    print(f"    FPS={fps}  |  טיסה מדומה מעל תל אביב")
    print(f"    Ctrl+C לעצירה\n")

    frame_interval = 1.0 / fps
    frame_num = 0

    try:
        while True:
            t_start = time.time()
            telemetry = drone.step()

            klv_bytes = encode_klv(
                lat=telemetry["lat"],
                lon=telemetry["lon"],
                alt=telemetry["alt"],
                heading=telemetry["heading"],
                pitch=telemetry["pitch"],
                roll=telemetry["roll"],
                airspeed=telemetry["airspeed"],
            )
            klv_sock.sendto(klv_bytes, (host, klv_port))

            frame_num += 1
            if frame_num % fps == 0:
                status = "📡 KLV+Video" if (gst_proc and gst_proc.poll() is None) else "📡 KLV only"
                print(f"  {status} [frame {frame_num:05d}]  "
                      f"lat={telemetry['lat']:.5f}  "
                      f"lon={telemetry['lon']:.5f}  "
                      f"alt={telemetry['alt']:.0f}m  "
                      f"hdg={telemetry['heading']:.1f}°")

            elapsed = time.time() - t_start
            sleep_t = frame_interval - elapsed
            if sleep_t > 0:
                time.sleep(sleep_t)

    except KeyboardInterrupt:
        print("\n[✓] עצרת את השידור")
    finally:
        stop_event.set()
        klv_sock.close()
        if gst_proc:
            gst_proc.terminate()
            gst_proc.wait(timeout=3)


# ══════════════════════════════════════════════════════════════════════════════
#  KLV-ONLY SENDER  (כלי עזר — שולח KLV בלבד, ללא RTP)
# ══════════════════════════════════════════════════════════════════════════════

def run_klv_only(host: str, port: int, interval: float = 0.04):
    """שולח KLV בלבד ב-~25Hz — שימושי לבדיקת הטלמטריה ללא וידאו."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    drone = DroneSimulator()
    print(f"[→] שולח KLV בלבד ל-{host}:{port}  (~{1/interval:.0f}Hz)")
    print(f"    Ctrl+C לעצירה\n")
    count = 0
    try:
        while True:
            t_start = time.time()
            t = drone.step()
            klv = encode_klv(**{k: t[k] for k in
                                ["lat", "lon", "alt", "heading", "pitch", "roll", "airspeed"]})
            sock.sendto(klv, (host, port))
            count += 1
            if count % 25 == 0:
                print(f"  [#{count:05d}] lat={t['lat']:.5f}  lon={t['lon']:.5f}  "
                      f"alt={t['alt']:.0f}m  hdg={t['heading']:.1f}°  {len(klv)}B")
            sleep_t = interval - (time.time() - t_start)
            if sleep_t > 0:
                time.sleep(sleep_t)
    except KeyboardInterrupt:
        print("\n[✓] עצרת")
    finally:
        sock.close()


# ══════════════════════════════════════════════════════════════════════════════
#  VERIFY KLV  (בדיקה מקומית — ללא רשת)
# ══════════════════════════════════════════════════════════════════════════════

def verify_klv():
    """מקודד ומפענח packet לבדיקת תקינות."""
    import json

    # ייבוא הפרסר מהפרויקט (אם קיים)
    try:
        import sys, os
        sys.path.insert(0, os.path.dirname(__file__))
        from klv_parser import parse_klv_packet
        HAS_PARSER = True
    except ImportError:
        HAS_PARSER = False

    klv = encode_klv(
        lat=32.0853, lon=34.7818, alt=500.0,
        heading=270.0, pitch=-3.0, roll=5.0,
        airspeed=22.5, mission_id="TEST-MISSION-01",
        platform="TEST-UAV-1"
    )

    print(f"[✓] KLV packet encoded: {len(klv)} bytes")
    print(f"    HEX (first 32B): {klv[:32].hex()}")

    if HAS_PARSER:
        result = parse_klv_packet(klv)
        if result:
            print(f"[✓] Parsed back OK:")
            print(json.dumps(result.to_dict(), indent=2))
        else:
            print("[✗] Parse failed")
    else:
        print("[i] klv_parser.py לא נמצא — הרץ מתוך תיקיית backend/ לאימות")


# ══════════════════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="UDP KLV Sender — בדיקת DeepStream WebRTC KLV Dashboard"
    )
    parser.add_argument("--host",     default="127.0.0.1", help="כתובת IP (default: 127.0.0.1)")
    parser.add_argument("--port",     type=int, default=5004, help="פורט UDP (default: 5004)")
    parser.add_argument("--fps",      type=int, default=25,   help="FPS (default: 25)")
    parser.add_argument("--klv-only", action="store_true",    help="שלח KLV בלבד (ללא RTP video)")
    parser.add_argument("--verify",   action="store_true",    help="בדוק KLV encoding/decoding ויצא")

    args = parser.parse_args()

    if args.verify:
        verify_klv()
        sys.exit(0)

    if args.klv_only:
        run_klv_only(args.host, args.port)
    else:
        run_sender(args.host, args.port, args.fps, klv_only=False)
