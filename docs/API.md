# TradingAgents HTTP API

Async job API around `TradingAgentsGraph`, meant to run in Docker and be called from Rails (or any HTTP client).

Analyses can take several minutes (many LLM + tool calls), so the API is **submit + poll**, not a single blocking request.

## Quick start

```bash
# local
pip install -e ".[dev]"
export OPENAI_API_KEY=...
export TRADINGAGENTS_API_KEY=change-me   # optional locally; recommended in Docker
tradingagents-api
# → http://0.0.0.0:8000  (docs at /docs)
```

```bash
# Docker
cp .env.example .env   # add LLM keys + TRADINGAGENTS_API_KEY
docker compose up --build tradingagents-api
```

## Auth

If `TRADINGAGENTS_API_KEY` is set, every `/api/v1/*` route requires either:

- `Authorization: Bearer <key>`, or
- `X-API-Key: <key>`

`/health` is always open (for Docker healthchecks / load balancers).

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Liveness |
| `POST` | `/api/v1/analyses` | Start analysis → `202` + job id |
| `POST` | `/api/v1/context/check` | Dry-run the `context` checks: free, no model call |
| `GET` | `/api/v1/analyses` | List jobs in this process (newest first) |
| `GET` | `/api/v1/analyses/{id}` | Job status / result |

With `TRADINGAGENTS_API_MAX_WORKERS=1` (default), only **one** analysis runs at a time.
Extra POSTs stay `queued` until the active job finishes (often many minutes).
`POST` always returns `queued`; poll `GET` (or list) for `running` / `completed` / `failed`.

OpenAPI UI: `http://localhost:8000/docs`

### Create analysis

```http
POST /api/v1/analyses
Content-Type: application/json
X-API-Key: change-me

{
  "ticker": "PLTR",
  "trade_date": "2026-07-26",
  "analysts": ["market", "social", "news", "fundamentals"],
  "asset_type": "stock",
  "max_debate_rounds": 1,
  "max_risk_discuss_rounds": 1
}
```

Optional overrides: `llm_provider`, `deep_think_llm`, `quick_think_llm`, `output_language`, `save_reports`.

Response `202`:

```json
{
  "id": "uuid",
  "status": "queued",
  "ticker": "PLTR",
  "trade_date": "2026-07-26",
  "links": { "self": "/api/v1/analyses/uuid" }
}
```

### Poll job

Statuses: `queued` → `running` → `completed` | `failed`.

On `completed`, the body includes `decision` (e.g. `BUY`) and `reports` (markdown sections matching the CLI report files).

### Supplying verified data (`context`)

`POST /api/v1/analyses` accepts an optional `context` with data your app has
already verified. Every agent reads it as the source for those figures, in
preference to what the service fetches itself, and cites it by source and period.
Only the blocks below are accepted; anything else (`technicals`, `instructions`,
...) returns `422`. The shape is in [`context.schema.json`](context.schema.json).

