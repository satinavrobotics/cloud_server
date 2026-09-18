# Self-hosted LiveKit over Tailscale

Replaces LiveKit Cloud with a self-hosted `livekit-server` on `admin-satinav-pc`
(the RTX 5090 box), reachable only over the Tailscale tailnet, plus a
role-scoped token service for it. Migrated here from the standalone
`~/livekit` build/validate area (2026-09-18); the design, test history and
reasoning below came with it.

## Which LiveKit service is which

Two token services and one media server sit side by side in
`docker_compose/mission_dispatch_services.yaml`. The Cloud one is pre-existing;
the SFU pair is what this document is about.

| | `livekit-service` | `livekit-sfu-tokens` | `livekit-sfu` |
|---|---|---|---|
| Status | pre-existing | new | new |
| What it is | token issuer (:8006) | token issuer (:8008) | the LiveKit media server (7880/7881, UDP 50000-60000) |
| Tokens are valid for | LiveKit Cloud | the self-hosted SFU | (it verifies them) |
| Key pair | Cloud key, `docker_compose/.env` | SFU key, gitignored `livekit_sfu.env` | same SFU key |
| Who picks permissions | the caller (`canPublish`, `canSubscribe`, `canPublishData` in the request) | the server, by `role` (`robot` / `operator`) | enforces the token |
| Identity rules | none | dashboards `WEB-`, robots may not, 1-128 chars | none |
| TTL | one default (36000 s) | robots 36000 s, operators 3600 s | none |
| Reached via | `api-delegation-service` (`LiveKitClient`), publicly through nginx | tailnet only; the client gateway routes only the operator route | tailnet only |
| Code | `packages/services/livekit/` | `packages/services/livekit_sfu_tokens/` | `livekit/livekit-server` image |

They are separate processes on purpose: different secrets, and an outage of
one backend must not look like an outage of the other. They share exactly one
thing, `packages/utils/livekit_tokens.py` (`mint_room_token`), which builds and
signs the JWT. Policy (grants, identity rules, URLs, TTLs) stays in each
service. The self-hosted stack is otherwise additive: production clients use
the Cloud service until the cutover checklist in §6 is followed.

## Layout

```
docker_compose/mission_dispatch_services.yaml # services `livekit-sfu` and `livekit-sfu-tokens` (next to `livekit-service`)
docker_compose/livekit_sfu.env.example        # settings template; real file is gitignored
docker_compose/livekit/livekit.yaml           # SFU config (§1, §4)
packages/services/livekit_sfu_tokens/         # role-scoped token service (:8008)
packages/utils/livekit_tokens.py              # JWT minting shared with packages/services/livekit
scripts/livekit/robot_client.py               # the one script every robot/operator launches
tests/unit/test_livekit_sfu_tokens_service.py # token grant/shape tests (real JWTs)
tests/performance/livekit/                    # synthetic load test, monitoring, ICE-candidate check
docs/livekit_sfu/                             # this file, TESTING.md, TAILSCALE_ADMIN_STEPS.md, DASHBOARD_VIA_CLIENT_DOMAIN.md
```

## Running it

```bash
cp docker_compose/livekit_sfu.env.example docker_compose/livekit_sfu.env
# fill in LIVEKIT_SFU_API_KEY / _SECRET (livekit-server generate-keys)
./restart_services.sh                # or: docker compose -f docker_compose/mission_dispatch_services.yaml up -d --build livekit-sfu livekit-sfu-tokens
curl http://127.0.0.1:7880/          # SFU: "OK"
curl http://127.0.0.1:8008/health    # token service
```

Both services are part of the main compose file, so `restart_services.sh`
rebuilds the token service and restarts them with everything else. **That
restarts the SFU too: live video drops for a few seconds and clients
reconnect** (the dashboard rejoins by itself; verified). To touch only these two:
`docker compose -f docker_compose/mission_dispatch_services.yaml up -d --build --no-deps livekit-sfu livekit-sfu-tokens`.

The env file is optional as far as compose is concerned (`required: false`), so a
checkout without it can still build/start/stop every other service; without it
the SFU exits with a message naming the missing variables and the token service
refuses to start.

The key pair lives only in `docker_compose/livekit_sfu.env` (gitignored,
`chmod 600`). Both containers read it: the token service directly, the SFU via
`LIVEKIT_KEYS` assembled in its entrypoint. It is deliberately **not**
`docker_compose/.env`, which holds the LiveKit Cloud pair and is tracked in git.

The SFU uses host networking and binds :7880, so only one SFU can run on this
box at a time — stop any other instance first.

## 0. Confirmed facts this is built on

- `admin-satinav-pc` **is** the RTX 5090 box, a tailnet member at `100.85.3.47`,
  MagicDNS suffix `tail055f44.ts.net`.
