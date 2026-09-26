# SatiNav Fleet Agent — Phase 1: Explain with Evidence

**Goal:** the agent answers **"why did run X fail"**, **"what's going wrong on robot Y / at site Z"** and **"what changed since version V"**, and every claim cites a stable `run_id`, `event_id` or timeline range that an operator can open in sati-client. It also raises these explanations **proactively** when a run ends badly. It explains; it does not act.

**Constraints**

- **No new long-running containers.** Phase 1 evolves the existing `agent-orchestrator-service` (port 8007) in place.
- **Read-only.** The agent has no tool that changes robots, missions, maps or settings (§8).
- **No authentication** (decided 2026-09-25). §8.4 lists where it plugs in later.
- History starts at **2026-09-24** (WP12 backfill dropped). `sw_version` is usually **null** (robot-side WP3 deferred). GNSS is out of scope.
- Most robots record at `events_only`, so time series are sparse. The agent must tell **"nothing happened"** from **"nothing was recorded"** (Phase 0 principle 6).
- Charts and visualisations stay in the sati-client UI, not Grafana.

**Duration:** about 4 weeks for about 1.5 engineers (1 backend on the agent service and DB, 0.5 frontend on sati-client), plus about 2 h/week of team time for labelling and judging.

**Exit test** (§11 WP12):

1. **Real incidents:** the team picks 10 real incidents since rollout (at least 4 non-`COMPLETED` runs, 1 disconnect, 1 question whose answer lies inside a `not_recorded` gap, 1 site-level and 1 version-level question). At least **8 of 10** are judged correct. **All** cited IDs resolve, and **no** answer claims an absence of events inside a `not_recorded` interval.
2. **Eval set:** at least 40 golden cases pass the thresholds in §9.4 on the offline replay.
3. **Proactive:** for one week in production, every `FAILED` / `ABORTED` / `TIMEOUT` run gets an insight within 2 min of `MISSION.RUN_FINISHED`, with at most 1 duplicate per incident. Spend stays within the daily budget (§7).

---

## 1. Where we start

The service in `packages/services/agent_orchestrator/` today:

| Aspect | Today |
|---|---|
| Input | Subscribes to the VDA5050 `state` MQTT stream directly. No database access at all (the Dockerfile copies only `packages/utils` and `config.py`). |
| Triggers | `triggers.py`: pure, edge-triggered diff of consecutive `state` messages: new error, `edgeBlocked`, e-stop, field violation, operating-mode change, failed action, battery crossing 20 %. Unit-tested offline (`tests/unit/test_agent_triggers.py`). |
| LLM | One synchronous `messages.create` per trigger burst via the Anthropic SDK (`anthropic>=0.40.0`, unpinned), model `AGENT_MODEL` = `claude-haiku-4-5`, offloaded with `asyncio.to_thread`. Returns JSON `{severity, summary, suggested_action}`. Degraded (template) mode when there's no key or the call fails. Optional LiteLLM proxy for a free dev model. |
| Caching | `cache_control` is set on the ~700-token system prompt, but Haiku 4.5's minimum cacheable prefix is 4096 tokens, so **nothing is cached today**. |
| Output | Insights live in a per-robot in-memory `deque(100)`: they're lost on restart, have no link to runs or events, and have no feedback. Exposed as `GET /insights[/{robot}]`, `WS /ws/insights[/{robot}]` and `/stats`. |
| UI | sati-client `AgentModal` (per-robot panel, read-only list over REST plus WS). `useAgentFailureSuggestion` matches insights to failed missions **by a ±10 min time window**, because "there's no direct id to match on". |

What Phase 0 added that the agent doesn't use yet: `mission_runs`, `fleet_events` (27 codes, deterministic `event_id`), the cause rules (`packages/events/causes.py`, 20 codes), the telemetry rollups, `robot_latest`, sites and assignment history, and the read endpoints in `packages/api/fleet_reads.py`, including `not_recorded`.

**Why evolve this service rather than add one.** It already owns the right seams: the per-robot session, the insight WS fan-out, the degraded mode, the nginx routes `/api/agent/*` and `/ws/agent/*`, and the UI panel. A second LLM service would duplicate all of it. Phase 1 replaces its input (raw MQTT → Phase 0 data), its reasoning (one call → a tool loop) and its storage (a deque → Postgres).

---

## 2. Architecture summary

| Container | Status | Phase 1 responsibility |
|---|---|---|
| **agent-orchestrator** | existing, evolved | Event-trigger poller, tool loop (triage / investigate / deep), citation validator, insight store writer, `/ask` chat, feedback. Talks to the API over HTTP (GET only) and to Postgres through two small roles. The MQTT subscription is removed at the end of the phase (WP8). |
| **api** | existing | Alembic migration: the `agent` schema of views, two roles, and the agent tables. One new read route (recording history for any window). |
| **postgres** | existing | Hosts the views and tables. No new extension. |
| **mission-dispatch** | existing, small change (optional WP8b) | Emits 4 new event codes for conditions only the agent's MQTT diff sees today (e-stop, field violation, operating mode, action failed). |
| sati-client | existing | Agent panel: persisted insights with citation chips, an Ask tab, feedback, and failure cards matched by `run_id`. |
| graph-builder, mission-planner, livekit | existing | unchanged |

**No** new container, broker, queue or database.

```mermaid
flowchart LR
  subgraph PG[(Postgres + TimescaleDB)]
    T[Phase 0 tables] --> V[agent.* views]
    AT[agent_insights / feedback /<br/>investigations / trigger_log]
  end
  API[api :8000<br/>fleet_reads] -->|READ ONLY txn| T
  AG[agent-orchestrator :8007<br/>poller · tool loop · validator] -->|GET allowlist| API
  AG -->|role agent_ro: SELECT on views| V
  AG -->|role agent_rw: agent_* tables only| AT
  AG -->|Messages API| CL[(Claude)]
  UI[sati-client agent panel] -->|/api/agent/* · /ws/agent/*| AG
  UI -->|run detail / timeline| API
```

---

## 3. Principles

