#!/usr/bin/env python3
"""
UDP Sender — H.264 video + KLV Telemetry (MISB ST 0601)
========================================================
שימוש:
  python klv_udp_sender.py flight_fixed.ts        # TS + real KLV
  python klv_udp_sender.py flight_fixed.ts --loop # לופ
  python klv_udp_sender.py                        # test pattern + simulated KLV
"""

import socket, struct, time, math, argparse, subprocess, threading, sys, os

# ══════════════════════════════════════════════════════════════════════════════
#  KLV ENCODING  (MISB ST 0601)
# ══════════════════════════════════════════════════════════════════════════════

MISB_UL_KEY = bytes([
    0x06,0x0E,0x2B,0x34,0x02,0x0B,0x01,0x01,
    0x0E,0x01,0x03,0x01,0x01,0x00,0x00,0x00
])

def _ber(n):
    if n < 0x80:   return bytes([n])
    if n < 0x100:  return bytes([0x81, n])
    return bytes([0x82]) + n.to_bytes(2, "big")

def _tlv(tag, val):
    return bytes([tag]) + _ber(len(val)) + val

def _map(v, vmin, vmax, imin, imax):
    return int(imin + (v - vmin) / (vmax - vmin) * (imax - imin))

def encode_klv(lat, lon, alt, heading, pitch=0.0, roll=0.0,
               airspeed=20.0, mission_id="MISSION-01", platform="UAV-1",
               frame_lat=None, frame_lon=None, slant_range=None, hfov=None):
    ts = int(time.time() * 1_000_000)
    i  = b""
    i += _tlv(2,  struct.pack(">Q", ts))
    i += _tlv(3,  mission_id.encode())
    i += _tlv(9,  platform.encode())
    i += _tlv(5,  struct.pack(">H", _map(heading%360, 0,360, 0,65535)))
    i += _tlv(6,  struct.pack(">h", _map(max(-20,min(20,pitch)), -20,20, -32768,32767)))
    i += _tlv(7,  struct.pack(">h", _map(max(-20,min(20,roll)),  -20,20, -32768,32767)))
    lr = _map(lat, -90,  90,  -(2**31), 2**31-1)
    lo = _map(lon, -180, 180, -(2**31), 2**31-1)
    i += _tlv(13, struct.pack(">i", lr))
    i += _tlv(14, struct.pack(">i", lo))
    i += _tlv(15, struct.pack(">H", _map(max(-900,min(19000,alt)), -900,19000, 0,65535)))
    i += _tlv(82, struct.pack(">H", _map(max(0,min(100,airspeed)), 0,100, 0,65535)))
    # Frame center
    flr = _map(frame_lat if frame_lat else lat, -90,  90,  -(2**31), 2**31-1)
    flo = _map(frame_lon if frame_lon else lon, -180, 180, -(2**31), 2**31-1)
    i += _tlv(23, struct.pack(">i", flr))
    i += _tlv(24, struct.pack(">i", flo))
    # Slant range
    if slant_range:
        i += _tlv(21, struct.pack(">H", _map(min(slant_range,5000), 0,5000, 0,65535)))
    # HFOV
    if hfov:
        i += _tlv(16, struct.pack(">H", _map(min(hfov,180), 0,180, 0,65535)))
    i += _tlv(65, bytes([13]))
    return MISB_UL_KEY + _ber(len(i)) + i