```json
"context": {
  "schema_version": 1,
  "financials": {
    "ticker": "MU", "currency": "USD", "as_of": "2026-11-04", "fiscal_year_end": "2026-09-03",
    "annual": [{
      "fiscal_period": "FY2026", "period_start": "2025-09-05", "period_end": "2026-09-03",
      "kind": "annual", "source": "sec_xbrl", "filed_at": "2026-10-20",
      "income": { "revenue": 133188000000, "gross_profit": 107504000000,
                  "operating_income": 99340000000, "net_income": 84969000000,
                  "eps_basic": 75.62, "eps_diluted": 74.91 },
      "cash_flow": { "net_cash_flow_from_operations": 89700000000,
                     "capital_expenditure": 30700000000, "free_cash_flow": 59000000000 }
    }],
    "quarterly": [{
      "fiscal_period": "Q4 FY2026", "period_start": "2026-05-29", "period_end": "2026-09-03",
      "kind": "standalone", "source": "sec_xbrl", "income": { "revenue": 54225000000 }
    }],
    "balance": [{
      "fiscal_period": "FY2026", "as_of": "2026-09-03", "source": "sec_xbrl",
      "total_assets": 195888000000, "total_liabilities": 57510000000,
      "shareholders_equity": 138378000000, "cash_and_equivalents": 38400000000,
      "current_assets": 90000000000, "current_liabilities": 25000000000
    }]
  },
  "price": { "ticker": "MU", "currency": "USD", "source": "getmarketalerts",
             "as_of": "2026-11-04", "close": 795.23 },
  "technicals": {
    "ticker": "MU", "currency": "USD", "source": "getmarketalerts", "as_of": "2026-11-04", "close": 795.23,
    "sma_20": 914.9, "sma_50": 972.08, "sma_200": 753.73, "ema_10": 878.2, "rsi_14": 35.38,
    "macd": { "line": -38.89, "signal": -29.12, "histogram": -9.77 },
    "volume": 35400000, "volume_avg_50d": 21800000, "high_52w": 1213.56, "low_52w": 201.37
  },
  "valuation_history": {
    "ticker": "MU", "currency": "USD", "source": "getmarketalerts", "as_of": "2026-11-04",
    "pe_ttm": { "latest": 21.5, "min_5y": 8.1, "median_5y": 14.2, "max_5y": 38.0, "percentile_5y": 0.88, "years": 5 },
    "peg": 0.29
  },
  "market_backdrop": {
    "source": "getmarketalerts", "as_of": "2026-11-04",
    "vix": { "value": 22.4, "as_of": "2026-11-04" },
    "fear_and_greed": { "value": 31, "label": "Fear", "as_of": "2026-11-04" },
    "buffett_indicator_pct": { "value": 215, "as_of": "2026-09-30" },
    "credit_spread_hy_bps": { "value": 310, "as_of": "2026-11-03" }
  },
  "prior_reports": [
    { "ticker": "MU", "as_of": "2026-10-12", "decision": "Overweight", "target": 1100.0,
      "price_then": 950.1, "horizon": "3-6 months" }
  ],
  "revenue_breakdown": {
    "ticker": "GOOG", "currency": "USD", "source": "financial_datasets", "as_of": "2026-11-04",
    "fiscal_period": "Q3 2026", "period_end": "2026-09-30", "kind": "standalone",
    "total_revenue": 100000000000, "prior_year_total": 80000000000,
    "views": [{ "dimension": "product", "segments": [
      { "name": "Search & other", "revenue": 50000000000, "prior_year_revenue": 45000000000,
        "includes": "Search and ads on other Google properties" },
      { "name": "Cloud", "revenue": 25000000000, "prior_year_revenue": 12000000000 },
      { "name": "YouTube ads", "revenue": 10000000000, "prior_year_revenue": 9000000000 },
      { "name": "Network", "revenue": 8000000000, "prior_year_revenue": 8000000000 },
      { "name": "Other Bets", "revenue": 1000000000 },
      { "name": "Hedging gains", "revenue": 6000000000, "prior_year_revenue": 6000000000 }] }],
    "footnotes": ["Workspace revenue is reported inside Cloud."]
  },
  "theme_backdrop": {
    "name": "AI & semis", "source": "getmarketalerts", "as_of": "2026-11-06",
    "basket": ["NVDA", "AVGO", "AMD", "TSM", "ASML", "MU", "MRVL", "ARM", "AMAT", "LRCX", "KLAC", "SMCI", "PLTR",
               "MSFT", "GOOGL", "META", "AMZN", "ORCL"],
    "basket_size": 18, "basket_size_used": 18,
    "below_200d_pct": 77.8, "below_50d_pct": 88.9, "rsi_below_30_pct": 33.3,
    "avg_drawdown_from_52w_close_high_pct": 31.5,
    "return_1m_pct": -14.2, "return_3m_pct": -9.0,
    "benchmark": { "ticker": "SPY", "return_1m_pct": -2.1, "return_3m_pct": 3.4 },
    "sector_etf": { "ticker": "SMH", "return_1m_pct": -16.0, "return_3m_pct": -8.5 },
    "put_call_ratio": { "value": 1.1, "as_of": "2026-11-06" }
  },
  "earnings_release": {
    "ticker": "ISRG", "source": "sec_edgar", "form": "8-K", "filed_on": "2026-10-20", "period_end": "2026-09-30",
    "accession": "0001035267-26-000067",
    "url": "https://www.sec.gov/Archives/edgar/data/1035267/000103526726000067/q326ex-991earningsrelease.htm",
    "text": "Intuitive Announces Third Quarter Earnings ..."
  },
  "ownership": {
    "ticker": "MU", "currency": "USD", "as_of": "2026-11-04",
    "funds": { "source": "sec_13f", "quarter_end": "2026-09-30", "managers_tracked": 24,
      "holders": [{ "manager": "David Tepper", "firm": "Appaloosa", "shares": 1200000, "value_usd": 954000000,
                    "weight_pct": 4.1, "shares_change": 300000, "shares_change_pct": 33.3, "action": "added",
                    "filed_on": "2026-10-30", "estimated_cost": 640.0 }] },
    "insiders": { "source": "sec_form4", "window_days": 90, "buys": 0, "sells": 1, "net_value_usd": -4100000,
      "trades": [{ "name": "Jane Doe", "title": "Director", "is_director": true,
                   "transaction_date": "2026-10-02", "filing_date": "2026-10-04", "kind": "sale",
                   "shares": 5000, "price_per_share": 820.0, "value_usd": 4100000,
                   "shares_owned_after": 40000, "form_type": "4",
                   "under_10b5_1_plan": true, "from_option_exercise": false }] },
    "congress": { "source": "congress_disclosures", "window_days": 90, "buys": 1, "sells": 0,
      "trades": [{ "member": "Dan Newhouse", "chamber": "House", "district": "WA04", "owner": "spouse",
                   "side": "buy", "traded_on": "2026-07-10", "disclosed_on": "2026-08-15",
                   "amount_min": 1001, "amount_max": 15000 }] }
  },
  "company": {
    "ticker": "MU", "currency": "USD", "source": "financial_datasets", "as_of": "2026-11-04",
    "name": "Micron Technology, Inc.", "sector": "Technology", "industry": "Semiconductors",
    "exchange": "NASDAQ", "market_cap": 898000000000, "shares_outstanding": 1128000000
  },
  "events": {
    "ticker": "MU", "currency": "USD", "as_of": "2026-11-05", "source": "financial_datasets",
    "next_earnings": { "date": "2026-12-17", "fiscal_period": "Q1 FY2027", "confirmed": false },
    "recent_earnings": [{
      "fiscal_period": "Q4 FY2026", "period_end": "2026-09-03", "reported_on": "2026-10-09",
      "eps_actual": 12.5, "eps_estimate": null,
      "revenue_actual": 54229000000, "revenue_estimate": null
    }]
  },
  "notes": "Optional free text. Unverified: context only, never a source for a figure."
}
```

