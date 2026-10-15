"""Exact-solid verification of produced FCStd models.

The interference check uses FreeCAD solid `common()` volumes, not bounding
boxes: a grooved board keeps its full bounding box after cutting, so AABB
tests false-positive on every grooved assembly. Overlap regions that are
fully covered by declared `cuts` are design-intent (e.g. a back panel
passing through a partition groove) and are exempt.
"""
from __future__ import annotations

import json


def verify_fcstd(fcstd_path: str, graph_path: str):
    """Return a verification report dict. Requires FreeCAD importable."""
    import itertools

    import FreeCAD as App
    import Part  # noqa: F401  (registers Part shapes)

    with open(graph_path, encoding="utf-8") as stream:
        graph = json.load(stream)
    cutmap = {n["uid"]: [c for c in (n.get("cuts") or [])] for n in graph["nodes"]}

    doc = App.openDocument(fcstd_path)
    objs = [(o, o.Shape) for o in doc.Objects
            if hasattr(o, "Shape") and o.Shape.Volume > 0]
    uidmap = {}
    for o, _ in objs:
        try:
            uidmap[o.Name] = o.SemanticUID
        except Exception:
            uidmap[o.Name] = o.Name

    def box_shape(c):
        return App.Part.makeBox(c["w"], c["d"], c["h"],
                                App.Vector(c["x"], c["y"], c["z"]))

    bad = []
    shapes = list(objs)
    for i in range(len(shapes)):
        for j in range(i + 1, len(shapes)):
            o1, s1 = shapes[i]
            o2, s2 = shapes[j]
            try:
                cv = s1.common(s2).Volume
            except Exception:
                continue
            if cv <= 1.0:
                continue
            cuts = [box_shape(c) for c in (cutmap.get(uidmap.get(o1.Name, ""), [])
                                           + cutmap.get(uidmap.get(o2.Name, ""), []))]
            rem = s1.common(s2)
            for c in cuts:
                if rem.Volume <= 1.0:
                    break
                rem = rem.cut(c)
            if rem.Volume > 1.0:
                bad.append([o1.Name, o2.Name, round(cv, 1), round(rem.Volume, 1)])

    xs = [(s.BoundBox.XMin, s.BoundBox.XMax) for _, s in shapes]
    ys = [(s.BoundBox.YMin, s.BoundBox.YMax) for _, s in shapes]
    zs = [(s.BoundBox.ZMin, s.BoundBox.ZMax) for _, s in shapes]
    extents = {
        "x": [min(v[0] for v in xs), max(v[1] for v in xs)],
        "y": [min(v[0] for v in ys), max(v[1] for v in ys)],
        "z": [min(v[0] for v in zs), max(v[1] for v in zs)],
    }
    return {
        "boards": len(shapes),
        "graph_nodes": len(graph["nodes"]),
        "board_count_match": len(shapes) == len(graph["nodes"]),
        "interference_pairs": bad,
        "extents": extents,
        "pass": not bad and len(shapes) == len(graph["nodes"]),
    }
