# Local setup — what runs where, and what was changed on this machine

Written 31 Aug 2026. This file exists so that (a) the environment is reproducible, and
(b) every change made to the machine outside this repo is recorded with a way to undo it.

## Machine changes made for this project

### Ollama was broken, not merely un-started

`CLAUDE.md` recorded Ollama as "installed but not running." It was actually failing to start
on every attempt, and had been since at least 26 Aug. Root cause:

```
Couldn't find 'C:\Users\Divyansh\.ollama\id_ed25519'. Generating new private key.
Error: could not create directory mkdir C:\Users\Divyansh\.ollama: Cannot create a file when that file already exists.
```

`C:\Users\Divyansh\.ollama` was a **symlink into an unrelated local project**, whose
target Ollama could not use. Ollama saw a path that existed (the link) but could not create the
directory it needed, so the server exited 1 in a loop and port 11434 never opened.

That project is off-limits to this repo, so the fix deliberately avoided touching it.

**Changes made** (decision taken 31 Aug 2026):

| What | From | To |
|---|---|---|
| `C:\Users\Divyansh\.ollama` | symlink into an unrelated project | real directory |
| `C:\Users\Divyansh\.ollama.backup` | — | the renamed symlink, still pointing at its original target |
| `OLLAMA_MODELS` (User env) | a path inside that project | `E:\Coding\PROGRAMS\Ollama_Models` |

The symlink was **renamed, not deleted**, and renaming a link never touches its target — nothing
inside that project was read, modified or removed.

**To restore the previous arrangement:**

```powershell
Remove-Item 'C:\Users\Divyansh\.ollama' -Recurse -Force
Move-Item 'C:\Users\Divyansh\.ollama.backup' 'C:\Users\Divyansh\.ollama'
[Environment]::SetEnvironmentVariable('OLLAMA_MODELS','<original path>','User')
```

**On `OLLAMA_MODELS`.** The running server turned out to be reading
`E:\Coding\PROGRAMS\Ollama_Models` — not the path the User env var claimed, and not
the default. The var was therefore set to match the path actually in use, which is outside
that project and so carries no such problem. Leaving the mismatch in place would have meant
a future restart silently pointing at an empty directory and appearing to lose every model.

### Not changed

- No git remote was added, changed or pushed to. Hard rule 3 holds.
- `.env` and `rzp-key.csv` remain gitignored and were never printed in full.
- No unrelated local project was read or modified.

## Running things

### Ollama

```bash
ollama serve          # or just launch the Ollama app from the Start menu
ollama list           # llama3.1:8b is the model this project uses
```

Health check: port 11434 should accept a connection. If the server exits immediately, check
`C:\Users\Divyansh\AppData\Local\Ollama\server.log` — that is where the failure above was found,
and the CLI's own output hides it.

**Model: `llama3.1:8b`** (~4.9 GB), running on the RTX 4060 Laptop GPU's 8 GB of VRAM. Its only
jobs are composing message wording and parsing inbound replies. It never decides whether to
contact anyone — that is the deterministic state machine's job, per the design commitments.

`qwen2.5:7b` and `llama3:8b` were already present on this machine and make usable fallbacks if
`llama3.1:8b` misbehaves; note `llama3:8b` is not the same model as `llama3.1:8b`.

### Phase 0 gate artifacts

Both regenerate their own evidence, so no figure in the docs is hand-typed:

```bash
python scripts/gate_0_7_power.py            # → results/phase0/0.7-power-and-cost-check.json
cd spikes && python test_webhook_receiver.py # → results/phase0/0.4-webhook-receiver-local.json
```

The power script is fully deterministic and its output is byte-identical across runs. The webhook
proof is deterministic in every check *except* the measured ACK latency, which is a timing
observation and varies (4.8 ms and 18.1 ms across two runs, against a 5 s budget).

## The webhook endpoint — permanent, not session-scoped

**Public URL:** `https://<zrok-share-name>.shares.zrok.io/webhook`, where the share name comes
from `RAZORPAY_WEBHOOK_ZROK_NAME` in `.env` (gitignored). It is not a credential — unsigned
requests get a 400 and never reach the queue — but it is a live endpoint on this machine,
and a public repo has no reason to advertise where to aim.
**Secret:** `RAZORPAY_WEBHOOK_SECRET` in `.env` (gitignored). It must match the Dashboard
value **exactly** — trailing whitespace on it once caused 18 real Razorpay deliveries to be
silently rejected while the endpoint reported perfectly healthy.

Razorpay retries a failing webhook with exponential backoff for 24 hours and then
**auto-disables it**. An endpoint that fails silently is worse than none at all.

