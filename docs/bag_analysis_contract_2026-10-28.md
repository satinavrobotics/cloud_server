# Bag analysis contract: proposal (2026-10-28)

Status: **proposal for the robot team**, nothing is implemented. Context and the full pipeline design: `web_stack/BAG_ANALYSIS_PIPELINE_PLAN_20261028.md`.
Files next to this document (`docs/bag_analysis/`): `result.schema.json`, `analyzer.schema.json`, `lost_time.analyzer.yaml` (example manifest),
`lost_time.result.example.json` (example result built from the stored baseline `live_20261004_200002`). Both examples validate against the schemas.

## 1. What this contract is for

The server will analyse uploaded robot bags automatically. The analyzers (lost time first, others later) stay in the navstack repo, because
they parse that stack's log lines and move with its logic. The server must run them without knowing what any of them mean. This document
is the only thing the two sides share: **a manifest going in, a result envelope coming out, and a runner that connects them.**

Goals: add an analyzer without a server release; re-run old bags with a newer analyzer and keep both results; reproduce an old result
with its old flags; never confuse "the analysis failed" with "the robot did badly".

Non-goals (v1): thresholds and pass/fail judgements on the robot's performance (a later benchmark layer reads the metrics this contract
produces); analyzers that depend on each other; streaming or live analysis; analysing on the Pi.

## 2. Three layers

```
extract   bag -> x/ (jsonl)              one shared step, versioned (extractor_version). Only step that needs ROS.
analyze   x/ (+ bag, params, ...) -> result.json   one per analyzer + profile. Lost time is one.
benchmark named metric sets + thresholds + baselines over results     LATER; this contract only makes sure results can feed it.
```

## 3. The runner (owned by the navstack repo)

One entry point, used by the server worker and by the robot's `post_run_readout.sh` alike:

```
run_analysis.py --bag DIR --out DIR --bag-id ID
                [--params DIR] [--episodes FILE] [--vda-log FILE]
                [--only id[:profile],...] [--list] [--stack-build TAG]
```

- `--list` prints the manifests found (json) and exits; the server learns what an image offers from this, nothing is configured server-side.
- It extracts into `OUT/x/` (the server always re-extracts; a skip when `OUT/x/meta.json` carries the current `extractor_version` is optional and
  only possible once the extractor writes that version, which it does not yet), then runs each applicable
  analyzer profile into `OUT/<id>/<profile>/`, and writes `OUT/run.json` (extractor version, per analyzer: status, duration, exit code) and `OUT/run.log`.
- It reads the bag only (mount it read-only), writes only under `--out`, and passes every path to the analyzers itself. No analyzer may fall
  back to a robot path such as `/mnt/ssd/...` (the lost-time tool's `--episodes` default does today: `$EPISODE_FILE` or `/mnt/ssd/recordings/EPISODES.log`; `--clearance-dir` is a `score_confirmation_run.py` default, not the ledger's); a missing
  *required* input is an error, a missing *optional* one is a note.
- Exit codes: 0 = every analyzer ended ok / warn / n/a; 1 = at least one analyzer ended `error`; 2 = bad arguments or missing required input;
  3 = extraction failed. One analyzer's failure never stops the others.
- argv is always a list, never a shell string. Per-analyzer timeout and memory limit come from the manifest; the server applies container limits on top.

## 4. The manifest (`analyzer.yaml`, schema `analyzer.schema.json`)

One per analyzer, next to its code. Fields:

| Field | Meaning |
|---|---|
| `contract` | `1` |
| `id`, `version`, `title`, `owner` | identity. `version` is an integer, bumped when any metric's meaning or value for the same input changes |
| `requires.extractor` | minimum extractor version |
| `applies_to` | `description_regex`, `bag_id_regex`, `topics_all`. A non-matching bag ends `n/a`, not `error` (the server holds calibration and other-robot bags too) |
| `inputs` | `x` always; `bag`, `params`, `episodes`, `vda_log` as `required / optional / none` |
| `profiles` | named run configurations; `default` is required. Each: `argv` (placeholders `{tools} {bag} {x} {out} {bag_id}`), `optional_argv` (fragment added only when that input exists), `timeout_s`, `memory_mb` |
| `adapter` | python module (stdlib only) with `build_result(ctx) -> dict`; maps the tool's own output to the envelope fields below |
| `metrics` | every metric the result may carry, declared once: `unit`, `direction` (lower / higher / neutral is better), `description`, optional `headline` |

