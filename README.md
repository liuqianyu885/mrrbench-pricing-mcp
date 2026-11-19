# mrrbench-pricing-mcp

An MCP server that answers **"what does this SaaS tool cost?"** and **"did the price
change?"** from a verified pricing index. Single file, standard library only, JSON-RPC
2.0 over HTTP `POST /mcp` — the minimal subset of Streamable HTTP that MCP clients use.

Hosted instance: `https://mrrbench.com/mcp` — no key, read-only.
Discovery card: `https://mrrbench.com/mcp.json` · registry manifest: `server.json`.

This repo is the same code plus a tiny **fictional** dataset so it runs in one command.
The verified index those endpoints serve is not in this repository.

## Quick start

```bash
python3 pricing_mcp.py --port 8097
curl -s localhost:8097/mcp -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Connect from a client (Claude Code, Cursor, any MCP client):

```json
{"mcpServers": {"mrrbench": {"type": "http", "url": "https://mrrbench.com/mcp"}}}
```

```bash
curl -s https://mrrbench.com/mcp -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_pricing","arguments":{"domain":"jasper.ai"}}}'
```

## Tools

| Tool | What it answers | Notes |
|---|---|---|
| `search_tools` | "Is there a tool for X?" | substring match on name/domain/category, ≥2 chars, ≤20 rows |
| `get_pricing` | "What does X charge?" | every tier with the **verbatim pricing-page text** and the date it was checked |
| `price_changes` | "Did X raise its price?" | one plan read on two dates; `kind: renamed` rows carry `pct: null` |
| `stack_cost` | "What would these cost together?" | cheapest monthly plan per tool; annual reported as the printed total, never ÷12 |

Design rules the code enforces, not just documents:

- **No invented monthly figures.** A plan published at `$144/year` is reported as $144/year.
  Dividing by twelve produces a number that appears on no invoice.
- **No silent guessing.** Missing data returns `found: false` with a note, never a plausible default.
- **Evidence travels with the number.** Every price points at the text it was read from.

## Configuration

| Env | Default | Meaning |
|---|---|---|
| `MRR_MCP_DATA` | `./sample_data` | directory holding `tools.json`, `verified_plans.json`, `plan_price_changes.jsonl` |
| `MRR_MCP_USAGE` | `$MRR_MCP_DATA/mcp_usage.jsonl` | one JSON line per tool call |
| `MRR_MCP_TOKEN` | empty | set it and clients must send `Authorization: Bearer <token>` |
| `MRR_MCP_MAX_SEARCH` | `20` | max rows returned by `search_tools` |
| `MRR_MCP_DAILY_QUOTA` | `500` | max `tools/call` per IP per day, then HTTP 429 |
| `MRR_MCP_BASE_URL` | `https://mrrbench.com` | host used to build page links |

The three anti-scrape guards exist because a substring search over an index is enumerable:
an empty query that returns a page of results lets a scraper walk the whole set. Queries
shorter than two characters are refused outright.

## Data formats

```jsonc
// tools.json — one row per tool
{"d": "example-analytics.io", "n": "Example Analytics", "s": "example-analytics-io",
 "p": 29.0, "cur": "USD", "pl": "Starter", "c": "analytics", "g": "A", "st": "active"}

// verified_plans.json — keyed by domain
{"example-analytics.io": {"url": "…/pricing", "checked": "2026-11-02", "confidence": "high",
  "plans": [{"plan": "Starter", "price": 29.0, "billing": "monthly", "period": "month",
             "evidence": "Starter $29 per month, billed monthly."}]}}

// plan_price_changes.jsonl — one row per plan read on two dates
{"domain": "…", "plan": "Starter", "changed_on": "2026-11-06", "from": 19.0, "to": 29.0,
 "pct": 52.6, "confirmed": true, "kind": "price"}
```

`sample_data/` is **fictional**: invented domains, invented prices, shaped like the real
records so the code paths run. No real pricing data is in this repository.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

They cover the guards (query length, result cap, daily quota) — the parts that fail
silently if someone removes them.

## Deploy

```ini
# /etc/systemd/system/mrr-pricing-mcp.service
[Service]
ExecStart=/usr/bin/python3 /opt/mrrbench-pricing-mcp/pricing_mcp.py --port 8097
Restart=always
Environment=MRR_MCP_DATA=/var/lib/mrrbench-pricing

[Install]
WantedBy=multi-user.target
```

```nginx
location = /mcp {
    limit_req zone=mcp burst=15 nodelay;   # its own zone; scanners are quieter than API traffic
    access_log /var/log/nginx/mcp_access.log;  # keep bot polling out of the main traffic numbers
    proxy_pass http://127.0.0.1:8097;
}
```

Serve `/.well-known/mcp/server-card.json` as a static file — directory sites fall back to
it when their scan of `/mcp` fails.

## License

MIT. See `LICENSE`.
