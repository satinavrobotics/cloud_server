#!/usr/bin/env python3
"""Render a machine's compose env file from tracked profiles plus its untracked secrets.

    scripts/render_env.sh <profile> [--check] [--hash] [--out FILE] [--secrets FILE]

    profile   server | pi4 | pi5   (docker_compose/profiles/<profile>.env)

The file is base.env, then the profile, then secrets.env, then CONFIG_PROFILE / CONFIG_HASH. server renders to
docker_compose/.env (compose reads it by default), a Pi to docker_compose/.env.robot. It stops, writing nothing, if a
required secret is missing, empty or still CHANGE_ME (the keys of secrets/<kind>.env.example; an empty value there means
optional) or if secrets.env sets a key that belongs in a profile, or a profile holds a secret.

    --check   write nothing: list what would change (key names only, values of secrets never shown); exit 1 if anything would
    --init-secrets [FROM]
              one-time move to profiles: write secrets.env (mode 600, never overwritten) from the secret keys of FROM, by default
              the profile's current output file (.env / .env.robot); then run --check to see what else differs
    --hash    print the config hash of the profile and stop (what a machine reports at /api/v1/server-info as config_hash)
    --out F   render to F instead of the default

CONFIG_HASH covers the non-secret keys only (not IMAGE_TAG, which a deploy changes), so two machines on one profile agree and a
machine whose profile moved on shows a different hash. IMAGE_TAG already in the output file is kept: a render must not undo a deploy.
"""
import argparse
import hashlib
import os
import re
import sys
import tempfile
from pathlib import Path

DC = Path(__file__).resolve().parent.parent / "docker_compose"
PROFILES = {"server": ("server", ".env"), "pi4": ("robot", ".env.robot"), "pi5": ("robot", ".env.robot")}
KEEP = ("IMAGE_TAG",)        # machine state, not config
NOT_HASHED = set(KEEP) | {"CONFIG_PROFILE", "CONFIG_HASH"}
SECRET_NAME = re.compile(r"(PASSWORD|_SECRET|_KEY|ROOT_USER)$")
PLACEHOLDER = "CHANGE_ME"
_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


class RenderError(Exception):
    pass


def parse(path: Path) -> dict:
    """KEY=VALUE lines of an env file (comments and blanks skipped; raw values, as compose reads them)."""
    out = {}
    for n, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            raise RenderError(f"{path.name}:{n}: not KEY=VALUE: {raw[:40]!r}")
        out[m.group(1)] = m.group(2)
    return out


def is_secret(key: str) -> bool:
    return bool(SECRET_NAME.search(key))


def config_hash(settings: dict) -> str:
    body = "\n".join(f"{k}={v}" for k, v in sorted(settings.items()) if k not in NOT_HASHED and not is_secret(k))
    return hashlib.sha256(body.encode()).hexdigest()[:12]


def merged_profile(profile: str, dc: Path = DC) -> dict:
    if profile not in PROFILES:
        raise RenderError(f"unknown profile {profile!r} (one of: {', '.join(sorted(PROFILES))})")
    settings = {**parse(dc / "profiles" / "base.env"), **parse(dc / "profiles" / f"{profile}.env")}
    held = sorted(k for k in settings if is_secret(k))
    if held:
        raise RenderError(f"profile {profile} holds secrets, which belong in secrets.env: {', '.join(held)}")
    return settings


def required_secrets(profile: str, dc: Path = DC) -> tuple:
    """(required, optional) key names from secrets/<kind>.env.example."""
    example = parse(dc / "secrets" / f"{PROFILES[profile][0]}.env.example")
    return ([k for k, v in example.items() if v == PLACEHOLDER], [k for k, v in example.items() if v != PLACEHOLDER])


