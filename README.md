# DeepStream WebRTC KLV Dashboard

מערכת סטרימינג מלאה עם:
- קלט **RTSP** ו-**UDP**
- עיבוד וידאו עם **DeepStream / GStreamer**
- שידור **WebRTC** לדפדפן
- פענוח **KLV (MISB ST 0601)** — telemetry מרחפנים ומטוסים
- תצוגת **מפה חיה** עם מיקום הרחפן ב-real-time

---

## דרישות

- Docker + Docker Compose
- **NVIDIA GPU** + NVIDIA Container Toolkit (לשימוש ב-DeepStream GPU)
- או: CPU-only mode (ראה למטה)

---

## מבנה הפרויקט

```
deepstream-webrtc-klv/
├── Dockerfile
├── docker-compose.yml
├── backend/
│   ├── main.py          ← FastAPI server (REST + WebSocket)
│   ├── pipeline.py      ← GStreamer pipeline management
│   ├── klv_parser.py    ← MISB ST 0601 KLV decoder
│   └── requirements.txt
├── ui/
│   ├── index.html       ← Main dashboard UI
│   ├── app.js           ← WebRTC + Map + KLV client
│   └── style.css        ← Dashboard styles
└── nginx/
    └── nginx.conf       ← Reverse proxy config
```

---

## הפעלה

### 1. בנה את ה-Docker image

```bash
cd deepstream-webrtc-klv
docker-compose build
```

### 2. הפעל

```bash
docker-compose up
```

פתח דפדפן: **http://localhost:8080**

### הפעלה ידנית בתוך ה-container (recommended לפיתוח)

```bash
# הפעל container ידנית
docker run --rm -it \
  --runtime=nvidia \
  --network=host \
  -v $(pwd)/backend:/app/backend \
  -v $(pwd)/ui:/var/www/html \
  deepstream-webrtc-klv:latest \
  bash

# בתוך ה-container:
nginx
python3 /app/backend/main.py
```

---

## שימוש

### הוספת stream מה-UI

1. לחץ **+ Add Stream**
2. הכנס URL:
   - RTSP: `rtsp://192.168.1.100:554/live`
   - UDP: `udp://0.0.0.0:5004`
3. לחץ **Add Stream**

### הוספת stream מה-API

```bash
curl -X POST http://localhost:8080/api/streams \
  -H "Content-Type: application/json" \
  -d '{"uri": "rtsp://192.168.1.100:554/stream", "name": "Camera 1"}'
```

### Auto-start streams בהפעלת Container

```bash
docker-compose run -e STREAMS="rtsp://cam1 rtsp://cam2" deepstream-webrtc
```

---

## KLV Data — מה מוצג במפה

המערכת מפענחת KLV לפי תקן MISB ST 0601:

| Field | Tag | תיאור |
|-------|-----|--------|
| Sensor Latitude | 13 | מיקום הרחפן — lat |
| Sensor Longitude | 14 | מיקום הרחפן — lon |
| Sensor Altitude | 15 | גובה במטרים |
| Platform Heading | 5 | כיוון טיסה (0-360°) |
| Pitch / Roll | 6, 7 | יציבות הפלטפורמה |
| Airspeed | 82 | מהירות אוויר (m/s) |
| Frame Center Lat/Lon | 23, 24 | מרכז שדה ראייה המצלמה |
| Slant Range | 21 | מרחק אלכסוני מהמצלמה ליעד |
| Mission ID | 3 | זיהוי משימה |

---

## בדיקת KLV Parser

```bash
python3 backend/klv_parser.py
```

יפיק:
```json
{
  "lat": 32.0853,
  "lon": 34.7818,
  "alt": 500.0,
  "heading": 270.0,
  "platform": "TEST-UAV-1",
  ...
}
```

---

## CPU-only Mode (ללא GPU)

ערוך `docker-compose.yml` — הסר `runtime: nvidia`

ערוך `backend/pipeline.py` — שנה:
```python
use_gpu = False
```

הפלא שנה ב-`build_pipeline_str()`:
- `nvvideoconvert` → `videoconvert`
- `nvv4l2h264enc` → `x264enc tune=zerolatency`
- `nvstreammux` → `videomixer`

---

## API Reference

| Method | Path | תיאור |
|--------|------|--------|
| GET | `/api/streams` | רשימת streams |
| POST | `/api/streams` | הוסף stream |
| DELETE | `/api/streams/{id}` | הסר stream |
| GET | `/api/health` | בדיקת תקינות |
| WS | `/ws/signaling/{stream_id}` | WebRTC signaling |
| WS | `/ws/telemetry/{stream_id}` | KLV broadcast |

---

## WebRTC Signaling Protocol

```
Client → Server:   { "type": "join" }
Server → Client:   { "type": "sdp", "data": { "type": "offer", "sdp": "..." } }
Client → Server:   { "type": "sdp", "data": { "type": "answer", "sdp": "..." } }
Client ↔ Server:   { "type": "ice", "data": { "candidate": "...", "sdpMLineIndex": N } }
```

---

## Troubleshooting

**הוידאו לא מגיע לדפדפן:**
- וודא שה-STUN server נגיש: `stun:stun.l.google.com:19302`
- אם ברשת מבודדת, הקם TURN server (coturn) ועדכן `RTC_CONFIG` ב-`app.js`
- בדוק `GST_DEBUG=3` ב-docker-compose

**KLV לא מפוענח:**
- וודא שה-stream מכיל KLV metadata (לא כל RTSP stream מכיל)
- KLV מוטמע ב-MPEG-TS PES streams — וודא שהמצלמה מוגדרת לשלוח metadata
- בדוק עם: `gst-launch-1.0 rtspsrc location=rtsp://... ! fakesink dump=true`

**Pipeline לא מתחיל:**
- בדוק `docker logs deepstream-webrtc`
- וודא שהפלאגינים מותקנים: `gst-inspect-1.0 webrtcbin`
