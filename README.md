# Investor Trade Tracker

Watches SEC and congressional filings and pushes an alert when someone with
inside knowledge trades. Runs free on GitHub Actions — no server, and your
Mac can be off.

**Everything here is public record.** The STOCK Act *requires* members of
Congress to disclose trades, and SEC Form 4 *requires* corporate insiders to
do the same within 2 business days. Reading those filings faster than other
people is not insider trading. This is a data pipeline, not financial advice.

---

## How fast is each source, really?

| Source | What it is | Real lag |
|---|---|---|
| **SEC Form 4** | CEOs, CFOs, directors, 10%+ owners | **~2 business days** |
| House / Senate PTR | Members of Congress | **30–45 days** (statutory) |
| 13F | Hedge funds (Buffett, Burry, Ackman) | up to **4.5 months** |

Form 4 is the only genuinely fast feed, which is why it was built first.
Congressional data *cannot* be fast — the reporting window is written into the
law. Any product advertising "real-time congressional trades" is selling you
month-old data.

---

## Setup

### 1. Discord (required — this is the reliable channel)

1. Open Discord → pick a server you own (or **+** → *Create My Own* → *For me and my friends*).
2. Make a channel for this, e.g. `#trades`.
3. Hover the channel → ⚙️ **Edit Channel** → **Integrations** → **Webhooks**.
4. **New Webhook** → **Copy Webhook URL**.

### 2. Add it to GitHub

1. Go to this repo on github.com → **Settings** (top bar) → **Secrets and variables** → **Actions**.
2. **New repository secret**, one at a time:

| Name | Value |
|---|---|
| `DISCORD_WEBHOOK_URL` | the URL you copied |
| `SEC_USER_AGENT` | `Rorey Shen roreyshen@gmail.com` — the SEC **bans IPs** that don't send real contact info |
| `GMAIL_ADDRESS` | `roreyshen@gmail.com` (only for the free SMS attempt) |
| `GMAIL_APP_PASSWORD` | see below |

> Secrets stay hidden even though the repo is public. Never put these in a file.

### 3. Gmail app password (only if you want the free SMS attempt)

