# WQBrain-Tele

Telegram front-end for the WorldQuant BRAIN API.

**Built:** headless authentication with expiry alerting, `/sim` for one guided alpha,
`/fields` datafield discovery, and a persistent queue that batches many alphas into
multi-simulations. Every result is recorded to SQLite.

**Not built yet:** querying the alpha store from chat (beyond `/alphas` and
`/export`), and the orchestration agent.

```
ACE_API/            WorldQuant's ACE library + tutorial notebook. Vendored; never edited.
bot/                The Telegram bot.
tests/              Auth, timers, spec building, result extraction, storage, formatting.
```

## Why there is a `bot/` layer at all

ACE is written for a notebook, and three of its behaviours are fatal in a
long-running process:

- `ace.start_session()` blocks on `input()` / `getpass.getpass()`, and its
  biometric branch waits on a terminal keypress. A rejected password makes it
  erase `~/secrets/platform-brain.json` and then **recurse**.
- `ace.get_credentials()` prefers `~/secrets/platform-brain.json` over the
  environment, so a file left by a notebook run silently shadows `.env` forever.
- `ace.check_session_and_relogin()` refreshes whenever under 2000s remain, which
  would pre-empt a 5-minute warning entirely. Worse, every simulation entry point
  opens with it (`ace_lib.py:718, 778, 887`), so a simulation started with under
  2000s left would reach `get_credentials()` and **park a worker thread on
  `input()` forever**.

So `bot/brain_session.py` reimplements the handshake — and only the handshake —
against the same `SingleSession` singleton every other ACE function uses, and
`install_session_guards()` in `bot/ace_bridge.py` disarms ACE's internal
re-login at runtime. `ACE_API/` stays byte-for-byte as WorldQuant ships it so
their updates drop in cleanly.

## Setup

```bash
pip install -r requirements-bot.txt
cp .env.example .env
```

Fill in `.env`:

