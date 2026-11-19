#!/usr/bin/env python3
"""MRR Bench pricing MCP server — let AI clients ask what SaaS tools cost.

A single file, no dependencies, JSON-RPC 2.0 over HTTP POST /mcp
(the minimal subset of Streamable HTTP that MCP clients actually use).

The hosted instance at https://mrrbench.com/mcp serves a verified index; this repo is
the same code plus a tiny fake dataset so you can run it locally and see the shapes.

Four tools:
  search_tools   find a tool by name or category
  get_pricing    every tier of one tool, with the pricing-page text and check date
  price_changes  list-price changes (filter by domain and start date)
  stack_cost     monthly / annual total for a set of tools

Run it:
  python3 pricing_mcp.py --port 8097                # local
  curl -s localhost:8097/mcp -H 'Content-Type: application/json' \
       -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import threading
import time
import sys
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

BASE_URL = os.environ.get("MRR_MCP_BASE_URL", "https://mrrbench.com").rstrip("/")
# Data dir is configurable: this repo only ships sample_data (fake rows).
DATA_DIR = Path(os.environ.get("MRR_MCP_DATA",
                               Path(__file__).resolve().parent / "sample_data"))
TOOLS = DATA_DIR / "tools.json"
PLANS = DATA_DIR / "verified_plans.json"
CHANGES = DATA_DIR / "plan_price_changes.jsonl"
USAGE = Path(os.environ.get("MRR_MCP_USAGE", DATA_DIR / "mcp_usage.jsonl"))
TOKEN = os.environ.get("MRR_MCP_TOKEN", "").strip()   # empty = public; set = require Bearer

# Anti-scrape guards. A public index with a substring search is enumerable:
# an empty query returning a page of results lets a scraper walk the whole set.
#   1) query must be at least 2 characters
#   2) at most MRR_MCP_MAX_SEARCH results per call (default 20)
#   3) at most MRR_MCP_DAILY_QUOTA tool calls per IP per day (default 500)
# All three are env-tunable, so operators can change limits without a redeploy.
MAX_SEARCH = int(os.environ.get("MRR_MCP_MAX_SEARCH", "20"))
QUOTA = int(os.environ.get("MRR_MCP_DAILY_QUOTA", "500"))
_quota: dict[tuple[str, str], int] = {}
_quota_lock = threading.Lock()


def _count_today(ip: str) -> int:
    """Quota is per day, not per process: count today's existing log lines on first hit."""
    if not USAGE.exists() or not ip:
        return 0
    today = date.today().isoformat()
    n = 0
    try:
        for line in USAGE.read_text(encoding="utf-8").splitlines():
            if today in line and f'"{ip}"' in line:
                n += 1
    except OSError:
        return 0
    return n


def check_quota(ip: str) -> tuple[bool, int]:
    """Returns (allowed, remaining) for this IP today."""
    key = (date.today().isoformat(), ip or "-")
    with _quota_lock:
        if key not in _quota:
            for old in [k for k in _quota if k[0] != key[0]]:
                del _quota[old]
            _quota[key] = _count_today(ip)
        if _quota[key] >= QUOTA:
            return False, 0
        _quota[key] += 1
        return True, QUOTA - _quota[key]
_cache: dict[str, tuple[float, object]] = {}


def log_usage(tool: str, args: dict, ms: int, ip: str) -> None:
    """Append one line per tool call, so usage can be counted per day and per IP."""
    try:
        USAGE.parent.mkdir(parents=True, exist_ok=True)
        with USAGE.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                                "tool": tool, "ip": ip, "ms": ms,
                                "args": {k: v for k, v in (args or {}).items() if k != "token"}},
                               ensure_ascii=False) + "\n")
    except OSError:
        pass


def _load(path: Path, jsonl: bool = False) -> Any:
    key = str(path)
    mtime = path.stat().st_mtime if path.exists() else 0
    hit = _cache.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    if not path.exists():
        obj = [] if jsonl else {}
    elif jsonl:
        obj = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    obj.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    else:
        obj = json.loads(path.read_text(encoding="utf-8"))
    _cache[key] = (mtime, obj)
    return obj


def _plans_for(domain: str) -> list[dict]:
    vp = _load(PLANS)
    rec = vp.get(domain) or {}
    out = []
    for p in rec.get("plans") or []:
        price = p.get("price")
        if not isinstance(price, (int, float)):
            continue
        out.append({
            "plan": p.get("plan"),
            "price_usd": round(float(price), 2),
            "billing": p.get("billing") or "monthly",
            "period": p.get("period"),
            "evidence": (p.get("evidence") or "")[:240],
        })
    return out


