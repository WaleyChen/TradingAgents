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
      { "name": "bull", "price": 330, "probability": 0.25 },
      { "name": "base", "price": 300, "probability": 0.5 },
      { "name": "bear", "price": 210, "probability": 0.25 }
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