Why profiles: the lost-time tool has many behaviour flags and flipped defaults (decisions 214, 330, 334). A named profile such as
`legacy_rules` would let us run one bag under an older reading next to the current default; the profile is part of the result key. **The robot team
asked not to ship a legacy profile for lost time in v1** (see section 8); the mechanism stays in the contract for later analyzers, and `lost_time` ships `default` only.
Phase 0 found that a stored baseline cannot be reproduced exactly this way: on `live_20261004_200002` goal time, ideal and lost time match,
but `unjustified_s` is 218.8 with current defaults and 252.1 with all legacy flags, against 234.8 in the baseline, because that run's rule
combination no longer exists behind any flags. So profiles give comparable side-by-side readings, not a time machine; every result carries the tool git sha.

Why metrics are declared in the manifest: the server and client can show names, units and "which way is better" for any analyzer without
code, and the benchmark layer can compare runs without parsing tool-specific JSON.

Existing tools are **not rewritten**. The adapter reads the JSON the tool already writes and fills the envelope.

## 5. The result envelope (`result.json`, schema `result.schema.json`)

| Field | Meaning |
|---|---|
| `schema` | `1` |
| `analyzer`, `analyzer_version`, `profile`, `extractor_version`, `bag_id` | the result key (the server stores one row per combination, never overwrites) |
| `status` | `ok` computed and trustworthy; `warn` computed with caveats in `notes`; `n/a` does not apply; `error` the analyzer failed or its output was invalid |
| `notes[]` | `{code, level, text}`; machine-readable `code` (e.g. `input.params_missing`, `o2.fallback`), short text |
| `producer` | `tool_git_sha`, `analysis_image`, `stack_build` (null when unknown), `command`, `started_at`, `duration_s` |
| `inputs` | `bag_dir`, and which optional inputs were present |
| `metrics` | flat `name -> number or null`; names declared in the manifest; null = not computable for this bag |
| `tables` | small display tables `{columns, rows}` (caps: 20 tables, 200 rows, 30 columns) |
| `artifacts[]` | `{name, path, media_type, size, sha256}`; path is relative to the analyzer's output dir (no absolute path, no `..`) |

**Status describes the analysis, not the robot.** A bad run with trustworthy numbers is `ok`. This replaces the `fail` status in my earlier
sketch: a pass/fail verdict needs a threshold, thresholds change with the goal, and they belong to the benchmark layer. Large things go
in artifacts, not in `metrics` or `tables` (the envelope should stay well under 256 KB).

Compatibility: consumers ignore unknown fields. Adding fields or metrics is non-breaking; removing or changing the meaning of a metric
bumps `analyzer_version`; changing the envelope itself bumps `schema`.

## 6. Worked example: lost time

Manifest: `lost_time.analyzer.yaml`. Mapping of the ledger json (`totals`, `by_state`, `owner_unjustified_s`, `per_goal`) to the envelope,
checked against the baseline file `behaviour/tools/baseline/live_20261004_200002_ledger.json`:

| Envelope | From the ledger json |
|---|---|
| `metrics.goal_time_s, ideal_s, lost_s, unjustified_s, detour_s, unassigned_s, tick_identity_max_residual_s` | `totals.<same name>` |
| `metrics.unjustified_share` | `totals.unjustified_share_of_goal_time` |
| `metrics.goals_n`, `goals_reached_n` | `len(per_goal)`, count of `per_goal[].reached` |
| `tables.by_state` | `by_state` (justified / unjustified / unjudged / total per state) |
| `tables.unjustified_by_owner` | `owner_unjustified_s` (A / B / C, X when present) |
| `artifacts` | the tool's `.md` and `.json` |
| `status` | `n/a` when there are no goals; `warn` when `tick_identity_max_residual_s` > 1e-6, or the tool printed a fallback note (`o2_fallbacks`, a missing params dump), or `abs(unassigned_share_signed)` > 0.05 (that last threshold is my suggestion, the owners should set it); else `ok` |

