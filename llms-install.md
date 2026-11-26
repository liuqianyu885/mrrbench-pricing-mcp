# Installing MRR Bench Pricing MCP (for agents)

This server is **remote HTTP** — there is nothing to clone, build, or install.

## Config to write

```json
{"mcpServers": {"mrrbench": {"type": "http", "url": "https://mrrbench.com/mcp"}}}
```

- Transport: Streamable HTTP, `POST https://mrrbench.com/mcp`
- Auth: none. Read-only.
- No environment variables required.
- Stateless: no session setup, `tools/list` works on a bare request.

## Verify it works

```bash
curl -s https://mrrbench.com/mcp -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Expect four tools: `search_tools`, `get_pricing`, `price_changes`, `stack_cost`.

## Then ask it something

"What does jasper.ai cost?" → `search_tools` then `get_pricing`.
"Has Notion changed its price this year?" → `price_changes`.

## Notes

- Search queries need at least 2 characters.
- 500 `tools/call` requests per IP per day; over that the server returns `429` with `Retry-After`.
- Local fallback: `python3 pricing_mcp.py --port 8097` in this repo runs the same code over a fictional sample dataset.
