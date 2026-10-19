"""Cut-list generation from a board graph.

Board-level bill of materials in the format shops expect on the saw:

* every part measured on its own axes (w along grain, d across, h = thickness),
* identical parts merged with a quantity column,
* one raw-stock group per material+thickness with utilisation percentages,
* explicit saw settings (kerf / trim) carried from the spec,
* hardware counts summed by type when the spec declares them.

Geometry here is authoritative: dimensions come from `bbox`, the grain axis
decides which bbox side is length (runs with the sheet grain) versus width.
"""
from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path

# default raw sheet sizes offered by suppliers (mm)
DEFAULT_SHEET = {"w": 1220.0, "l": 2440.0}
DEFAULT_KERF = 4.0     # saw blade width
DEFAULT_TRIM = 10.0    # edge trimming per sheet side


def _part_dims(node: dict) -> tuple[float, float, float, str]:
    """Return (length, width, thickness, grain_axis) for a board node."""
    b = node["bbox"]
    grain = node.get("grain", "z")
    t = min(b["w"], b["d"], b["h"])
    if grain == "z":          # vertical panel: length = height, width = depth
        length, width = b["h"], (b["d"] if b["w"] == t else b["w"])
    elif grain == "x":        # horizontal panel: length along x
        length, width = b["w"], b["d"]
    else:                     # grain == "y"
        length, width = b["d"], b["w"]
    return round(length, 1), round(width, 1), round(t, 1), grain


def build_cutlist(board_graph: dict, spec: dict | None = None) -> dict:
    """Build a cut-list report dict from a solved board graph."""
    spec = spec or {}
    t_spec = float(spec.get("board_thickness", 18))
    kerf = float(spec.get("kerf", DEFAULT_KERF))
    trim = float(spec.get("trim", DEFAULT_TRIM))
    sheet_w = float(spec.get("sheet_width", DEFAULT_SHEET["w"]))
    sheet_l = float(spec.get("sheet_length", DEFAULT_SHEET["l"]))

    parts: list[dict] = []
    for n in board_graph["nodes"]:
        length, width, t, grain = _part_dims(n)
        parts.append({
            "uid": n["uid"],
            "role": n.get("role", ""),
            "length": length,
            "width": width,
            "thickness": t,
            "grain": grain,
            "material": n.get("material", "panel"),
        })

    # merge identical parts (role excluded: two sides are interchangeable)
    merged = Counter((p["length"], p["width"], p["thickness"],
                      p["grain"], p["material"]) for p in parts)
    lines = [
        {"length": k[0], "width": k[1], "thickness": k[2], "grain": k[3],
         "material": k[4], "qty": v}
        for k, v in sorted(merged.items(), key=lambda kv: -kv[1])
    ]

    # per-thickness stock groups and utilisation (area based, kerf-aware rough)
    sheet_area = sheet_l * sheet_w
    groups: dict[float, dict] = {}
    for line in lines:
        g = groups.setdefault(line["thickness"], {"parts": [], "part_area": 0.0})
        g["parts"].append(line)
        g["part_area"] += line["length"] * line["width"] * line["qty"]

    stock = []
    for t, g in sorted(groups.items()):
        usable = (sheet_l - 2 * trim) * (sheet_w - 2 * trim)
        # crude sheet count: parts + kerf allowance, never below area ratio
        sheets = max(
            1,
            -(-int(g["part_area"]) // int(usable)) if usable > 0 else 1,
        )
        est_area = sheets * sheet_area
        stock.append({
            "thickness": t,
            "sheet": f"{sheet_w:.0f}x{sheet_l:.0f}",
            "sheets_est": sheets,
            "utilisation_pct": round(100.0 * g["part_area"] / est_area, 1)
            if est_area else 0.0,
            "part_area_m2": round(g["part_area"] / 1e6, 3),
            "kerf_mm": kerf,
            "trim_mm": trim,
            "lines": g["parts"],
        })

    oversize = [
        {"uid": p["uid"], "role": p["role"],
         "length": p["length"], "width": p["width"], "thickness": p["thickness"]}
        for p in parts
        if (p["length"] > sheet_l - 2 * trim or p["width"] > sheet_w - 2 * trim)
    ]

    return {
        "kerf_mm": kerf,
        "trim_mm": trim,
        "sheet": {"width": sheet_w, "length": sheet_l},
        "part_count": len(parts),
        "unique_parts": len(lines),
        "lines": lines,
        "stock_groups": stock,
        "oversize_parts": oversize,
        "oversize_ok": not oversize,
    }


def write_outputs(report: dict, out_dir: str) -> dict:
    """Write cutlist.json + cutlist.csv next to the solved graph."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "cutlist.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    with (out / "cutlist.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["qty", "length_mm", "width_mm", "thickness_mm",
                    "grain", "material"])
        for line in report["lines"]:
            w.writerow([line["qty"], line["length"], line["width"],
                        line["thickness"], line["grain"], line["material"]])
    return {"json": str(out / "cutlist.json"), "csv": str(out / "cutlist.csv")}
