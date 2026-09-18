# Isolated testing & benchmarking

Goal: prove the self-hosted server works and is fast enough, without touching a
single real robot, without touching any production config
(`cloud_server/docker_compose/.env`, `satibot_orchestrator/config.yaml`,
`sati-client/constants/Config.ts` all stay pointed at LiveKit Cloud throughout
this whole document), and with the minimum possible Tailscale admin-console
involvement. Four phases, each strictly more "real" than the last — don't skip
ahead if a phase fails.

## Isolation principles

- **Separate credentials.** `docker_compose/livekit_sfu.env` holds its own API key/secret,
  generated fresh for this box. Production's `cloud_server/docker_compose/.env`
  keys are never touched or reused here — a token minted by this test server
  cannot be used against Cloud or vice versa.
- **Separate room namespace.** Every load-test room is named `bench-*`
  (`tests/performance/livekit/run_loadtest.sh` does this automatically). Trivial to spot in logs,
  impossible to confuse with a real fleet room.
- **No robots until phase 3, and only already-tailnet ones then.** Phases 1-2
  use synthetic clients (the `lk` CLI) — software only, simulates however many
  publishers/subscribers you ask for, no Jetson involved.
- **No production file ever edited during testing.** The cutover checklist
  (`README.md` §6) is a separate, later, explicit step — not part of this
  document.

## Phase 1 — Local capacity test (no Tailscale changes at all)

Everything happens on `admin-satinav-pc` against `127.0.0.1`. No other device,
no admin-console step, nothing to ask anyone for.

1. `cp docker_compose/livekit_sfu.env.example docker_compose/livekit_sfu.env`
   and fill in a key/secret pair from `livekit-server generate-keys`.
2. `docker compose -f docker_compose/mission_dispatch_services.yaml up -d --build livekit-sfu livekit-sfu-tokens`
3. Install the load-test CLI once: `curl -sSL https://get.livekit.io/cli | bash`
4. In one terminal: `tests/performance/livekit/monitor.sh`
5. In another: `tests/performance/livekit/run_loadtest.sh capacity 2m`
   — simulates 100 video-publishing "robots" + 5 dashboard subscribers for 2
   minutes, matching your real connection shape (many publishers, few
   viewers), not LiveKit's own meeting/livestream examples.
6. Also try `tests/performance/livekit/run_loadtest.sh dashboard 2m` (20 publishers/20 subscribers
   — stresses the case where several operators watch several robots at once).

**Pass criteria** (from LiveKit's own published self-host benchmark, run on a
16-core reference box that hit 80-92% CPU at 150 publishers/150 subscribers —
this box has 24 cores / 187GB RAM, so 100 publishers + 5 subscribers should sit
well under that): CPU comfortably below saturation, no dropped/retransmitted
packets climbing in `tests/performance/livekit/monitor.sh` output, all 100 simulated publishers
show as connected in the `lk` output for the full duration.

Then run `tests/performance/livekit/run_loadtest.sh soak 4h` (or your actual daily duration) once
the short run passes, to catch anything that only shows up over time (memory
growth, port exhaustion — file-descriptor limit was already 1,048,576 on this
box, well above LiveKit's recommended 65,535, so no `ulimit` change was needed
here).

### Phase 1 results (already run, 2026-09-02)

_Recorded before the migration into cloud_server: paths below refer to the
original `~/livekit` layout (`server/` → the `livekit-sfu` service in
`docker_compose/mission_dispatch_services.yaml` + `docker_compose/livekit/`, `test/bench/` → `tests/performance/livekit/`,
`client/` → `scripts/livekit/`, `token-server/` → `packages/services/livekit_sfu_tokens/`)._

Ran twice (90s and 45s, 100 video-publishers/5 subscribers): **0% packet loss,
0 errors in the `lk` summary, 100/100 publishers connected both times.** CPU
never exceeded ~3%, memory ~19-810MB depending on run — nowhere near this
box's capacity. Findings along the way, kept here rather than presented as a
clean success story because two of them are real:

