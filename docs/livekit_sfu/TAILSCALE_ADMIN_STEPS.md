# Tailscale admin console — human tasks

Everything in this file needs a human with admin-console access — none of it
is scriptable from this box. Do these in order; each is scoped to exactly one
phase in `TESTING.md`, and each is small and reversible on its own.

**Scope principle, applies to every task below:** every ACL rule here is
purely additive and scoped to LiveKit's own ports
(`7880,7881,8008,50000-60000`) on one direction of traffic only (robot/
operator → server). Tailscale ACLs are an allow-list — adding a rule can
never revoke or restrict a different rule. So none of this touches SSH or
any other management access to the server or to any robot, in either
direction — that's exactly why tasks 3 and 4 scope by IP/host-alias instead
of tagging the server (or requiring operators to be tagged): tagging a
device is the operation that actually changes its access model, not adding
a narrowly-scoped rule. Robots being tagged already (pre-existing, from
fleet provisioning, nothing to do with this plan) doesn't change this either
— SSH into a robot is a completely different `src`/`dst`/port combination
than the robot-to-server rule added here, governed by whatever rule already
grants it, untouched by anything in this file.

---

## Human task 1 — Enable HTTPS certificates — ✅ done

Already enabled, and `tailscale serve` is on (2026-09-18, §3 of `README.md`):
`wss://admin-satinav-pc.tail055f44.ts.net` (port 443, tailnet-only) proxies to
the SFU. Reachable from a tailnet peer with no additional ACL rule (the rules in
this file are additive-only, so existing owner access covers it) and no ufw rule.
Nothing further here.

---

## Human task 2 — Confirm `cimbi` can reach this box

**When:** only needed before `TESTING.md` phase 2, and only if the check below
fails.

**Why:** phase 2 uses your own laptop `cimbi` (100.78.100.16) to send real
Tailscale-routed test traffic to `admin-satinav-pc`. Since both are currently
owned by `admin@`, most default ACL policies already allow this — check
before assuming anything needs to change.

**Steps:**
1. On `cimbi`, try: `curl -v http://100.85.3.47:7880/` — if you get any HTTP
   response (even an error page), the path is already open, skip straight to
   `TESTING.md` phase 2. No console visit needed.
2. If it times out or connection-refuses at the network level (not an HTTP
   error), go to the admin console → **Access Controls** tab.
