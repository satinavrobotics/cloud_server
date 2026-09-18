# Dashboard access via client.satinavrobotics.com (self-hosted LiveKit)

Status (2026-09-18): **deployed and working end to end on the client domain (Chrome); docs synced; connect is slow (open issue below); remaining: commits, worktree cleanup after merge, cloud-path live check. Safari/off-tailnet/resilience checks intentionally skipped for now.**
Tick the checkboxes as work lands. Section "Review findings" lists what changed
from the first draft and why.

## Problem

`https://client.satinavrobotics.com` is served from a GCP VM (34.19.29.212,
nginx 1.22.1, HTTP basic auth) that reaches the dev box through the reverse SSH
tunnel started by `sati-client/scripts/start-dev.sh` (`client.sh`,
`-R 6006:localhost:80`). With `EXPO_PUBLIC_LIVEKIT_BACKEND=selfhost` the
dashboard fails there:

| Symptom | Cause |
|---|---|
| `POST /api/livekit-selfhost/createToken` -> 403 | Tunnel traffic reaches the gateway nginx as `127.0.0.1`; the route allows only tailnet source IPs (localhost was removed from the allow list on purpose, see below). |
| Would fail even with a token | Page is https, the SFU URL is `ws://100.85.3.47:7880` (mixed content), and the SFU is Tailscale-only. |

The VM is not a tailnet member. Every dashboard user has Tailscale (confirmed).

## Design (Option A): a `wss://` name that resolves only on the tailnet

```
browser (on tailnet) --https--> client.satinavrobotics.com (VM, basic auth)
        |                              | reverse tunnel
        |                              v
        |                     gateway nginx :80 --/api/livekit-selfhost/--> token svc :8008 /api/operator/createToken
        |
        +--wss://admin-satinav-pc.tail055f44.ts.net (:443)--> tailscale serve --> SFU 127.0.0.1:7880
        +--media (unchanged, direct over tailnet)--> SFU 7881/tcp, 50000-60000/udp
robots  --ws://100.85.3.47:7880 (unchanged)--> SFU
```

1. `tailscale serve --bg` (https, **port 443**) proxies to `http://127.0.0.1:7880`.
   Tailnet-only (`serve`, never `funnel`); Tailscale issues and renews the cert.
   Verified today: the SFU listens on `*:7880` (so `127.0.0.1` works), nothing
   listens on 443, and `tailscale serve status` -> `No serve config`.
2. The token service hands **operators** the `wss://` name and **robots**
   `ws://100.85.3.47:7880`. Robots keep the plain IP on purpose: the video
   publish path must not depend on MagicDNS, cert renewal or an extra proxy hop.
3. The dashboard reaches the token service only through a dedicated
   `POST /api/operator/createToken` (role fixed server-side). The public route
   therefore cannot mint a robot token, and nginx no longer needs the tailnet
   allow/deny that the tunnel can never satisfy.