**Shape rules**

- `ticker` is required on `financials` and `price` and must match the request's
  `ticker`; `currency` is required and is `USD` for now.
- Amounts are **whole dollars as integers** (`133188000000`, not `133188000000.5`).
  Per-share fields (`eps_basic`, `eps_diluted`) and `price.close` are decimals.
  **Unknown is `null`, never `0`**: a zero where a value is required (gross profit,
  operating income, net income, capex, free cash flow, liabilities, equity) is
  rejected as a missing value.
- `source` is one of `sec_xbrl`, `financial_datasets`, `getmarketalerts`, and is set
  on each statement, balance sheet and price; there is no source for the whole block.
  One statement per period, so there is nothing to rank sources against.
- `kind` says how long a statement is: `standalone` (a quarter, about 60-120 days),
  `ytd` (120-300) or `annual` (300-400).
- `period_start` is optional on `annual`. A `standalone` quarter may leave it out
  only if another supplied quarter ends 60-120 days before it; the service then
  derives the length, and never guesses a start. The earliest quarter and every `ytd`
  row must send it.
- At most 10 annual, 16 quarterly and 26 balance rows. One balance sheet per date;
  where it falls on a statement's `period_end`, it must carry the same
  `fiscal_period` label as one of the statements ending that day.
- `technicals` are the levels your chart shows. When sent, they are the readings the plan is
  checked against (its triggers, the "already true" test, the figures the summary may cite
  as verified), so the report and your chart agree. Only `close` is required: anything you
  leave out (an average, RSI, the MACD histogram) is filled from the service's own readings,
  and if that fails too your values stand alone. With no `technicals` the readings are the
  service's own. `close` must be within 0.5% of `price.close` when both are dated the same
  day; `macd.histogram` must be `line` less `signal`; `close` must lie inside the 52-week
  range; and `as_of` must be within 7 days before the run date.
- `earnings_release` is the company's earnings press release (the latest 8-K with Item 2.02, Exhibit
  99.1, filed on or before the run date) as plain text, up to 120,000 characters; leave the block out when
  there is none. One model call reads it into at most six notable items: one-time charges and gains with their
  amounts, the company's own explanation of large changes, and guidance changes. An item is kept only if its
  quote appears in your text word for word and any amount it states is an amount the quote states, so what the
  analysts and the summary see has been checked against the filing. The raw text is never passed to them. The
  amounts can be cited in the summary as figures with `source: "filing"`. The text is part of `context_digest`.
  `url` must start `https://www.sec.gov/Archives/`, `accession` must look like `0001035267-26-000067`, `source`
  is `sec_edgar`, and `form` is `8-K` or `6-K`. A release filed more than 130 days before the run date means a
  newer one was missed (`release_stale`); text that still has HTML tags is refused (`text_not_plain`). This
  step runs by default; `TRADINGAGENTS_RELEASE_NOTES=0` turns it off.
- `revenue_breakdown` is where one period's revenue came from, cut up to three ways (`views`; each
  `dimension` is `product`, `segment` or `geography`). Each bucket carries `revenue` and, if the company
  gives it, `prior_year_revenue` (the same period a year earlier; `null` if not, never 0). Send the buckets
  the company reports, including negative lines such as hedging or eliminations, so they add to
  `total_revenue` within 1% (`revenue_breakdown_total`). The service works out each bucket's share of the
  total and its growth in code and returns the table as `revenue_breakdown` on the job (see below), so you
  can render it without asking a model. The fundamentals analyst reads it and is told to say which buckets
  make up the business and which drives growth; the decision-makers read a one-line mix. It will not split a
  bucket or estimate a product the company does not report on its own line (Workspace, Gemini, Waymo and
  the like): put what the company says about that in `footnotes` (up to 5, 200 characters each). `includes`
  and `footnotes` are passed on as unverified text. Bucket revenues, shares and growth can be cited in the
  summary as verified figures. With `financials`, a total that differs by more than 1% from that period's
  revenue is refused (`revenue_breakdown_vs_financials`).
- `theme_backdrop` says how a theme's basket of stocks is doing, so one stock's chart can be read against
  it ("AI & semis" is weak across the board, or only this name is). The caller owns the basket: a fixed list of
  3 to 40 tickers, never tuned per request. Send the measures you can compute from daily closes; each is
  optional and `null` means unknown (never 0): `below_200d_pct`, `below_50d_pct`, `rsi_below_30_pct` (shares of
  the basket, 0-100), `avg_drawdown_from_52w_close_high_pct` (from the 52-week CLOSING high, not the intraday
  high; a positive number), the equal-weight `return_1m_pct` and `return_3m_pct`, and the same two returns for
  `benchmark` (the market, e.g. SPY) and `sector_etf` (e.g. SMH). `put_call_ratio` is a market-wide value, not
  per stock. `basket_size` (optional) is your own count of the list and must equal it. `basket_size_used` is how many
  listed names had enough history to measure; count every share on those same names, because each share must be a
  whole number of them (`share_not_whole_names`, within 0.5 points of rounding), and `as_of` is the
  OLDEST latest-close date among them (more than 7 days before the run is `theme_backdrop_stale`). There is
  no per-stock positioning here: leverage, crowding, short interest and fund flows are not measured, and the
  agents are told never to claim anything about them. The service counts in code how many of five washout
  signals are met, from fixed thresholds: at least 70% of the basket below its 200-day average, at least 30%
  with RSI under 30, an average drawdown of at least 25%, a basket return of -10% or worse over a month, and
  trailing the market by 8 points or more over a month. A signal with no input is "not measured", never
  "met". The market analyst reads the block and the decision-makers a one-line digest. They may call the theme
  washed out or capitulated only if at least 3 signals are met, naming each figure; even then it is not a buy
  signal. The shares, returns and signal count can be cited in the summary as verified figures.
