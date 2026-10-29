# jobmail

Losing track of job applications in your inbox? This Python script digests an Apple Mail mbox, classiefies each mail and extracts data via an LLM, groups them into job applications, scrapes the job ad for skills/pay (if a url is in the email), and creates a report as a CSV file that can then be further analyzed.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env   # GEMINI_API_KEY + models / RPM / RPD
```

Drop the Apple export at `./mbox` in the repo root. The `table_of_contents` binary next to it is ignored.

## Run

```bash
python analyze.py              # progress bars; chatter goes to data/analyze.log
tail -f data/analyze.log
make run-debug                 # first 5 mails, loud console
make lint
make test
```

Free-tier Gemini quotas are per model, so `.env` defaults to round-robin on `gemini-3.5-flash-lite` + `gemini-3.1-flash-lite` (`GEMINI_RPM=15`, `GEMINI_RPD=500` each). On 429 we wait for the API's `retryDelay`; if a model burns its daily quota we stop using it until the Pacific-midnight roll. State is in `data/gemini_quota.json`. Events are checkpointed after every mail — Ctrl+C doesn't throw the run away.

Flags I actually use:

- `--debug` / `--limit N` — smoke tests
- `--skip-enrich` — classify + cluster only
- `--skip-browser-enrich` — don't open Chrome when httpx is blocked
- `--browser-reauth [HOST]` — wipe login state (all, or one host) and force a fresh login
- `--source apple` — ingest plugin (gmail/outlook are stubs)
- `--reclassify-all` — ignore the Message-ID store
- `--model NAME` — pin one model, skip the pool
- `--log-file PATH` — default is `data/analyze.log`

Classify gets a company→roles catalog of apps we already know, so Stepstone/Indeed mails can stick to the real employer key instead of inventing a new one.

## Outputs (`./data`)

| file | what |
|------|------|
| `events.jsonl` / `events.csv` | one row per email (+ `*_norm` fields) |
| `applications.csv` / `applications.json` | one row per application + timing + enrich |
| `stats.json` | funnel / latency |
| `analyze.log` | `tail -f` this while it runs |
| `gemini_quota.json` | per-model RPD for the current Pacific day |
| `job_ads/` | enrich cache (`extracted` / `inactive` / `retry` / `blocked`) |
| `browser_profile/` | Chromium cookies (under `data/`, gitignored) |
| `browser_hosts.json` | which hosts you've already unlocked |

Browser enrich keeps one Chromium open for the whole pass. Headless first; a real window only if the page still looks blocked/login-walled (or you passed `--browser-reauth`).

Indeed is skipped for Playwright by default — captcha hell. Those land as `blocked`. Empty `BROWSER_SKIP_HOSTS=` in `.env` if you want to try anyway, or `--browser-reauth de.indeed.com` for one shot. Hosts already marked `blocked` in `browser_hosts.json` stay skipped until reauth.

Re-run with a fresher mbox and we skip Message-IDs we've already classified. Enrich only retries `retry` cache entries; `blocked` / `extracted` / `inactive` stay put unless you clear them.

Backlog notes live in `improvements.md` when there's anything in there.

## Adding another mail source

New `MailSource` under `jobmail/ingest/`, register it in `registry.py`. 