Rejected: **Option B** (expose the SFU publicly: TLS hostname, public UDP/TCP
media or TURN, VM-to-SFU path, real auth on the token route). More moving parts
and it undoes the Tailscale-only design.
Also rejected: **one wss URL for every role** (deletes the per-role URL code,
but puts the robots' video path behind DNS + TLS + a proxy for no gain).

Accepted trade-offs: video needs the viewer on the tailnet; the SFU stays a
single point of failure on this PC (`unless-stopped`).

### Security posture (stated plainly)

The token service has **no authentication**. Anyone who can reach `:8008` on the
tailnet can already mint any role for any room; `roomName` = user email is a
convention, not an authorization. The operator route does not change that. What
it does do: anyone holding the VM's basic-auth password but **not** on the
tailnet can neither mint a robot token nor use an operator token (SFU
unreachable). Adequate while every tailnet member is trusted. Real per-user
authorization is out of scope here.

## Review findings (what changed vs. the first draft)

| # | Finding | Resolution |
|---|---|---|
| 1 | Draft used port **7443**; `README.md` §3 already documents `serve` on **443**. 443 is free. An odd port adds nothing and would need its own ACL/ufw consideration. | Use 443. URL becomes `wss://admin-satinav-pc.tail055f44.ts.net`. |
| 2 | Draft named the env var `LIVEKIT_OPERATOR_URL` / `LIVEKIT_URL`; the service's real names are `LIVEKIT_SFU_*`. | `LIVEKIT_SFU_OPERATOR_URL`, falling back to `LIVEKIT_SFU_SERVER_URL`. |
| 3 | "Force `role=operator` in nginx" is not possible (role is in the JSON body). | Dedicated service route `/api/operator/createToken`; nginx maps the public path onto it. Client stops sending `role`, so both backends send the identical body and `buildTokenRequest` shrinks to a path switch. |
| 4 | "Compare cached URL to what the token service would return" needs a fetch, defeating the cache. | Replace with self-heal: clear the cache when a *cached* token fails to connect, refetch once. Also covers key rotation and URL changes, which the URL compare did not. Marked recommended, not required (see Phase 2). |
| 5 | A `tailscale serve` setup **script** for one idempotent command is over-engineering. `serve --bg` is idempotent and persists in tailscaled state. | One documented command in `README.md` §3 plus a status check. No script. |
| 6 | ACL task assumed a restrictive policy. `TAILSCALE_ADMIN_STEPS.md` describes **additive-only** rules, so existing owner access already covers 443. | ACL/ufw become *conditional*: only if the Phase 0 test is blocked. |
| 7 | Riskiest unknown (browser local-network restrictions on `wss://` to a `100.x` name) was the last verification item. | Moved to Phase 0 as a go/no-go spike before any code. |
| 8 | "Unit tests", "robots unchanged", "cloud path unaffected" were separate verification items duplicating per-task tests. | Folded into each task's done-criteria; one manual smoke each remains. |
| 9 | Draft ignored where the sati-client code runs. The live dev server (8081) and `sati_nginx_gateway` run from the **main** checkout and mount its `nginx/nginx-http.conf`; our changes are on a worktree branch. | Phase 3 states the two ways to test (user-run), and that `EXPO_PUBLIC_LIVEKIT_BACKEND` is baked in at bundle time, so one dev server serves one backend. |
| 10 | "VM basic auth still fronts the route" was assumed. The VM's nginx config has not been seen. | Verification item: confirm the VM proxies `/api/livekit-selfhost/` and prompts for auth; identify who answered the 403 (`Server:` header: gateway vs VM nginx 1.22.1). |
| 11 | Docs still say serve is "not configured yet" (`README.md` §0/§3/§5, `TAILSCALE_ADMIN_STEPS.md`, env comments). | Doc sync task after Phase 3. |
| 12 | Once operators use 443, they no longer need `7880` in any ACL scoping (robots still do). | Optional tighten when ACL task 4 is applied. |

## Tasks

Legend: **[me]** code/tests I can do without touching live systems ·
**[you]** needs your sudo/tmux/admin console, or touches a live service ·
**[gate]** decision point.

### Phase 0: spike, no code. Go/no-go for the whole design  [you + me]
- [x] **[you]** Check syntax, then enable serve (additive, reversible with
      `tailscale serve reset`, touches no existing port):
      `tailscale serve --help`, then
      `sudo tailscale serve --bg --https=443 http://127.0.0.1:7880`
      and `tailscale serve status`.
      Done: `tailscale serve status` -> `https://admin-satinav-pc.tail055f44.ts.net (tailnet only) / proxy http://127.0.0.1:7880`; listens on 100.85.3.47:443 and the IPv6 tailnet address.
- [x] **[me]** From this PC: `curl -sv https://admin-satinav-pc.tail055f44.ts.net/`
      -> valid cert, LiveKit answers `OK`.
      Done: HTTP/2 200 `OK`, cert valid, ~15 ms. The very first request timed out after 10 s (likely lazy cert provisioning); later requests were instant. Watch for a slow first browser connect.
- [~] **[you]** From a tailnet laptop, on the **https client-domain page**, browser
      console: `new WebSocket('wss://admin-satinav-pc.tail055f44.ts.net/rtc')`
      and watch for a permission prompt, a block, or a normal handshake failure
      (401/400 = TLS and browser policy passed). Chrome and Safari. If Chrome
      shows a local-network prompt, note the wording: it is acceptable if
      one-time; a hard block means revisit the hostname before Phase 1.
      Chrome: PASS 2026-09-18 18:53Z (console shows an empty `failed:`, but the SFU logged `GET /rtc` 401 "no permissions to access the room", i.e. TLS + policy passed; no ufw/ACL rule needed). Safari: pending.
- [ ] **[you]** Same from a device off the tailnet: name should not resolve
      (fast failure, no hang).
- [x] **[gate]** Pass -> Phase 1. If blocked by ACL/ufw: add the minimal rule
      (ACL for `100.85.3.47:443`, or `sudo ufw allow in on tailscale0 to any port 443 proto tcp`),
      re-test. Do not assume either is needed.

      Passed on Chrome; no ufw/ACL rule was needed (request reached the SFU).
### Phase 1: cloud_server (branch `feat/livekit-sfu-tokens`)  [me]
Files: `packages/services/livekit_sfu_tokens/{server.py,main.py}`,
`tests/unit/test_livekit_sfu_tokens_service.py`,
`docker_compose/livekit_sfu.env(.example)`, docs.
- [x] `server.py`: constructor gains `operator_server_url: Optional[str] = None`;
      `create_token` returns it for `role == "operator"`, else `server_url`.
- [x] `main.py`: read optional `LIVEKIT_SFU_OPERATOR_URL` in `lifespan`
      (not added to the required-vars list); add
      `POST /api/operator/createToken` taking `{participantName, roomName}` only
      and always issuing an operator token; list it in `root`. Existing
      `/api/createToken` (robots, `role` in body) is unchanged.
- [x] Tests: operator gets the operator URL, robot gets the plain URL; fallback
      when the operator URL is unset; operator route works without `role`,
      ignores a smuggled `role: "robot"`, and its token has the operator grants;
      response keys stay `{token, ttl, server_url}`. Existing 15 tests still pass
      (run in the service image, `PYTHONPATH=/src`, `--noconftest`).
      Result: 22 passed (15 existing + 7 new). Run with `pip install pytest 'httpx<0.28'` in the service image (newer httpx breaks the old starlette TestClient).
- [x] `livekit_sfu.env` (gitignored, chmod 600) + `.env.example`: add
      `LIVEKIT_SFU_OPERATOR_URL=wss://admin-satinav-pc.tail055f44.ts.net`;
      fix the stale "once `tailscale serve` is set up" comment.
- [x] Pre-deploy check without touching the live service: run the new image on
      a spare port (e.g. 8018, host network) with the env file and `curl` both
      routes; confirm URLs and grants.

      Result: operator route -> `wss://` URL, canPublish=false/canSubscribe=true; a smuggled `role: robot` is ignored; `/api/createToken` robot -> `ws://100.85.3.47:7880`. Test image tag `livekit_sfu_tokens:operator-url-test`; live `:latest` untouched.
### Phase 2: sati-client (worktree `~/satinavrobotics/sati-client-livekit-sfu-tokens`)  [me]
- [x] `constants/LiveKit.ts`: `buildTokenRequest` selfhost branch drops `role`
      (body identical to cloud; only the path differs). Update
      `__tests__/LiveKit.test.ts` and the MSW handler in `mocks/handlers.ts`.
- [x] `nginx/nginx-http.conf` **and** `nginx/nginx.conf`: in
      `location /api/livekit-selfhost/` delete `allow`/`deny`, change
      `proxy_pass` to `http://livekit_selfhost_api/api/operator/;`, rewrite the
      comment (operator-only; safe on the public path because tokens only work
      from the tailnet). `nginx -t` on both.
      `nginx -t` passes for `nginx-http.conf`; `nginx.conf` only fails on the missing Let's Encrypt cert files in a scratch container (same text change).
- [x] Route test on my scratch nginx (`nginx_selfhost_route_test`, port 8090)
      against the spare-port service from Phase 1: 200 with the wss URL, no 403.
      Never the live gateway.
      Result: 200 with the wss URL and operator grants through `:8090` from localhost (simulating the tunnel), with and without `role` in the body.
- [x] **Recommended:** self-heal in `LiveKitContext.start()`: if `connectToRoom`
      fails while using a *cached* token, `multiRemove(tokenStorageKeys)` and
      retry once with a fresh fetch. Jest test with a rejected first connect.
      Today nothing clears a bad cache except the manual Settings action, so a
      bad cached token persists up to 10 h. Applies to both backends. As built it
      does not retry by itself: it clears the cache (and the stuck connecting
      flag) and the user's next connect fetches fresh.
      Implemented in `connectToRoom`'s catch (not `start()`): clears `tokenStorageKeys` and also resets `isConnectingRef`. That flag was only reset by Connected/Disconnected events, so after a failed connect `start()` returned early and the user had to reload the page. Test added; verified it fails without the fix. Applies to both backends.
- [x] Full jest run; `CLAUDE.md` note updated.
      90 suites / 1231 tests pass. `CLAUDE.md` updated.
- [ ] *Optional, not planned unless wanted:* runtime backend override (e.g.
      `?livekit=selfhost` persisted in storage) so cloud and self-hosted can be
      compared on one dev server without a restart.

### Phase 3: deploy to live systems  [you, on my confirmation each step]
- [x] Recreate only the token service; SFU untouched, robots unaffected:
      `docker compose -f docker_compose/mission_dispatch_services.yaml up -d --build --no-deps livekit-sfu-tokens`.
      Done 2026-09-18 18:54Z: SFU stayed up; live `:8008` returns wss URL + operator grants on `/api/operator/createToken`, ws URL + robot grants on `/api/createToken`.
- [x] Get the sati-client branch onto the running dev flow. Either (a) check out
      / merge the branch in the main checkout, or (b) stop the 8081 dev server
      (pid 2762611) and run
      `EXPO_PUBLIC_LIVEKIT_BACKEND=selfhost scripts/start-dev.sh` from the
      worktree. Both recreate `sati_nginx_gateway` with the new config (brief
      gateway blip). Tunnel stays on for the client-domain test.
      Done 2026-09-18 ~18:57Z via (b): gateway `nginx -s reload` (already mounted the worktree config), then `EXPO_PUBLIC_LIVEKIT_BACKEND=selfhost scripts/start-dev.sh --no-tunnel` from the worktree in tmux `saticlientdev` (Metro pid 639008 on 8081). `--no-tunnel` because the existing tunnel (orphan ssh pid 1015874, up 3 days, `-R 6006:localhost:80`) already forwards to the gateway; a second one would fail on the remote port. Via the live gateway from localhost (as the tunnel arrives): `POST /api/livekit-selfhost/createToken` -> 200 wss URL (was 403); bundle contains the selfhost route.
- [x] Confirm the VM forwards `/api/livekit-selfhost/` and still challenges for
      basic auth (finding 10).

      Done: the dashboard on the client domain obtained a token through the VM (token service logged `WEB-5lx6fncp`, 19:00:02Z). Basic-auth challenge itself not separately inspected (browser was already authenticated).
### Phase 4: verification  [you + me]
- [x] End to end on `https://client.satinavrobotics.com` from a tailnet laptop:
      pill CONNECTED, synthetic robot video plays
      (`scripts/livekit/robot_client.py --force-synthetic --token-server http://100.85.3.47:8008`).
      This is also the check that media ICE to `100.x` works from a public https
      page, not just signaling.
      Done 2026-09-18: connects and plays synthetic robot video; user reports video speed good. **Connect action is slow, see Known issue below.** Media path: direct UDP (IPv4 then tailnet IPv6), LAN RTT 4-21 ms, no relay/TCP.
- [ ] Off-tailnet user: pill shows a clear error within a reasonable time.
- [ ] Resilience: laptop sleep/wake; `docker restart sati_livekit_sfu`
      mid-session (client reconnects); `sudo systemctl restart tailscaled` and
      a reboot leave `tailscale serve status` intact.
- [x] Old cached token (`ws://`) is recovered by the self-heal (or Settings ->
      clear token if self-heal was skipped).
      Covered by jest (`LiveKitContext.test.tsx`: failed connect with a cached token clears the cache and a retry is allowed; verified to fail without the fix). Not exercised in a real browser.
- [x] Robots: still receive `ws://100.85.3.47:7880` and publish.
      Done (service level): live `/api/createToken` robot role -> `ws://100.85.3.47:7880`; synthetic robot connected and published to the SFU with that URL.
- [~] Default LiveKit Cloud path unaffected (dev server without the env var).

      Covered by jest/MSW (`buildTokenRequest('cloud')` unchanged, full suite 1231 pass). No live run against the cloud backend; do that when convenient (start the dev server without `EXPO_PUBLIC_LIVEKIT_BACKEND`).
### Phase 5: close-out
- [x] **[me]** Docs sync: `README.md` §0/§3/§5, `TAILSCALE_ADMIN_STEPS.md`
      (serve enabled, port 443, any ACL/ufw rule actually added), this file
      marked done. Do not touch `~/livekit`.
      Done 2026-09-18: `README.md` (§0, §1, §2 per-role URL + operator route, §3 serve, §5 no ufw rule needed, open questions), `TAILSCALE_ADMIN_STEPS.md` task 1, `docs/deployment_ontology.md`, `CLAUDE.md`. `~/livekit` untouched.
- [ ] Commit both branches (`cloud_server` and `sati-client`
      `feat/livekit-sfu-tokens`), **only when asked**.
- [~] **[me]** Cleanup: `git worktree remove` the sati-client worktree (and its
      hard-linked `node_modules`), stop my scratch Expo (pid 394305) and
      `docker stop nginx_selfhost_route_test`.

      Partly done: scratch Expo 8082, `nginx_selfhost_route_test` container and the `operator-url-test` image tag removed. **Deferred:** the worktree (live gateway + Metro run from it: merge/checkout the branch in the main checkout first) and the synthetic robot (pid 646287, left running for testing).
## Audit follow-ups (2026-09-18, after the deploy)

Full audit of both sides; each claim was verified before being applied.

Verified on the live SFU first: (1) a second participant joining under an
existing identity disconnects the first (reason 2); (2) a 15 s token stays
connected for 45 s, so token expiry does not drop a connected participant.

Applied (code + tests, uncommitted; deployed, see "Rename and merge" below):
- [x] **Identity guard.** The public operator route only accepts `WEB-`-prefixed
      `participantName`s; robots may not use that prefix; both routes require 1-128
      character names. Closes: anyone on the gateway path could claim a robot's
      identity and kick it out of its room.
- [x] **Operator TTL** `LIVEKIT_SFU_OPERATOR_TTL` (default 3600), robots keep
      `LIVEKIT_SFU_TTL`. Env example updated; the live (gitignored) env file
      needs no change because the default applies.
- [x] **Client:** a duplicate-identity disconnect now also clears the cached token
      (its JWT carries the old identity, so the next connect collided again for up
      to 10 h). `CONNECTION_TIMEOUT` and `TOKEN_EXPIRATION_BUFFER` are used instead of
      the hardcoded 30000/60; dead `LIVEKIT.SERVER_URL` removed; stale http-only
      comment fixed; README documents the backend flag; `start-dev.sh` prints the
      bundled backend.
- [x] **Tests:** service 22 -> 38 (pydantic 1.9 service image and pydantic 2 test
      env); client +5 (selfhost request path, cached-backend mismatch, cache reuse,
      rejected token request, duplicate-identity cleanup), each mutation-checked;
      full suite 90 suites / 1236 tests. Restoring the `Room` mock is now in a `finally`.
- [x] **`robot_client.py`:** token fetch no longer blocks the event loop (SIGTERM
      while the token server is down: was up to 40 s, now instant); always waits
      before reconnecting, backoff reset only after a session that stayed up 30 s
      (two clients with one identity: 1/2/4/8 s instead of a tight loop); a 4xx from
      the token server stops the client; requirements pinned to ranges.
- [x] **Docs:** README §2 heading restored (was cited but missing), diagram and
      §6 cutover row corrected (the sati-client switch is the env flag, not
      `Config.ts` `SERVER_URL`), `TAILSCALE_ADMIN_STEPS.md` task 4 (operators need
      443/7881/UDP, not 8008), stale paths in `livekit.yaml` fixed.

Deliberately not done:
- `room.prepareConnection()`: `connect()` already does that work; it only helps if
  called earlier than the click, and the cause of the slow connect is unproven.
- Automated ICE-candidate check in `check_health.sh`, and replacing the hardcoded
  `interfaces.excludes` names: larger, separate change.
- nginx `limit_except`/rate limit on the public route, `--proxy-headers` for real
  client IPs in the token service log (shows `None:0` behind nginx), compose
  `depends_on`/healthchecks: low value, left as is.
- Two unexplained SFU 401s on `/rtc` at 19:03:29Z: not attributed.
- Rotating the Cloud key pair that is still a `getenv` default in
  `packages/services/livekit/main.py` (pre-existing, README §4 already says rotate).

## Rename and merge into the main compose file (2026-09-18)

Done after the audit, before anything was committed:
- **Renamed** the cloud_server side from `livekit-selfhost` to what it does:
  service `livekit-sfu-tokens` (package `packages/services/livekit_sfu_tokens`, image
  `livekit_sfu_tokens`, env vars `LIVEKIT_SFU_*`, env file `docker_compose/livekit_sfu.env`,
  docs `docs/livekit_sfu/`, tests `test_livekit_sfu_tokens_service.py`). It sits next to
  `livekit-service` (Cloud tokens) and `livekit-sfu` (the media server). Older entries in
  this file were renamed in place, so their commands are copy-pasteable.
  **Not renamed:** sati-client's vocabulary (`EXPO_PUBLIC_LIVEKIT_BACKEND=selfhost`,
  `/api/livekit-selfhost/createToken`, nginx upstream `livekit_selfhost_api`): there
  `selfhost` is the counterpart of `cloud`, and keeping it meant no client or gateway change.