- `valuation_history` is the stock's P/E against its own past. `pe_ttm` is price over
  trailing-twelve-month EPS; the service's own `pe_latest_annual` is price over the latest fiscal
  year's diluted EPS. They are named apart and the agents are told never to mix them. The range
  must be ordered and contain `latest`, and the percentile must fit where `latest` sits (a P/E at
  the 5-year maximum cannot be at the 50th percentile). The fundamentals analyst reads it, and the
  decision-makers read a one-line digest.
- `market_backdrop` holds market-wide measures, each with its own `as_of`; none may be after the
  block's. A `fear_and_greed` label must fit its value (0-24 Extreme Fear, 25-44 Fear, 45-55 Neutral,
  56-75 Greed, 76-100 Extreme Greed). The news analyst reads it, and the agents are told to state
  each reading's date and never call an older one current.
- `prior_reports` (up to 3, newest first) are the service's own earlier outputs on this company; each
  carries `ticker`, which must match the request. Only the summary reads them, so the analysts are not
  anchored on an old rating. The summary says how the view changed, in plain labels ("Weak Buy"), and
  the service works out in code how far the price has moved and whether that is toward or away from the
  old target. A buy-side rating needs a target above `price_then` and a sell-side one below it. Each
  report must be dated before the run date.
- Each insider trade may carry `under_10b5_1_plan` (the Form 4 checkbox) and `from_option_exercise`, both
  strict `true` / `false`, omitted or `null` when unknown. The service works out in code how much of their
  direct holding a sale was, `shares / (shares + shares_owned_after)`, and the agents are told that share, the
  plan status and any option exercise, so a small sale under a pre-arranged plan is not mistaken for an
  executive leaving. Nothing is stated about a flag you did not send.
- `ownership` has three optional parts: `funds` (13F positions, up to 10 holders), `insiders`
  (Form 4 open-market buys and sales, up to 20 trades) and `congress` (up to 20 trades). Nothing may
  be dated after `as_of`; the insider window runs on `transaction_date` and the Congress window on
  `disclosed_on`, because disclosures lag. A holder's `action` must agree with the sign of
  `shares_change` (`new`: the change is the whole position; `exited`: shares 0), and its percentage with
  the shares. The `buys` and `sells` counts must cover every listed trade and equal the lists when fewer
  than 20 are listed; `net_value_usd` is checked against the list only when the list is complete.
  The agents are told: a 13F is a quarter-end position filed up to 45 days later, never "recent
  buying"; funds cover only the managers you track ("of the funds we track"); `estimated_cost` is an
  estimate; Congress amounts are ranges disclosed up to 45 days late; insider value counts open-market
  trades only. Fund managers are named in the report; insiders are described by title and members of
  Congress by chamber and relationship, never by name. The sentiment analyst reads the full block and the
  decision-makers a one-line digest. Counts and totals can be cited as verified figures.
- `company` gives the name, sector, industry, exchange, `market_cap` and
  `shares_outstanding` (all optional except the identity fields). With `price`, the market
  cap must be within 3% of price times shares, or the request is refused with
  `market_cap_mismatch`. `financials.balance[].total_debt` and
  `financials.annual|quarterly[].income.depreciation_and_amortization` are optional and
  unlock the metrics below; `total_debt` may not exceed `total_liabilities`.
