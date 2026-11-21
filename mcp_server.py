"""MCP server exposing Signal Autopsy to any LLM client.

Registered by adding this to the client's MCP config:

    {"mcpServers": {"signal-autopsy": {"command": "python", "args": ["mcp_server.py"]}}}

It speaks the JSON-RPC subset that MCP clients use: initialize, tools/list, tools/call. Keeping
it dependency-free means it runs anywhere Python does, which matters for judges who will try to
run it.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

import cmc
import signal_autopsy as sa

TOOLS = [
    {
        "name": "resolve_coin",
        "description": "Resolve a ticker symbol to its CoinMarketCap id, canonical name and slug.",
        "inputSchema": {"type": "object", "properties": {"symbol": {"type": "string"}},
                        "required": ["symbol"]},
    },
    {
        "name": "quote_at",
        "description": "Price a coin at a UTC instant using CoinMarketCap historical quotes.",
        "inputSchema": {"type": "object", "properties": {
            "symbol": {"type": "string"},
            "when": {"type": "string", "description": "ISO-8601 UTC timestamp"},
            "interval": {"type": "string", "enum": ["1m", "5m", "15m", "1h", "1d"], "default": "1h"},
        }, "required": ["symbol", "when"]},
    },
    {
        "name": "score_calls",
        "description": ("Score a list of signal calls against CMC prices with fees and slippage. "
                        "Each call is {coin, direction: long|short, called_at: ISO-8601 UTC}. "
                        "Returns a ledger plus a verdict."),
        "inputSchema": {"type": "object", "properties": {
            "calls": {"type": "array", "items": {"type": "object"}},
            "horizon_hours": {"type": "number", "default": 24},
            "fee_bps": {"type": "number", "default": 11},
            "slippage_bps": {"type": "number", "default": 30},
        }, "required": ["calls"]},
    },
    {
        "name": "extract_calls",
        "description": ("Turn a raw channel scrape into validated calls (coin, direction, "
                        "called_at). A schema-constrained model does the reading step only; every "
                        "field is validated and rejections come back with reasons. Cached by "
                        "content hash, so repeating a call costs no quota."),
        "inputSchema": {"type": "object", "properties": {
            "text": {"type": "string",
                     "description": "raw posts, one per line, each prefixed with its timestamp"},
            "source": {"type": "string", "description": "label stored with every call"},
            "model": {"type": "string", "description": "override the extraction model"},
        }, "required": ["text"]},
    },
]


def _parse(calls_json: list[dict]) -> list[dict]:
    out = []
    for c in calls_json:
        out.append(dict(coin=str(c["coin"]),
                        direction=str(c["direction"]).lower(),
                        called_at=datetime.fromisoformat(
                            str(c["called_at"]).replace("Z", "+00:00")).astimezone(timezone.utc),
                        source=c.get("source", "")))
    return sorted(out, key=lambda c: c["called_at"])


def call_tool(name: str, args: dict) -> dict:
    if name == "resolve_coin":
        return cmc.resolve_coin(args["symbol"])
    if name == "quote_at":
        ident = cmc.resolve_coin(args["symbol"])
        when = datetime.fromisoformat(args["when"].replace("Z", "+00:00")).astimezone(timezone.utc)
        q = cmc.quote_at(ident["id"], when, args.get("interval", "1h"))
        return dict(symbol=args["symbol"], cmc_id=ident["id"], **q)
    if name == "score_calls":
        calls = _parse(args["calls"])
        rows = sa.score(calls, float(args.get("horizon_hours", 24)),
                        float(args.get("fee_bps", 11)), float(args.get("slippage_bps", 30)))
        return dict(summary=sa.report(rows, float(args.get("horizon_hours", 24))), calls=rows)
    if name == "extract_calls":
        import extract_calls as ex
        payload = ex.extract(str(args["text"]), source=str(args.get("source") or ""),
                             model=str(args.get("model") or ex.MODEL))
        return {k: payload[k] for k in ("calls", "rejected", "duplicates")} | {"meta": payload["meta"]}
    raise ValueError(f"unknown tool {name}")


def handle(req: dict) -> dict | None:
    method = req.get("method")
    rid = req.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "signal-autopsy", "version": "0.1.0"}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = req.get("params") or {}
        try:
            result = call_tool(params.get("name"), params.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": json.dumps(result, indent=2)}]}}
        except Exception as exc:
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": f"error: {type(exc).__name__}: {exc}"}],
                "isError": True}}
    if method in ("notifications/initialized", "initialized"):
        return None
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"no method {method}"}}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(req)
        if resp is not None:
            print(json.dumps(resp), flush=True)


if __name__ == "__main__":
    main()
