"""Check fleet_events against diagnostics_ts: every thermal/node event must be exactly the
transition implied by the stored rows (no duplicates, no spurious events after restarts or
failovers), and NAV.RECOVERY_* must alternate strictly."""
import json
import sys

import psycopg

conn = psycopg.connect(sys.argv[1])
rows = conn.execute("SELECT ts, temp_max, nodes_down FROM diagnostics_ts WHERE robot_name='itbot' "
                    "ORDER BY ts").fetchall()
events = conn.execute("SELECT ts, code, payload FROM fleet_events WHERE robot_name='itbot' "
                      "ORDER BY ts, code").fetchall()
dup = conn.execute("SELECT count(*) - count(DISTINCT (event_id, ts)) FROM fleet_events").fetchone()[0]
dup_rows = conn.execute("SELECT count(*) - count(DISTINCT (robot_name, ts)) FROM diagnostics_ts").fetchone()[0]

expected = []
hot = None
down = None
for ts, temp, nodes in rows:
    if hot is None:
        hot = temp >= 85
    elif not hot and temp >= 85:
        hot = True
        expected.append((ts, "SYSTEM.THERMAL_HIGH"))
    elif hot and temp <= 78:
        hot = False
        expected.append((ts, "SYSTEM.THERMAL_OK"))
    if down is not None and nodes != down:
        expected.append((ts, "SYSTEM.NODE_DOWN" if nodes > down else "SYSTEM.NODE_UP"))
    down = nodes

actual = [(ts, code) for ts, code, _ in events if code.startswith("SYSTEM.")]
nav = [(ts, code, payload) for ts, code, payload in events if code.startswith("NAV.")]
alternates = all(a[1] != b[1] for a, b in zip(nav, nav[1:]))
durations = [p.get("duration_s") for _, c, p in nav if c == "NAV.RECOVERY_EXITED"]

print(f"diagnostics_ts rows: {len(rows)} (duplicate (robot, ts): {dup_rows})")
print(f"fleet_events: {len(events)} (duplicate ids: {dup})")
print(f"thermal/node events: stored {len(actual)}, implied by rows {len(expected)}, "
      f"identical: {sorted(actual) == sorted(expected)}")
print(f"nav events: {len(nav)}, strictly alternating: {alternates}, first={nav[0][1] if nav else None}, "
      f"exits with null duration: {sum(d is None for d in durations)}")
if sorted(actual) != sorted(expected):
    print("missing:", sorted(set(expected) - set(actual))[:10])
    print("extra:", sorted(set(actual) - set(expected))[:10])
ok = dup == 0 and dup_rows == 0 and sorted(actual) == sorted(expected) and alternates
print("RESULT:", "PASS" if ok else "FAIL")