### The share name was rotated on 2 Sept 2026

The original name reached GitHub before the placeholder work landed — the branch had already
been pushed twice, so scrubbing the working tree and rewriting history could not un-publish
it. A force-push removes the branch pointer but GitHub keeps unreachable objects addressable
by SHA, so the old value stayed reachable to anyone holding the hash.

The repo was private and the name was never a credential (unsigned requests get a 400 and
never reach the queue), so the exposure was small. Rotating was still the cheaper and
strictly safer fix than trying to erase it: the old name and its share were **deleted**, so
whatever survives in any object store now resolves to nothing. Verified: the old URL returns
404, the new one returns 400 unsigned and 200 to a signed event.

**No code changed.** That is the point of keeping the name in `.env` — a rotation is an env
edit plus a Dashboard update, not a commit. If it has to be rotated again, the steps are:

```powershell
tools\zrok\zrok2.exe create name rzp-wh-<new>      # reserve the new name
# edit RAZORPAY_WEBHOOK_ZROK_NAME in .env
tools\zrok\zrok2.exe delete share <old-token>      # tear the old one down
tools\zrok\zrok2.exe delete name <old-name>
powershell -ExecutionPolicy Bypass -File scripts\webhook_daemon.ps1   # rebuild on the new name
```

Then update the URL in the Razorpay Dashboard (Test mode → Account & Settings → Webhooks,
OTP `754081`). **The secret does not change**, and until the Dashboard is updated every
delivery 404s — which starts the 24-hour auto-disable clock.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\webhook_status.ps1              # health
powershell -ExecutionPolicy Bypass -File scripts\webhook_daemon.ps1              # force a repair
powershell -ExecutionPolicy Bypass -File scripts\install_webhook_task.ps1        # (re)install
powershell -ExecutionPolicy Bypass -File scripts\install_webhook_task.ps1 -Uninstall
Get-Content runtime\webhook-daemon.log -Tail 20                                  # quiet when healthy
```

### Two design corrections, both made after the first version failed

**1. One-shot, not a supervisor loop.** The first version was a `while ($true)` loop run as a
scheduled task. It died within seconds of every start with exit code `0xC000013A`
(`STATUS_CONTROL_C_EXIT`) — the console it was attached to closed and took it down — and the
endpoint sat broken for hours. A long-lived supervisor is itself a single point of failure.
`webhook_daemon.ps1` now does **one check-repair pass and exits**; Task Scheduler's 5-minute
repetition is the watchdog. Nothing long-lived means nothing to kill.

**2. The health check probes the public URL, not the process list.** The first version checked
"is there a `zrok2` process". That reported healthy while the endpoint returned **502** — the
process was alive, the share record existed, the tunnel was not serving. The probe now sends an
**unsigned** request and expects **HTTP 400** *"invalid signature"*: that proves tunnel →
receiver → handler end to end, while creating no event, so health checks never pollute
`results/phase0/0.4c-received-events.jsonl`.

### What it repairs

A zrok share record persists server-side after the process serving it dies, so re-running
`share public` returns `409 name already in use`. The daemon looks the stale share up via
`zrok2 list shares --json`, deletes it, then recreates — without that, any crash wedges the
endpoint permanently.

The Python interpreter is **pinned by absolute path**. Launching `python` by name resolved via
PATH into an unrelated tool's virtualenv, which would break the receiver if
that venv ever moved.

### Verified, 2 Sept 2026

Killed the receiver and the tunnel outright and touched nothing else. Endpoint went 502; the
watchdog detected it, deleted the stale share, rebuilt it, and confirmed 400 — **auto-repaired
in 312 s with no human action**. Worst case is one repetition interval plus ~30 s of zrok
registration.

### Known gaps, stated rather than papered over

- **Logon-triggered only.** An `-AtStartup` trigger needs elevation (non-elevated registration
  fails `0x80070005`), so the endpoint is down between boot and sign-in. The heartbeat picks it
  up within 5 minutes of logon.
- It cannot survive the machine being off or fully asleep. Neither triggers Razorpay's 24-hour
  disable unless the gap is that long, but a multi-day absence would.

## Still outstanding — needs a human

- **Register the webhook** in the Razorpay Dashboard: Test mode → Account & Settings → Webhooks
  → + Add New Webhook. URL and secret above; events `payment.failed` and `payment.captured`;
  test-mode OTP `754081`. Razorpay rejects `localhost` and any URL whose domain contains
  "razorpay", and delivers only to public URLs — which is what the zrok share is for.
- **The Razorpay support ticket** in `docs/support-ticket-draft.md` needs a logged-in dashboard
  session.