- **Merged** `livekit-sfu` + `livekit-sfu-tokens` into `docker_compose/mission_dispatch_services.yaml`
  (the separate `livekit_selfhost.yaml` / project `sati-livekit` is gone) and added
  `livekit-sfu-tokens` to the build list of `restart_services.sh`. The env file is
  `required: false`, so a checkout without it still builds/starts every other service
  (a plain `env_file:` fails the whole stack: verified); the SFU entrypoint prints which
  variables are missing.
- **Live switch** 19:36:48Z-19:36:58Z: old project taken down, both services started under
  the main project (~10 s without an SFU). The synthetic robot recovered in 0.3 s and the
  dashboard session rejoined by itself. New token-service code (identity guard, operator TTL)
  is live and verified against the running service.
- **`restart_services.sh` flow verified for these services** (`down` -> `build` -> full `up -d`):
  both answer again ~13 s later including the image build, and the other 10 production
  containers kept identical start times. **Not run:** the actual script (it restarts every
  production service).
- Caveat now true by design: `restart_services.sh` restarts the SFU with everything else.
  Use `up -d --no-deps livekit-sfu livekit-sfu-tokens` (or restart other services one by
  one) when video must not blink. Never add `--remove-orphans` to that script: the sati-client
  gateway shares the project name `docker_compose` and would be removed.