def render(profile: str, secrets_file: Path, previous: dict, dc: Path = DC) -> str:
    settings = merged_profile(profile, dc)
    if not secrets_file.is_file():
        raise RenderError(f"no {secrets_file}: copy docker_compose/secrets/{PROFILES[profile][0]}.env.example there and fill it in")
    secrets = parse(secrets_file)
    required, optional = required_secrets(profile, dc)
    missing = [k for k in required if secrets.get(k, "") in ("", PLACEHOLDER)]
    if missing:
        raise RenderError(f"{secrets_file.name} is missing: {', '.join(missing)}")
    stray = sorted(k for k in secrets if k not in required and k not in optional)
    if stray:
        raise RenderError(f"{secrets_file.name} has keys that are not secrets for {profile} (put settings in profiles/{profile}.env): {', '.join(stray)}")
    config = {**settings, "CONFIG_PROFILE": profile}
    config["CONFIG_HASH"] = config_hash(settings)
    lines = [f"# Rendered by scripts/render_env.sh {profile}: do not edit; change profiles/{profile}.env or secrets.env and render again.",
             "# Not tracked by git. It holds secrets."]
    lines += [f"{k}={v}" for k, v in config.items()]
    kept = {k: previous[k] for k in KEEP if k in previous}
    lines += [f"{k}={v}" for k, v in kept.items()]
    lines += [f"{k}={secrets[k]}" for k in [*required, *optional] if k in secrets]
    return "\n".join(lines) + "\n"


def diff_keys(old: dict, new: dict) -> list:
    """What would change, by key; a secret's value is never shown."""
    rows = []
    for k in sorted(set(old) | set(new)):
        if old.get(k) == new.get(k):
            continue
        shown = (lambda v: "<set>" if v else "<empty>") if is_secret(k) else (lambda v: repr(v))
        if k not in old:
            rows.append(f"+ {k}={shown(new[k])}")
        elif k not in new:
            rows.append(f"- {k}   (now {shown(old[k])})")
        else:
            rows.append(f"~ {k}: {shown(old[k])} -> {shown(new[k])}")
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("profile")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--hash", action="store_true")
    ap.add_argument("--init-secrets", nargs="?", const="", metavar="FROM")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--secrets", type=Path, default=DC / "secrets.env")
    args = ap.parse_args(argv)
    try:
        if args.hash:
            print(config_hash(merged_profile(args.profile)))
            return 0
        out = args.out or DC / PROFILES[args.profile][1]
        if args.init_secrets is not None:
            return init_secrets(args.profile, Path(args.init_secrets) if args.init_secrets else out, args.secrets)
        previous = parse(out) if out.is_file() else {}
        text = render(args.profile, args.secrets, previous)
        if args.check:
            rows = diff_keys(previous, parse_text(text))
            print("\n".join(rows) if rows else f"{out.name} is up to date")
            return 1 if rows else 0
        fd, tmp = tempfile.mkstemp(dir=out.parent, prefix=out.name + ".")
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, out)
        print(f"wrote {out} (profile {args.profile}, config {config_hash(merged_profile(args.profile))})")
        return 0
    except RenderError as e:
        print(f"render_env: {e}", file=sys.stderr)
        return 2


def init_secrets(profile: str, source: Path, target: Path) -> int:
    if target.exists():
        raise RenderError(f"{target} exists; not overwriting it")
    if not source.is_file():
        raise RenderError(f"no {source} to take the secrets from")
    required, optional = required_secrets(profile)
    found = parse(source)
    wanted = [k for k in [*required, *optional] if k in found]
    absent = [k for k in required if k not in found]
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("".join(f"{k}={found[k]}\n" for k in wanted))
    print(f"wrote {target} (mode 600) with {len(wanted)} keys: {', '.join(wanted)}")
    if absent:
        print(f"still to add by hand (required for {profile}, not in {source.name}): {', '.join(absent)}")
    return 0


def parse_text(text: str) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".env") as f:
        f.write(text)
        f.flush()
        return parse(Path(f.name))


if __name__ == "__main__":
    sys.exit(main())
