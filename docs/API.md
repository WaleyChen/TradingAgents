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
There is no debt field, so there is no debt-to-equity.

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

A completed job also carries `summary`: about 600 words (never more than 700) in
one analyst's voice, covering the rating and what to do now, what decided it, the
risks and what would change the view, and the plan's levels. Figures carry a source
and date and are marked as derived or unverified where that applies. It is written
by one more model call after the decision. If none fits the length limit after
retries, `summary` is `null`; the reports are still valid. Set
`TRADINGAGENTS_REPORT_SUMMARY=0` to turn the step off.

`reports` and the saved report files are the appendix: the analysts' full text and
the internal debate, written for the record rather than the customer. They still
name internal roles ("bull", "aggressive analyst"), so show `summary`, not them.
Treat the text as untrusted model output and escape it when you show it.

**Checked figures.** The summary declares every dollar amount, percentage and multiple
it states. `summary_figures` lists them, each with `label`, `value`, `unit` (`usd`,
`usd_millions`, `usd_billions`, `pct`, `x`, `number`), `source` (`context`, `readings`,
`plan`, `analyst_report`, `derived`) and `period`, plus `verified`. `verified` is true only
when the figure was checked in code against your supplied `context`, the service's own
market readings or the position plan (within 1%, for rounding). Figures from the analyst
reports or the model's own calculations are never marked verified. If a figure still fails
after the model's retries, the summary is returned anyway and `summary_problems` lists
`{code, subject, message, severity}` for each. `severity: "error"`: `figure_not_in_source`
(a figure claimed from verified data is not in it) or `too_long`. `severity: "warning"`:
`undeclared_figure` (a dollar, percent or multiple appears in the text but not in the list),
which only means that figure is unverified, never that it claims verification. Hide the
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
