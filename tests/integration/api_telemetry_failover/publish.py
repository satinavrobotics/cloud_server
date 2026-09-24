"""Synthetic robot for the WP7 integration test: <robot>/diagnostics at 4 Hz, and
<robot>/nav_supervisor on every DRIVE<->RECOVER switch. Usage: publish.py <host> <seconds> <start>"""
import json
import sys
import time

import paho.mqtt.client as mqtt

host, seconds, start = sys.argv[1], float(sys.argv[2]), int(sys.argv[3])
ROBOT = "itbot"
TEMPS = [60.0, 90.0, 80.0, 70.0]          # HIGH at 90, OK at 70 (80 is in the dead band)

client = mqtt.Client(client_id=f"wp7-it-publisher-{start}")
client.connect(host, 1883)
client.loop_start()
i = start
deadline = time.time() + seconds
while time.time() < deadline:
    now = time.time()
    stale_gps = (i // 6) % 2 == 1
    diag = {
        "timestamp": now, "robot_name": ROBOT,
        "jtop": {"gpu_percent": 30, "cpu_temp_c": TEMPS[i % 4], "gpu_temp_c": 50.0,
                 "soc_temp_c": 45.0, "power_total_mw": 7000, "power_avg_mw": 6500},
        "host_stats": {"cpu_percent": 25.0, "ram_percent": 55.0},
        "ros_health": {"esp32_stale": False, "gps_stale": stale_gps, "sati_pose_stale": False},
        "topic_availability": {"/scan": {"exists": True, "publishing": True}},
    }
    client.publish(f"{ROBOT}/diagnostics", json.dumps(diag), qos=1)
    if i % 5 == 0:
        state = "RECOVER" if (i // 5) % 2 else "DRIVE"
        nav = {"state": state, "last_drive_cause": "FROZEN", "blocked_pending": False,
               "stamp": {"sec": int(now), "nanosec": int((now % 1) * 1e9)}}
        client.publish(f"{ROBOT}/nav_supervisor", json.dumps(nav), qos=1)
    i += 1
    time.sleep(0.25)
client.loop_stop()
client.disconnect()
print(f"published up to sample {i}")
