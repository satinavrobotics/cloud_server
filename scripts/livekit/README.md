# robot_client.py — the one script every robot *and* operator launches

No per-device config, no identity file, no API secret on the device. Same
script, same command line, on every robot and every operator laptop — and
just as well on a dev machine for testing (this is what `docs/livekit_sfu/TESTING.md` phase
2/3 use from `cimbi` and the fleet Jetsons).

```
pip install -r requirements.txt
python robot_client.py                    # role=robot (default)
python robot_client.py --role operator    # room defaults to admin@satinavrobotics.com ($LIVEKIT_ROOM / --room)
```

- **Identity** resolves automatically from this device's own `tailscale
  status --json` (`Self.HostName` / `Self.TailscaleIPs`) — every robot and
  every operator laptop is already a tailnet member, so there's nothing to
  configure per-device. Requires `tailscaled` running and logged in; fails
  loudly if not. (Running two roles from the *same* machine, e.g. for a local
  test, will hit a `DuplicateIdentity` disconnect — both resolve to the same
  hostname. Not an issue in real use: robots and operators are always
  different physical devices.)
- **Auth**: fetches a short-lived, *role-scoped* token from the `livekit-sfu-tokens`
  service (`packages/services/livekit_sfu_tokens/`; `--token-server`, default `http://100.85.3.47:8008`) — the device never
  holds the raw `LIVEKIT_API_KEY`/`SECRET`, only that service does.
  `--role robot` (default) gets a token that publishes tracks + data and
  subscribes to nothing; `--role operator` gets one that subscribes and
  publishes data (teleop, RPC) but **cannot publish tracks**. This is
  enforced server-side, not just by the script choosing not to try —
  verified directly: an operator-scoped token attempting `publish_track` is
  refused by the SFU (`NOT_ALLOWED`; client-side it times out) rather than
  accepted.
- **Video** (`--role robot` only): real camera (index 0 by default) if one's
  available, a synthetic color-cycle frame if not (auto-detected;
  `--force-synthetic` to skip the camera deliberately). Same I420/RGBA
  publish path LiveKit's own SDK examples use.
- **Viewing** (`--role operator`): publishes nothing, just connects,
  auto-subscribes (LiveKit's default), and logs `track_subscribed`/
  `participant_connected` events — a minimal headless viewer, useful for
  confirming a robot's stream is actually reachable before wiring up a real
  dashboard.
- **Resilience**: if the token service is briefly unreachable (retries with
  backoff) or the room connection drops (4G blips expected on robots —
  RUT901 + cellular; WiFi roaming on operator laptops), reconnects
  automatically rather than exiting. It always waits before reconnecting
  (1 s, doubling up to 30 s; reset only after a session that stayed up more
  than 30 s), even after a clean disconnect: two clients with the same identity
  kick each other, and would otherwise spin. A token-server answer of 4xx (a
  refusal, e.g. a reserved identity) stops the client instead of retrying.
  `Ctrl-C`/SIGTERM disconnects cleanly, including while the token server is
  unreachable.

Known-benign log line: the SDK's Rust core tries a newer `/rtc/v1` signal
path first, gets a 404 from this server's version (1.9.1), and falls back to
`/rtc` — self-heals, not an error to chase.

Not the same tool as `tests/performance/livekit/` (synthetic bulk load-testing via the `lk`
CLI, no real media) — this is the real thing, one participant at a time.

## Least-privilege on the network layer too

Token scoping (above) closes the *application-layer* gap — an operator
token can't publish. The matching *network-layer* piece is giving operator
devices their own Tailscale ACL scoping instead of relying on account
ownership (which is all `cimbi` has today) — see Human task 4 in
`docs/livekit_sfu/TAILSCALE_ADMIN_STEPS.md`.