1. **Tools are thin wrappers over existing reads.** Per-run and per-event reads go through the §5.5 endpoints, so there's one implementation of pagination, statement timeouts and `not_recorded`. Only aggregates and "robot now" read the `agent.*` views directly.
2. **Every claim carries evidence.** An answer is a list of claims, and each claim cites IDs that a tool returned during *this* investigation. A validator rejects everything else (§6).
3. **Silence is not evidence.** Every tool result carries a `coverage` block (recording level, `not_recorded` intervals, share of null `sw_version`, and whether the window starts before 2026-09-24). The prompt and the eval both treat "absent because not recorded" as a distinct answer.
4. **Deterministic first, LLM second.** Rules decide *when* to look, Python summarises *what* to look at, and the model explains. Simple triggers (battery low, map delete failed) get template insights with no LLM call.
5. **Read-only by construction, not by prompt.** No write tools, a GET-only HTTP allowlist, and a database role that can't see base tables.
6. **Everything is replayable.** Each investigation logs its model, prompt version, tool calls and result hashes, so it can be re-run offline against a snapshot and scored.
7. **Cheap model for volume, strong model for reasoning.** Routes, models and budgets are config, and they're chosen by eval results (WP6), not by taste.

---

## 4. Schema

All DDL goes through Alembic in the API entrypoint, in one revision `…_01_phase1_agent`, following Phase 0's conventions: raw SQL, date-prefixed, no autogenerate.

### 4.1 The `agent` schema (read-only views)

The views are owned by the migration role and are **not** `security_invoker`, so `agent_ro` needs no grant on the base tables. They hide columns the agent doesn't need or shouldn't send to a third party.

| View | Source | Notes |
|---|---|---|
| `agent.runs` | `mission_runs` ⟕ `cause_codes` | All columns except `mission_tree` and `created_by`; adds `duration_s`, `cause_title`, `cause_category`, and `sw_version_or_unknown` = `coalesce(sw_version, 'unknown')` for `GROUP BY` |
| `agent.events` | `fleet_events` | All columns; `payload` as is |
| `agent.robot_now` | `robot_latest` ⟕ open `robot_site_assignments` | Extracted, not raw jsonb: `state`, `operating_mode`, `battery`, `errors` (type, level, description), `driving`, `nav_reasoning`, `temp_max`, `nodes_down`, `active_run_id`, `site_id`, `sw_version`, `last_seen`, `stale_s` |
| `agent.robot_state_1m`, `agent.diagnostics_1m` | the rollups | Pass-through. No raw `*_ts`: the rollups cover the 30-day raw window at the resolution the agent needs |
| `agent.site_assignments` | `robot_site_assignments` | |
| `agent.sites` | `siteobjectv1` | `site_id`, `display_name`, `timezone` only. **No** `customer`, `geofence`, `gps_datum` or `rtk_base` (open question Q1) |
| `agent.cause_codes` | `cause_codes` | |
| `agent.insights` | `agent_insights` | So the agent can find prior insights about the same robot, run or cause |

**Archived and deleted runs.** Archived runs (`mission_runs.archived_at` set) are still data: `agent.runs` keeps them (with `archived_at`), and `list_runs` passes `archived=include` so the agent sees them. Runs deleted together with their mission are gone for good (rows, events, trajectory); only a `MISSION.DELETED` event with the counts remains.

**Not exposed:** `idempotency_keys` (holds response bodies), `audit_log` (empty; F4 dropped), `*objectv1` specs, `mission_trajectory` (read through the timeline endpoint instead).

### 4.2 Roles

```sql
-- created NOLOGIN by the migration; a deploy step enables LOGIN with a password from
-- docker_compose/.env (never committed; see AUDIT_BACKLOG B1).
CREATE ROLE agent_ro NOLOGIN;
GRANT USAGE ON SCHEMA agent TO agent_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA agent TO agent_ro;         -- views only
ALTER ROLE agent_ro SET default_transaction_read_only = on;
ALTER ROLE agent_ro SET statement_timeout = '3s';
ALTER ROLE agent_ro SET idle_in_transaction_session_timeout = '10s';
ALTER ROLE agent_ro SET work_mem = '16MB';
ALTER ROLE agent_ro CONNECTION LIMIT 4;

CREATE ROLE agent_rw NOLOGIN;                                    -- the insight writer
GRANT SELECT, INSERT, UPDATE ON agent_insights, agent_feedback,
      agent_investigations, agent_trigger_log TO agent_rw;
GRANT SELECT ON cause_codes, fleet_events TO agent_rw;          -- the trigger poller reads fleet_events
ALTER ROLE agent_rw SET statement_timeout = '5s';
ALTER ROLE agent_rw CONNECTION LIMIT 3;
```

Neither role can write any Phase 0 table or any `*objectv1` table.

### 4.3 Agent tables

```sql
CREATE TABLE agent_insights (
  insight_id      uuid PRIMARY KEY,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  kind            text NOT NULL,        -- triage | investigation | answer | template
  scope           text NOT NULL,        -- run | robot | site | fleet | version
  robot_name      text, site_id text, run_id uuid, sw_version text,
  severity        text NOT NULL,        -- info | warning | critical  (UI contract unchanged)
  title           text NOT NULL,
  summary         text NOT NULL,
  cause_code      text REFERENCES cause_codes(code),
  confidence      text,                 -- high | medium | low
  claims          jsonb NOT NULL DEFAULT '[]',   -- [{text, citations:[...]}]  (§6)
  data_gaps       jsonb NOT NULL DEFAULT '[]',   -- not_recorded / unknown-version notes
  suggested_action text,                -- advisory text only
  trigger         jsonb,                -- {rule, event_ids[]} | {question} for answers
  dedup_key       text,
  occurrences     int NOT NULL DEFAULT 1,
  last_seen_at    timestamptz NOT NULL DEFAULT now(),
  status          text NOT NULL DEFAULT 'open', -- open | acknowledged | resolved | superseded
  model           text, prompt_version text, degraded boolean NOT NULL DEFAULT false
);
CREATE INDEX ON agent_insights (robot_name, created_at DESC);
CREATE INDEX ON agent_insights (run_id) WHERE run_id IS NOT NULL;
CREATE UNIQUE INDEX agent_insights_open_dedup ON agent_insights (dedup_key) WHERE status = 'open';

CREATE TABLE agent_feedback (
  id bigserial PRIMARY KEY, insight_id uuid NOT NULL REFERENCES agent_insights,
  ts timestamptz NOT NULL DEFAULT now(),
  verdict text NOT NULL,                -- up | down
  corrected_cause text REFERENCES cause_codes(code),
  comment text,
  author text,                          -- self-declared until auth exists (§8.4)
  eval_status text NOT NULL DEFAULT 'new'   -- new | promoted | rejected (golden-set triage)
);

CREATE TABLE agent_investigations (    -- one row per LLM loop; the replay and cost log
  investigation_id uuid PRIMARY KEY, insight_id uuid REFERENCES agent_insights,
  started_at timestamptz NOT NULL, ended_at timestamptz,
  route text NOT NULL,                  -- triage | investigate | deep | ask
  model text NOT NULL, effort text, prompt_version text NOT NULL,
  input_tokens int, cache_read_tokens int, cache_write_tokens int, output_tokens int,
  cost_usd numeric(10,4), tool_calls jsonb,  -- [{name, args, rows, result_sha256, ms}]
  citation_check jsonb, outcome text     -- ok | repaired | invalid | budget | error
);

CREATE TABLE agent_trigger_log (       -- durable "already handled" set for the poller
  event_id uuid PRIMARY KEY, ts timestamptz NOT NULL, code text NOT NULL,
  rule text, handled_at timestamptz NOT NULL DEFAULT now(),
  insight_id uuid, state text NOT NULL   -- pending | done | skipped | rate_limited
);
```

