#!/usr/bin/env python3
"""MRR Bench 定价数据的 MCP server —— 让 AI 客户端直接来查我们的价格库。

为什么做这个：生财亦仁的判断是「垂直领域的付费数据型 MCP」最适合个人玩家，
Agent 流量已经占全网一半以上。我们有 4,164 个工具、14,490 条计划、144 条已核改价
记录，这些正是 AI 回答「X 多少钱 / 涨价了吗」时缺的原料。AI 来调就是分发。

实现：单文件、零依赖、JSON-RPC 2.0 over HTTP POST /mcp（Streamable HTTP 的最小子集）。
暴露四个工具：
  search_tools   按名字/分类找工具
  get_pricing    某个工具的全部档位（含证据原文与核对日期）
  price_changes  改价记录（可按域名、起始日期过滤）
  stack_cost     给一组工具算月/年总价（取每域最便宜档）

用法：
  python3 tools/pricing_mcp.py --port 8097          # 本机调试
  curl -s localhost:8097/mcp -H 'Content-Type: application/json' \
       -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import time
import sys
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "site" / "tools.json"
PLANS = ROOT / "data" / "verified_plans.json"
CHANGES = ROOT / "data" / "plan_price_changes.jsonl"

USAGE = ROOT / "data" / "mcp_usage.jsonl"
TOKEN = os.environ.get("MRR_MCP_TOKEN", "").strip()   # 留空=公开；设了就要 Bearer
_cache: dict[str, tuple[float, object]] = {}


def log_usage(tool: str, args: dict, ms: int, ip: str) -> None:
    """记一行调用，用来证明「有 AI 在调我们的数据」——这是后面做付费额度的依据。"""
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
    """最便宜的月付档（年付折算成月），用于栈成本。"""
    best = None
    for p in _plans_for(domain):
        price = p["price_usd"]
        if p["billing"] == "one-time":
            continue
        per_month = price / 12 if p["billing"] in ("yearly", "annual", "year") else price
        if per_month <= 0:
            continue
        if best is None or per_month < best["per_month"]:
            best = {"domain": domain, "plan": p["plan"], "per_month": round(per_month, 2)}
    return best


SCHEMAS = [
    {
        "name": "search_tools",
        "description": "Search SaaS/AI tools by name or category in the MRR Bench index (4,164 tools with verified pricing).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Tool name or keyword, e.g. 'jennifer' or 'seo'"},
                "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_pricing",
        "description": "All pricing tiers for one tool, with the verbatim pricing-page evidence behind each price and the date it was checked.",
        "inputSchema": {
            "type": "object",
            "properties": {"domain": {"type": "string", "description": "e.g. 'jasper.ai'"}},
            "required": ["domain"],
        },
    },
    {
        "name": "price_changes",
        "description": "Verified list-price changes. Use it to answer 'did X raise its price' with dates and percentages.",
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
        "description": "Total monthly and yearly cost of a set of tools, using the cheapest paid monthly-equivalent tier of each. Useful for 'what would this stack cost' questions.",
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
        limit = min(int(args.get("limit") or 10), 50)
        rows = []
        for t in _load(TOOLS):
            blob = f"{t.get('d','')} {t.get('n','')} {t.get('st','')} {t.get('c','')}".lower()
            if q and q not in blob:
                continue
            rows.append({"domain": t.get("d"), "price_usd": t.get("p"),
                         "plan": t.get("pl"), "category": t.get("c"),
                         "grade": t.get("g"), "page": f"https://mrrbench.com/t/{t.get('s')}.html"})
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
            if c.get("kind") == "renamed" or c.get("from") == c.get("to"):
                continue   # 改名不是改价
            rows.append({"domain": c["domain"], "plan": c.get("plan"),
                         "from_usd": c.get("from"), "to_usd": c.get("to"),
                         "pct": c.get("pct"), "date": c.get("changed_on"),
                         "billing": c.get("billing")})
            if len(rows) >= limit:
                break
        rows.sort(key=lambda r: r["date"] or "", reverse=True)
        return {"count": len(rows), "since": since, "changes": rows}

    if name == "stack_cost":
        doms = [d.strip().lower() for d in (args.get("domains") or []) if d and d.strip()][:40]
        items, missing = [], []
        for d in doms:
            got = _cheapest_monthly(d)
            (items.append(got) if got else missing.append(d))
        monthly = round(sum(i["per_month"] for i in items), 2)
        return {"monthly_usd": monthly, "yearly_usd": round(monthly * 12, 2),
                "tools": items, "no_price_on_record": missing}

    raise ValueError(f"unknown tool: {name}")


class H(BaseHTTPRequestHandler):
    server_version = "mrrb-mcp/0.1"

    def _send(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
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
                p = req.get("params") or {}
                t0 = time.time()
                out = call(p.get("name") or "", p.get("arguments") or {})
                log_usage(p.get("name") or "", p.get("arguments") or {},
                          int((time.time() - t0) * 1000),
                          (self.headers.get("X-Real-IP") or self.address_string()))
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
