#!/usr/bin/env bash
# One-time setup of a robot host (Raspberry Pi 4, 64-bit OS) for the local stack, then (optionally) the first deploy.
# Run it from the repo on the Pi, as a user that may use docker (and sudo, for the docker daemon config):
#
#   scripts/setup_robot.sh [--profile NAME] [--deploy] [--tag TAG] [--minio-dir DIR] [--registry HOST:PORT] [--yes]
#
# It checks the prerequisites (64-bit, docker, compose >= 2.24, tailscale up), allows the plain-HTTP registry in
# /etc/docker/daemon.json (asks first, restarts docker), creates the sati_pgdata17 volume and the MinIO data directory,
# then sets up the machine's config the profile way (docs/CONFIG_PROFILES.md): the profile docker_compose/profiles/NAME.env
# (default NAME: this host's name; created from this host's tailnet IP and name if it does not exist: review and commit it),
# docker_compose/secrets.env with FRESH random passwords (never overwritten), and docker_compose/.env.robot rendered from both.
# With --deploy it then runs scripts/deploy_robot.sh. Safe to run again.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DC_DIR="$ROOT/docker_compose"
ENV_FILE="$DC_DIR/.env.robot"
REGISTRY="100.85.3.47:5050"
MINIO_DIR="/srv/sati/minio_data"
TAG=""; DEPLOY=0; YES=0; PROFILE=""

die() { printf "setup_robot: %b\n" "$*" >&2; exit 1; }
log() { printf "setup_robot: %b\n" "$*" >&2; }
usage() { sed -n '2,/^set -e/p' "${BASH_SOURCE[0]}" | sed -e '$d' -e 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
    case "$1" in
        --profile) [ $# -ge 2 ] || die "--profile needs a value"; PROFILE="$2"; shift ;;
        --deploy) DEPLOY=1 ;;
        --tag) [ $# -ge 2 ] || die "--tag needs a value"; TAG="$2"; shift ;;
        --minio-dir) [ $# -ge 2 ] || die "--minio-dir needs a value"; MINIO_DIR="$2"; shift ;;
        --registry) [ $# -ge 2 ] || die "--registry needs a value"; REGISTRY="$2"; shift ;;
        --yes) YES=1 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument $1 (see --help)" ;;
    esac
    shift
done

confirm() { [ "$YES" = 1 ] && return 0; read -r -p "$1 [y/N] " a; [ "$a" = y ] || [ "$a" = Y ]; }
SUDO=""; [ "$(id -u)" = 0 ] || SUDO="sudo"

# --- prerequisites
[ -w "$DC_DIR" ] || die "$DC_DIR is not writable by $(id -un) (it holds .env.robot and the deploy tag files); run: sudo chown -R $(id -un) \"$ROOT\""
[ "$(uname -m)" = aarch64 ] || log "warning: this host is $(uname -m), not aarch64 (fine for a test, the Pi image set is arm64)"
command -v docker >/dev/null || die "docker is not installed (https://docs.docker.com/engine/install/)"
ver="$(docker compose version --short 2>/dev/null | sed 's/^v//')" || true
[ -n "${ver:-}" ] || die "docker compose plugin is missing"
[ "$(printf '%s\n2.24.0\n' "$ver" | sort -V | head -1)" = "2.24.0" ] || die "docker compose $ver is too old (need >= 2.24)"
docker info >/dev/null 2>&1 || die "cannot talk to the docker daemon (is it running, is this user in the docker group?)"
if docker info 2>&1 | grep -qi "no memory limit support"; then
    die "the kernel has no memory cgroup: add 'cgroup_enable=memory cgroup_memory=1' to /boot/firmware/cmdline.txt and reboot"
fi
command -v tailscale >/dev/null || die "tailscale is not installed"
TS_IP="$(tailscale ip -4 2>/dev/null | head -1)"
[ -n "$TS_IP" ] || die "tailscale is not up (no tailnet IPv4); run 'sudo tailscale up'"
log "tailnet IP $TS_IP, docker compose $ver"
if ! (timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -qx yes); then
    log "warning: the clock is not NTP-synchronised; presigned URLs fail on clock skew (install chrony or enable systemd-timesyncd)"
fi

# --- registry: plain HTTP on the tailnet
if ! curl -fsS -m 5 "http://$REGISTRY/v2/" >/dev/null 2>&1; then
    log "warning: registry http://$REGISTRY/v2/ is not reachable from here (tailnet ACL? registry down?)"
fi
if ! docker info 2>/dev/null | sed -n '/Insecure Registries/,/^ [A-Za-z]/p' | grep -q "$REGISTRY"; then
    log "docker does not allow the plain-HTTP registry $REGISTRY yet"
    confirm "add it to /etc/docker/daemon.json and restart docker?" || die "cannot pull without it; aborting"
    python3 - "$REGISTRY" <<'PY' | $SUDO tee /etc/docker/daemon.json.new >/dev/null
import json, os, sys
p = "/etc/docker/daemon.json"
d = json.load(open(p)) if os.path.exists(p) else {}
d.setdefault("insecure-registries", [])
if sys.argv[1] not in d["insecure-registries"]:
    d["insecure-registries"].append(sys.argv[1])