## Known issue: slow connect (open)

User report (2026-09-18): the LiveKit connect action on the client domain is slow; video itself is fine.

Evidence so far:

| Step | Time | Source |
|---|---|---|
| Token issued by service | 19:00:02.3Z | token service log |
| SFU accepts WebSocket, starts RTC session | 19:00:07.0Z | SFU log |
| **Gap: token issued -> WS join reaches SFU** | **~4.7 s** | difference |
| Join -> participant active | 1.1 s | SFU log |
| Full TLS + WS handshake to `tailscale serve` from this PC | ~17 ms | curl timings |
| Media | direct UDP, LAN RTT 4-21 ms | SFU candidates, `tailscale ping` |

So the server side and media path are fast; the 4.7 s is spent in the browser or on
the laptop's route before the WebSocket reaches the SFU. Candidates: MagicDNS lookup
of the `.ts.net` name on the laptop, first TLS connection, a Chrome local-network
prompt (a human clicking Allow would look like this), or JS-client steps before
opening the socket. Not yet distinguished.

Most likely (unproven): the token *response* travels back over the reverse ssh
tunnel, one TCP connection shared with the dev bundle download (12.5 MB raw), so
it can queue behind the bundle. A production build would not show it.

Next: on the laptop, DevTools -> Network (Preserve log), click connect, record the
`createToken` Timing tab and the `rtc` WebSocket row's DNS / Initial connection / SSL /
Stalled, and note any permission prompt. Alternatively add temporary
`[livekit-timing]` console logs around the token fetch and `room.connect`. Possible
fixes depending on the result: warm the connection / preconnect, or revisit the
hostname if the browser classifies it as a private-network target.
The 2nd session (cached token, no token request) is not comparable: click time unknown.

