# Security notes

What protects this system today, what does not, and what is deliberately left for later. Written 2026-10-21. The root `SECURITY.md` is
NVIDIA's text for the upstream project; this folder is about our deployment.

| File | What |
|---|---|
| [open-items-2026-10-21.md](open-items-2026-10-21.md) | The list of known gaps, each with its risk, what it takes to fix, and its state. Nothing here is being worked on yet unless it says done. |
| [trust-model.md](trust-model.md) | Who can reach what, and what the only barriers are (the tailnet, the operations token, the config profiles). |

Related: [../CONFIG_PROFILES.md](../CONFIG_PROFILES.md) (where secrets live), [../SERVER_OPERATIONS.md](../SERVER_OPERATIONS.md) (restart and deploy from the app).