1. `TELEGRAM_BOT_TOKEN` — from [@BotFather](https://t.me/BotFather), send `/newbot`.
2. `BRAIN_CREDENTIAL_EMAIL` / `BRAIN_CREDENTIAL_PASSWORD` — your BRAIN login.
3. `TELEGRAM_ALLOWED_CHAT_IDS` — leave empty for now.

Then:

```bash
python -m bot
```

Send `/start` to your bot. It replies with your numeric chat ID. Put that in
`TELEGRAM_ALLOWED_CHAT_IDS` and restart.

**The allowlist is the only thing standing between a stranger and your BRAIN
account.** `SingleSession` is a process-wide singleton, so the bot has exactly one
identity — whoever can talk to it acts as you. An empty allowlist authorises
nobody, and every command except `/start` is filtered twice.

## Commands

| Command | |
|---|---|
| `/start` | Greet; echo this chat's ID. The only command a stranger can reach. |
| `/login` | Authenticate, including the biometric flow. |
| `/status` | Time remaining, expiry, last login. Re-checks against BRAIN. |
| `/whoami` | Read account data — proves the session works, not just that login returned 201. |
| `/relogin` | Force a fresh session. |
| `/logout` | Drop the session and clear timers. |
| `/sim` | Build and run one alpha, guided. |
| `/fields <query>` | Search datafields, paginated. |
| `/datasets <query>` | List datasets. |
| `/batch` | Queue many alphas from pasted expressions. |
| `/queue` | Queue state; `/queue cancel`, `/queue retry`. |
| `/alphas` | The 10 most recently simulated alphas. |
| `/export` | CSV of every recorded alpha. |
| `/cancel` | Abandon a `/sim` or `/batch` in progress. |

### `/sim`

```
/sim
  -> "Send me the expression"
  <- ts_sum(vec_avg(nws18_qmb),120)
  -> settings card: Region, Universe, Delay, Neutralization,
                    Decay, Truncation, Test period
     (tap any button to change it; Run when ready)
  -> confirmation card -> Confirm & run
  -> results posted when the simulation finishes
```

The menus are built from `ace.get_instrument_type_region_delay`, fetched once per
process, so only combinations BRAIN actually accepts can be selected. Changing
region snaps universe and neutralization to valid values rather than letting a
simulation fail after submission.

A simulation blocks for minutes inside ACE's polling loop, so it runs as a
background task — the chat stays responsive and the result arrives as its own
message. `BRAIN_MAX_CONCURRENT_SIMS` caps how many run at once; beyond that they
wait for a slot.

### `/fields` — datafield discovery

```
/fields sentiment
/fields news18 type:VECTOR
/fields dataset:fundamental23 type:MATRIX region:CHN delay:0
```

Bare text goes to BRAIN's own search; `dataset:`, `type:`, `region:`, `delay:` and
`universe:` are pulled out as filters. Results come back as cards — id, type,
description, coverage and usage — eight per page with Prev/Next and an Export CSV
button.

`ace.get_datafields` is *not* used for this. It hardcodes `limit=50&offset=0`
([ace_lib.py:1285](ACE_API/ace_lib.py#L1285)) and discards the response's `count`,
so there is no way to reach result 51 or to tell "50 results" from "50 of 800".
`bot/datafields.py` calls the endpoint directly — still through ACE's session and
its `_check_rate_limit` throttling. A `type:` filter is applied client-side, which
makes the total an upper bound; the UI shows `~137` rather than claiming precision
it doesn't have.

### `/batch` — many alphas at once

Two ways in, one path out:

```
/fields news18 type:VECTOR → [ Use these 8 fields ]
  → "Send a template. {f} is the field."
  ← ts_sum(vec_avg({f}),120)
  → 8 expressions previewed, 3 flagged as already simulated
  → settings card → Queue all / Skip duplicates

/batch
  ← ts_rank(close,20)
    ts_corr(close,volume,10)
  → same settings card → queued
```

If the selected fields are VECTOR and the template has no `vec_` operator, the bot
says so before you queue. That mistake fails on BRAIN's side, and because a
template applies to every field at once it costs the whole batch rather than one
simulation.

Duplicates are detected on expression **and** every settings column, so the same
expression at a different decay is correctly a new experiment.

### The queue

Queued alphas live in `data/alphas.db` beside the results, so a batch survives a
restart or a closed lid. A worker drains it one bundle at a time:

```
queued ──► running ──► done
               │  └──► failed
               └─────► interrupted   (process died mid-flight)
```

Bundles never mix region, delay or universe — multi-simulation requires them to
match (notebook cell 42) — and are capped at BRAIN's limit of 10. `/sim` and the
queue share one concurrency semaphore, so `BRAIN_MAX_CONCURRENT_SIMS` holds across
both rather than each keeping its own budget.

The worker re-checks the session **before every bundle**, not once per batch: forty
alphas outlive a four-hour session, and ACE's own refresh is disarmed.

A row still `running` at startup means the process died after submitting. It is
parked as `interrupted` rather than retried, because the simulation may well have
completed on BRAIN's side — `/queue retry` requeues those deliberately.

Reporting is deliberately quiet: a running tally per bundle, a full report only for
alphas that pass every check, and everything else in the store behind `/alphas` and
`/export`.

`ace.simulate_alpha_list_multi` is not used — it runs its own `ThreadPool`
([ace_lib.py:987](ACE_API/ace_lib.py#L987)), which would compound with the
semaphore and push past the account limit. `run_batch` drives the single-bundle
primitive instead, with a fallback to individual simulations when ACE's
`multisimulation_progress` hits its `len(int)` bug
([ace_lib.py:494](ACE_API/ace_lib.py#L494)).

### The alpha store

Every completed simulation is written to `data/alphas.db` — expression, all
settings, sharpe/fitness/turnover/drawdown/margin/returns/PnL, and the full check
grid as JSON. Re-simulating an alpha updates its row rather than duplicating it.

SQLite rather than CSV because simulation workers write concurrently, expressions
contain commas and quotes, and "which alphas passed with sharpe > 1.5" should be a
query. `AlphaStore.export_csv()` produces a CSV whenever a spreadsheet is what you
want.

### Biometric login

If BRAIN asks for biometrics, the bot sends you the one-time link and an
*I've completed it* button. Open the link, complete it, tap the button. The bot
also polls every 5s for 10 minutes, so the button is a shortcut rather than a
requirement.

### Expiry alerting

A session lasts about 4 hours. On login the bot arms two one-shot timers — the
warning at `WARN_BEFORE_EXPIRY_SECONDS` (default 300) before expiry, and a notice
at expiry — plus a reconciler that re-checks the real expiry every 10 minutes.

The reconciler exists for the two cases a timer cannot see: the host sleeping
(APScheduler works in wall-clock, so a pending job fires late or is dropped) and
the session being killed from the platform in a browser. It re-anchors the timers
against the truth, so closing your laptop for an hour does not lose the warning.

## Tests

```bash
python -m pytest tests/ -q
```

134 tests, no network and no credentials required. They cover the paths that are
impractical to check by hand: a rejected password, the biometric poll, a
server-side session kill, timer re-arming after a sleep, the concurrency ceiling,
a process dying mid-batch, and a session expiring halfway through a queue. The
result-extraction fixtures reproduce the exact DataFrame shapes saved in
`ACE_API/how_to_use.ipynb` (cells 54 and 56), so parsing is tested against what
BRAIN really returns.

## Verifying against the real thing

- **Bad config** — remove a key from `.env`; startup must name it and exit 1.
- **Unauthorised** — `/status` from a chat not in the allowlist gets no reply at all.
- **Login** — `/login`, then `/status` shows ~240 minutes; `/whoami` returns your account.
- **The 5-minute warning, without waiting 4 hours** — set
  `WARN_BEFORE_EXPIRY_SECONDS=14000`, restart, `/login`. The warning arrives within
  seconds. Restore `300` afterwards.
- **Bad password** — put a wrong one in `.env`; `/login` must return a readable error,
  and `shasum ~/secrets/platform-brain.json` must be unchanged.
- **Server-side kill** — log out on the BRAIN platform in a browser; within 10
  minutes the bot reports the session gone.
- **First simulation** — `/sim`, send a simple expression such as
  `ts_rank(close, 20)`, accept the defaults, Confirm & run. Expect a results
  message in a few minutes, then `/alphas` to confirm it was recorded.
- **Datafield search** — `/fields news18 type:VECTOR`; check Prev/Next paging and
  that the count looks plausible against Data Explorer.
- **First batch** — from that page, *Use these N fields*, template
  `ts_sum(vec_avg({f}),120)`, queue it, then `/queue` for progress.
- **Duplicate detection** — queue the same batch again; expect every alpha flagged.
- **Restart resilience** — restart the bot mid-batch. Expect `running` rows to
  become `interrupted`, and `/queue retry` to drain them.

## Notes

- ACE writes an `ace.log` into the working directory at import time. It is
  gitignored and harmless.
- `.env` is gitignored. Never commit it.
- The biometric link is a one-time credential that transits Telegram. It is
  short-lived, but it is the one secret this design cannot keep off the wire.
