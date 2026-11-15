# MRR Bench — SaaS pricing MCP server

A remote MCP server that gives AI clients verified SaaS pricing data.

- **Endpoint**: `https://mrrbench.com/mcp` (Streamable HTTP, JSON-RPC 2.0)
- **Discovery card**: `https://mrrbench.com/mcp.json`
- **No auth, read-only.** No API key needed.

## Tools

| Tool | What it returns |
|---|---|
| `search_tools` | Tools in the index (4,164 domains) by name or category |
| `get_pricing` | All tiers for one tool, each with the verbatim pricing-page evidence and the date it was checked |
| `price_changes` | Verified list-price changes with dates and percentages |
| `stack_cost` | Monthly/yearly cost of a set of tools (cheapest monthly-equivalent tier each) |

## Connect

Claude Code / Cursor / any MCP client that supports remote HTTP servers:

```json
{"mcpServers":{"mrrbench":{"type":"http","url":"https://mrrbench.com/mcp"}}}
```

## Example

```bash
curl -s https://mrrbench.com/mcp -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_pricing","arguments":{"domain":"jasper.ai"}}}'
```

Returns the Pro tier at $69/month with the exact pricing-page text it came from.

## Data policy

Prices come from public pricing pages, re-checked on a schedule. Every price carries the source
snippet, so answers can cite where the number came from. Promotional and per-unit prices are
excluded from the change log — only list-price moves are reported.

The server in this repo (`pricing_mcp.py`) is the implementation we run; it reads the same index
that powers https://mrrbench.com.