1. [myaccount.google.com](https://myaccount.google.com) → **Security**.
2. Turn on **2-Step Verification** if it isn't already (app passwords require it).
3. Search the page for **App passwords** → create one named `tracker`.
4. Copy the 16-character code — that's `GMAIL_APP_PASSWORD`. Spaces don't matter.

### 4. Turn it on

Repo → **Actions** tab → **Track trades** → **Run workflow**.

The **first run alerts on nothing by design.** The SEC feed still remembers
~1,100 old filings, and dumping those on you would be noise — so run one marks
them as seen and starts clean. Every run after that alerts only on genuinely
new filings.

---

## About the texts

You asked for SMS. Here is the honest state of it.

**The free route** emails the carrier's email-to-SMS gateway, which turns it
into a normal text. Costs nothing, no registration. It's enabled and it will
try. **But:** 408-748-6507 is on Xfinity Mobile, a Verizon MVNO, and Xfinity
customers widely report these gateways bouncing. Verizon has also announced a
hard shutdown of the gateway on **2027-03-31**.

So find out rather than assume:

```bash
python -m src.notify.test --channel sms
```

Then check two things — SMTP **cannot** tell you whether a carrier delivered:

1. Did a text arrive within ~2 minutes?
2. Any bounce/undeliverable in roreyshen@gmail.com?

Text but no bounce → it works, you're done. Bounce and no text → Xfinity blocks
it, and Discord is your channel unless you upgrade below.

### Upgrading to real SMS (if the free route fails)

Twilio is a service that rents you a phone number and sends texts via API.
Since 2023 US carriers require every automated sender to register ("A2P
10DLC"), which is why there's no free tier that reaches US numbers.

Cost: **$4** one-time brand + **$15** campaign vetting + **~$3–4/month**.
Approval takes 1–3 days. You must be 18+ with a payment method, so a parent
may need to own the account — the texts still come to your phone either way.

The code is already written. To switch on:
1. Register at twilio.com, complete sole-proprietor A2P 10DLC, buy a number.
2. Add secrets `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`.
3. In `config/settings.yml` set `twilio.enabled: true` and fill `from_number`.

---

## Tuning what you get alerted about

Everything lives in **`config/watchlist.yml`** and **`config/settings.yml`** —
edit, commit, push. No code changes.

`watchlist.yml` — people who alert on **every** trade at any size. It ships
with a seed list of frequently-cited filers; **replace it with yours.** Name
matching is order-independent, so `Nancy Pelosi`, `PELOSI NANCY`, and
`Hon. Pelosi, Nancy` all match the same person.

`settings.yml` — the thresholds for people *not* on your list:

- `min_insider_buy_usd` (default **$1,000,000**) — open-market buys only
- `min_insider_sell_usd` (default **$10,000,000**) — sells are weaker signal
- `min_congress_usd` (default **$50,000**) — congressional filings disclose a
  *band* (`$1,001 - $15,000`), never an exact figure, so this compares against
  the bottom of the band. It's set low on purpose: the entire Congress files
  only ~1,000 transactions a year, so the volume problem that forces the
  insider thresholds high doesn't exist here.
- `cluster_min_insiders` (default **3**) — 3+ insiders buying the same stock
  within a week, at *any* size. Historically the strongest insider signal,
  because it catches conviction that size thresholds miss.

**Why the filtering is aggressive:** Form 4 runs ~1,500 filings/day, and the
vast majority are compensation mechanics — grants vesting (`A`), shares
withheld for taxes (`F`), option exercises (`M`). None of those mean anyone
*chose* to buy. Only code `P` (open-market purchase, own money) and code `S`
(open-market sale) are counted.

### Tune it against real data before trusting it

```bash
python -m src.main --dry-run --backfill 7
```

Replays the last 7 days of real filings and prints exactly what *would* have
been sent, without sending anything or touching state. If that's too many
alerts, raise `min_insider_buy_usd`.

---

## Running locally

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python -m src.main --dry-run --limit 40
```

| Flag | Purpose |
|---|---|
| `--dry-run` | print alerts, send nothing, don't write state |
| `--backfill N` | also sweep the SEC daily index N days back |
| `--limit N` | cap filings per run (safety valve) |
| `--source form4` | run one source only |
| `-v` | verbose logging |

---

## Known limitations

- **Congress is 30–45 days late.** Statutory. Nothing fixes it.
- **GitHub's cron drifts.** Scheduled runs can be 5–15+ minutes late under
  load, so worst case an alert lands ~30 min after filing. The only fix is a
  paid always-on host, which isn't worth it here.
- **Following disclosed trades is not a strategy.** By the time a Form 4 is
  public the market has often already moved. This tells you what happened; it
  does not tell you whether to act.

## Status

- [x] Phase 1 — SEC Form 4 + filtering + Discord + Actions
- [x] Phase 2 — House & Senate congressional filings
- [ ] Phase 3 — SMS verification / Twilio switch
- [ ] Phase 4 — 13F fund tracking

### On parsing the congressional filings

The House publishes trade details only as PDFs, and they fight back. Field
labels are letter-spaced with **NUL bytes**, so "Filing Status:" extracts as
`F\x00\x00\x00\x00\x00 S...`. A free-text "Description" field wraps across
lines that look exactly like asset names. The `(TICKER)` and `[ST]` markers can
land on separate lines. An asset and its transaction sometimes share one line.
Plenty of holdings have no ticker at all (`Aalo Atomics [OI]`).

The parser handles each of these, and correctness is checked by counting raw
transaction rows per page and comparing against what was parsed:
**967 of 967 rows across 100 random filings, zero mismatches.** About 2% of
rows sit inside a description block that swallows the security name; those
alert with the amount and date and a note pointing at the filing.

Roughly 1 filing in 10 is a **scanned paper document** with no extractable
text. Those alert with a link rather than silently vanishing.