def _cheapest_monthly(domain: str) -> dict | None:
    """Cheapest monthly plan. Annual plans are never divided into a fake monthly figure —
see _cheapest_annual for those."""
    best = None
    for p in _plans_for(domain):
        price = float(p["price_usd"] or 0)
        if (p["billing"] or "").lower() in ("one-time", "one_time", "yearly", "annual", "year"):
            continue
        if price <= 0:
            continue
        if best is None or price < best["per_month"]:
            best = {"domain": domain, "plan": p["plan"], "per_month": round(price, 2)}
    return best


def _cheapest_annual(domain: str):
    """Cheapest annual plan for a domain, reported as the printed annual total."""
    best = None
    for p in _plans_for(domain):
        if p["billing"] not in ("yearly", "annual", "year"):
            continue
        price = float(p["price_usd"] or 0)
        if price <= 0:
            continue
        if best is None or price < best["annual_total"]:
            best = {"domain": domain, "plan": p["plan"], "annual_total": round(price, 2),
                    "billing": "annual"}
    return best


def _index_size() -> int:
    """Counted at runtime: a hard-coded total is a number that quietly goes stale."""
    try:
        return len(_load(TOOLS))
    except Exception:
        return 0


SCHEMAS = [
    {
        "name": "search_tools",
        "description": ("Search the MRR Bench index by name or category. The index holds "
                        f"{_index_size()} tools with pricing on record; the count is read from the index, not written here."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 2,
                          "description": "Tool name or keyword, at least 2 characters, e.g. 'jennifer' or 'seo'"},
                "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": MAX_SEARCH},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_pricing",
        "description": "All pricing tiers for one tool, with the verbatim pricing-page evidence behind each price and the date it was checked.",
        "inputSchema": {
            "type": "object",
            "properties": {"domain": {"type": "string", "description": "e.g. 'example-analytics.io'"}},
            "required": ["domain"],
        },
    },
    {
        "name": "price_changes",
        "description": ("Price changes read from public pricing pages and matched on domain, plan name, billing "
                        "period and billing mode. A row is one plan read on two dates. Rows with kind \"renamed\" "
                        "record a plan name change, not a price change: their price fields are equal and pct is null. "
                        "Prices are in USD as published; each row carries the billing mode it was read in."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "Optional: limit to one tool"},
                "since": {"type": "string", "description": "Optional YYYY-MM-DD; defaults to last 90 days"},
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200},
            },
        },
    },
    {
        "name": "stack_cost",
        "description": ("Total cost of a set of tools. Each tool reports its cheapest monthly paid plan; where a tool "
                        "publishes only an annual price, the response carries the annual total as printed and never a "
                        "monthly equivalent. Monthly and annual totals come back in separate fields."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "domains": {"type": "array", "items": {"type": "string"}, "maxItems": 40},
            },
            "required": ["domains"],
        },
    },
]


def call(name: str, args: dict) -> dict:
    if name == "search_tools":
        q = (args.get("query") or "").strip().lower()
        if len(q) < 2:
            # Empty or single-letter queries are the enumeration path: refuse them
            # instead of returning "the first page".
            raise ValueError("query must be at least 2 characters: empty or single-letter queries "
                             "would page through the whole index")
        limit = min(int(args.get("limit") or 10), MAX_SEARCH)
        rows = []
        for t in _load(TOOLS):
            blob = f"{t.get('d','')} {t.get('n','')} {t.get('st','')} {t.get('c','')}".lower()
            if q and q not in blob:
                continue
            rows.append({"domain": t.get("d"), "price_usd": t.get("p"),
                         "plan": t.get("pl"), "category": t.get("c"),
                         "grade": t.get("g"), "page": f"{BASE_URL}/t/{t.get('s')}.html"})
            if len(rows) >= limit:
                break
        return {"count": len(rows), "results": rows}

    if name == "get_pricing":
        dom = (args.get("domain") or "").strip().lower().removeprefix("https://").removeprefix("www.").strip("/")
        vp = _load(PLANS)
        rec = vp.get(dom) or {}
        plans = _plans_for(dom)
        if not plans:
            return {"domain": dom, "found": False, "plans": [],
                    "note": "No verified pricing on record for this domain."}
        return {"domain": dom, "found": True, "url": rec.get("url"),
                "checked": rec.get("checked"), "confidence": rec.get("confidence"),
                "plans": plans}

    if name == "price_changes":
        dom = (args.get("domain") or "").strip().lower()
        since = args.get("since") or (date.today() - timedelta(days=90)).isoformat()
        limit = min(int(args.get("limit") or 50), 200)
        rows = []
        for c in _load(CHANGES, jsonl=True):
            if not c.get("confirmed"):
                continue
            if (c.get("changed_on") or "") < since:
                continue
            if dom and c.get("domain") != dom:
                continue
            if c.get("price_kind") in ("promo", "usage"):
                continue
            # A renamed plan is not a price change, but it is not noise either:
            # the tier someone subscribed to last month no longer exists. Keep the row,
            # set pct to null (0 would read as "price unchanged"), and mark it renamed.
            _renamed = c.get("kind") == "renamed" or c.get("from") == c.get("to")
            rows.append({"domain": c["domain"], "plan": c.get("plan"),
                         "from_usd": c.get("from"), "to_usd": c.get("to"),
                         "pct": None if _renamed else c.get("pct"),
                         "kind": "renamed" if _renamed else "price",
                         "renamed_from": c.get("renamed_from") or None,
                         "date": c.get("changed_on"),
                         "period": c.get("period") or None,
                         "billing": c.get("billing")})
            if len(rows) >= limit:
                break
        rows.sort(key=lambda r: r["date"] or "", reverse=True)
        return {"count": len(rows), "since": since, "changes": rows}

    if name == "stack_cost":
        # Annual totals are not divided by 12: a derived "monthly" figure is not a bill
            # Never invent a monthly equivalent: people put this into a budget and then
            # the next invoice does not match. If only an annual price is published, say so.
        doms = [d.strip().lower() for d in (args.get("domains") or []) if d and d.strip()][:40]
        items, monthly_only, missing = [], [], []
        for d in doms:
            m = _cheapest_monthly(d)
            if m:
                items.append({**m, "billing": "monthly"})
                monthly_only.append(m["per_month"])
                continue
            a = _cheapest_annual(d)
            (items.append(a) if a else missing.append(d))
        monthly = round(sum(monthly_only), 2)
        annual = round(sum(i["annual_total"] for i in items if i.get("billing") == "annual"), 2)
        # note is always present and never empty, so callers never have to guess.
        return {"monthly_usd": monthly,
                "annual_usd_published": annual,
                "note": ("Monthly prices are summed as published. Annual prices are the total the page "
                         "prints, not a monthly equivalent; we never divide a yearly price by twelve."),
                "billing_fields": {"monthly_usd": "monthly", "annual_usd_published": "annual_total_as_printed"},
                "tools": items, "no_price_on_record": missing}

    raise ValueError(f"unknown tool: {name}")