- **Fixed:** `server/livekit.yaml` had `prometheus_port: 6789` (top-level) —
  the running server logged `prometheus_port is deprecated, please switch
  prometheus.port instead` on first boot. Changed to the nested
  `prometheus: {port: 6789}` form; confirmed the warning is gone on restart.
- **Fixed:** `test/bench/run_loadtest.sh` broke after the `test/` reorg — it
  did `cd "$(dirname "$0")/.."`, which was correct when the script lived at
  `bench/` directly under the repo root, but only climbs to `test/` (not the
  repo root, where `server/.env` actually is) now that it's nested one level
  deeper at `test/bench/`. Fixed to `cd "$(dirname "$0")/../.."`.
- **Fixed:** `test/bench/monitor.sh`'s bandwidth column was silently useless —
  `docker stats` reports `0B/0B` NET I/O for any `network_mode: host`
  container (Docker attributes host-network traffic to the host's interfaces,
  not the container; this is a general Docker limitation, not specific to
  this setup). CPU%/MEM stayed accurate (cgroup-based, not affected).
  Confirmed real traffic was flowing the whole time via
  `ip -s link show tailscale0` (17GB+ accumulated) despite `docker stats`
  showing zero. Rewrote the script to track `tailscale0`'s own RX/TX byte
  counters instead — now shows real per-interval throughput (tens of KB/s at
  this load, as expected for 100 low-bitrate synthetic tracks).
- **Real finding, corrected after an initially-wrong call:** both runs logged
  repeated `ERROR livekit.transport types/ice.go:264 could not match local
  candidate` for a `fe80::...` (IPv6 link-local) host address. First pass:
  tried `rtc.interfaces.excludes` on `eno1`/`wlp132s0`/`docker0`, saw the
  *error line* just move to `fe80::...%tailscale0`, and concluded the exclude
  didn't work and reverted it. **That conclusion was wrong** — it was checking
  one log pattern, not what actually matters. Running `client/robot_client.py`
  for real (see below) and reading the *full* candidate list showed the
  server was gathering — and in one run, a same-box client actually
  *selected* — a real, publicly-routable IPv6 host candidate
  (`2001:4c4e:1eaa:1600::/64`, confirmed via `ip -6 route get` to be this
  box's actual internet-routed address on `eno1`). That's not cosmetic: it
  means a client with public IPv6 connectivity could potentially reach this
  server directly, bypassing Tailscale entirely, regardless of `node_ip`/
  `use_external_ip: false` (those only control STUN-based external-IP
  discovery, not host-candidate gathering on other local interfaces).
  **Re-applied `rtc.interfaces.excludes: [eno1, wlp132s0, docker0]`**, this
  time verified against the complete `[local]` candidate list (not just an
  error-log grep): server-gathered candidates are now exactly
  `100.85.3.47` and `fd7a:115c:a1e0::3b01:33c` (Tailscale v4/v6), nothing
  else — confirmed clean. Keeping the exclude. The `fe80::...%tailscale0`
  log line the first pass got confused by is still there and is genuinely
  harmless (link-local, non-routable) — the mistake was treating that log
  line as the thing to optimize for instead of checking actual candidate
  exposure.
- **Noted, not fixed:** `lk load-test --help` has no flag to control how many
  tracks each simulated subscriber subscribes to — in both the `capacity` run
  (100 publishers/5 subscribers) each subscriber only ever showed 6/100
  tracks. That's a hardcoded default in the `lk` tool itself, not something
  `run_loadtest.sh` controls. It's fine for proving the server can *carry*
  100 concurrent publishers, but it means the `dashboard` scenario doesn't
  actually prove "one operator watching many robots at once" the way its name
  suggests — for that, use `client/robot_client.py` as the subscriber side
  instead (real SDK, no such cap) once phase 2 starts.
- **Fixed (unrelated inconsistency spotted while here):** `token-server/dev_local.sh`
  had blank `LIVEKIT_API_KEY`/`SECRET` placeholders that wouldn't actually
  work — filled in with the same pair now in `server/.env`.

## Phase 2 — Real Tailscale-path validation (one minimal, reversible ACL check)

Phase 1 proves the server can handle the load; it says nothing about the
Tailscale-routed network path, since loopback bypasses it entirely. Phase 2
uses **your own laptop, `cimbi`** (100.78.100.16, owned by `admin@` — same
owner as `admin-satinav-pc` today, not one of the tagged fleet robots
`jetson-golya`/`jetson-kolibri`) to run test traffic *from a different machine
on the tailnet*, so it actually crosses Tailscale instead of loopback. Two
ways to do this, either or both:

1. **Load shape:** on `cimbi`, install `lk` (same curl command), copy
   `docker_compose/livekit_sfu.env`'s key/secret over, then
   `lk load-test --url ws://100.85.3.47:7880 --api-key ... --api-secret ... --room bench-tailnet --video-publishers 5 --duration 1m`
   — a small run, this step is about validating the *path*, not capacity
   (phase 1 already proved that).
2. **Functional shape:** on `cimbi`, `python scripts/livekit/robot_client.py --token-server http://100.85.3.47:8008`
   — identity resolves automatically from `cimbi`'s own Tailscale status (no
   flag needed), fetches a token from the `livekit-sfu-tokens` service (never holds the raw
   API secret), and publishes `cimbi`'s webcam if it has one or a synthetic
   frame otherwise. Exercises the real connection-establishment code path,
   not a load generator — good for actually watching a session end-to-end
   before trusting the load test's aggregate numbers. This is also literally
   the script every real robot runs, unmodified.
