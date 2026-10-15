"""FreeCAD-side runner used by `opencabinet export`.

Executed inside freecadcmd; environment carries OC_GRAPH / OC_OUT.
"""
import os
import sys

graph = os.environ.get("OC_GRAPH")
out = os.environ.get("OC_OUT")
if not graph or not out:
    print("OC_GRAPH / OC_OUT not set", file=sys.stderr)
    raise SystemExit(2)

here = os.path.dirname(os.path.abspath(__file__))
target = os.path.join(here, "export_fcstd_grooved.py")

ns = {"__name__": "__oc_export__", "__file__": target}
exec(compile(open(target, encoding="utf-8").read(), target, "exec"), ns)
sys.argv = ["export_fcstd_grooved.py", graph, out]
rc = ns["main"]()
print("OC-EXPORT-RC", rc, flush=True)
raise SystemExit(rc)