- If you send no `events.next_earnings`, the service looks up Yahoo's date for the next report
  and, when it is after the run date, uses it as `next_earnings` with `confirmed: false`; the
  agents are told it is Yahoo's estimate. This adds an `events` block with `source:
  "yahoo_finance"` when you sent none, or fills only `next_earnings` when you sent `events`
  without one. A date you send is never replaced. If the lookup fails or finds no future date,
  nothing is added and earnings items stay undated. Set `TRADINGAGENTS_ESTIMATE_NEXT_EARNINGS=0`
  to turn it off. `yahoo_finance` is also accepted as a `source`.
- `events` gives the next earnings date and up to four recent reports (estimates may be
  `null`). When it is sent, a dated `plan.watch` item about reporting (one that mentions earnings,
  a print, results, guidance, a filing or 10-Q/10-K, a quarterly, annual or next report, or a
  fiscal period like "Q4 FY2026") may carry only `next_earnings.date` or no date. With no `events`, or a null `next_earnings`, such an item may carry no date at
  all, so a model cannot invent one. This applies to every request, with or without `context`.
- `notes` is shown to the agents as unverified context, with a length limit.

**Checks.** A request is refused, before any model call, if:

| `code` | When |
|---|---|
| `ticker_mismatch` | `financials` or `price` is for a different company than the request (or than each other) |
| `missing_period_start` | a `ytd` row, or a quarter that cannot be measured from an earlier one, has no start |
| `period_length` | a period's length does not match its `kind` |
| `period_after_as_of` | a period or balance date is after the block's `as_of` |
| `as_of_after_trade_date` | `financials.as_of` is after the request's `trade_date` |
| `filed_before_period_end` | `filed_at` is before the period ended |
| `duplicate_period` | two statements share a `period_end`, or two balance sheets a date |
| `quarters_mismatch_year` | four quarters do not add up to the year (within 1%) |
| `quarters_exceed_year` | fewer quarters already exceed the year |
| `balance_mismatch` | assets differ from liabilities plus equity |
| `balance_order` | current assets or liabilities exceed their totals |
| `balance_label_mismatch` | a balance sheet's label disagrees with the statement ending that day |
| `fcf_mismatch` | free cash flow differs from operating cash flow less capex (either sign for capex) |
| `income_order` | gross profit, operating income or net income exceed what they should |
| `zero_placeholder` | a value that looks like a missing value was sent as `0` |
| `earnings_date_passed` | `events.next_earnings.date` is not after the block's `as_of` |
| `reported_before_period_end` / `reported_after_as_of` | a recent report is dated before its period ended, or after `as_of` |
| `market_cap_mismatch` | `company.market_cap` is more than 3% from price times shares |
| `technicals_date` | `technicals.as_of` is after the run date or more than 7 days before it |
| `technicals_price_mismatch` | `technicals.close` differs from `price.close` by over 0.5% on the same date |
| `macd_inconsistent` | the MACD histogram is not the line less the signal |
| `range_inconsistent` | the close is outside the supplied 52-week range |
| `date_after_as_of` | a holder, trade or disclosure date, or a quarter end, is after the block's `as_of` |
| `filed_before_period_end` | a 13F is dated before its quarter ended (also used for statements) |
| `holder_change_inconsistent` / `managers_tracked_too_low` | a fund's action, shares and change disagree, or more holders are listed than tracked |
| `filing_before_transaction` / `disclosed_before_traded` | a filing or disclosure predates its trade |
| `trade_outside_window` / `trade_value_mismatch` | a trade falls outside its window, or its value is not shares times price |
| `insider_count_mismatch` / `insider_net_mismatch` / `congress_count_mismatch` | counts or net value disagree with the listed trades |
| `amount_range_inverted` | a Congress `amount_min` exceeds `amount_max` |
| `revenue_breakdown_total` / `revenue_breakdown_prior_total` | a view's buckets do not add to the reported total, or its prior-year buckets to the prior-year total |
| `revenue_breakdown_vs_financials` | the breakdown's total differs from the financials' revenue for the same period |
| `segment_duplicate` / `breakdown_period_after_as_of` | a bucket name is repeated in a view, or the period ends after the data's date |
| `basket_duplicate` / `basket_size_inconsistent` | a ticker is listed twice in the basket, or more names were measured than are listed |
| `share_not_whole_names` | a basket share is not a whole number of the names measured |
| `theme_backdrop_stale` | the basket is dated more than 7 days before the run date |
| `valuation_range_inconsistent` / `percentile_inconsistent` | `pe_ttm` is unordered, outside its own range, or its percentile does not fit |
| `label_inconsistent` | a Fear & Greed label does not fit its value |
| `prior_after_run` / `prior_order` / `prior_target_inconsistent` | a previous report is not before the run, not newest first, or its target is on the wrong side of its price |
| `release_stale` / `text_not_plain` | the earnings release is over 130 days before the run date, or its text still has HTML tags |
| `price_date` | the price is after the run date or more than 7 days before it |

A passing request is internally consistent. That does not make it true: the checks
cannot know whether the source matches the filings.

**Failures are machine-readable.** The `422` carries
`detail[0].ctx.problems = [{"code", "path", "message"}, ...]`, where `path` points at the
value, e.g. `financials.annual[0].income.net_income`. Because the blocks are
independent, drop the failing block (`financials` or `price`) and resend: a bad context
never has to cost a report. A malformed context (wrong type, unknown key) is also a
`422`, in the usual FastAPI form.

**What the service computes.** Margins, capex to revenue, trailing revenue,
year-over-year growth, liabilities to equity, working capital, and price to earnings
(price over the latest annual diluted EPS) come from your values. Do not send them.
From `technicals` it computes how far the close is from each average and the 52-week high,
and volume against its 50-day average. With `company` and `total_debt` it also computes net debt, enterprise value, debt to
equity and, with D&A, EV to EBITDA; with a market cap (yours, or price times shares) it
computes the free cash flow yield. Without `total_debt` there is no debt-to-equity, only
liabilities to equity, and the agents are told not to calculate one.

**Digest.** The `202`, the job and `/context/check` return `context_digest`:
`sha256:` of the context as the service parsed it, as canonical JSON (sorted keys, no
whitespace, nulls kept). Store it with the job to record what a report was built on.

**Dry run.** `POST /api/v1/context/check` runs every check above without starting
an analysis and costs nothing. Send the `context` object as the body; add
`?ticker=MU&trade_date=2026-11-04` to apply the ties to the request as well. It
returns `{"ok": false, "problems": [...], "metrics": {...}, "context_digest": "..."}`:
a well-formed context that fails a check is a `200` with every problem listed.

**Versions.** `schema_version` changes only for a breaking change: a removed or
renamed field, a new required field, or a tighter rule. New optional fields, new blocks
and new enum values are additive and keep the version. `/health` lists the versions the
service reads in `context_schema_versions`. The schema file is generated from the same
models that validate requests, and a test fails if it is stale.

### Revenue table

When the request sent `context.revenue_breakdown`, every poll of the job carries the computed table, ready to
render as a table or chart (it is there from `queued`, since it is worked out from the request):

```json
"revenue_breakdown": { "fiscal_period": "Q3 2026", "period_end": "2026-09-30", "source": "financial_datasets",
  "total_revenue": 100000000000, "total_yoy_pct": 25.0, "footnotes": ["Workspace revenue is reported inside Cloud."],
  "views": [{ "dimension": "product", "rows": [
    { "name": "Cloud", "revenue": 25000000000, "share_pct": 25.0, "yoy_pct": 108.3, "includes": null }] }] }
