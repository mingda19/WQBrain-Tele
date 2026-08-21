# WQBrain-Tele

Telegram front-end for the WorldQuant BRAIN API.

**Phase 1 (built): headless authentication + session-expiry alerting.**
Phases 2–4 — alpha submission, datafield lookup, the result store, and an
orchestration agent — are not built yet.

```
ACE_API/            WorldQuant's ACE library + tutorial notebook. Vendored; never edited.
bot/                The Telegram bot.
tests/              Auth state machine, timer arithmetic, end-to-end flow.
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
  would pre-empt a 5-minute warning entirely.

So `bot/brain_session.py` reimplements the handshake — and only the handshake —
against the same `SingleSession` singleton every other ACE function uses.
`ACE_API/` stays byte-for-byte as WorldQuant ships it so their updates drop in
cleanly.

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

27 tests, no network and no credentials required. They cover the paths that are
impractical to check by hand: a rejected password, the biometric poll, a
server-side session kill, timer re-arming after a sleep.

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

## Notes

- ACE writes an `ace.log` into the working directory at import time. It is
  gitignored and harmless.
- `.env` is gitignored. Never commit it.
- The biometric link is a one-time credential that transits Telegram. It is
  short-lived, but it is the one secret this design cannot keep off the wire.