- HTTPS certificates are enabled for the tailnet and **`tailscale serve` is
  configured** (2026-09-18, §3): `https://admin-satinav-pc.tail055f44.ts.net`
  (port 443, tailnet-only) proxies to `127.0.0.1:7880`. Operators are handed that
  `wss://` name, robots keep `ws://100.85.3.47:7880` (§2).
- Two robots are tailnet members and tagged: `jetson-golya`, `jetson-kolibri`
  (exact tag string still to confirm, see `TAILSCALE_ADMIN_STEPS.md` task 3).
- `livekit-server` v1.9.1 (image `livekit/livekit-server:v1.9.1`).
- An earlier abandoned attempt used LiveKit's stock **public-IP** install
  (`use_external_ip: true`, open UDP 50000-60000, TURN over TLS/443 via Caddy) —
  the pattern that fights carrier CGNAT on the RUT901s.
- Production today: LiveKit Cloud at `wss://satinav-b22o2lgk.livekit.cloud` (§6).

## 1. Architecture

```
Tailscale tailnet (tail055f44.ts.net) — ACL-scoped, no public exposure

  Jetson Orin Nano (RUT901 4G)          admin-satinav-pc  (100.85.3.47)
  tag:robot                             owner-owned (admin@) — referenced by
                                         IP/host-alias in the ACL, not tagged
  ┌────────────────────┐  http://100.85.3.47:8008 (token)    ┌─────────────────────────┐
  │ robot media client │ ──────────────────────────────────▶ │ livekit-sfu-tokens│
  │                    │  ws://100.85.3.47:7880 (signaling)   │ livekit-sfu             │
  │                    │ ◀──────────────────────────────────▶ │  (docker, host net)     │
  └────────────────────┘  UDP 50000-60000 (media, direct)     └─────────────────────────┘
  Ops laptop / sati-client dashboard (on tailnet)
  ── token: via the gateway (/api/livekit-selfhost/createToken → :8008) ──▶
  ── wss://admin-satinav-pc.tail055f44.ts.net :443 (tailscale serve → 127.0.0.1:7880) ──▶  same box
  ── media: UDP 50000-60000 / TCP 7881, direct ──▶
```

Key decisions and why:

- **Single node, no Redis.** Redis only coordinates multiple `livekit-server`
  instances. One node removes a whole category of self-hosting failures.
- **No TURN.** TURN relays media for peers that can't reach each other on the
  public internet. Every peer here is a tailnet member; Tailscale already
  guarantees connectivity (direct WireGuard, or DERP-relayed as a fallback).
