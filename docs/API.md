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