```

`share_pct` is the bucket over `total_revenue`, so rows with negative lines (hedging) can add to a little
over or under 100. `yoy_pct` is `null` when no year-ago figure was sent. It is `null` too when the year-ago
figure is zero or negative, because growth from there is undefined.

### Theme signals

When the request sent `context.theme_backdrop`, every poll of the job carries the signal count, counted by the
service (it is there from `queued`):

```json
"theme_backdrop": { "name": "AI & semis", "as_of": "2026-11-06", "basket_size": 18, "basket_size_used": 18,
  "names_below_200d": 14, "names_below_50d": 16, "names_rsi_below_30": 6,
  "relative_1m_pct": -12.1, "relative_3m_pct": -12.4, "signals_met": 5, "signals_measured": 5,
  "signals": [{ "name": "most of the basket is below its 200-day average", "met": true, "value": 77.8,
                "threshold": 70.0, "direction": "at least" }] }
```

`signals_measured` counts only the signals whose inputs were sent. `relative_*` is the basket's return minus
the market's, in points, and is `null` without a `benchmark`.

### Progress, usage and restarts

While a job is `running`, the poll response carries `progress`:

```json
"progress": { "stage": "debate", "stage_index": 2, "stages_total": 5,
              "started_at": "2026-11-05T14:02:11Z", "analysts_done": ["market", "news"], "eta_seconds": 140 }
