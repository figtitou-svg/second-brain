# Second Brain

*[Version française](README.fr.md)*

A personal "second brain" that lives in Telegram. It reads my mail, documents and voice notes, answers questions about them with sources, and — above all — **turns tasks into calendar blocks and keeps nagging me until they are done**.

Built to run for free on a small always-on VM (free-tier APIs, 1 GB of RAM).

## What it does

| Area | Features |
| --- | --- |
| **Capture** | Free text and voice notes on Telegram, Gmail (read-only), Google Drive documents (PDF…), filed automatically |
| **Extraction** | An LLM turns any text into *tasks* (with deadline, duration estimate, priority) and *events* (fixed date/time). Duplicates are detected across mail, notes, Notion and the calendar |
| **Planning** | A pure, tested planner places every task in Google Calendar inside allowed hours, around blocked slots and existing events. A task never starts "too early" for its size (a 5-min errand is planned the day it is due, a big file days ahead) |
| **Coaching** | Morning briefing, reminders before each block, follow-up after it (done / not finished / reschedule), weekly recap, a daily "focus" ranking with the reason for each priority |
| **Projects** | `/projet` interviews me to define a project, then splits it into milestones, proposes the next tasks, and warns when a project stalls or a deadline gets tight |
| **Memory** | `/ask` answers from my notes, mails and documents (SQLite FTS5 search) and cites the source |
| **Integrations** | Notion deadlines database (two-way), Google Docs journal, free-tier quota tracking, health alerts when a job fails |

## Privacy-aware LLM routing

Every stored item carries a `sensitivity` (`private` or `public`). `private` content is **never** sent to a cloud model; the router (LiteLLM) refuses. Public content goes to Groq (primary) with Gemini as fallback; a local Ollama model can serve private content on a machine that can run it.

## Architecture

```
run.py                  entry point: Telegram bot + scheduler + Gmail/Drive/Notion jobs
bot/                    Telegram layer (handlers, ports, voice transcription)
coach/                  domain logic: planner, scheduler, extraction, focus, projects, dedupe, SQLite
llm_router/             LiteLLM wrapper: sensitivity routing, fallback chain, timeouts, usage logging
drive_sync.py ...       Google Drive / Gmail / Notion / journal integrations
scripts/                one-off helpers (Google re-authorisation)
deploy/                 example systemd unit
tests/                  570+ tests (planner, extraction, scheduler, bot, integrations)
```

The planner (`coach/planner.py`) is pure — no network, no I/O — which is what makes its behaviour easy to test. Planning rules (allowed hours, blocked slots, buffers, lead time per task size) live in [`coach/rules.yaml`](coach/rules.yaml).

## Getting started

Requirements: Python 3.12+, a Telegram bot token, a Google Cloud OAuth client (Gmail, Calendar, Drive, Docs), and at least one LLM API key (Groq and/or Gemini).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-server.txt        # runtime
pip install -r requirements-dev.txt           # + pytest

cp .env.example .env                          # fill in your keys
mkdir secrets && cp /path/to/client_secret.json secrets/
python -m scripts.reauth_google               # opens a browser, writes secrets/token.json

python run.py                                 # add --no-gmail to skip mail ingestion
pytest                                        # run the test suite
```

`secrets/`, `.env` and `data/` are git-ignored: keep them that way.

## Deployment

It runs as a systemd service on a small Linux VM — see [`deploy/second-brain.service`](deploy/second-brain.service). Run it from **one** machine only: two instances with the same Telegram token conflict, and two databases would create duplicate calendar events.

## Tech

Python · python-telegram-bot · APScheduler · SQLite (FTS5) · Pydantic · LiteLLM (Groq, Gemini, Ollama) · Google APIs (Gmail, Calendar, Drive, Docs) · Notion API · pytest
