# Agent Orchestrator Service

LLM-based triage for the robot fleet. Subscribes to the VDA5050 `state` MQTT
stream (the same stream the mission controller consumes), filters it down to
*notable* events, and asks Claude to summarize each one in operator-friendly
language. Summaries ("insights") are exposed over REST and WebSocket.

This is **Phase 1 (summarize only)**. It never publishes orders or takes
actions, so it cannot stall the mission-critical dispatch loop. Phase 2 adds a
gated action surface via the Anthropic tool runner.

## Architecture

```
Robot ──VDA5050 state──▶ Mosquitto (:1883) ──▶ agent-orchestrator (:8007)
                                                  │  triggers.py  (filter — no LLM on quiet telemetry)
                                                  │  agent.py     (Claude summary)
                                                  ▼
                                          REST /insights + WS /ws/insights + log
```

- **`triggers.py`** — pure, dependency-free, edge-triggered detection. Fires on
  new errors, e-stop / field violation, operating-mode change, failed actions,
  and battery crossing a low threshold. Returns nothing for quiet telemetry, so
  the LLM is only called on real events. Fully unit-tested offline.
- **`agent.py`** — `FleetAgent`, the **single shared** Anthropic client (default
  model `claude-haiku-4-5`) reused by every session. Runs in **degraded mode**
  (deterministic, non-LLM summaries) when `ANTHROPIC_API_KEY` is unset, so the
  service still boots in dev.
- **`session.py`** — `RobotAgentSession`, **one robot's reasoning context**
  (state baseline + insight history + a conversation seam for the future
  interactive mode). Per-robot context lives here; there is no per-robot process
  or client.
- **`server.py`** — ingestion router + cross-cutting concerns: one MQTT
  subscription routed by robot name to per-robot sessions, global insight id,
  WebSocket fan-out (global + per-robot channels), and a no-op
  `_notify_coordinator` seam for the future fleet coordinator. The blocking LLM
  call is offloaded with `asyncio.to_thread` so the event loop stays responsive.

This is the foundation of the agreed **hierarchical** design: per-robot agent
sessions now, a fleet coordinator on top later (it will consume sessions'
insights, not raw telemetry) — all in one process / one MQTT subscription / one
shared LLM client.

## Configuration (`packages/config.py` / env)

| Env var | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | _(unset)_ | LLM key. Unset → degraded mode. |
| `ANTHROPIC_BASE_URL` | _(unset)_ | Optional Anthropic-compatible proxy URL (e.g. local LiteLLM → Gemini). Unset → real Anthropic API. |
| `AGENT_MODEL` | `claude-haiku-4-5` | Model for triage (must match the proxy's `model_name` when using one). |
| `AGENT_BATTERY_LOW_THRESHOLD` | `20.0` | SoC (%) at/below which low-battery fires. |
| `MQTT_VDA5050_PREFIX` | `uagv/v2/RobotCompany` | Must match the mission controller's prefix. |
| `MQTT_HOST` / `MQTT_PORT` | `localhost` / `1883` | Broker. |

## Run

```bash
# Standalone (dev)
ANTHROPIC_API_KEY=sk-ant-... python -m packages.services.agent_orchestrator.main --port 8007

# Full stack
docker compose -f docker_compose/mission_dispatch_services.yaml up agent-orchestrator-service
```

### Free dev LLM (no Anthropic credits) — Gemini via LiteLLM

The agent talks to Claude through the Anthropic SDK; pointing `ANTHROPIC_BASE_URL`
at a local [LiteLLM](https://github.com/BerriAI/litellm) proxy runs a free model
(Gemini by default) with **zero application-code changes** — LiteLLM exposes an
Anthropic-compatible `/v1/messages` endpoint and translates to the provider.

```
agent.py (Anthropic SDK) ──ANTHROPIC_BASE_URL──▶ LiteLLM :4000 ──▶ Gemini (free)
```

1. Free key from <https://aistudio.google.com/apikey> into `.env`:
   ```bash
   GEMINI_API_KEY=...
   LITELLM_MASTER_KEY=sk-litellm-local     # shared secret; == the agent's ANTHROPIC_API_KEY
   ```
   The proxy loads these from `.env` via `env_file` (see `llm_proxy.yaml`), so it
   works regardless of the directory you run compose from.
2. Start the proxy, then (re)build the agent with the dev override that points it
   at the proxy. The override sets the agent's three seam vars as explicit
   literals (`ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY`,
   `AGENT_MODEL=gemini-flash-lite-latest`) so it never touches the shared
   secrets `.env`:
   ```bash
   docker compose -f docker_compose/llm_proxy.yaml up -d
   docker compose -f docker_compose/mission_dispatch_services.yaml \
                  -f docker_compose/agent_gemini_override.yaml \
                  up -d --build agent-orchestrator-service   # --build: the seam changed agent code
   ```
   Drop the second `-f` to go back to real Anthropic / degraded mode.

> Use a **current** model id — `gemini-2.0-flash` was retired 2026-06-01.
> `gemini-flash-lite-latest` / `gemini-3.5-flash` / `gemini-flash-latest` work.
> `AGENT_MODEL` must match a `model_name` in `litellm_config.yaml`.
>
> **Free-tier rate limits are per-model and low** (e.g. `gemini-3.5-flash` ≈ 5
> req/min; Flash-Lite is higher — hence the default). On a 429 the agent logs the
> error and emits a degraded (deterministic) summary for that event, so a burst
> of alarms can produce a few non-LLM entries. Switch providers in
> `litellm_config.yaml` if you need more headroom.

Swap to Groq / NVIDIA NIM / Ollama / real Claude by editing
`docker_compose/litellm_config.yaml` + `AGENT_MODEL` — the seam stays the same.
If the proxy is unreachable, `summarize` falls back to a degraded summary per call.

## Endpoints

- `GET /health` — service + dependency health
- `GET /stats` — message/event/insight counters, model, degraded flag
- `GET /insights?robot=<name>&limit=<n>` — recent insights, newest first (fleet-wide, or one robot)
- `GET /insights/{robot}` — recent insights for a single robot's session
- `WS /ws/insights` — replays recent buffer, then streams new insights (fleet feed)
- `WS /ws/insights/{robot}` — same, scoped to one robot's channel (powers the sati-client per-robot Agent panel)

## Tests

```bash
# No Docker, no API key needed (degraded agent)
pytest tests/unit/test_agent_triggers.py tests/unit/test_agent_session.py \
       tests/unit/test_agent_service_routing.py -v
```

## Phase 2 (next)

Swap the single `messages.create` for `client.beta.messages.tool_runner` and a
gated tool surface: `notify_operator`, `flag_for_teleop`, `send_instant_action`
(MQTT publish to `{prefix}/{robot}/instantActions`), `create_mission` /
`request_charging_mission` (POST to the API delegation service on :8000).
Hard-to-reverse actions stay behind operator confirmation.
