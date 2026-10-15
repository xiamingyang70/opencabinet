"""Command-line interface.

    opencabinet solve  SPEC.json --output DIR   solve layout -> board_graph + self_check
    opencabinet export GRAPH.json --output DIR  export FCStd (requires freecadcmd)
    opencabinet verify FCStd --graph GRAPH.json exact-solid interference + extent checks
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

from . import __version__


def _load(path: str):
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def cmd_solve(args: argparse.Namespace) -> int:
    from .solver import solve_to_directory

    spec = _load(args.spec)
    os.makedirs(args.output, exist_ok=True)
    result = solve_to_directory(spec, args.output)
    report = result["self_check"]
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if report.get("passed") else 1


def cmd_export(args: argparse.Namespace) -> int:
    # groove the graph first (back-panel grooves are part of the model)
    from .groover import process_file
    grooved = args.graph.rsplit(".", 1)[0] + ".grooved.json"
    process_file(args.graph, grooved)
    args.graph = grooved
    fcad = (os.environ.get("FREECADCMD")
            or shutil.which("freecadcmd") or shutil.which("FreeCADCmd"))
    if not fcad:
        print("freecadcmd not found on PATH; install FreeCAD or add it to PATH.", file=sys.stderr)
        return 2
    here = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(here, "_fc_run.py")
    env = dict(os.environ)
    env.update({"OC_GRAPH": os.path.abspath(args.graph), "OC_OUT": os.path.abspath(args.output)})
    proc = subprocess.run([fcad, script], env=env, capture_output=True, text=True)
    sys.stdout.write(proc.stdout[-4000:])
    sys.stderr.write(proc.stderr[-2000:])
    return proc.returncode


def cmd_verify(args: argparse.Namespace) -> int:
    from .verify import verify_fcstd

    report = verify_fcstd(args.fcstd, args.graph)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if report.get("pass") else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="opencabinet", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("solve", help="solve a wardrobe spec into a board graph")
    p.add_argument("spec")
    p.add_argument("--output", required=True)
    p.set_defaults(func=cmd_solve)

    p = sub.add_parser("export", help="export a board graph to FCStd (needs freecadcmd)")
    p.add_argument("graph")
    p.add_argument("--output", required=True)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("verify", help="verify a produced FCStd against its graph")
    p.add_argument("fcstd")
    p.add_argument("--graph", required=True)
    p.set_defaults(func=cmd_verify)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
