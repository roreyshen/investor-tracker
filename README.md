# Investor Trade Tracker

Watches SEC and congressional filings, texts you when someone worth watching
trades, and scores every trade against the S&P 500 so you can see whether any
of it actually beats just holding the index.

**Live dashboard:** https://investor-tracker-sigma.vercel.app
(also on GitHub Pages at https://roreyshen.github.io/investor-tracker/)

The dashboard carries automated **24-hour, 7-day and 30-day recaps** — what
became public in each window, the biggest trades, who was most active, and
what beat or lost to the index.

Everything here is a public record that the filer is legally *required* to
publish. Reading those filings faster than other people is not insider trading.
This is a data pipeline, not financial advice.

---

## How it fits together

```
GitHub Actions (free, 24/7)          Your Mac (when awake)
  ├─ every 15 min: poll filings        └─ every 5 min: read the queue,
  │    ├─ alerts  -> Discord                 text you via Messages.app
  │    └─ queue   -> state/outbox.json  ◄────┘
  └─ every 6 h: price + score trades
       └─ docs/data.json -> Vercel (auto-deploys on push)
```

Detection lives in the cloud so it never misses a filing. Texting lives on the
Mac because that's the only way to reach your phone for free. The two are
decoupled through a queue in the repo, so your Mac being asleep delays a text
but never loses one.

---

## Speed: what's actually fast

| Source | Who | Real lag |
|---|---|---|
| **SEC Form 144** | Affiliates giving notice of a sale | **before the sale** |
| **SEC Form 4** | Officers, directors, 10%+ owners | **~2 business days** |
| House / Senate PTR | Members of Congress | **30–45 days** (statutory) |
| 13F | Berkshire, Scion, Pershing Square | up to **4.5 months** |

**Form 144 is the earliest signal available.** An affiliate selling restricted
or control stock files it at or before placing the order, so it lands *ahead*
of the trade rather than after it. The catch: it states an *intent* to sell.
The sale can come in smaller, or never happen. Alerts say "intends to sell".

Form 4 is the fastest confirmation of a completed trade. **Congressional data cannot be fast** —
the reporting window is written into the STOCK Act. Anything advertising
"real-time congressional trades" is selling you month-old data.

---

## Getting alerts

### Texts (working)

Your Mac sends them through Messages.app. Already installed and running:

```bash
python3 mac_agent/agent.py --test     # send yourself a test
tail -f mac_agent/agent.log           # watch it work
bash mac_agent/install.sh remove      # stop it
```

Details and tuning: [`mac_agent/README.md`](mac_agent/README.md).

Why not a normal SMS service: `408-748-6507` is on Xfinity Mobile, whose
email-to-SMS gateway is widely reported to bounce, and Verizon retires that
gateway on 2027-03-31. Twilio works but costs ~$4 setup plus ~$3–4/month and
needs an 18+ account. That sender is written and sits behind
`notify.twilio.enabled` if you ever want it.

### Discord (recommended, still to set up)

The Mac only texts while it's awake. Discord is the always-on channel.

1. Discord → your server → hover a channel → ⚙️ → Integrations → Webhooks →
   **New Webhook** → **Copy URL**
2. Add it at **repo → Settings → Secrets and variables → Actions →
   New repository secret**, named `DISCORD_WEBHOOK_URL`

Also worth adding: `SEC_USER_AGENT` = `Rorey Shen roreyshen@gmail.com`. The SEC
bans IPs that don't declare real contact info. It falls back to the value in
`config/settings.yml`, so this is belt-and-braces.

---

## The dashboard

Every trade with a ticker and a date is priced at its trade date and today,
then compared against SPY, DIA and QQQ **over the identical window**. That's
the point: it measures the decision, not the market.

- **A buy** is credited with the stock's return minus the index.
- **A sell** is credited with the *negative* of it — getting out before a drop
  is a good call. Without that sign flip, every correct sell would score as a
  loss.
- **"Win rate" means beat the index**, not merely went up.

You can slice it by person, chamber, sector, industry, company size (including
a penny-stock bucket under $5), and buy vs sell.

**Caveat that matters:** congressional entry prices are the *disclosed trade
date*, which is 30–45 days before you could have known about it. Those returns
are not returns you could have captured. The comparison is still the honest way
to rank filers against each other.

```bash
python scripts/build_site.py --max-tickers 150   # incremental
python scripts/build_site.py --full              # price everything (slow)
python scripts/backfill.py                       # load congressional history
```

Prices come from Nasdaq's public API — no key needed. Stooq now sits behind a
JavaScript proof-of-work wall and Yahoo rate-limits unauthenticated clients
(and blocks cloud IPs hardest, which is exactly where this runs).

---

## Tuning what alerts you

Everything is in `config/watchlist.yml` and `config/settings.yml`. Edit, commit,
push. No code changes.

**`watchlist.yml`** — anyone here alerts on *every* trade at any size. Name
matching is order-independent, so `Nancy Pelosi`, `PELOSI NANCY` and
`Hon. Pelosi, Nancy` all match.

> Read the header of that file before trusting the names. **None of 2025's top
> ten congressional performers were in 2024's top ten.** Last year's winners are
> not a strategy — which is why the dashboard exists: replace the guesses with
> your own measured numbers after a few months.

**`settings.yml`** — thresholds for everyone *not* on your list:

| Setting | Default | Why |
|---|---|---|
| `min_insider_buy_usd` | $1,000,000 | open-market buys only |
| `min_senior_buy_usd` | $250,000 | CEO/CFO buys are stronger signal, so lower bar |
| `min_insider_sell_usd` | $10,000,000 | sells are weaker signal |
| `min_form144_usd` | $5,000,000 | proposed sales; lower bar since it's early |
| `min_congress_usd` | $50,000 | compares the bottom of the disclosed band |
| `cluster_min_insiders` | 3 | 3+ insiders buying one stock in a week, any size |

**Why the filtering is this aggressive:** Form 4 runs ~1,500 filings/day, and
most of it is compensation mechanics — grants vesting (`A`), shares withheld
for taxes (`F`), option exercises (`M`). Nobody *chose* to buy. Only code `P`
(own money, open market) and code `S` count.

Tune it against real data before trusting it:

```bash
python -m src.main --dry-run --backfill 7
```

That replays a week of real filings and prints exactly what *would* have fired,
sending nothing.

---

## Running locally

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python -m src.main --dry-run --limit 40
./.venv/bin/python tests/run_tests.py
```

| Flag | Purpose |
|---|---|
| `--dry-run` | print alerts, send nothing, don't write state |
| `--backfill N` | sweep the SEC daily index N days back |
| `--limit N` | cap filings per run (safety valve) |
| `--source form4` | one source only (`form4`/`form144`/`house`/`senate`/`13f`) |

The **first run alerts on nothing by design** — the SEC feed remembers ~1,100
old filings and dumping those on you would be noise, so run one marks them seen
and starts clean.

---

## Notes from building it

**The House PDFs fight back.** Field labels are letter-spaced with **NUL
bytes**, so "Filing Status:" extracts as `F\x00\x00\x00\x00\x00 S...`. A
free-text Description field wraps onto lines indistinguishable from asset
names. `(TICKER)` and `[ST]` can land on separate lines. An asset and its
transaction sometimes share a line. Many holdings have no ticker at all
(`Aalo Atomics [OI]`) — anchoring on the ticker made *entire filings* parse to
zero rows.

Correctness is measured, not assumed: raw transaction rows are counted per page
and compared against parsed output. **967 of 967 rows across 100 random
filings, zero mismatches.** About 2% of rows sit inside a description block that
swallows the security name; those alert with amount and date plus a link.
Roughly 1 filing in 10 is a **scanned paper document** with no extractable
text — those alert with a link rather than vanishing.

**Bugs that only appeared when running the real pipeline**, not from reading
the code: the Senate source mutated the *shared* HTTP session's User-Agent to a
browser string (efdsearch requires one), which made every subsequent SEC
request return 403 — so 13F worked in isolation and broke in production. And
13F's `sshPrnamt` is nested under `<shrsOrPrnAmt>`; reading it as a direct child
returned 0 shares for every holding, which would have made every quarterly diff
read as "no change" forever.

---

## Status

- [x] SEC Form 4 insider tracking (the fast feed)
- [x] SEC Form 144 — proposed sales, filed *before* they happen
- [x] House & Senate congressional filings
- [x] 13F fund tracking
- [x] Native Mac texting via Messages.app
- [x] Dashboard with S&P/Dow/Nasdaq benchmarking, deployed to Vercel
- [ ] Discord webhook — 2 minutes, see above
- [ ] Let it run a few weeks, then rebuild the watchlist from measured results

## Limitations, stated plainly

- **Congress is 30–45 days late.** Statutory. Nothing fixes it.
- **GitHub cron drifts.** Runs can be 5–15+ min late under load.
- **The Mac only texts while awake.** Discord covers the gap.
- **Following disclosed trades is not a strategy.** By the time a Form 4 is
  public the market has often moved. This tells you what happened; it does not
  tell you whether to act.
