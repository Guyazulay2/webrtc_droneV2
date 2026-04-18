import subprocess
import argparse
import os

def run_real_flight_stream(filename, host, port):
    klv_port = port + 1
    
    if not os.path.exists(filename):
        print(f"[!] Error: {filename} not found!")
        return

    # פיפליין שמוציא את הוידאו וה-KLV המקוריים מהקובץ
    # הווידאו עובר דרך h264parse ו-rtph264pay בשביל ה-WebRTC
    # ה-KLV נשלח כ-Raw UDP (בלי RTP) כדי שהאפליקציה שלך תקרא אותו בקלות
    cmd = (
        f"gst-launch-1.0 -v filesrc location={filename} ! tsparse ! tsdemux name=demux "
        f"demux. ! queue ! h264parse ! rtph264pay config-interval=1 pt=96 ! udpsink host={host} port={port} sync=true async=false "
        f"demux. ! 'meta/x-klv' ! queue ! udpsink host={host} port={klv_port} sync=true async=false"
    )

    print("="*60)
    print(f" 🛸 STREAMING REAL FLIGHT DATA")
    print(f" 📁 File: {filename}")
    print(f" 📹 Video (RTP): {port}")
    print(f" 🛰️  KLV (RAW):   {klv_port}")
    print("="*60)
    
    try:
        subprocess.run(cmd, shell=True, check=True)
    except KeyboardInterrupt:
        print("\n[✓] Stream stopped.")

if __name__ == "__main__":
    run_real_flight_stream("flight_fixed.ts", "127.0.0.1", 5004)
