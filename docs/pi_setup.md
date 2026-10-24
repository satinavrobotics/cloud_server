# Setting up a robot Pi (Pi 4 or Pi 5), start to finish

One page, in order. Details live in [robot_deploy.md](robot_deploy.md) (what runs, deploy, gateway), [pi_tailscale_offline.md](pi_tailscale_offline.md)
(Tailscale offline behaviour) and [CONFIG_PROFILES.md](CONFIG_PROFILES.md) (profiles). Two scripts do the work:

| Script | Does |
|---|---|
| `scripts/configure_pi_network.sh --profile pi4\|pi5 check\|apply\|rollback\|offline-test` | **All network setup:** Tailscale client, second DNS, chrony NTP server, docker-after-tailscaled, and the fixed `eth0` address on the robot LAN |
| `scripts/setup_robot.sh --profile NAME [--deploy --tag SHA]` | the stack: registry, volumes, profile, secrets, `.env.robot`, first deploy |

## The two machines

| | Pi 4 | Pi 5 |
|---|---|---|
| Host name, tailnet IP | `raspberrypi`, 100.116.20.49 (offline since 2026-10; not the live server) | `rasp`, 100.110.187.108 (the live server) |
| Profile | `docker_compose/profiles/pi4.env` | `docker_compose/profiles/pi5.env` |
| RAM | 4 GB (compose defaults) | 8 GB (larger limits in the profile) |
| Robot LAN address (`eth0`) | 192.168.1.60, no gateway | 192.168.1.70, gateway 192.168.1.1 (the router) |
| Wi-Fi | on: the internet and Tailscale come through `wlan0` | off (`LAN_WLAN_OFF=true`): everything comes through `eth0` |
| Kernel | stock | the 4 KB-page kernel: `/boot/firmware/config.txt` has `kernel=kernel8.img`, `getconf PAGESIZE` must say 4096. Do not remove the line or update firmware |
| Time source for the LAN | not the NTP server | not yet: the Jetson uses the public pool until the RTC battery is fitted and tested |
| Login | | `satipi`, key `~/.ssh/rasp_satipi` |

The Jetson is 192.168.1.50. A new Pi needs its own `docker_compose/profiles/<name>.env` (step 3 creates one from the host's tailnet IP: review and commit it).

## 0. Rules for a robot Pi

- **No upgrades.** No `apt upgrade`, `rpi-update`, unattended upgrades, kernel or firmware change. The scripts install only `tailscale` (pinned) and `chrony`. `apt-daily`, `apt-daily-upgrade` and `unattended-upgrades` stay disabled.
- **Run network changes over Tailscale, never over `eth0`**, and detached (`tmux`/`nohup`) for the Pi 5: the connection drops while the route moves from `wlan0` to `eth0`.
- **The robot LAN holds only the robot's own devices.** The gateway (80) and the MQTT broker (1883) have no login. `ROBOT_LAN_IP` is one address, never `0.0.0.0`.

## 1. Base OS (by hand, once)

64-bit Raspberry Pi OS, data on an SSD (`/srv/sati/minio_data`), Docker Engine with Compose >= 2.24 (`systemctl enable docker`), the login user in the `docker` group, `cgroup_enable=memory cgroup_memory=1` in `/boot/firmware/cmdline.txt` if `docker info` says "No memory limit support", swap or zram, Tailscale installed and up (`sudo tailscale up`). Pi 5: the 4 KB kernel line above.

## 2. Network: the one script

```bash
git clone <repo> && cd cloud_server            # on the Pi
sudo scripts/configure_pi_network.sh --profile pi5 check     # read-only: Tailscale, DNS, clock, docker order, LAN address
sudo scripts/configure_pi_network.sh --profile pi5 apply     # then change what is reported; idempotent
```

(Use `--profile pi4` on the Pi 4.) `apply` does, in this order, and the LAN step is last because it can move the routes and its checks need Tailscale up:

1. Tailscale client 1.104.1, pinned with `apt-mark hold`; netfilter-mode off, auto-update off. This restarts tailscaled if the client was older.
2. Second DNS server (1.1.1.1): NetworkManager default-route profile, else a systemd-resolved `FallbackDNS` drop-in.
3. **Clock:** only a `systemd-timesyncd` poll drop-in. **chrony (this Pi as the LAN's NTP server) is opt-in: `--ntp-server`, and NOT on the Pi 5 yet.** The Pi has no RTC battery, so after a power loss its clock is wrong and the Jetson would follow it. The robot team keeps the public pool on the Jetson and leaves `NTP=192.168.1.70` unset (2026-10-24). Do not use `--ntp-server` until a battery is fitted and the power-off test repeated and the result sent to them. The flag also refuses while the clock is before the build date (`NTP_CLOCK_FLOOR`, default the HEAD commit time).
4. Docker starts after tailscaled (drop-in), so mosquitto and `minio-tailnet` do not restart-loop at boot.
5. The LAN address from the profile: `eth0` gets a fixed `ROBOT_LAN_IP`/24. Whoever owns `eth0` is edited (NetworkManager, else dhcpcd). Without Wi-Fi-off, `eth0` gets no gateway. With `LAN_WLAN_OFF=true` (Pi 5) `eth0` also gets the gateway and DNS, the script verifies the internet and Tailscale through `eth0` while `wlan0` is still up, then switches `wlan0` off (rfkill), then verifies again. Any failed check restores everything. `rollback` restores the last backup and turns `wlan0` back on. Check after a reboot that `wlan0` stayed off.

Two things stay manual and `check` reports them as TODO: the `cache-network-maps` node attribute in the Tailscale admin console, and disabling node-key expiry ([pi_tailscale_offline.md](pi_tailscale_offline.md) section 5).

Check afterwards: `ip -br addr show eth0`, `ping 192.168.1.50`, `tailscale status`.

## 3. The stack

```bash
scripts/setup_robot.sh --profile pi5 --deploy --tag <sha>
```

`setup_robot.sh` checks the prerequisites, allows the plain-HTTP registry (asks first), creates the volumes, the profile if missing, `docker_compose/secrets.env` (fresh passwords; never copy the cloud's) and `.env.robot`. After a change to `ROBOT_LAN_IP`: `scripts/render_env.sh <profile>` and a deploy, which recreates the gateway and the broker.

## 4. Releases afterwards

On the cloud host: `scripts/build_push.sh` (clean tree; prints the tag). On the Pi: `git pull && scripts/deploy_robot.sh <tag>` (or the app's Deploy button). Before a deploy check `GET /api/v1/missions` for RUNNING/PENDING, because the gateway is recreated. A deploy rolls itself back if the new tag is not healthy.

## 5. Offline forest run: what has to hold

- The Jetson reaches the Pi on the LAN: MQTT `192.168.1.<pi>:1883`, token service `http://192.168.1.70:8008/api/createToken` (open on the LAN for now: `TODO(security)` in `nginx-robot.conf.template`), video SFU `ws://192.168.1.70:7880`.
- Clock: the Jetson keeps its own (public pool); `NTP=192.168.1.70` stays unset until the Pi 5 has an RTC battery and a passed power-off test.
- Run the offline restart test once ([pi_tailscale_offline.md](pi_tailscale_offline.md) section 6): `sudo PEER=<100.x> scripts/configure_pi_network.sh offline-test`.

## Status of the scripts

The merged script's `check` ran on the dev machine only (read-only, no `eth0` there). Its `apply` on a Pi, the chrony part and the nginx LAN door have **not been run on hardware**; the LAN logic is the old `configure_pi_lan.sh` unchanged, wrapped in a function. Run `check`, read it, then `apply`, and report what differs.