- **`node_ip`, not the `interfaces`/`ips` include filter.** The
  `rtc.interfaces.includes: [tailscale0]` approach was checked against LiveKit's
  GitHub issues and found unreliable; setting `rtc.node_ip` to the Tailscale IP
  is the confirmed-working knob (livekit/livekit#2088).
- **`interfaces.excludes: [eno1, wlp132s0, docker0]` is required**, not cosmetic:
  without it ICE gathered — and in one test selected — this box's public IPv6
  on `eno1`. `node_ip`/`use_external_ip: false` don't stop host-candidate
  gathering on other interfaces (TESTING.md phase 1 notes).
- **`use_external_ip: false`.** Never attempt STUN public-IP discovery.
- **TLS via `tailscale serve`, not Caddy + Let's Encrypt** (enabled, §3).
  `tailscale serve` fronts only the HTTP/WS signaling port; media still flows
  directly to `node_ip`.
- **No egress/ingress containers.** Nothing uses recording or RTMP/WHIP ingest.
  **Confirm this assumption** — if sessions are recorded or RTMP ingested
  anywhere, these come back.
- **Part of the main compose file** (originally a separate stack, merged
  2026-09-18 so `restart_services.sh` covers it and there is one place to look).
  The trade-off: an application redeploy via `restart_services.sh` now also
  restarts the SFU (a few seconds of dropped video). Use the `--no-deps` command
  above, or restart other services individually, when video must not blink.
  Don't add `--remove-orphans` to that script: the sati-client gateway shares
  the compose project name `docker_compose` and would be removed.
- **A separate token service, not a change to `livekit-service`.** Keeps
  production (LiveKit Cloud) token minting untouched while the self-hosted
  path is piloted. It is tailnet-only; the only route exposed to sati-client's
  gateway nginx is the operator-only `/api/operator/createToken` (below).

## 2. Token roles and URLs

### Token roles

`POST /api/createToken` with `{participantName, roomName, role}` — all three
required. The caller names a role; it cannot request raw permissions:

| role | publish tracks | subscribe | publish data |
|---|---|---|---|
| `robot` | yes | no | yes |
| `operator` | no | yes | yes |

Operators publish data because sati-client drives robots over it (teleop
`cmd_vel/<robot>` topics and RPC); they still cannot publish tracks. This is
a deliberate change from the original Node token-server, whose operator
tokens were subscribe-only. Robots receive data despite `canSubscribe: false`
(verified 2026-09-18: `cmd_vel`, reliable data and RPC all reach a robot-role
participant, and the SFU still refuses an operator's video publish).

`can_publish_data` is set explicitly per role: LiveKit resolves an omitted
`canPublishData` to the `canPublish` value, and the Python SDK otherwise always
writes `true`, so neither default may decide data rights
(`tests/unit/test_livekit_sfu_tokens_service.py` pins this).

Role scoping is enforced by the SFU for whatever the token says, but the role
itself is **self-declared by the caller** — any tailnet device that can reach
:8008 can request a `robot` token. Network reachability (ACL + firewall) is
the actual gate. The token service has no authentication of its own, and
`roomName` (a user's email in sati-client) is a convention, not an authorization.

Two limits keep the unauthenticated dashboard route from being abused:

- **Identity.** LiveKit disconnects an existing participant when another joins
  its room under the same identity (verified on the SFU: the first participant
  gets disconnect reason 2). So `/api/operator/createToken` only accepts
  `participantName`s starting with `WEB-` (the dashboard's `PARTICIPANT_ID_PREFIX`),
  and `/api/createToken` refuses that prefix for robots: a dashboard client
  cannot take over a robot's identity. Both routes require 1-128 character names.
- **Lifetime.** Operator tokens live `LIVEKIT_SFU_OPERATOR_TTL` seconds
  (default 3600), robot tokens `LIVEKIT_SFU_TTL` (36000). They carry
  data-publish (teleop) rights and are minted on the public route, so a leaked
  one should expire soon. Expiry does not drop a connected participant (verified:
  a 15 s token stayed connected for 45 s); it only bounds joins and reconnects,
  and the client refetches when its cached token has expired.

### Server URL per role, and the dashboard route

The token response's `server_url` depends on the role:

| role | `server_url` | env var |
|---|---|---|
| `robot` | `ws://100.85.3.47:7880` | `LIVEKIT_SFU_SERVER_URL` |
| `operator` | `wss://admin-satinav-pc.tail055f44.ts.net` | `LIVEKIT_SFU_OPERATOR_URL` (falls back to the above when unset) |

Robots keep the plain IP so their video path doesn't depend on MagicDNS, cert
renewal or an extra proxy hop. Operators need `wss://` because the dashboard
is served over https (`https://client.satinavrobotics.com`), where browsers
refuse `ws://` (mixed content).

`POST /api/operator/createToken` takes `{participantName, roomName}` only and
always issues an operator token (a `role` in the body is ignored; the name must
start with `WEB-`). It is the
only route sati-client's gateway nginx proxies (`/api/livekit-selfhost/createToken`
→ `:8008/api/operator/createToken`), so the public dashboard path can never mint
a robot token; the tokens are useless off the tailnet anyway. Robots keep using
`/api/createToken` directly over the tailnet.

Design, review and rollout log for this: `DASHBOARD_VIA_CLIENT_DOMAIN.md`.

## 3. Tailscale setup

Human-only admin-console steps are in **`TAILSCALE_ADMIN_STEPS.md`**, each
scoped to the `TESTING.md` phase it unblocks:

- Task 1 (enable HTTPS certs) — ✅ done.
- Task 2 (confirm `cimbi` can reach this box) — before phase 2, only if needed.
- Task 3 (robot ACL rule, scoped to this box by IP/host-alias, not by tagging
  it) — before phase 3.

### `tailscale serve` (✅ enabled 2026-09-18)

```
sudo tailscale serve --bg --https=443 http://127.0.0.1:7880
tailscale serve status     # https://admin-satinav-pc.tail055f44.ts.net (tailnet only) / proxy http://127.0.0.1:7880
```

Exposes the SFU's signaling port as `wss://admin-satinav-pc.tail055f44.ts.net`
**on the tailnet only** (`serve`, never `funnel`), with a Tailscale-issued cert
that renews automatically. The command is idempotent and the config persists in
tailscaled's state, but it is host state outside git: a rebuilt PC must re-run
it. `tailscale serve reset` removes it. Then set
`LIVEKIT_SFU_OPERATOR_URL` (in `docker_compose/livekit_sfu.env`) to
the `wss://` name and recreate only the token service:

```
docker compose -f docker_compose/mission_dispatch_services.yaml up -d --build --no-deps livekit-sfu-tokens
```

Verified: from a tailnet Mac on the https dashboard page the browser reached
the SFU through it (the SFU logged the `/rtc` request), and the first request
after enabling took several seconds (cert provisioning) while later ones take
milliseconds. Port 443 needed **no ufw rule and no ACL change** (§5).

## 4. SFU configuration

`docker_compose/livekit/livekit.yaml`: no Redis, no TURN, `node_ip` set to this
box's Tailscale IPv4, `use_external_ip: false`, non-Tailscale interfaces
excluded, keys passed via `LIVEKIT_KEYS` (never embedded in the YAML), and
`prometheus.port: 6789` for the benchmarking in `TESTING.md`.

**Do not reuse** the key pair from the old public-IP attempt or the LiveKit
Cloud pair in `docker_compose/.env` — those have been in plaintext in several
files, including git history. Rotate them regardless.

## 5. Host firewall — ✅ applied 2026-09-02

Required for "Tailscale is the only way in" to be true. Without it the token
service (no auth of its own by design) and the SFU listen on `0.0.0.0`,
reachable over `eno1`/`wlp132s0` by anything on the same physical LAN,
bypassing the Tailscale ACL. Applied:

```
sudo ufw allow in on tailscale0 to any port 7880,7881,8008 proto tcp
sudo ufw allow in on tailscale0 to any port 50000:60000 proto udp
sudo ufw deny 7880,7881,8008/tcp
sudo ufw deny 50000:60000/udp
```

Confirmed 2026-09-02 from `cimbi` on the same LAN segment: `192.168.0.123:7880`
and `:8008` timed out; `100.85.3.47:7880`/`:8008` succeeded. The migration
keeps the same ports, so the rules carry over unchanged — re-check with
`sudo ufw status` after moving hosts or compose files.

`tailscale serve` (§3) listens on `100.85.3.47:443` and the tailnet IPv6
address only. It needed no ufw rule: a tailnet peer's WebSocket to it reached
the SFU while ufw only allowed 7880/7881/8008 (tailscaled handles serve ports
itself). It is not reachable from the LAN interfaces.

Egress filtering was considered and dropped: WebRTC mandates DTLS/SRTP on
every media path, so a same-LAN hairpin is a path-purity question, not a
confidentiality gap. This section is about inbound reachability.

## 6. Production cutover checklist (only after `TESTING.md` phases pass)

Today three places point production at LiveKit Cloud:

| File | Setting | Change |
|---|---|---|
| `cloud_server/docker_compose/.env` | `LIVEKIT_SERVER_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` (read by `livekit-service`) | self-hosted URL + rotated pair, or retire in favour of this service |
| `satibot_orchestrator/config.yaml:30` | `livekit.url` | self-hosted URL |
| sati-client build | `EXPO_PUBLIC_LIVEKIT_BACKEND` (`constants/Config.ts`; default `cloud`) | `selfhost` for the self-hosted SFU. The URL itself comes back with the token, so nothing else to edit |

Open questions that block cutover (found 2026-09-18, not solved by this
migration):

- **Off-tailnet dashboard users.** Production tokens come from the public
  domain (nginx → :8000 → :8006) and the dashboard runs in browsers that may
  not be on the tailnet. A tailnet-only SFU URL would be unreachable for them.
- **Data channel under scoped roles.** Resolved: operators may publish data
  (see §Token roles). Still validate the real ROS bridge against the `robot`
  role (it only needs to publish tracks + data and receive data).
- **Room naming.** Resolved: one room per user email
  (`admin@satinavrobotics.com`), as production, the ROS bridge and
  sati-client already use; `robot_client.py` now defaults to it.
- **Credentials.** Robot repos call the public token endpoint with a hardcoded
  basic-auth password (`sati_livekit_bridge/utils.py`, `fetch_token.py`); the
  Cloud pair is in git history. Rotate both.

`training_server/docker/sati_fleet/docker-compose.yaml` needs no edit — it
takes `LIVEKIT_URL` from its environment.

## 7. Testing, benchmarking, and the pilot

Phased plan — synthetic load first, then one non-fleet device over the real
tailnet path, then 2-3 real robots, all before any production file is touched.
See **`TESTING.md`** (phases, pass criteria, results so far) and
`tests/performance/livekit/` for the scripts.

## Open questions

- Egress/ingress genuinely unused (recording, RTMP/WHIP)? — §1.
- Exact robot ACL tag string — `TAILSCALE_ADMIN_STEPS.md` task 3.
- `wss://` via `tailscale serve` vs. plain `ws://` — resolved: operators (the
  https dashboard) get `wss://`, robots keep plain `ws://` (§2).
- Dashboard connect takes several seconds before the WebSocket reaches the SFU;
  not yet diagnosed, see `DASHBOARD_VIA_CLIENT_DOMAIN.md` → Known issue.
- The cutover blockers in §6.