```

`stage` is one of `analysts`, `debate`, `decision`, `plan`, `summary` (a run without the plan or summary
step has fewer: see `stages_total`). The four analysts run in parallel, so they are one stage, and
`analysts_done` says which have finished. A stage never goes backwards. `eta_seconds` is the time left,
the remaining stages at their median duration over the last 20 finished jobs, kept across restarts. It
is `null` until there is history, before a job starts and once it is done. `progress` is `null` when the
job is queued, completed or failed.

While a job is `queued`, `queue` says where it stands:

```json
"queue": { "position": 2, "jobs_ahead": 2, "starts_in_seconds": 1500, "eta_seconds": 1900 }
```

The service runs `TRADINGAGENTS_API_MAX_WORKERS` reports at once (default 1) and the rest wait in the order they
were submitted. `jobs_ahead` counts the jobs running or waiting in front. `starts_in_seconds` and `eta_seconds`
(until it finishes, queue included) come from the typical length of recent finished jobs and the time left on
the ones running, and are `null` until there is history. `queue` is `null` once the job starts. A report takes
about 8 to 11 minutes to run, and the wait in line is on top of that, so a client's timeout should count from
`started_at`, or from submission with room for the queue, and should fetch the job before deciding it has timed
out.

A finished job carries `usage`: `calls`, `input_tokens`, `output_tokens` and an estimated `cost_usd` for each
stage, and `total_cost_usd`. A cost is `null` when a model has no known price, never a wrong number.

Jobs are kept on the service's persistent disk (SQLite, the newest 500), so a job id still answers after a
restart or a deploy. A job that was `queued` or `running` when the service stopped cannot be resumed: it
comes back as `failed` with the error "the service restarted while this analysis was running; submit it
again". A `failed` job's `error` is always a fixed message; the detail is in the service log.
`GET /health` reports `jobs_persisted` and `storage_boots` (how many times the service has started on this
storage; 2 or more means the disk persists). If the folder cannot be written the service logs a warning,
runs in memory and `jobs_persisted` is `false`. The location is `TRADINGAGENTS_JOBS_DB_PATH` (default
`~/.tradingagents/jobs/jobs.db`).

### Rating in plain words

`decision` is the internal rating (`Buy`, `Overweight`, `Hold`, `Underweight`, `Sell`) and
does not change. For retail readers the job also carries `rating_label` and
`rating_meaning`, from a fixed table in code (never written by the model):

| `decision` | `rating_label` | `rating_meaning` |
|---|---|---|
| `Buy` | Strong Buy | Open a position or add to the one you have. |
| `Overweight` | Weak Buy | Build a position gradually, in stages, rather than all at once. |
| `Hold` | Hold | Keep what you have. Don't add and don't sell. |
| `Underweight` | Weak Sell | If you own it, sell some of it. If you don't, don't buy it yet. |
| `Sell` | Strong Sell | If you own it, sell it. If you don't, stay away. |

Both are `null` when `decision` is `REVIEW` or anything else. Show the label, not
`decision`. The summary and the plan are written in the same plain terms, about "your
account" and "your position", and never use "overweight", "underweight", "standard
weight" or "model portfolio".

### Summary

A completed job also carries `summary`: about 450 words (never more than 550) in one analyst's voice,
written for a retail reader. It covers the rating and what to do now, what decided it, the risks and what
would change the view, and the plan's levels. It is written in plain words, in short paragraphs that each
start with a bold lead-in, with rounded numbers ("$133 billion", "64%") and no source tags or filing dates in
brackets: each figure's exact value, source and period are in `summary_figures`, which the page shows
beside it. It is written by one more model call after the decision. If none fits the length limit after
retries, `summary` is `null`; the reports are still valid. Set `TRADINGAGENTS_REPORT_SUMMARY=0` to turn the
step off.

`glossary` lists the defined market terms the summary uses ("RSI", "Free cash flow", "Moving average", ...),
in the order they first appear, each with a one-sentence plain meaning (`[{"term", "meaning"}]`). The meanings
are fixed text in the service, found in the summary by code, so a term means the same on every report.
Show them as tooltips or a short list. It is `null` when there is no summary.

`reports` and the saved report files are the appendix: the analysts' full text and the internal debate,
written for the record rather than the customer. They still name internal roles ("bull", "aggressive
analyst"), so show `summary`, not them. Treat the text as untrusted model output and escape it when you
show it.

**Checked figures.** The summary declares every dollar amount, percentage and multiple
it states, with its exact value. `summary_figures` lists them, each with `label`, `value`, `unit` (`usd`,
`usd_millions`, `usd_billions`, `pct`, `x`, `number`), `source` (`context`, `readings`,
`plan`, `filing`, `analyst_report`, `derived`) and `period`, plus `verified`. `verified` is true only
when the figure was checked in code against your supplied `context`, the service's own
market readings, the position plan or the earnings-release items (within 1%, for rounding). Figures from the analyst
reports or the model's own calculations are never marked verified. If a figure still fails
after the model's retries, the summary is returned anyway and `summary_problems` lists
`{code, subject, message, severity}` for each. `severity: "error"`: `figure_not_in_source`
(a figure claimed from verified data is not in it) or `too_long`. `severity: "warning"`:
`undeclared_figure` (a dollar, percent or multiple appears in the text but not in the list),
which only means that figure is unverified, never that it claims verification. A summary that declares fewer than half of the figures it states fails a check
(`figures_not_declared`, an error) and is retried; a warning alone is never retried. It also gets warnings,
never retries, for sentences that average over 22 words (`long_sentences`) or paragraphs of more than five
sentences or 90 words (`long_paragraph`). Hide the
summary or show a warning when any problem has `severity: "error"`; warnings need no action. Both fields are `null` when the provider could not return structured output.
This cannot see a bare number with no `$`, `%` or `x`, or a wrong figure within 1% of a real one.

### Position plan

A completed job also carries `plan`: entries and exits as data, checked against
verified market data before it is returned. When no plan passes the checks,
`plan` is `null` and `plan_error` says why. The report and `decision` are still
valid, so your app decides whether a missing plan costs the customer a credit
or is retried.

```json
{
  "plan": {
    "as_of_price": 247.19,
    "as_of_date": "2026-11-02",
    "horizon": "3-6 months",
    "now": { "portfolio_pct": 2.5, "summary": "Hold a moderate position; add on dips." },
    "entries": [
      { "portfolio_pct": 3.0, "reason": "200-day support",
        "trigger": { "price": { "when": "at_or_below", "value": 245.0 } } },
      { "portfolio_pct": 4.0, "reason": "Oversold washout",
        "trigger": { "all_of": [
          { "indicator": "rsi_14", "when": "below", "value": 30 },
          { "price": { "when": "at_or_below", "value": 237.0 } } ] } }
    ],
    "exits": [
      { "kind": "take_profit", "portfolio_pct": 1.5, "reason": "Capex-payoff target",
        "trigger": { "price": { "when": "at_or_above", "value": 300.0 } } },
      { "kind": "stop", "portfolio_pct": 0.0, "reason": "Uptrend support fails",
        "trigger": { "indicator": "close_vs_sma_200", "when": "weekly_close_below" } }
    ],
    "scenarios": [
      { "name": "bull", "price": 330, "probability": 0.25,
        "driver": "Capex pays back faster than feared", "basis": "Judgment: needs two clean quarters." },
      { "name": "base", "price": 300, "probability": 0.5,
        "driver": "Growth holds, margins stay near today's level", "basis": "Judgment: the current run-rate." },
      { "name": "bear", "price": 210, "probability": 0.25,
        "driver": "Demand slows while spending continues", "basis": "Judgment: a repeat of the last downturn." }
    ],
    "expected_return_pct": 15.3,
    "target": { "price": 300.0, "method": "base_scenario", "basis": "Base-case value in 6 months." },
    "if_you_own": "Hold; add only on the entry levels above.",
    "if_you_dont": "Start small now and build on the entry levels.",
    "watch": [
      { "what": "Stock reclaims its 50-day average", "when": "2026-12-10", "effect": "supports" }
    ],
    "confidence": "medium",
    "confidence_reason": "Strong fundamentals, but capex return is unproven."
  },
  "plan_error": null
}
```

**What the numbers mean.** The plan is for a long-only *model portfolio*, not
the customer's holdings. `portfolio_pct` is the total position as a share of an
equity portfolio after the trigger fires (not an increment), between 0 and 10.
`now` is the recommended size today.

**The decision fields.**

- `target` is the one price the plan stands behind. `method` is `base_scenario` (the
  price equals the base scenario), `valuation` (the `basis` names the earnings or cash
  flow and the multiple behind it) or `technical_reference` (a level being watched,
  explicitly not a fair value). A moving average cannot be a `valuation` or
  `base_scenario` target.
- `expected_return_pct` is computed by the service from the scenarios against the
  verified price, in percent. It is not model output.
- Each scenario has a `driver` (what would have to happen) and a `basis` for its
  probability, which says so when it is a judgment.
- `if_you_own` and `if_you_dont` give one action each, so a trim is never read as a short.
- `watch` lists one to five measurable things that would change the view: `effect` is
  `supports` or `weakens` the plan's view, and `when` is a date after the plan date when
  it has one.

**Triggers** come from a closed list; nothing else is accepted. A trigger is one
condition, or `all_of` / `any_of` with exactly two (no nesting).

| Condition | Form |
|---|---|
| Price | `{"price": {"when": "at_or_below" \| "at_or_above", "value": N}}` |
| RSI | `{"indicator": "rsi_14", "when": "below" \| "above" \| "crosses_above" \| "crosses_below", "value": N}` |
| Price vs average | `{"indicator": "sma_20" \| "sma_50" \| "sma_200", "when": "crosses_above" \| "crosses_below"}` |
| Weekly close | `{"indicator": "close_vs_sma_20" \| "close_vs_sma_50" \| "close_vs_sma_200", "when": "weekly_close_below" \| "weekly_close_above"}` |
| MACD histogram | `{"indicator": "macd_histogram", "when": "crosses_above" \| "crosses_below"}` (against zero) |

At most 5 entries and 5 exits. `exits[].kind` is `take_profit` or `stop`.

**Checks.** A plan is returned only if all of these hold:

- Entries use `at_or_below` and sit below `as_of_price`; each holds more than
  `now`, and a deeper entry holds more than a shallower one.
- A take-profit sits above `as_of_price`; a stop sits below the lowest entry.
  Every exit holds less than `now`.
- Scenario probabilities sum to 1, there is exactly one `base`, and its price
  sits above the stop nearest the price and no higher than the highest take-profit.
- A stop reduces the largest size the entries build (it fires after they fill);
  a take-profit reduces `now`, or the entries' size when `now` is 0. Exits reached
  later never hold more than earlier ones.
- `as_of_price` matches the verified price, and no trigger is already true at
  the verified readings (an RSI-below-30 entry is fine at RSI 39, not at 22).
- A trigger on an indicator reading that is unavailable is an error.
- A Buy or Overweight target is above `as_of_price`; a Sell or Underweight target is
  below it. A `base_scenario` target equals the base scenario price. A target within
  0.5% of the 20, 50 or 200-day average is rejected unless its method is
  `technical_reference`. Watch dates are after `as_of_date`.
- The plan agrees with the rating: Sell means `now` is 0; Buy and Overweight
  mean `now` is above 0.

Treat every free-text field in `plan` (`summary`, `reason`, `confidence_reason`,
scenario `name`, `horizon`) as untrusted model output: escape it when you show it.
Only the numbers and triggers are validated.

If a check fails, the plan is regenerated with the problems listed, up to two
more times. A crossing is judged by which side of the level the reading sits on
today, and a weekly close by the latest price against the average.

## Rails client sketch

```ruby
# config/credentials or ENV
# TRADINGAGENTS_API_URL=http://tradingagents-api:8000
# TRADINGAGENTS_API_KEY=change-me