Retention: `agent_investigations.tool_calls` is kept for 90 days (open question Q6). Insights and feedback are kept indefinitely.

---

## 5. Tools

### 5.1 Read tools

Every tool has `strict: true` and a JSON schema with `additionalProperties: false`, and returns compact JSON with a `coverage` block. Results are capped at about 8k tokens; the tool says so when it truncates and returns a cursor.

| Tool | Backed by | Returns |
|---|---|---|
| `list_runs(robot?, site?, state?, sw_version?, from?, to?, limit≤50, cursor?)` | `GET /api/v1/runs` | Runs without `mission_tree` |
| `get_run(run_id)` | `GET /api/v1/runs/{id}` | The run, its events (summarised, §7.3) and a one-paragraph `mission_tree` digest |
| `get_run_timeline(run_id, from?, to?, detail=summary\|full)` | `GET /api/v1/runs/{id}/timeline` | `summary` by default (§7.3); `full` only for a sub-window, capped at 200 events |
| `search_events(robot?, site?, codes[] incl. "NAV.*", severity[]?, run_id?, from, to, limit≤100, cursor?)` | `GET /api/v1/events` | Events, with long streaks collapsed |
| `aggregate_runs(group_by[] ⊂ {cause, site, sw_version, robot, mission, state, day}, filters, from, to)` | `agent.runs`, fixed parameterised SQL | Counts, failure rate, median duration per group, plus the `run_id`s of up to 3 examples per group (so an aggregate is citable) |
| `aggregate_events(group_by[] ⊂ {code, robot, site, sw_version, day, hour}, codes[], filters, from, to)` | `agent.events` | Counts per group, plus the first and last `event_id` per group |
| `get_robot_state(robot)` | `agent.robot_now` + `GET /api/v1/robots/{name}/recording` | Current state, errors, battery, site, effective recording level and its source, staleness |
| `get_recording_coverage(robot, from, to)` | **new** `GET /api/v1/robots/{name}/recording/history` (reuses `fleet_reads._recording`) | Level segments and `not_recorded` intervals for any window, not only a run's |
| `get_telemetry(robot, from, to, signals[])` | `agent.robot_state_1m` / `agent.diagnostics_1m` | Downsampled series and statistics, or an explicit `not_recorded` |
| `find_insights(robot?, run_id?, cause?, since?)` | `agent.insights` | Earlier insights, so the agent can say "same as incident …" instead of starting cold |
| `query_views(sql)` | `agent_ro`, all `agent.*` views | The escape hatch for questions the fixed aggregates can't express. One `SELECT` or `WITH` statement, 200-row cap, 3 s timeout. **Investigate and deep routes only.** The role is the real guard; the parser check is a courtesy. |