The identity check is why `warn` exists: the ledger's exact tick identity holds to ~1e-12 s on the bags tried in phase 0 (2.2e-12 and 2.5e-12), so a breach means a real defect.
Read it from `totals.tick_identity_max_residual_s` only. `ad10_split` (non-legacy mode only) and `o2_fallbacks` (only when a flipped O2 default fell back on that
bag) are OPTIONAL in the tool's output and the 2026-10-04 baseline has neither: the adapter must treat both as absent-able, and there is no top-level `identity` key.
A missing params dump is a `warn` with code `input.params_missing`, **set by the adapter when the runner did not pass `--params`** (`inputs.present.params` is false);
the tool has no structured flag for it and parsing its stdout WARNING is a brittle fallback. The ledger then silently uses default nominal speeds, footprint and maneuver
speeds and prints only a WARNING, which makes it the most result-affecting missing input.
The example result (`lost_time.result.example.json`) is the stored baseline turned into an envelope, for illustration only (not output of the
current tool): 24 goals, 22 reached, 1009.5 s goal time, 590.8 s lost, 234.8 s unjustified (23.3% of goal time). It carries `warn` because the
bag has no params dump. A real run of the current tool on that bag gives 218.8 s unjustified.

## 7. What the server does with it (for reference, not part of the robot team's work)

Validates against the schemas, stores the envelope as-is plus the metrics in queryable form, stores artifacts in MinIO, shows `metrics` as tiles
from the manifest's declarations. It treats everything in a result as untrusted text (escapes notes and table cells, resolves artifact paths
inside the analyzer's directory only, enforces the size caps). A result that fails validation is stored as `error` with the validation message.

## 8. Decisions (robot team reply 2026-10-28; "confirm" = needs the owners' sign-off)

Checked by the robot team against `lost_time_ledger.py` and the baseline: baseline numbers match, tick residual 2.16e-12 in the baseline, the mapped tables exist,
the tool writes `<label>_ledger.json` / `<label>_ledger.md` into `--out`. They did not reproduce our 218.8 / 252.1 numbers themselves.

| # | Question | Decision |
|---|---|---|
| 1 | Manifest next to each tool or one registry | next to each tool |
| 2 | Where the contract lives | `sati_benchmark/analysis/` (confirm); the ledger stays in `behaviour/tools/` |
| 3 | `warn` thresholds | tick residual 1e-6 s fine (2e-12 observed); `unassigned_share` 0.05 fine (0.0024 observed); both a policy choice for the owners (confirm) |
| 4 | Which metrics | the ten mapped are fine for v1; `q_rows` not reviewed yet |
| 5 | Legacy profile / baselines | no `legacy_rules` profile in v1; the tool git sha in each result is enough; keep old baselines as history, add new ones from the current tool marked with their build |
| 6 | `extractor_version` | robot team owns it, a constant in `bag_to_jsonl.py`, bumped on any output change; NOT added yet (meta.json carries no version today) |
| 7 | Reuse `x/` | always re-extract on the server |
| 8 | (numbering gap in the earlier text) | none |
| 9 | Extractor skips undecodable types | done in the working tree: skipped topics listed in `meta.json` `not_decodable` and on stderr; unit-tested, not run on a real bag; deserialisation errors on an importable type are not covered |
| 10 | Per-goal tables up to 200 rows, per-tick series as artifacts | OK |
| 11 | Finalize call after upload | the robot team would skip it while the server polls |

Robot-side changes already made (not committed or built yet):
- **Params dump:** the upload tar keeps the single top-level `<bag>/` and now also holds `<bag>/params/` (YAMLs, `config_hash.txt`, `bt_trees/`, `RUN.txt`). Older bags and bags without a params dir have no `params/`: keep accepting that. The upload-url request body is unchanged.
- **Build tag:** `params/RUN.txt` carries `git_describe`, `git_dirty_patch`, `build_image` and per-package build times, so no new field is needed. The runner passes `--params <bag>/params` when it exists and fills `producer.stack_build` from `RUN.txt`.
- **After a verified upload the bag and its `_params` dir are deleted on the robot.**
