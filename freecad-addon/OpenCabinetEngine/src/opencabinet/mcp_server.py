"""Minimal MCP stdio server: expose solve/verify as tools.

Any MCP-capable AI chat app (Claude Desktop, Claude Code, Cline, ...) can then
generate wardrobes conversationally:

    claude mcp add opencabinet -- opencabinet-mcp
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .solver import solve_to_directory


def tool_solve(args: dict):
    """Solve a wardrobe spec into a board graph + gate report."""
    out = args.get("output") or "/tmp/opencabinet-mcp"
    result = solve_to_directory(args["spec"], out)
    report = result["self_check"]
    return {
        "passed": report.get("passed"),
        "board_count": report.get("board_count"),
        "errors": report.get("layout_feasibility", {}).get("errors", []),
        "board_graph": out + "/board_graph.json",
        "next": "opencabinet export " + out + "/board_graph.json --output " + out + "/fc/",
    }


def tool_validate_spec(args: dict):
    """Cheap spec lint without solving (checks closure rules)."""
    spec = args["spec"]
    t = spec.get("board_thickness", 18)
    w = spec.get("width", 0)
    bays = spec.get("column_count", 0)
    widths = spec.get("column_widths", [])
    problems = []
    if len(widths) != bays:
        problems.append(f"column_count={bays} but {len(widths)} widths given")
    if widths and abs(sum(widths) + t * (bays + 1) - w) > 1e-6:
        problems.append(f"bay width closure fails: {sum(widths)}+{t}*{bays+1} != {w}")
    return {"ok": not problems, "problems": problems}


TOOLS = {
    "solve_wardrobe": (
        "Solve a wardrobe spec (JSON) into a verified board graph. "
        "Returns pass/fail gate report and where the files were written.",
        tool_solve,
    ),
    "validate_spec": (
        "Cheap spec lint: bay-width closure and structural field checks, without solving.",
        tool_validate_spec,
    ),
}

SCHEMAS = {
    "solve_wardrobe": {
        "type": "object",
        "properties": {
            "spec": {"type": "object", "description": "the wardrobe spec (see docs/spec.md)"},
            "output": {"type": "string", "description": "output directory"},
        },
        "required": ["spec"],
    },
    "validate_spec": {
        "type": "object",
        "properties": {"spec": {"type": "object"}},
        "required": ["spec"],
    },
}


def rpc(method: str, params: dict, _id=None):
    out = {"jsonrpc": "2.0"}
    if _id is not None:
        out["id"] = _id
    try:
        if method == "initialize":
            out["result"] = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "opencabinet", "version": "0.1.0"},
            }
        elif method == "tools/list":
            out["result"] = {"tools": [
                {"name": n, "description": d, "inputSchema": SCHEMAS[n]}
                for n, (d, _) in TOOLS.items()
            ]}
        elif method == "tools/call":
            name = params["name"]
            desc, fn = TOOLS[name]
            out["result"] = {"content": [
                {"type": "text", "text": json.dumps(fn(params.get("arguments") or {}), ensure_ascii=False)}
            ]}
        elif method == "notifications/initialized":
            return None
        else:
            out["error"] = {"code": -32601, "message": f"unknown method {method}"}
    except Exception as exc:  # noqa: BLE001
        out["error"] = {"code": -32000, "message": str(exc)}
    return out


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = rpc(msg.get("method"), msg.get("params") or {}, msg.get("id"))
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