d.setdefault("log-driver", "json-file")
d.setdefault("log-opts", {"max-size": "10m", "max-file": "3"})
print(json.dumps(d, indent=2))
PY
    [ ! -f /etc/docker/daemon.json ] || $SUDO cp /etc/docker/daemon.json /etc/docker/daemon.json.bak
    $SUDO mv /etc/docker/daemon.json.new /etc/docker/daemon.json
    $SUDO systemctl restart docker
    sleep 3
    docker info >/dev/null 2>&1 || die "docker did not come back after the restart; see /etc/docker/daemon.json (backup: daemon.json.bak)"
fi

# --- volume and data directory
docker volume inspect sati_pgdata17 >/dev/null 2>&1 || { docker volume create sati_pgdata17 >/dev/null; log "created volume sati_pgdata17"; }
[ -d "$MINIO_DIR" ] || { $SUDO mkdir -p "$MINIO_DIR"; $SUDO chown "$(id -u):$(id -g)" "$MINIO_DIR"; log "created $MINIO_DIR"; }

# --- config: profile, secrets, rendered .env.robot
[ -n "$PROFILE" ] || PROFILE="$(hostname -s)"
case "$PROFILE" in *[!A-Za-z0-9_-]*|"") die "bad profile name '$PROFILE' (letters, digits, - and _ only)" ;; esac
PROFILE_FILE="$DC_DIR/profiles/$PROFILE.env"
SECRETS_FILE="$DC_DIR/secrets.env"
if [ -f "$PROFILE_FILE" ]; then
    grep -q "^ROBOT_TAILNET_IP=$TS_IP\$" "$PROFILE_FILE" || log "warning: $PROFILE_FILE does not say ROBOT_TAILNET_IP=$TS_IP (this host's tailnet IP)"
    log "profile $PROFILE_FILE exists, left unchanged"
else
    cat > "$PROFILE_FILE" <<EOP
# Robot server $PROFILE (created by scripts/setup_robot.sh on $(date +%F)): review, then commit. Renders to docker_compose/.env.robot.
ROBOT_TAILNET_IP=$TS_IP
MINIO_DATA_DIR=$MINIO_DIR
IMAGE_REGISTRY=$REGISTRY/sati

# Who this is (GET /api/v1/server-info)
SERVER_KIND=robot
SERVER_NAME=$(hostname -s)
COMPOSE_PROFILES=diagnostics
SERVER_DIAGNOSTICS_AVAILABLE=true
SERVER_LIVEKIT_AVAILABLE=true

PG_TUNE_MEMORY=512MB

# Operator actions from the desktop app (OPERATIONS_TOKEN in secrets.env for callers off the tailnet)
OPERATIONS_ENABLED=true
OPERATIONS_TRUST_TAILNET=true
EOP
    log "created $PROFILE_FILE from this host (tailnet IP $TS_IP): review it and commit it"
fi
if [ -f "$SECRETS_FILE" ]; then
    log "$SECRETS_FILE exists, left unchanged"
else
    python3 - "$DC_DIR/secrets/robot.env.example" "$SECRETS_FILE" <<'PY'
import os, secrets, sys
example, target = sys.argv[1:]
rand = lambda n=24: "".join(secrets.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789") for _ in range(n))
shared = rand()
out = []
for line in open(example):
    line = line.rstrip("\n")
    if line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    if value != "CHANGE_ME":
        out.append(line)                                    # optional: left empty
    elif key in ("POSTGRES_DATABASE_PASSWORD", "POSTGRES_PASSWORD"):
        out.append(f"{key}={shared}")
    elif key == "MINIO_ROOT_USER":
        out.append(f"{key}=sati{rand(8)}")
    elif key == "LIVEKIT_SFU_API_KEY":
        out.append(f"{key}=API{rand(12)}")
    elif key == "LIVEKIT_SFU_API_SECRET":
        out.append(f"{key}={rand()}{rand()}")
    else:
        out.append(f"{key}={rand()}")
fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as f:
    f.write("\n".join(out) + "\n")
PY
    log "wrote $SECRETS_FILE with fresh random passwords (chmod 600); back it up: Postgres/Arango/MinIO keep them in their volumes"
fi
"$ROOT/scripts/render_env.sh" "$PROFILE"
if [ -n "$TAG" ]; then
    if grep -q '^IMAGE_TAG=' "$ENV_FILE"; then sed -i "s/^IMAGE_TAG=.*/IMAGE_TAG=$TAG/" "$ENV_FILE"; else printf 'IMAGE_TAG=%s\n' "$TAG" >> "$ENV_FILE"; fi
fi
grep -qE 'CHANGE_ME|100\.x\.y\.z' "$ENV_FILE" && die "$ENV_FILE still has placeholders; edit the profile or secrets.env and run scripts/render_env.sh $PROFILE"

if [ "$DEPLOY" = 1 ]; then
    exec "$ROOT/scripts/deploy_robot.sh" ${TAG:+"$TAG"}
fi
log "ready. Next: scripts/deploy_robot.sh <tag>   (tag printed by scripts/build_push.sh)"