class DroneSimulator:
    """
    נתונים מהסרטון:
      ACFT: 54°40.82N 110°08.58W  alt=5024ft  hdg=084°
      TGT:  54°44.95N 110°02.81W  (קבוע — לאן המצלמה מסתכלת)
    טיסה איטית מאוד לכיוון 084°, המצלמה תמיד מסתכלת על ה-TGT.
    """
    # TGT קבוע מהסרטון
    TGT_LAT = 54.7492
    TGT_LON = -110.0468

    def __init__(self, lat=54.6804, lon=-110.1430, alt=1531.0,
                 heading=84.0, speed_deg_per_sec=0.000015):
        self.lat     = lat
        self.lon     = lon
        self.alt     = alt
        self.heading = heading
        self.speed   = speed_deg_per_sec   # איטי מאוד ~1.5 מטר/שניה
        self.t       = 0.0
        self._hdg_r  = math.radians(heading)

    def step(self):
        self.t += 0.04  # 25Hz

        # טיסה איטית קדימה
        self.lat += math.cos(self._hdg_r) * self.speed
        self.lon += math.sin(self._hdg_r) * self.speed

        # גובה יציב
        alt = self.alt + 3 * math.sin(self.t * 0.2)

        # heading יציב עם רעש קטן
        heading = (self.heading + 1.5 * math.sin(self.t * 0.15)) % 360

        # pitch/roll ריאליסטי
        pitch = -2.0 + 0.3 * math.sin(self.t * 0.3)
        roll  = 1.5  * math.sin(self.t * 0.1)

        # slant range — מרחק ל-TGT
        dlat  = self.TGT_LAT - self.lat
        dlon  = self.TGT_LON - self.lon
        slant = math.sqrt((dlat*111000)**2 + (dlon*111000*math.cos(math.radians(self.lat)))**2)

        return dict(
            lat         = self.lat,
            lon         = self.lon,
            alt         = alt,
            heading     = heading,
            pitch       = pitch,
            roll        = roll,
            airspeed    = 55.0,
            frame_lat   = self.TGT_LAT,   # תמיד על ה-TGT
            frame_lon   = self.TGT_LON,
            slant_range = slant,
            hfov        = 26.0,
        )