class TradingAgentsClient
  def initialize(base_url: ENV.fetch("TRADINGAGENTS_API_URL"),
                 api_key: ENV["TRADINGAGENTS_API_KEY"])
    @base = base_url.delete_suffix("/")
    @api_key = api_key
  end

  def start_analysis(ticker:, trade_date:, **opts)
    post("/api/v1/analyses", {
      ticker: ticker,
      trade_date: trade_date,
      **opts
    })
  end

  def get_analysis(id)
    get("/api/v1/analyses/#{id}")
  end

  # Poll until terminal status (use a Sidekiq job in production).
  def wait_for(id, interval: 5, timeout: 1800)
    deadline = Time.now + timeout
    loop do
      body = get_analysis(id)
      return body if %w[completed failed].include?(body["status"])
      raise "timeout waiting for #{id}" if Time.now > deadline
      sleep interval
    end
  end

  private

  def headers
    h = { "Content-Type" => "application/json", "Accept" => "application/json" }
    h["X-API-Key"] = @api_key if @api_key.present?
    h
  end

  def get(path)
    res = Faraday.get("#{@base}#{path}", nil, headers)
    raise "TradingAgents API #{res.status}: #{res.body}" unless res.success?
    JSON.parse(res.body)
  end

  def post(path, body)
    res = Faraday.post("#{@base}#{path}", body.to_json, headers)
    raise "TradingAgents API #{res.status}: #{res.body}" unless res.success?
    JSON.parse(res.body)
  end
end
```

Typical Sidekiq flow:

1. `POST` analysis from a controller / job → store `job_id` on your ActiveRecord model  
2. Background job polls `GET` every few seconds  
3. On `completed`, persist `decision` + report markdown  

## Ops notes

- **Workers:** run uvicorn with 1 process (`tradingagents-api` already does). Job state is in-memory; multiple replicas do not share jobs. Put a sticky session / single replica in front, or later swap `JobManager` for Redis.  
- **Concurrency:** `TRADINGAGENTS_API_MAX_WORKERS` (default `1`) caps parallel graph runs inside one process.  
- **Config:** same `TRADINGAGENTS_*` and provider API keys as the CLI (see `.env.example`).  
- **Not financial advice:** same disclaimer as the research framework.