3. On `admin-satinav-pc`: `tests/performance/livekit/verify_candidates.sh` — confirm every
   ICE candidate offered is `100.x.x.x`, never a public or LAN address.

Since `cimbi` and `admin-satinav-pc` are both currently owned by `admin@`,
this likely needs **no ACL edit at all** — just the verification check in
**Human task 2** of `TAILSCALE_ADMIN_STEPS.md`. Only fall back to an actual
policy change if that check fails.

**Pass criteria:** connection succeeds, `verify_candidates.sh` shows only
Tailscale addresses, no fallback to a public/STUN-discovered candidate.

## Phase 3 — Fleet pilot (2-3 real robots, still zero production changes)

Covered in `README.md` §7: use the `livekit-sfu-tokens` token service (not
production's `livekit-service`), join `jetson-golya`/`jetson-kolibri` (already
tailnet members) to a test room, confirm end-to-end. Needs the real
robot ACL rule (scoped to this box by IP/host-alias, not by tagging it — see
below) — **Human task 3** in `TAILSCALE_ADMIN_STEPS.md`.

## Phase 4 — Production cutover

Not testing — this is `README.md` §6, done only after phases 1-3 all pass.

## Tuning playbook — measure first, then tune

Don't pre-guess performance flags. If phase 1's `tests/performance/livekit/monitor.sh` /
`:6789/metrics` shows a specific bottleneck, these are the confirmed real
knobs (`config-sample.yaml` in livekit/livekit) to reach for, in order of
likely relevance to this workload:

- **CPU-bound** (high `process_cpu` in metrics, publishers/subscribers
  otherwise healthy): enable `rtc.batch_io` (`batch_size`,
  `max_flush_interval`) — merges network write syscalls specifically to cut
  CPU usage.
- **Packet loss under load**: raise `rtc.packet_buffer_size_video` /
  `packet_buffer_size_audio` above their defaults (500 / 200 packets).
- **Hitting connection ceilings**: `limit.num_tracks` defaults to 400
  tracks/CPU (up to 8000) and `limit.bytes_per_sec` to ~1GB/s — raise
  explicitly if a legitimate need to exceed either shows up in testing, don't
  raise pre-emptively.
- **Congestion control**: `rtc.congestion_control.enabled` — check current
  default behavior under the sustained soak test before touching this.

One correction to keep in mind while reading LiveKit's own docs: the RTX 5090
in this box is not used by `livekit-server` at all — it's an SFU (forwards
packets, doesn't transcode), so it's CPU/network-bound, not GPU-bound. The GPU
matters for whatever else runs on this box, not for LiveKit capacity.