def _plugin_exists(name):
    try:
        r = subprocess.run(["gst-inspect-1.0","--exists",name],
                           capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def _launch(cmd, label):
    """הפעל פקודה כ-subprocess עם logging."""
    proc = subprocess.Popen(cmd, shell=True,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE)
    def _log():
        for line in proc.stderr:
            txt = line.decode(errors="replace").strip()
            if any(k in txt for k in ("Error","error","warn","PLAYING","EOS","frame=")):
                print(f"  [{label}] {txt}")
    threading.Thread(target=_log, daemon=True).start()
    return proc


# ══════════════════════════════════════════════════════════════════════════════
#  KLV THREADS
# ══════════════════════════════════════════════════════════════════════════════

def simulated_klv_thread(host, port, stop_event, fps=25,
                          lat=54.6804, lon=-110.1430, alt=1531.0,
                          heading=84.0):
    """שולח KLV מדומה — טיסה ישרה מהמיקום האחרון."""
    drone    = DroneSimulator(lat, lon, alt, heading=heading)
    sock     = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    interval = 1.0 / fps
    next_t   = time.monotonic()
    frame    = 0
    try:
        while not stop_event.is_set():
            tel = drone.step()
            klv_data = {k: tel[k] for k in
                        ("lat","lon","alt","heading","pitch","roll","airspeed",
                         "frame_lat","frame_lon","slant_range","hfov")
                        if k in tel}
            sock.sendto(encode_klv(**klv_data), (host, port))
            frame += 1
            if frame % fps == 0:
                print(f"  📡 SIM KLV #{frame:05d}  "
                      f"lat={tel['lat']:.5f}  lon={tel['lon']:.5f}  "
                      f"alt={tel['alt']:.0f}m  hdg={tel['heading']:.1f}°")
            next_t += interval
            sleep = next_t - time.monotonic()
            if sleep > 0:
                time.sleep(sleep)
    finally:
        sock.close()


def real_klv_thread(filepath, host, port, stop_event, loop=False):
    """
    שולח KLV אמיתי מ-TS.
    מקבל את המיקום האחרון דרך klv_parser.
    אחרי שנגמר — ממשיך DroneSimulator מהמיקום האחרון.
    """
    abs_path = os.path.abspath(filepath)
    last_pos = {"lat": 54.6813, "lon": -110.1686, "alt": 1532.0, "heading": 86.0}

    # נסה לקרוא klv_parser לקבלת מיקום אחרון
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from klv_parser import parse_klv_from_buffer
        HAS_PARSER = True
    except ImportError:
        HAS_PARSER = False

    # שלב 1: שלח KLV אמיתי + האזן ל-UDP לקבלת מיקום אחרון
    def _send_and_capture():
        """שולח KLV אמיתי ובמקביל מאזין לקבלת המיקום האחרון."""
        sock_listen = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # האזן על פורט זמני
        tmp_port = port + 100
        try:
            sock_listen.bind(("127.0.0.1", tmp_port))
            sock_listen.settimeout(0.5)
        except Exception:
            sock_listen = None

        # ffmpeg שולח ל-2 יעדים: פורט האמיתי + פורט הזמני שלנו
        if sock_listen and HAS_PARSER:
            cmd = (
                f'ffmpeg -re -i "{abs_path}" '
                f'-map 0:d:0 -c copy -f data '
                f'udp://{host}:{port} '
                f'-map 0:d:0 -c copy -f data '
                f'udp://127.0.0.1:{tmp_port}'
            )
        else:
            cmd = (
                f'ffmpeg -re -i "{abs_path}" '
                f'-map 0:d:0 -c copy -f data '
                f'udp://{host}:{port}'
            )

        print(f"  [KLV] Real KLV from TS → {host}:{port}")
        proc = subprocess.Popen(cmd, shell=True,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)

        # קרא packets ועדכן מיקום אחרון
        if sock_listen and HAS_PARSER:
            def _capture():
                while proc.poll() is None and not stop_event.is_set():
                    try:
                        data, _ = sock_listen.recvfrom(65535)
                        klv = parse_klv_from_buffer(data)
                        if klv:
                            d = klv.to_dict()
                            if d.get("lat"):     last_pos["lat"]     = d["lat"]
                            if d.get("lon"):     last_pos["lon"]     = d["lon"]
                            if d.get("alt"):     last_pos["alt"]     = d["alt"]
                            if d.get("heading"): last_pos["heading"] = d["heading"]
                    except socket.timeout:
                        pass
                    except Exception:
                        pass
                if sock_listen:
                    sock_listen.close()
            threading.Thread(target=_capture, daemon=True).start()

        # חכה לסיום ffmpeg
        while not stop_event.is_set():
            if proc.poll() is not None:
                break
            time.sleep(0.2)
        proc.terminate()

    while not stop_event.is_set():
        _send_and_capture()

        if stop_event.is_set():
            return

        if loop:
            print("  [KLV] Real KLV loop restart...")
            time.sleep(0.3)
            continue

        # עבור ל-simulated מהמיקום האחרון
        print(f"  [KLV] Real KLV ended → simulated from "
              f"lat={last_pos['lat']:.5f} lon={last_pos['lon']:.5f}")
        simulated_klv_thread(host, port, stop_event,
                             lat=last_pos["lat"],
                             lon=last_pos["lon"],
                             alt=last_pos["alt"],
                             heading=last_pos["heading"])
        return


# ══════════════════════════════════════════════════════════════════════════════
#  PIPELINES
# ══════════════════════════════════════════════════════════════════════════════

def build_ts_cmd(filepath, host, port):
    abs_path = os.path.abspath(filepath)
    return (
        f'gst-launch-1.0 -v '
        f'filesrc location="{abs_path}" '
        f'! tsparse set-timestamps=true '
        f'! tsdemux name=demux '
        f'demux. ! queue '
        f'! h264parse '
        f'! avdec_h264 '
        f'! videorate '
        f'! video/x-raw,framerate=30/1 '
        f'! videoconvert '
        f'! video/x-raw,format=I420 '
        f'! x264enc tune=zerolatency bitrate=2000 speed-preset=ultrafast '
        f'key-int-max=30 bframes=0 '
        f'! video/x-h264,profile=constrained-baseline,stream-format=byte-stream,alignment=au '
        f'! h264parse config-interval=-1 '
        f'! rtph264pay config-interval=-1 aggregate-mode=zero-latency pt=96 '
        f'! udpsink host={host} port={port} sync=true async=false'
    )


def build_test_cmd(host, port, fps=25):
    for enc in ["x264enc","openh264enc","avenc_h264"]:
        if _plugin_exists(enc): break
    enc_str = {
        "x264enc":     "x264enc tune=zerolatency bitrate=1500 speed-preset=ultrafast",
        "openh264enc": "openh264enc",
        "avenc_h264":  "avenc_h264 bitrate=1500000",
    }[enc]
    return (
        f'gst-launch-1.0 -v '
        f'videotestsrc pattern=smpte is-live=true '
        f'! video/x-raw,width=1280,height=720,framerate={fps}/1 '
        f'! videoconvert ! {enc_str} ! h264parse '
        f'! rtph264pay config-interval=1 pt=96 '
        f'! udpsink host={host} port={port} sync=false async=false'
    )


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def run(filepath, host, port, loop, fps):
    klv_port   = port + 1
    stop_event = threading.Event()

    if filepath:
        if not os.path.exists(filepath):
            print(f"[✗] קובץ לא נמצא: {filepath}")
            sys.exit(1)
        # בדוק אורך
        try:
            r = subprocess.run(
                ["ffprobe","-v","quiet","-show_entries","format=duration",
                 "-of","csv=p=0", filepath],
                capture_output=True, text=True, timeout=10)
            dur = float(r.stdout.strip()) if r.stdout.strip() else 0
            print(f"[✓] TS duration: {dur:.1f}s")
            if dur < 10 and not loop:
                print(f"[!] קובץ קצר — מפעיל לופ אוטומטי")
                loop = True
        except Exception:
            pass

        gst_cmd = build_ts_cmd(filepath, host, port)
        mode    = f"TS: {os.path.basename(filepath)}" + (" [loop]" if loop else "")

        # KLV thread — real מה-TS
        klv_t = threading.Thread(
            target=real_klv_thread,
            args=(filepath, host, klv_port, stop_event, loop),
            daemon=True, name="klv"
        )

    else:
        gst_cmd = build_test_cmd(host, port, fps)
        mode    = "Test pattern (smpte)"
        loop    = False

        # KLV thread — simulated
        klv_t = threading.Thread(
            target=simulated_klv_thread,
            args=(host, klv_port, stop_event, fps),
            daemon=True, name="klv"
        )

    print(f"\n{'='*55}")
    print(f"  📡 {mode}")
    print(f"  Video → {host}:{port}   (RTP H.264)")
    print(f"  KLV   → {host}:{klv_port}")
    print(f"{'='*55}")
    print(f"  ב-UI הוסף: udp://0.0.0.0:{port}")
    print(f"{'='*55}\n")

    klv_t.start()

    # הרץ video (עם loop אם צריך)
    try:
        while not stop_event.is_set():
            print(f"\n[VIDEO] {gst_cmd[:100]}...")
            proc = _launch(gst_cmd, "VIDEO")
            try:
                proc.wait()
            except KeyboardInterrupt:
                proc.terminate()
                break
            if stop_event.is_set() or not loop:
                break
            print("[VIDEO] EOS — restarting in 0.5s...")
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()

    print("\n[✓] stopped")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("file",   nargs="?", default=None)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5004)
    p.add_argument("--fps",  type=int, default=25)
    p.add_argument("--loop", action="store_true")
    args = p.parse_args()
    try:
        run(args.file, args.host, args.port, args.loop, args.fps)
    except KeyboardInterrupt:
        print("\n[✓] stopped")
