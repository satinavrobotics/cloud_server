#!/usr/bin/env python3
"""Render a machine's compose env file(s) from tracked profiles plus its untracked secrets.

    scripts/render_env.sh <profile> [--check] [--hash] [--out FILE] [--secrets FILE] [--init-secrets [FROM]]

    profile   server | staging | pi4 | pi5 | any robot profile   (docker_compose/profiles/<profile>.env)

The file is base.env, then the profile, then the machine's local overrides (docker_compose/local/overrides.env, written by the app,
never committed), then secrets.env, then CONFIG_PROFILE / CONFIG_HASH / CONFIG_OVERRIDES. server renders to docker_compose/.env
(compose reads it by default) and also livekit_sfu.env (the LIVEKIT_SFU_* keys, which the LiveKit services read as an env_file);
staging renders .env.staging; a robot (a profile with SERVER_KIND=robot) renders .env.robot. It stops, writing nothing, if a
required secret is missing, empty or still CHANGE_ME (the keys of secrets/<kind>.env.example; an empty value there means optional)
or if secrets.env sets a key that belongs in a profile, or a profile holds a secret.

    --check   write nothing: list what would change (key names only, values of secrets never shown); exit 1 if anything would
    --init-secrets [FROM]
              one-time move to profiles: write secrets.env (mode 600, never overwritten) from the secret keys of FROM, by default
              the profile's current output file(s) (.env / livekit_sfu.env / .env.robot); then run --check to see what else differs
    --hash    print the config hash of the profile and stop (what a machine reports at /api/v1/server-info as config_hash)
    --out F   render the primary file to F instead of the default (extra files are then written next to it)

CONFIG_HASH covers the profile's non-secret keys only (not the local overrides: CONFIG_OVERRIDES is their own hash, empty when there are none) (not IMAGE_TAG, which a deploy changes), so two machines on one profile agree and a
machine whose profile moved on shows a different hash. IMAGE_TAG already in the output file is kept: a render must not undo a deploy.
"""
import argparse
import hashlib
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Tuple

DC = Path(__file__).resolve().parent.parent / "docker_compose"
# profile -> (secrets kind, primary output file). A profile not listed here that says SERVER_KIND=robot is a robot.
BUILT_IN = {"server": ("server", ".env"), "staging": ("staging", ".env.staging")}
ROBOT = ("robot", ".env.robot")
# profile -> [(extra output file, key prefix)]: those keys go to the extra file only (a service reads it as its own env_file)
SPLIT = {"server": [("livekit_sfu.env", "LIVEKIT_SFU_")]}
NO_BASE = {"staging"}       # profiles that do not inherit base.env (the rehearsal is its own small world)
KEEP = ("IMAGE_TAG",)        # machine state, not config
NOT_HASHED = set(KEEP) | {"CONFIG_PROFILE", "CONFIG_HASH", "CONFIG_OVERRIDES"}
SECRET_NAME = re.compile(r"(PASSWORD|_SECRET|_KEY|_TOKEN|ROOT_USER)$")
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


def overrides_hash(overrides: dict) -> str:
    """12 hex characters over the override lines, or "" when there are none (the app's service computes the same)."""
    if not overrides:
        return ""
    return hashlib.sha256("\n".join(f"{k}={v}" for k, v in sorted(overrides.items())).encode()).hexdigest()[:12]


def local_overrides(dc: Path = DC) -> dict:
    """docker_compose/local/overrides.env: settings changed on this machine from the app. Never a secret."""
    path = dc / "local" / "overrides.env"
    if not path.is_file():
        return {}
    found = parse(path)
    held = sorted(k for k in found if is_secret(k))
    if held:
        raise RenderError(f"local/overrides.env holds secrets, which belong in secrets.env: {', '.join(held)}")
    return found


def available(dc: Path = DC) -> List[str]:
    return sorted(p.stem for p in (dc / "profiles").glob("*.env") if p.stem != "base")


def profile_info(profile: str, dc: Path = DC) -> Tuple[str, str]:
    """(secrets kind, primary output file name)."""
    if profile in BUILT_IN:
        return BUILT_IN[profile]
    path = dc / "profiles" / f"{profile}.env"
    if profile != "base" and path.is_file() and parse(path).get("SERVER_KIND") == "robot":
        return ROBOT
    raise RenderError(f"unknown profile {profile!r} (one of: {', '.join(available(dc))})")


def merged_profile(profile: str, dc: Path = DC) -> dict:
    profile_info(profile, dc)
    base = {} if profile in NO_BASE else parse(dc / "profiles" / "base.env")
    settings = {**base, **parse(dc / "profiles" / f"{profile}.env")}
    held = sorted(k for k in settings if is_secret(k))
    if held:
        raise RenderError(f"profile {profile} holds secrets, which belong in secrets.env: {', '.join(held)}")
    return settings