class H(BaseHTTPRequestHandler):
    server_version = "mrrb-mcp/0.1"

    def _send(self, obj: dict, code: int = 200, headers: dict | None = None) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):                                        # noqa: N802
        if self.path.rstrip("/") in ("/mcp", "/health"):
            self._send({"ok": True, "server": "mrrbench-pricing-mcp", "tools": len(SCHEMAS)})
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):                                       # noqa: N802
        if not self.path.rstrip("/").endswith("/mcp"):
            self._send({"error": "not found"}, 404)
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._send({"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32700, "message": "parse error"}}, 400)
            return
        rid = req.get("id")
        method = req.get("method")
        if TOKEN and method in ("tools/list", "mcp/tools/list", "tools/call", "mcp/tools/call"):
            given = ""
            auth = self.headers.get("Authorization") or ""
            if auth.lower().startswith("bearer "):
                given = auth[7:].strip()
            given = given or (self.headers.get("X-MCP-Token") or "").strip()
            if given != TOKEN:
                self._send({"jsonrpc": "2.0", "id": rid,
                            "error": {"code": -32001, "message": "unauthorized"}}, 401)
                return
        try:
            if method == "initialize":
                res = {"protocolVersion": "2025-06-18",
                       "capabilities": {"tools": {}},
                       "serverInfo": {"name": "mrrbench-pricing", "version": "0.1.0"}}
            elif method in ("tools/list", "mcp/tools/list"):
                res = {"tools": SCHEMAS}
            elif method in ("tools/call", "mcp/tools/call"):
                ip = (self.headers.get("X-Real-IP") or self.address_string())
                allowed, _left = check_quota(ip)
                if not allowed:
                    self._send({"jsonrpc": "2.0", "id": rid,
                                "error": {"code": -32002,
                                          "message": (f"daily quota reached: {QUOTA} calls per IP per day. "
                                                      "For bulk use, email us — one query per call is not the "
                                                      "way to read this index.")}},
                               429, {"Retry-After": "3600"})
                    return
                p = req.get("params") or {}
                t0 = time.time()
                out = call(p.get("name") or "", p.get("arguments") or {})
                log_usage(p.get("name") or "", p.get("arguments") or {},
                          int((time.time() - t0) * 1000), ip)
                res = {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
                       "isError": False}
            elif method in ("notifications/initialized", "initialized"):
                self._send({}, 202)
                return
            else:
                self._send({"jsonrpc": "2.0", "id": rid,
                            "error": {"code": -32601, "message": f"unknown method {method}"}}, 200)
                return
        except ValueError as e:
            self._send({"jsonrpc": "2.0", "id": rid,
                        "error": {"code": -32602, "message": str(e)}}, 200)
            return
        self._send({"jsonrpc": "2.0", "id": rid, "result": res})

    def log_message(self, format, *args):                    # noqa: A002
        sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8097)
    a = ap.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), H)
    print(f"MRR Bench pricing MCP on http://{a.host}:{a.port}/mcp", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