## Live state to remember (not in git)

- `tailscale serve` on this PC: `https://admin-satinav-pc.tail055f44.ts.net` -> `127.0.0.1:7880` (`tailscale serve reset` undoes it).
- `sati_nginx_gateway` mounts the **worktree's** `nginx-http.conf` (reloaded, not recreated).
- Metro on 8081 runs from the worktree (`EXPO_PUBLIC_LIVEKIT_BACKEND=selfhost`, tmux `saticlientdev`, started with `--no-tunnel`); the tunnel is an orphan ssh (pid 1015874) that `start-dev.sh` will not restart.
- Token service `docker_compose-livekit-sfu-tokens-1` and SFU `sati_livekit_sfu` run under the main compose project `docker_compose` with the identity guard and operator TTL (redeployed 2026-09-18, see above).
- Synthetic robot (scratch venv, room `admin@satinavrobotics.com`, pid 646287) is still publishing; stop with `kill 646287`.
- Scratch containers/Expo/image tag from testing were removed 2026-09-18.

## Risks

| Risk | Mitigation |
|---|---|
| Browser local-network policy blocks or prompts on `wss://` / media to `100.x` from a public https page (unverified) | Phase 0 spike before any code; Phase 4 covers media. |
| Cert or serve config lost (tailscaled restart, expiry) breaks all operators | Serve config persists; verify after restart/reboot; robots are unaffected (plain `ws://`). |
| Stale cached token / rotated keys | Self-heal on failed cached connect. |
| No token-service authn; shared basic-auth password | Documented above; operator-only public route; tailnet trust. |
| VM nginx does not forward the route or skips auth | Phase 3 check. |
| One dev server = one backend (env baked at bundle time) | Documented; optional runtime override. |
| Signaling proxy hop / DERP-relayed media | Signaling only; media direct where NAT traversal works (same as today). |
| SFU on a single PC | Accepted. |

## Shared token minting (2026-09-18)

`livekit-service` (Cloud) and `livekit-sfu-tokens` built their JWTs with
duplicated code. That step now lives in `packages/utils/livekit_tokens.py`
(`mint_room_token`); grants, identity rules, URLs and TTLs stay in each service.
Token contents are unchanged (checked by decoding one of each in the built
images), so the running containers were not restarted: both pick the shared
module up on the next `restart_services.sh` / rebuild. Also fixed:
`tests/unit/test_livekit_service.py` no longer replaces the `livekit` SDK with a
mock for the whole pytest session when the SDK is installed (it broke the SFU
token tests when both files ran together). The Cloud service was deliberately
not moved to the role model: that changes its request shape for the API proxy
and the cloud clients, so it needs its own decision.