The event-code catalogue (§3.3 of Phase 0 plus WP8b's), the cause catalogue, the recording-level semantics and the citation grammar go into the **system prompt**, not a tool. They're static, and they make the cacheable prefix larger than the model minimums (§7.2).

**HTTP guard.** The API wrapper is a single `httpx.AsyncClient` with an allowlist of `GET` path patterns; anything else raises before a request is made. This matters because the API has no auth (AUDIT_BACKLOG B2): the agent must not become a path to mutating routes.

**Replay clamp.** Every tool takes the investigation's `as_of` (now in production; the snapshot time in replay). It clamps `to ≤ as_of`, drops rows after `as_of`, and in replay answers `get_robot_state` from the last rollup and events before `as_of` (`robot_latest` has no history).

### 5.2 Coverage block

```json
"coverage": {
  "window": {"from": "…", "to": "…"},
  "before_history": false,                  // window starts before 2026-09-24
  "recording": [{"from": "…", "to": "…", "level": "events_only"}],
  "not_recorded": [{"from": "…", "to": "…", "missing": ["time_series"]}],
  "sw_version_known_share": 0.0,            // over the rows in the result
  "truncated": false
}
```

Rules in the prompt, checked by the eval:

- No statement that something "did not happen" in an interval where `events` is missing.
- No time-series claim (CPU, temperature, battery curve) where `time_series` is missing; say "not recorded (level `events_only`)".
- No version attribution when `sw_version_known_share < 0.8`; say what share is unknown.

### 5.3 Citations

A claim cites with inline tokens. The UI renders each token as a chip.

| Token | Meaning | Opens in sati-client |
|---|---|---|
| `[run:<run_id>]` | A run | `RunDetailView` |
| `[ev:<event_id>]` | One event | Run event list, scrolled to the event (or the events view) |
| `[tl:<run_id>@<from>/<to>]` | A timeline range of a run | `TimelineChart` zoomed to the range |
| `[rt:<robot>@<from>/<to>]` | A robot's window with no run | Robot runs section, filtered to the window |
| `[agg:<investigation_id>/<n>]` | The n-th aggregate tool result of this investigation | Stored query + result (reproducible) |
| `[ins:<insight_id>]` | An earlier insight | Agent panel |

`event_id` and `run_id` are stable (deterministic uuid5, primary keys), and ranges are ISO-8601 UTC, so a citation stays valid forever.

---

## 6. The investigation loop

The loop uses the SDK tool runner (`client.beta.messages.tool_runner` on `AsyncAnthropic`), replacing the blocking `messages.create` plus `asyncio.to_thread`.

1. **Context.** The user turn holds the question or trigger, `as_of`, the scope (robot, run, site), and a deterministic pre-fetch for run-scoped triggers: the `get_run` summary and the coverage. This saves 1–2 round trips.
2. **Tools.** Up to 12 tool calls (triage: 3), parallel calls allowed.
3. **Finish.** The model ends by calling `submit_finding` (strict schema):
   `{title, summary, scope, cause_code, confidence, claims:[{text, citations[]}], data_gaps[], suggested_action?}`.
   Opus 5.5 rejects forced `tool_choice`, so the call is requested with `auto` plus an instruction. If the model ends without calling it, one nudge turn follows.
4. **Validate** (pure Python, `citations.py`). Every token parses; every ID exists (`agent_ro` lookup); every ID appeared in a tool result of this investigation; `cause_code` is in `cause_codes`; every claim has at least one citation, except claims whose subject is a data gap.
5. **Repair once.** On failure, the validator's messages go back as a tool result and the model gets one repair turn. If it still fails, the insight is stored with `confidence=low` and the invalid claims are dropped and flagged. It is **never** shown as validated.
6. **Store and fan out.** The loop writes `agent_insights` and `agent_investigations` in one transaction, then publishes on the existing WS channels.

**Degraded mode stays.** With no key, over budget, or on an API error, a template insight is built from the trigger and the Phase 0 `abort_cause`, still citing the trigger's `event_id`s.

### 6.1 Models

| Route | Used for | Model | Effort | Why |
|---|---|---|---|---|
| `triage` | Every proactive trigger that isn't template-only: a 1–2 sentence insight, severity, and whether to escalate | **Claude Haiku 4.5** `claude-haiku-4-5-20251001` | n/a (no thinking) | Highest volume, needs < 10 s, and 3 tool calls at most. What the service already runs. |
| `investigate` | Non-`COMPLETED` runs, escalated triage, and chat by default | **Claude Sonnet 5** `claude-sonnet-5` | `medium` | Multi-step tool use over a few runs at half the Opus price and lower latency |
| `deep` | Fleet, site or version questions over many runs; chat with "deep" ticked; retry when `investigate` fails validation twice or returns `UNKNOWN` with low confidence | **Claude Opus 5.5** `claude-opus-5-5` | `high` | Best reasoning over aggregates and ambiguous evidence; low volume keeps cost bounded. Thinking can't be disabled on Opus 5.5; effort is the control, and it defaults to `medium`, so set it explicitly. |
| eval judge | Claim-support grading in WP6 | **Claude Opus 5.5** | `high` | Offline only; the judge should be at least as strong as the model it grades |

WP6 runs a bake-off on the golden set: Sonnet 5 `medium` vs Opus 5.5 `medium` vs Opus 5.5 `high` for `investigate`. If Opus 5.5 at `medium` beats Sonnet 5 by 5 points or more on cause accuracy at an acceptable cost, it becomes the `investigate` default. The route-to-model mapping is env config (`AGENT_MODEL_TRIAGE`, `AGENT_MODEL_INVESTIGATE`, `AGENT_MODEL_DEEP`); `AGENT_MODEL` stays as an alias for triage. The LiteLLM dev override keeps working for triage only; tool-loop routes need real Claude.

---

## 7. Triggers, cost and context

### 7.1 Triggers

**On demand (chat).** `WS /ws/agent/ask`: the client sends `{question, robot?, run_id?, site?, deep?}`. The server streams progress (`{"type":"tool","name":"get_run_timeline"}`) and then the finding. Follow-ups reuse the session's `conversation` (the existing seam in `session.py`), capped at 10 turns. Answers are stored as `kind=answer` insights, so they can be cited and rated.

**Proactive.** A poller in the agent service, rather than MQTT:

- Every 10 s it runs `SELECT … FROM fleet_events WHERE code = ANY(:trigger_codes) AND ts > now() - interval '30 min'` as `agent_rw` (the `(code, ts DESC)` index covers it). Each event is claimed with `INSERT INTO agent_trigger_log … ON CONFLICT DO NOTHING`, so a restart never re-fires and never misses a claimed-but-pending event.
- The 30 min lookback tolerates robot clock skew, which is still unbounded while chrony (WP3) is deferred.
- The rules are a pure, table-driven module, `event_triggers.py`, next to `triggers.py` and tested the same way.

| Rule | Condition | Action | `dedup_key` |
|---|---|---|---|
| R1 run failed | `MISSION.RUN_FINISHED`, outcome ∈ {FAILED, ABORTED, TIMEOUT} | `investigate` | `run:<run_id>` |
| R2 run canceled | outcome `CANCELED` | template insight (the operator knows why) | `run:<run_id>` |
| R3 repeated nav blocks | ≥ 3 of `NAV.GOAL_BLOCKED` / `NAV.RECOVERY_ENTERED` / `MISSION.EDGE_BLOCKED` for one robot in 10 min | `triage` | `nav:<robot>:<edge or cause>` |
| R3b blocked spot | the same edge blocked for ≥ 2 robots in 24 h | `triage`, scope `site` | `edge:<site>:<edge>` |
| R4 thermal | `SYSTEM.THERMAL_HIGH` | `triage`; `investigate` if ≥ 2 in 1 h or during a run | `thermal:<robot>` |
| R5 errors | `ROBOT.ERROR_RAISED` with severity ≥ error | `triage` | `err:<robot>:<error_type>` |
| R6 comms | `ROBOT.HEARTBEAT_LOST` or `ROBOT.OFFLINE` with `run_id` set | `triage` (R1 covers the run's outcome) | `comms:<robot>` |
| R7 software | `SYSTEM.NODE_DOWN` | `triage` | `node:<robot>:<node>` |
| R8 simple | `BATTERY.LOW`, `MAP.DELETE_FAILED` | template, no LLM | `<code>:<robot or map>` |
| R9 storm | the same rule fires on ≥ 3 robots within 5 min | one `fleet` insight (likely broker, cloud or network); per-robot LLM calls are suppressed | `storm:<rule>:<5-min bucket>` |
| R10 safety *(needs WP8b)* | `ROBOT.ESTOP_ENGAGED`, `ROBOT.FIELD_VIOLATION`, `ROBOT.OPERATING_MODE_CHANGED`, `MISSION.ACTION_FAILED` | `triage` (severities as in `triggers.py`) | `safety:<robot>:<code>` |

**Dedup.** While an insight with the same `dedup_key` is `open` and was seen within 30 min, a new firing bumps `occurrences` and `last_seen_at` and adds the event to `trigger.event_ids`. It makes **no** LLM call and fans out as an update.

**Rate limits and budget** (env, with defaults):

| Limit | Default | When exceeded |
|---|---|---|
| Investigations per robot | 1 per 10 min, 6 per hour | Triage only; `trigger_log.state = rate_limited` |
| Investigations, fleet | 30 per hour | Queue; R1 is never dropped, only delayed |
| Chat | 3 concurrent, 60 per hour | 429 with `Retry-After` |
| Daily spend `AGENT_DAILY_BUDGET_USD` | $20 | Template-only insights plus a "budget reached" banner. Resets at 00:00 UTC. |

The queue is bounded and in memory, and `trigger_log` rows with `state=pending` are re-queued at startup. Nothing here can touch the command path: the agent has no MQTT client and reads Postgres through two tiny connection pools.

**MQTT removal (WP8).** The e-stop, field-violation, operating-mode and failed-action detectors only exist on the MQTT path. If WP8b lands, R10 replaces them and the MQTT subscription is removed. If not, the subscription stays for those 4 conditions only, and their insights cite `rt:` ranges instead of events. `new_error`, `edge_blocked` and `battery_low` move to R5, R3 and R8 either way.

### 7.2 Cost and latency budget

Prices per million tokens (input / output; a cache read is 0.1× input): Haiku 4.5 $1 / $5, Sonnet 5 $2 / $10, Opus 5.5 $4 / $20.

| Route | Tokens per call (estimate) | Cost | Latency target (p95) |
|---|---|---|---|
| triage | ~6k in (≥ 4.5k cached), ~300 out, ≤ 3 tools | ≈ $0.004 | < 10 s trigger → insight |
| investigate | ~150k cumulative in over ~10 turns (≈ 85 % cache reads), ~4k out | ≈ $0.10–0.15 | < 90 s from `RUN_FINISHED` |
| deep | ~250k in, ~8k out | ≈ $0.30–0.45 | < 3 min; progress streamed |
| ask | as investigate | ≈ $0.12 | first progress < 2 s, answer < 60 s |

At an assumed 20 failed runs, 200 triage calls, 20 chats and 3 deep questions per day, spend is about **$6/day**; the default cap is $20. Cost per call is logged in `agent_investigations` and totalled in `/stats`, and WP11 checks the estimate against real usage.

**Prompt caching.**

- The prefix is ordered tools → system: sorted, deterministic tool definitions, then the frozen system prompt (role, rules, both catalogues, citation grammar, coverage rules), with an explicit breakpoint at its end. Automatic caching covers the growing loop tail.
- Nothing volatile goes in the prefix: `now`, `as_of` and the question go in the first user turn.
- The prefix is about 5–6k tokens, above Haiku 4.5's 4096-token minimum (today's 700-token prompt never caches) and Sonnet 5's 1024.
- Caches are per model, so each route has its own; the 5-min TTL is enough because loops are bursty.
- `prompt_version` is a hash of the prefix, stored on every investigation. WP6 checks that `cache_read_input_tokens > 0` from turn 2 on.

### 7.3 Context limits: summarise before the model sees it

A run timeline can be 5000 events plus 2 × 2000 track points. That is far too much to paste, and most of it is `ROBOT.STATE_CHANGED` noise. `summarize.py` (pure, deterministic, unit-tested) produces the `detail=summary` form:

- **Lifecycle and every event with severity ≥ warning, verbatim** with their `event_id`s. Other events are collapsed into streaks (`ROBOT.STATE_CHANGED ×37, first [ev:…], last [ev:…]`) and counts per code.
- **Tracks:** about 20 equal segments with min / avg / max per signal (battery, temp_max, cpu, nodes_down), plus the last value before each warning event.
- **Trajectory:** distance, bounding box, last node, and the time and place of the stop.
- **`not_recorded` and the recording segments, verbatim.**

The model drills down with `get_run_timeline(from, to, detail=full)` or `search_events`. Hard limits: 12 tool calls, about 120k tokens of context per loop, and 8k tokens per tool result. Chat sessions past 10 turns start fresh, with the previous answers passed in as a list of `[ins:…]` citations.

---

## 8. Safety

### 8.1 Read-only in Phase 1

**Action tools are out of scope for Phase 1. Not "behind confirmation": absent.** This means no cancel, pause, reroute, instant action, mission creation or settings change. `suggested_action` is text only, and the UI shows it as advice next to the existing buttons (e.g. RERUN in the failure cards), which the operator presses. This is enforced four ways:

1. There are no mutating tools.
2. The HTTP client is GET-only against an allowlist.
3. `agent_ro` sees only views; `agent_rw` writes only the four `agent_*` tables.
4. The agent service has no MQTT client once WP8 is done.

The Phase 2 plan (§12) adds confirmed actions.

### 8.2 Untrusted text

`errorDescription`, `navReasoning`, mission names, site names and feedback comments are robot- or operator-supplied strings, and they reach the model inside tool results. The system prompt says tool content is data, never instructions. Because the agent has no action tools, the worst an injection can do is a misleading answer, and the citation validator limits even that: an answer can only cite what the tools actually returned.

### 8.3 Data sent to Anthropic

Robot names, mission names, site display names, events, errors and coarse telemetry are sent. Customer names, geofences, GPS datums, RTK bases, map images, video and credentials are not (§4.1). Q1 asks the owner to confirm this line.

### 8.4 No authentication: where it plugs in later

| Point | Now | With auth |
|---|---|---|
| `WS /ws/agent/ask` | Open; global rate limit and daily budget cap the damage | FastAPI dependency; per-user rate limit and budget |
| `POST /api/agent/insights/{id}/feedback` | `author` is self-declared | `author` from the token; feedback weighted by role |
| `PATCH /api/agent/insights/{id}` (status) | Open | Operator scope |
| Agent → API calls | No credentials | A service token with a read-only scope |

Until then, keep 8007 reachable only through the client's nginx on the tailnet, as today (AUDIT_BACKLOG B2).

---

## 9. Evaluation

### 9.1 Golden set

`eval/golden/cases/*.yaml`, one file per case, plus `eval/golden/incidents.yaml`, which Phase 1 **creates**: the Phase 0 WP4 recorder and `incidents.yaml` were never started, and DB snapshots make the recorder unnecessary for Phase 1.

```yaml
id: run-fail-2026-09-27-peacock-01
source: real            # real | staging-synthetic | feedback
as_of: 2026-09-27T15:10:00Z
snapshot: snap-2026-09-30
question: "Why did run 4f1c… fail?"
scope: {run_id: 4f1c…}
expect:
  cause_in: [NAV.PATH_BLOCKED]            # acceptable causes; UNKNOWN only if listed
  must_cite: {runs: [4f1c…], events_any_of: [9b2e…, 77aa…]}
  must_mention_gaps: [time_series]        # e.g. robot at events_only
  must_not_claim: ["battery", "no events after 14:05"]
labeled_by: <name>
notes: …
```

Target: **≥ 40 cases** by the end of week 3, in this mix:

| Share | Kind | Source |
|---|---|---|
| ≥ 15 | Real non-`COMPLETED` runs since 2026-09-24 | Labelled by the team in WP4 |
| ≥ 10 | Synthetic faults with known ground truth | Staging with the dummy robot in `goal` mode plus injected heartbeat loss, `edgeBlocked`, fake thermal diagnostics and FATAL errors |
| ≥ 6 | **Gap cases** | The answer must be "not recorded": robot at `events_only` or `off`, or a window before 2026-09-24 |
| ≥ 4 | Aggregate cases | Site-level; "since version V" with a mostly-null `sw_version`, where the correct answer states the unknown share |
| ≥ 4 | Negative / healthy cases | "Is anything wrong with robot Y?" when nothing is |
| ongoing | Feedback-derived | A 👎 with a correction becomes a candidate (`agent_feedback.eval_status`) and is promoted after review |

Cases are split 70/30 into dev and holdout. Prompt tuning only looks at dev, and the exit test reports holdout.

### 9.2 Offline replay

`tools/agent_eval.py` (a CLI, not a service):

1. **Snapshot.** `pg_dump --data-only` of the Phase 0 tables and the objects the recording policy needs (`robotobjectv1`, `siteobjectv1`, `settingsobjectv1`). It's taken from production (read-only) and stored off-repo with a name like `snap-YYYY-MM-DD`.
2. **Restore** into a throwaway TimescaleDB container plus an API container built from the branch (`--memory=2g`, `timeout -s KILL`), with migrations applied.
3. **Run** each case through the real agent code with `as_of` set and the tools pointed at the replay API and DB. Cases run sequentially, capped at $15 per full run.
4. **Score** (§9.3) and write `eval/reports/<date>-<prompt_version>.md` plus a JSON line per case.

It runs on demand and before every prompt or model change is deployed. Snapshots are deterministic, so two runs of the same `prompt_version` are comparable, up to model sampling; the report shows the mean of 2 runs on the holdout.

### 9.3 Metrics

| Metric | How | Kind |
|---|---|---|
| **Citation validity** | Share of citation tokens that parse, resolve and were returned by a tool in that investigation | deterministic |
| **Claim support** | Share of claims whose citations actually support the text | Opus 5.5 judge with a rubric; 10 % spot-checked by a human |
| **Cause accuracy** | `cause_code ∈ expect.cause_in` | deterministic |
| **Gap honesty** | On gap cases: every `must_mention_gaps` is stated and no `must_not_claim` appears | deterministic plus judge |
| **UNKNOWN share** | Share of non-`COMPLETED` runs the agent labels `UNKNOWN`, compared with the Phase 0 rule baseline (`abort_cause = 'UNKNOWN'` share, recorded by Phase 0 WP13) | deterministic, on all real runs, not only golden |
| **Hallucinated IDs** | Count of cited IDs that don't exist | deterministic |
| Cost, latency, tool calls per case | From `agent_investigations` | deterministic |

A wrong specific cause is scored worse than a correct `UNKNOWN` with the gap stated. The agent must not buy a lower UNKNOWN share with guesses.

### 9.4 Thresholds (holdout)

| Gate | Threshold |
|---|---|
| Hallucinated IDs | **0** |
| Citation validity | ≥ 98 % |
| Claim support | ≥ 90 % |
| Cause accuracy | ≥ 80 % (real cases ≥ 70 %) |
| Gap honesty | **100 %** of gap cases |
| UNKNOWN share | ≤ 50 % of the Phase 0 baseline, with cause accuracy still at threshold |
| Cost per investigation | ≤ $0.25 median |

If Phase 0 WP13 hasn't recorded the baseline by WP6, WP6 computes it from `mission_runs` over the same window.

---

## 10. Component changes

### 10.1 `agent-orchestrator-service`

| File | Change |
|---|---|
| `triggers.py` | Kept for the MQTT-only conditions until WP8 (or deleted if WP8b lands); its tests stay green |
| `event_triggers.py` **new** | Pure rules R1–R10, dedup keys, storm detection; table-driven tests like `test_agent_triggers.py` |
| `poller.py` **new** | 10 s poll, `trigger_log` claim, bounded queue, rate limits, budget |
| `tools.py` **new** | §5 tools, HTTP GET allowlist, `as_of` clamp, `coverage` block |
| `summarize.py` **new** | §7.3 timeline and event summaries |
| `citations.py` **new** | Grammar, parser, validator |
| `agent.py` | `FleetAgent` becomes route-aware (`triage` / `investigate` / `deep`): `AsyncAnthropic` + tool runner, `submit_finding`, repair turn, usage and cost accounting; degraded mode kept |
| `store.py` **new** | `agent_rw` pool (2 conns): insights, investigations, feedback, trigger log |
| `session.py` | `insights` deque → read-through cache over `agent_insights`; `conversation` used by `/ask` |
| `server.py` / `main.py` | Routes (§10.2); MQTT removal (WP8); `/stats` gains spend, budget and per-route counts |
| `Dockerfile` / `requirements.txt` | Copy `packages/events` (the catalogues); add `psycopg[binary,pool]` and `httpx`; **pin** `anthropic` to a version that supports Sonnet 5, Opus 5.5 and the tool runner, and check its compatibility with the service's `pydantic==1.9.0` pin (§11) |

### 10.2 Agent routes

The existing shapes stay; new fields are additive, so the current `AgentInsight` type keeps parsing.

```
GET   /insights?robot=&run_id=&site=&status=&since=&limit=     from agent_insights
GET   /insights/{robot}                                       unchanged path
GET   /insights/by-id/{insight_id}                            with claims, gaps, investigation summary
PATCH /insights/{insight_id}            {status}              acknowledge / resolve
POST  /insights/{insight_id}/feedback   {verdict, corrected_cause?, comment?, author?}
WS    /ws/insights[/{robot}]                                   unchanged; now also sends updates (occurrences)
WS    /ws/ask                                                  chat (§7.1)
GET   /stats                                                   + spend_today_usd, budget_usd, per-route counts
```

nginx already maps `/api/agent/*` and `/ws/agent/*`, so the client needs no nginx change.

### 10.3 `api`

- The migration `…_01_phase1_agent` (§4).
- `GET /api/v1/robots/{name}/recording/history?from&to`: `_recording()` from `fleet_reads.py` over an arbitrary window (with the same 5 s `READ ONLY` transaction).
- Nothing else. The agent reads existing routes.

### 10.4 `mission-dispatch` (WP8b, optional but recommended)

New append-only codes, emitted from the `state` handler dispatch already runs, with `StateDiff` detectors: `ROBOT.ESTOP_ENGAGED` / `ESTOP_RELEASED`, `ROBOT.FIELD_VIOLATION`, `ROBOT.OPERATING_MODE_CHANGED`, `MISSION.ACTION_FAILED`. This makes every proactive insight citable and lets the agent drop MQTT entirely. Caveat: AUDIT_BACKLOG C15 (an unknown `operatingMode` drops the whole state message in dispatch) also hides mode changes to unknown values, such as `TELEOPERATION`. Fix C15 first or together.

### 10.5 sati-client (read-only here; the changes are the frontend WP)

- **`AgentModal`** becomes the agent panel with two tabs:
  - **Insights:** persisted, filterable by status. Each card shows severity, title, cause chip, confidence, a data-gap banner ("time series not recorded 14:02–14:30, level `events_only`"), claims with citation chips (§5.3), `occurrences`, acknowledge / resolve, and 👍 / 👎 with a cause picker and a comment.
  - **Ask:** scoped to the robot, prefilled with the selected run; streams progress; answers carry the same chips.
- **`useAgentFailureSuggestion`** matches **by `run_id`** (`GET /insights?run_id=`; mission objects already carry `run_id`) and keeps the time-window match only as a fallback for missions without one.
- Citation chips reuse the existing `RunDetailView`, `RunEventList` and `TimelineChart` (range zoom). No new chart library, no Grafana.

---

## 11. Step-by-step plan

Deploys follow the Phase 0 pattern: an explicit go-ahead, a rollback image `agent_orchestrator_service:pre-phase1` (plus `api:pre-phase1` for WP1–2), and kill switches `AGENT_PROACTIVE=false` (poller off) and `AGENT_LLM_ENABLED=false` (template-only).

### Week 1 — Data access and tools (backend: 1 engineer; team: labelling)

**WP1: Migration (days 1–2).**
- [ ] `agent` schema and views (§4.1), roles (§4.2) and agent tables (§4.3), in one revision.
- [ ] A deploy step enables `LOGIN` for both roles with passwords from `docker_compose/.env`.
- [ ] Test on the staging copy: `agent_ro` can't read base tables, can't write anything, and is cancelled at 3 s.
- [ ] Views return the expected columns; the migration downgrades cleanly.

**WP2: API recording history (day 2).**
- [ ] `GET /api/v1/robots/{name}/recording/history` plus tests; reuses the `level_segments` / `not_recorded` code.

**WP3: Tool layer (days 2–5).**
- [ ] `tools.py`, `summarize.py` and `citations.py` with unit tests against fake HTTP and DB.
- [ ] Golden tests for the timeline summary (5000-event fixture → < 8k tokens, all warning events kept).
- [ ] Allowlist test: a non-GET or unlisted path raises before sending.
- [ ] `as_of` clamp test.

**WP4: Golden set bootstrap (days 1–5, in parallel; owner: 1 engineer half-time plus 2 h of team time).**
- [ ] Create `eval/golden/incidents.yaml` and the case schema. Label every non-`COMPLETED` run since 2026-09-24 (cause, key events, gaps).
- [ ] Script the synthetic fault scenarios on staging (dummy robot `--mode goal` plus fault injection), each with its ground truth.
- [ ] Write the gap and negative cases.

**Week 1 acceptance:** the roles and views exist on staging, the tools return summarised, covered, citable results, and there are ≥ 20 golden cases.

### Week 2 — Investigator and eval harness

**WP5: Investigation loop (days 1–4).**
- [ ] Route-aware `FleetAgent`: tool runner, `submit_finding`, validation plus one repair turn, limits (12 tools, 120k tokens), usage and cost accounting, `agent_investigations` rows, degraded fallback.
- [ ] Prompt with catalogues and coverage rules; `prompt_version` hash.
- [ ] Verify `cache_read_input_tokens > 0` from turn 2 on every route.

**WP6: Eval harness and model bake-off (days 2–5).**
- [ ] `tools/agent_eval.py`: snapshot, restore, replay, score, report.
- [ ] Take `snap-<date>` (read-only `pg_dump`).
- [ ] Record the UNKNOWN baseline if Phase 0 WP13 hasn't.
- [ ] Bake-off (§6.1) → set the route defaults.
- [ ] Iterate the prompt on dev only.

**Week 2 acceptance:** an offline run over all cases produces a report; dev scores are within 10 points of every §9.4 threshold, and the gap cases are at 100 %.

### Week 3 — Triggers, outputs, UI (backend: 1 engineer; frontend: 0.5 engineer)

**WP7: Event triggers (days 1–3).**
- [ ] `event_triggers.py` (R1–R9) with table-driven tests: each rule, the dedup window, storm collapse, the rate limits and the budget cutoff.
- [ ] `poller.py` with the `trigger_log` claim. Test that a restart mid-queue neither re-fires nor loses events.

**WP8: Agent routes, persistence, MQTT retirement (days 2–4).**
- [ ] The §10.2 routes, backward-compatible with `AgentInsight`.
- [ ] The session cache becomes read-through over the DB.
- [ ] Remove the MQTT subscription, or keep it only for the 4 conditions if WP8b slips.

**WP8b: Dispatch safety codes (days 3–4, optional).** The §10.4 codes plus detector tests; fix C15 alongside.

**WP9: sati-client panel (days 1–5, frontend).** §10.5: insights tab, chips, gap banner, feedback, Ask tab, failure cards by `run_id`. MSW mocks and component tests, following the existing `__tests__/components` pattern.

**Week 3 acceptance:** on staging, a synthetic failed run produces a validated insight with chips in the UI within 2 min; feedback round-trips into `agent_feedback`; the golden set has ≥ 40 cases.

### Week 4 — Shadow, harden, exit

**WP10: Deploy in shadow (day 1).** Production deploy with `AGENT_PROACTIVE=true` but the insights tab behind a UI toggle for the team only. Watch spend, duplicates, latency and trigger-log backlog.

**WP11: Harden (days 2–4).**
- [ ] Tune the rate limits and dedup windows from shadow data; compare real cost with §7.2.
- [ ] Promote reviewed 👎 corrections into the golden set; re-run the holdout.
- [ ] Alerts in `/stats` (the Phase 0 WP13 metrics path): budget > 80 %, trigger-log pending > 20 for 5 min, validation-failure rate > 10 %.

**WP12: Exit test (day 5).** The team judges 10 real incidents (the exit test above), the holdout passes §9.4, and the one-week proactive criterion is met. Then the UI toggle is removed.

---

## 12. Definition of done

- [ ] No new long-running container; `agent-orchestrator-service` is the only agent runtime.
- [ ] The `agent` schema, `agent_ro` (SELECT on views only, read-only, 3 s timeout) and `agent_rw` (agent tables only) are live via Alembic.
- [ ] All §5 tools exist, each result carries `coverage`, and the HTTP client is GET-allowlisted.
- [ ] Every stored insight has validated citations, or is explicitly `confidence=low` with invalid claims removed. There are zero hallucinated IDs in the holdout.
- [ ] Proactive rules R1–R9 (R10 if WP8b) run from `fleet_events`, with durable dedup, rate limits and a daily budget cap.
- [ ] Insights are persisted, survive restarts, are linked to runs, events and robots, and are shown in the sati-client panel with citation chips and feedback.
- [ ] `/ask` chat works for run, robot, site and version questions.
- [ ] The golden set has ≥ 40 cases; the offline replay and report are reproducible from a snapshot.
- [ ] The §9.4 thresholds pass on the holdout; the UNKNOWN share and its Phase 0 baseline are both recorded.
- [ ] No action tools exist; the agent has no MQTT publish path.
- [ ] The exit test passes.

## 13. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| **Too few real failures** since 2026-09-24 to label ≥ 15 cases | medium | Synthetic staging faults fill the gap; the real-case count is reported separately; the exit test may run late in week 4 |
| **Sparse data** (`events_only`) makes many causes undecidable | high | That is a valid answer: gap honesty is a hard gate and the UNKNOWN target is relative. WP11 lists which robots would benefit from `full` |
| Robot clock skew (chrony deferred) misorders events or hides them from the poller | medium | 30 min poller lookback; the agent is told timestamps are robot time; skew appears as a data gap when `HEARTBEAT_*` disagrees |
| Mostly-null `sw_version` makes version questions unanswerable | high | Explicit `sw_version_known_share`; version cases expect "can't attribute"; unblocked by robot WP3 |
| The `anthropic` SDK needed for the new models requires Pydantic 2 while the service pins 1.9.0 | medium | Check in WP5 day 1. The agent service doesn't import `cloud_common` models, so it can move to Pydantic 2 on its own if needed; `packages.events` already runs under Pydantic 2 in the unit tests (AUDIT_BACKLOG C1) |
| Heavy agent reads hurt the production DB | low | 3 s timeout, 4-connection cap, views over indexed columns, rollups instead of raw series, `query_views` capped at 200 rows |
| Spend spikes (alarm storm, open `/ask`) | medium | R9 storm collapse, per-robot and fleet limits, a hard daily cap that falls back to templates |
| Judge bias (Opus judging Opus) | medium | Deterministic metrics carry the hard gates; the judge only scores claim support; 10 % human spot-check |
| Prompt overfits the dev set | medium | 70/30 split; holdout only at the exit; feedback cases keep arriving |

## 14. What this unlocks for Phase 2

- **Gated actions:** the Phase 2 list in the service README (`notify_operator`, `flag_for_teleop`, `request_charging_mission`, reroute avoiding a blocked edge, cancel). Each is a proposal the operator confirms in the panel, and it's logged against the insight that motivated it. Auth (per-operator identity) should land first.
- **`mission_runs.summary_metrics`**, filled from the same summariser, enabling trend questions without a tool loop.
- **The fleet coordinator** (the `_notify_coordinator` seam): cross-robot reasoning over insights, e.g. charging contention and repeatedly blocked spots per site.
- **Scheduled digests** (daily per site, per new `sw_version` once robot WP3 lands): the same tools on a timer.
- **Continuous eval:** feedback-driven golden growth, and a nightly holdout run on a fresh snapshot.

---

## 15. Open questions for the owner

1. **Data sent to Anthropic.** Is it OK to send robot, mission and site display names, errors and coarse telemetry? The plan excludes customer names, geofences, GPS datum and RTK base (§8.3). Is that the right line, and is zero data retention required?
2. **Budget.** Is a $20/day hard cap (≈ $6/day expected) right, and who should see the "budget reached" alert?
3. **Labelling.** Who labels the golden cases and judges the exit test? About 2 h/week in weeks 1–4.
4. **WP8b.** Should dispatch emit e-stop, field-violation, operating-mode and action-failed events, so the agent can drop MQTT entirely? It's a small dispatch change and it touches C15.
5. **Canceled runs.** Should they get template insights only (the plan), or a full investigation?
6. **Retention** of investigation tool logs: is 90 days right?
7. **Answer language.** English only, or Hungarian too, for operators?
8. **Model default.** If Opus 5.5 at `medium` clearly beats Sonnet 5 in the bake-off, should it become the `investigate` default at about 2× the cost?
