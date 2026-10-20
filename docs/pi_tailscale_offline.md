# Tailscale on the Pi: LAN reachability without internet or control plane

Handoff written 2026-10-19 from `jetson-boss` (100.125.64.82, tag:robot) and adapted for the Pi deployment.
Items marked (measured) were read from the live Jetson. Items marked (doc) come from the Tailscale docs or the repo docs. Items marked (unproven) have not been tested.

Automation: `scripts/configure_pi_network.sh` (`check`, `apply`, `offline-test`). Read section 5 for what it cannot do.

## 1. Goal

Peers on the same LAN should reach each other over Tailscale (100.x addresses) when the internet or the Tailscale control plane is down.
That includes after a restart of tailscaled or a reboot.

## 2. What the Jetson has (state, measured)

| Item | Value |
|---|---|
| Client version | 1.104.1 (first stable with client-side netmap caching) |
| `cache-network-maps` in `.Self.CapMap` | yes, granted by the tailnet policy to `tag:robot` |
| Boot log | `Start: loaded netmap from disk cache; 11 peers` (the old 1.102.2 boot said 0 peers) |
| Health | empty |
| Node key | no expiry |
| Prefs | `NetfilterMode: 0` (off), auto-update off |
| tailscaled restarts since boot | 0 |

## 3. Settings and what each is for

| Setting | Where it comes from | Effect on offline behaviour |
|---|---|---|
| Client 1.104.1, pinned with `apt-mark hold tailscale` | `configure_pi_network.sh apply` (`TAILSCALE_VERSION`); on the Jetson `satibot_orchestrator/scripts/entrypoint/entrypoint.sh` step 0 | **This is the offline feature.** Netmap caching needs 1.104 or later. |
| `cache-network-maps` node attribute | Tailnet policy file (admin console) | **Required.** Without it the client does not cache. Not scriptable from the Pi. |
| Node key expiry disabled | Admin console, Machines page | Required for fixed devices: an expired key must be renewed from the control plane. |
| `tailscale set --netfilter-mode=off` | script `apply` | None. Removes a false iptables health error (the Jetson kernel has no connmark or policy routing). |
| `tailscale set --auto-update=false` | script `apply` | None. Stops self-updates restarting tailscaled at a random time. |
| Second DNS server (default 1.1.1.1) | script `apply`: NetworkManager default-route profile if it runs, else a systemd-resolved `FallbackDNS` drop-in | Indirect. Name resolution survives a router DNS failure. |
| systemd-timesyncd drop-in, `PollIntervalMaxSec` 600 | script `apply`, `/etc/systemd/timesyncd.conf.d/50-satinav-poll.conf` | Indirect. After an offline boot the clock syncs soon after the link returns. Wrong clocks hurt key and TLS checks. A Pi has no RTC, so this matters more here. |
| Docker starts after tailscaled | script `apply`, `/etc/systemd/system/docker.service.d/10-after-tailscaled.conf` | Mosquitto and the other containers come up with the tailnet address present (see `docs/robot_deploy.md`). |

Only the version, the node attribute and the key expiry decide offline operation. The rest is hygiene.

## 4. Why a device may not work offline

Check these in order (`configure_pi_network.sh check` covers 1, 2, 5 and the cache log line):

1. **Client older than 1.104.** There is no caching. After a restart it has 0 peers and reaches nothing until it reaches the control plane once.
2. **Not covered by the attribute.** The `nodeAttrs` grant targets specific tags or users. If the device is not in the target, it does not cache. (doc)
3. **Unsupported client type.** Mobile clients and ephemeral nodes are not supported. (doc)
4. **Cache is stale.** Peers that joined after the cache was written are unreachable until the cache refreshes. The cache is first filled only once the feature is on and the node has reached the control plane. (doc)
5. **Key expired.** Expired keys must be renewed from the control plane. (doc)
6. **It was restarted during the outage.** A tailscaled that stayed up keeps its netmap in memory. A restarted one only has the disk cache, which is empty without 1 to 4.
7. **LAN path.** Even with a cache, Tailscale gives no guarantee. The LAN path must be reachable. `tailscale status` must show `direct <lan-ip>:41641` for the peer, not a relay.

## 5. Steps

Run on the Pi:

```bash
sudo scripts/configure_pi_network.sh check     # read-only report
sudo scripts/configure_pi_network.sh apply     # installs/pins the client, sets prefs, DNS, NTP, docker ordering
```

`apply` is idempotent. If the client is below 1.104 it installs `tailscale=1.104.1`, which restarts tailscaled; run it when nothing depends on the tailnet. On macOS or Windows install a 1.104+ build from tailscale.com instead.

Two things stay manual, and `check` reports them as TODO:

- **`cache-network-maps` missing from CapMap:** in the admin console, edit the access controls (policy file) and add a `nodeAttrs` entry that targets this device's tag or user. The shape is `{"target": ["tag:robot"], "attr": ["cache-network-maps"]}`. Check it against the current policy file before saving. (doc)
- **Key expiry set:** in the Machines page, use "Disable key expiry" for this device. Tagged devices normally have none.

Wait until the device has been online once after the change so the cache is filled. Then confirm `loaded netmap from disk cache; N peers` with N above 0 after a `systemctl restart tailscaled`.

### Pi state when this was written (measured on the cloud-server Pi)

Client 1.104.1 already, not held; `cache-network-maps` absent from CapMap; no tags; key expiry 2027-04-15; NetworkManager not running; no timesyncd drop-in. So the two manual items above are the blockers.

## 6. Offline restart test (unproven, still owed)

Run at the device, with no active order (from `satibot_orchestrator/docs/network-reliability-plan.md` section 6). The script automates steps 2 and 3:

1. Unplug the router WAN. Confirm `ping 1.1.1.1` fails and the gateway still answers.
2. `sudo PEER=<peer-100.x> scripts/configure_pi_network.sh offline-test` (restarts tailscaled, which drops the API briefly).
3. Within about 30 s: `tailscale status` lists the peer, `tailscale ping <peer-100.x>` answers, and the log shows `loaded netmap` with peers above 0.
4. Plug the WAN back in.

Pass means peers above 0 and the peer reachable with the WAN still unplugged. Fail means do the postponed fallback: a LAN address for the broker in config, with the tailnet as the fallback (not designed).
Repeat on every other device after it is fixed.

## 7. Open points

- LAN-direct communication after a cold start with no control plane is documented as "no guarantee" and has not been tested.
- Caching is a vendor trial feature, "not intended for long-term operation without access to the control plane". Expect it to work for short outages only.
- Rollback: `sudo apt-mark unhold tailscale && sudo apt-get install tailscale=1.102.5`.
- The script was only run in `check` mode so far; `apply` and `offline-test` are untested.

## 8. Sources

- Repo (robot side): `satibot_orchestrator/docs/network-reliability-plan.md` (sections 5, 6, 6.1), `scripts/configure_network.sh`, `scripts/entrypoint/entrypoint.sh`, `docs/entrypoint.md`.
- This repo: `docs/robot_deploy.md`, `docs/livekit_sfu/TAILSCALE_ADMIN_STEPS.md`.
- Tailscale docs: https://tailscale.com/docs/reference/client-side-netmap-caching