3. In the policy file editor, add this under the `"acls"` array (merge it in,
   don't replace the file):
   ```json
   {
     "action": "accept",
     "src":    ["100.78.100.16"],
     "dst":    ["100.85.3.47:7880,7881,8008,50000-60000"]
   }
   ```
4. Click **Save**.
5. Re-run the `curl` check from step 1.

This grants access from exactly `cimbi` to exactly `admin-satinav-pc`, on
exactly the LiveKit ports — nothing broader. (Port 8008 is the token
service: without it a device can reach the SFU but can't get a token. The
original version of these rules omitted it; `cimbi` only worked through
owner-based access.)

---

## Human task 3 — scope robots to the server, by IP (before phase 3)

**When:** before `TESTING.md` phase 3 (real fleet robots) — not needed for
phases 1-2.

**Why:** grants only tagged robot devices access to the LiveKit ports on
`admin-satinav-pc` — nothing else on this box becomes reachable by robots.

**Deliberately *not* tagging `admin-satinav-pc` itself.** Tagging it would
convert it from a user-owned device to a machine identity, which drops its
owner-based (`admin@`) default access to everything else — including your
own SSH access to it, and to every Jetson, exactly the thing you don't want
touched. It also isn't necessary: Tailscale ACL `dst` fields accept a plain
IP or a named host alias just as well as a tag, so the box can stay
owner-owned and SSH keeps working exactly as before — only the *robot* side
needs a tag (already true — `jetson-golya`/`jetson-kolibri` are already
tagged from your existing fleet provisioning, that part isn't changing).

**Steps:**
1. Admin console → **Access Controls** tab.
2. First, find the **exact tag** already applied to `jetson-golya` /
   `jetson-kolibri` — look in the policy file's `"tagOwners"` block, or the
   **Machines** tab (click either device → its tag is shown next to the
   name). `tailscale status` on this box only showed a display label
   (`tagged-devices`), not the literal `tag:...` string — get the real one
   here before continuing. Call it `<ROBOT_TAG>` below.
3. In the policy file, add a `"hosts"` entry naming this box (skip if
   `"hosts"` already has one for it):
   ```json
   "hosts": {
     "livekit-server": "100.85.3.47"
   }
   ```
4. Add an ACL rule referencing that alias, not a tag:
   ```json
   {
     "action": "accept",
     "src":    ["<ROBOT_TAG>"],
     "dst":    ["livekit-server:7880,7881,8008,50000-60000"]
   }
   ```
5. Click **Save**. That's it — no Machines-tab step, no tag applied to
   `admin-satinav-pc`, its owner stays `admin@`, SSH to it and to every
   Jetson is untouched.
6. Confirm: `tailscale status --self` on this box should still show
   `admin@` (unchanged), and the robot ACL rule should be visible in the
   policy file.

---

## Human task 4 — scope operator devices too, by IP (optional, do anytime)

**Why:** `cimbi` currently reaches `admin-satinav-pc` only because both are
owned by `admin@` — broad, implicit, account-based access, not a scoped
grant. `scripts/livekit/robot_client.py --role operator` already closes the
*application-layer* gap (operator tokens can't publish tracks, enforced
server-side — see `scripts/livekit/README.md`). This closes the matching
*network-layer* gap.

**Same reasoning as task 3: not tagging.** Tagging `cimbi` would strip its
own owner-based access to whatever else it currently reaches (SSH to other
boxes, say) — same problem as tagging the server would have caused. Scope by
IP instead, per operator device, using the same `livekit-server` host alias
from task 3.

**What an operator actually needs** (a dashboard user's browser):

| Port | Why |
|---|---|
| `443` | `wss://admin-satinav-pc.tail055f44.ts.net` (`tailscale serve`), the signaling connection |
| `50000-60000` (udp), `7881` (tcp) | media, direct or TCP fallback |

An operator does **not** need `8008`: the dashboard gets its token through the
gateway (`client.satinavrobotics.com` → `/api/livekit-selfhost/createToken`), and
:8008 is where the robot token route lives (`/api/createToken` mints any role),
so leave it out of operator rules. `7880` is not needed either now that `443`
fronts it.

**Steps:**
1. Admin console → **Access Controls** tab.
2. Add an ACL rule (merge into `"acls"`; `100.78.100.16` is `cimbi`'s
   Tailscale IP — add one `src` entry per operator device):
   ```json
   {
     "action": "accept",
     "src":    ["100.78.100.16"],
     "dst":    ["livekit-server:443,7881,50000-60000"]
   }
   ```
   A device that also runs `robot_client.py --role operator` (it fetches its
   token straight from :8008 and connects to `ws://100.85.3.47:7880`) needs
   `7880,8008` on top; treat that as a developer device, not a dashboard user.
3. Click **Save**. No Machines-tab step, nothing tagged, `cimbi`'s (and any
   other operator's) existing access to everything else is untouched.

Note that `443` is **not** in the robot rule of task 3 or in the phase 2 rule of
task 2: robots keep `ws://100.85.3.47:7880`, and those rules were written before
`tailscale serve` was enabled. Today none of this is needed (owner-based access
covers everything); it only matters once operator access stops depending on
account ownership.

This scales per-device rather than as a group the way a tag would — fine for
a handful of named operator laptops; if the operator pool grows large enough
that per-IP entries get unwieldy, that's the point to reconsider a tag (and
accept the access-model change that comes with it), not before.

This is independent of tasks 1-3 and doesn't block anything — `cimbi` keeps
working via owner-based access either way. Do this when you want operator
access to stop depending on account ownership.

---

## What's deliberately not here

Enabling Tailscale Funnel, changing DNS records, or anything that exposes this
box off the tailnet — none of that is part of this plan. If a step here ever
asks you to enable Funnel, stop and re-check against `README.md` — that's not
this architecture.