def required_secrets(profile: str, dc: Path = DC) -> tuple:
    """(required, optional) key names from secrets/<kind>.env.example."""
    example = parse(dc / "secrets" / f"{profile_info(profile, dc)[0]}.env.example")
    return ([k for k, v in example.items() if v == PLACEHOLDER], [k for k, v in example.items() if v != PLACEHOLDER])


def output_names(profile: str, dc: Path = DC) -> List[str]:
    return [profile_info(profile, dc)[1]] + [name for name, _ in SPLIT.get(profile, [])]


def _body(header: str, pairs: Dict[str, str]) -> str:
    return "\n".join([header, "# Not tracked by git. It holds secrets."] + [f"{k}={v}" for k, v in pairs.items()]) + "\n"


def render(profile: str, secrets_file: Path, previous: dict, dc: Path = DC) -> Dict[str, str]:
    """{output file name: text}. `previous` is the primary file as it is now (IMAGE_TAG is kept from it)."""
    settings = merged_profile(profile, dc)
    if not secrets_file.is_file():
        raise RenderError(f"no {secrets_file}: copy docker_compose/secrets/{profile_info(profile, dc)[0]}.env.example there and fill it in")
    secrets = parse(secrets_file)
    required, optional = required_secrets(profile, dc)
    missing = [k for k in required if secrets.get(k, "") in ("", PLACEHOLDER)]
    if missing:
        raise RenderError(f"{secrets_file.name} is missing: {', '.join(missing)}")
    stray = sorted(k for k in secrets if k not in required and k not in optional)
    if stray:
        raise RenderError(f"{secrets_file.name} has keys that are not secrets for {profile} (put settings in profiles/{profile}.env): {', '.join(stray)}")
    overrides = local_overrides(dc)
    config = {**settings, **overrides, "CONFIG_PROFILE": profile}
    config["CONFIG_HASH"] = config_hash(settings)
    config["CONFIG_OVERRIDES"] = overrides_hash(overrides)
    kept = {k: previous[k] for k in KEEP if k in previous}
    everything = {**config, **kept, **{k: secrets[k] for k in [*required, *optional] if k in secrets}}
    header = f"# Rendered by scripts/render_env.sh {profile}: do not edit; change profiles/{profile}.env or secrets.env and render again."
    out: Dict[str, str] = {}
    extras = SPLIT.get(profile, [])
    moved = {k for k in everything for _, prefix in extras if k.startswith(prefix)}
    out[profile_info(profile, dc)[1]] = _body(header, {k: v for k, v in everything.items() if k not in moved})
    for name, prefix in extras:
        out[name] = _body(header, {k: v for k, v in everything.items() if k.startswith(prefix)})
    return out


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


def _targets(profile: str, out: Path, dc: Path = DC) -> Dict[str, Path]:
    """output name -> path; with --out the primary goes there and the extras beside it."""
    names = output_names(profile, dc)
    return {names[0]: out, **{n: out.parent / n for n in names[1:]}}


def _write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


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
        primary = args.out or DC / profile_info(args.profile)[1]
        targets = _targets(args.profile, primary)
        if args.init_secrets is not None:
            sources = [Path(args.init_secrets)] if args.init_secrets else list(targets.values())
            return init_secrets(args.profile, sources, args.secrets)
        previous = parse(primary) if primary.is_file() else {}
        rendered = render(args.profile, args.secrets, previous)
        if args.check:
            rows = []
            for name, text in rendered.items():
                path = targets[name]
                old = parse(path) if path.is_file() else {}
                rows += [f"[{name}] {r}" if len(rendered) > 1 else r for r in diff_keys(old, parse_text(text))]
            print("\n".join(rows) if rows else f"{primary.name} is up to date")
            return 1 if rows else 0
        for name, text in rendered.items():
            _write(targets[name], text)
        print(f"wrote {', '.join(str(targets[n]) for n in rendered)} (profile {args.profile}, config {config_hash(merged_profile(args.profile))})")
        return 0
    except RenderError as e:
        print(f"render_env: {e}", file=sys.stderr)
        return 2


def init_secrets(profile: str, sources: List[Path], target: Path) -> int:
    if target.exists():
        raise RenderError(f"{target} exists; not overwriting it")
    existing = [s for s in sources if s.is_file()]
    if not existing:
        raise RenderError(f"no {' or '.join(str(s) for s in sources)} to take the secrets from")
    required, optional = required_secrets(profile)
    found: dict = {}
    for source in existing:
        found.update(parse(source))
    wanted = [k for k in [*required, *optional] if k in found]
    absent = [k for k in required if k not in found]
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("".join(f"{k}={found[k]}\n" for k in wanted))
    print(f"wrote {target} (mode 600) with {len(wanted)} keys: {', '.join(wanted)}")
    if absent:
        print(f"still to add by hand (required for {profile}, not in {', '.join(s.name for s in existing)}): {', '.join(absent)}")
    return 0


def parse_text(text: str) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".env") as f:
        f.write(text)
        f.flush()
        return parse(Path(f.name))


if __name__ == "__main__":
    sys.exit(main())
