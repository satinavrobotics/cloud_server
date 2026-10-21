# Config profiles (server, pi4, pi5)

Settings that differ per machine are tracked in git; secrets never are.

| What | Where | Tracked |
|---|---|---|
| Shared defaults | `docker_compose/profiles/base.env` | yes |
| One machine's settings (only its differences) | `profiles/server.env`, `pi4.env`, `pi5.env` | yes |
| Secrets: passwords, keys, callback secrets | `docker_compose/secrets.env` on that machine, mode 600 | **no** |
| Which secrets a machine needs | `secrets/server.env.example`, `secrets/robot.env.example` (`CHANGE_ME` = required, empty = optional) | yes |
| What compose reads | `docker_compose/.env` (server) or `.env.robot` (Pi), rendered | **no** |

## Render

    scripts/render_env.sh server          # writes docker_compose/.env
    scripts/render_env.sh pi4             # writes docker_compose/.env.robot
    scripts/render_env.sh pi4 --check     # writes nothing: lists what would change (secret values are never shown); exit 1 if it would
    scripts/render_env.sh pi4 --hash      # the config hash the repo expects for this profile

The render stops, writing nothing, if a required secret is missing, empty or `CHANGE_ME`; if `secrets.env` carries a setting; or if a
profile carries a secret. The `IMAGE_TAG` already in `.env.robot` is kept (a deploy owns it). `deploy_robot.sh` and
`robot_compose.sh` are unchanged: they still read `.env.robot`.

## What a machine reports

`GET /api/v1/server-info` has `config_profile` and `config_hash`: the profile the env file was rendered from and a hash of its
non-secret settings (not `IMAGE_TAG`). A Pi is out of date when its `config_hash` differs from `scripts/render_env.sh <profile> --hash`
on the commit it should be on. Both are null on a machine set up by hand.

## Moving an existing machine over (once)

1. Back up the file you have now: `cp docker_compose/.env ~/env.backup` (or `.env.robot`). It is the only copy of the secrets.
2. `scripts/render_env.sh <profile> --init-secrets` writes `secrets.env` (mode 600) from the secret keys of the current file. It never
   overwrites. Add by hand any required key it lists as missing.
3. `scripts/render_env.sh <profile> --check` shows every difference between what the profile would write and what runs now. Expect only
   `CONFIG_PROFILE` and `CONFIG_HASH` on a machine that was set up like the profile. Anything else is a setting that was changed by hand:
   move it into the profile (if every machine of that kind should have it) or accept the change.
4. `scripts/render_env.sh <profile>`, then `docker compose up -d` the services that read the changed values.

**On the cloud server, `docker_compose/.env` is tracked until this change reaches it.** Pulling the commit that untracks it deletes the
file, or the pull stops because of your local edits. Do step 1 and 2 first, then pull, then step 4. Passwords that were ever committed
stay in git history: rotate them (`.env`, and the four `.env.robot.bak-*` files that were committed by mistake).

## Other files

- `staging` (`profiles/staging.env`, `secrets/staging.env.example`) renders `.env.staging` for the staging rehearsal override.
- On the server profile the self-hosted LiveKit keys (`LIVEKIT_SFU_*`) are not written to `.env` but to `livekit_sfu.env`, which
  the two LiveKit services read as an `env_file`. `--init-secrets` takes them from that file too.
- Any profile that says `SERVER_KIND=robot` is a robot (renders `.env.robot`, secrets from `secrets/robot.env.example`): a new robot
  needs only its own profile file. `scripts/setup_robot.sh [--profile NAME]` creates the profile from the host (tailnet IP, name),
  writes `secrets.env` with fresh random passwords and renders `.env.robot`; it still does the Docker, registry and volume setup.
- `.env.robot.example` stays as a reference of the keys; nothing reads it any more.
