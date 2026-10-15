# export_fcstd_grooved.py
from __future__ import annotations

import csv
import json
import math
import sys
import traceback
from pathlib import Path

import FreeCAD as App
import Part


DEFAULT_GRAPH = "/home/ming/unitcab/out/semantic/board_graph.json"
DEFAULT_OUTPUT = "/home/ming/unitcab/out"
DOC_NAME = "UnitCab"
VOLUME_TOLERANCE = 0.5


def parse_args(argv):
    graph_path = DEFAULT_GRAPH
    output_dir = DEFAULT_OUTPUT
    positional = []

    for value in argv[1:]:
        if value == "--":
            continue
        if value.startswith("--graph="):
            graph_path = value.split("=", 1)[1]
        elif value.startswith("--out="):
            output_dir = value.split("=", 1)[1]
        elif value.startswith("-"):
            continue
        elif value.lower().endswith(".py"):
            continue
        else:
            positional.append(value)

    if positional:
        graph_path = positional[0]
    if len(positional) > 1:
        output_dir = positional[1]

    return Path(graph_path), Path(output_dir)


def load_graph(path):
    with open(path, "r", encoding="utf-8") as stream:
        graph = json.load(stream)

    if not isinstance(graph, dict):
        raise ValueError("Board graph root must be an object")
    if not isinstance(graph.get("nodes"), list):
        raise ValueError("Board graph must contain a nodes array")
    if not isinstance(graph.get("edges"), list):
        raise ValueError("Board graph must contain an edges array")

    return graph


def safe_name(value, used):
    name = re.sub(r"[^A-Za-z0-9_]", "_", str(value))
    name = re.sub(r"_+", "_", name).strip("_")
    if not name:
        name = "Board"
    if name[0].isdigit():
        name = "B_" + name

    base = name[:60]
    name = base
    index = 1
    while name in used:
        suffix = "_%d" % index
        name = base[: 60 - len(suffix)] + suffix
        index += 1

    used.add(name)
    return name


import re


def require_number(value, field, uid):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("%s.%s must be numeric" % (uid, field))
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("%s.%s must be finite" % (uid, field))
    return value


def normalized_box(box, field, uid):
    if not isinstance(box, dict):
        raise ValueError("%s.%s must be an object" % (uid, field))

    values = dict(box)
    for key in ("x", "y", "z", "w", "d", "h"):
        if key not in box:
            raise ValueError("%s.%s.%s is missing" % (uid, field, key))
        values[key] = require_number(box[key], "%s.%s" % (field, key), uid)

    if any(values[key] <= 0 for key in ("w", "d", "h")):
        raise ValueError("%s.%s has non-positive dimensions" % (uid, field))
    return values


def normalized_node(node):
    if not isinstance(node, dict):
        raise ValueError("Node must be an object")

    required = ("uid", "role", "bbox", "space_id", "operator_id")
    missing = [field for field in required if field not in node]
    if missing:
        raise ValueError("Node missing fields: %s" % ", ".join(missing))

    uid = str(node["uid"])
    result = dict(node)
    result["bbox"] = normalized_box(node["bbox"], "bbox", uid)

    cuts = node.get("cuts", [])
    if not isinstance(cuts, list):
        raise ValueError("%s.cuts must be an array" % uid)
    result["cuts"] = [
        normalized_box(cut, "cuts[%d]" % index, uid)
        for index, cut in enumerate(cuts)
    ]
    return result


def json_text(value):
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )


def add_string_property(obj, name, value):
    obj.addProperty("App::PropertyString", name, "Semantic")
    setattr(obj, name, str(value))


def add_float_property(obj, name, value):
    obj.addProperty("App::PropertyLength", name, "Geometry")
    setattr(obj, name, float(value))


def make_box(box):
    # cuts 的 x/y/z 与 bbox 使用相同的全局坐标，单位为 mm。
    return Part.makeBox(
        box["w"],
        box["d"],
        box["h"],
        App.Vector(box["x"], box["y"], box["z"]),
    )


def create_object(doc, node, used_names):
    bbox = node["bbox"]
    internal_name = safe_name(node["uid"], used_names)
    obj = doc.addObject("Part::Feature", internal_name)
    obj.Label = str(node.get("name") or node["uid"])

    shape = make_box(bbox)
    for cut in node.get("cuts", []):
        shape = shape.cut(make_box(cut))
    obj.Shape = shape

    add_string_property(obj, "SemanticUID", node["uid"])
    add_string_property(obj, "Role", node["role"])
    add_string_property(obj, "SpaceID", node["space_id"])
    add_string_property(obj, "OperatorID", node["operator_id"])
    add_string_property(obj, "MaterialName", node.get("material", "panel"))
    add_string_property(obj, "Grain", node.get("grain", "none"))
    add_string_property(obj, "SourceJSON", json_text(node.get("source", {})))

    add_float_property(obj, "BBoxX", bbox["x"])
    add_float_property(obj, "BBoxY", bbox["y"])
    add_float_property(obj, "BBoxZ", bbox["z"])
    add_float_property(obj, "WidthX", bbox["w"])
    add_float_property(obj, "DepthY", bbox["d"])
    add_float_property(obj, "HeightZ", bbox["h"])

    obj.addProperty("App::PropertyString", "ShapeKind", "Semantic")
    obj.ShapeKind = str(node.get("shape", "box"))
    return obj


def validate_edges(graph, uid_set):
    errors = []
    for index, edge in enumerate(graph["edges"]):
        if not isinstance(edge, dict):
            errors.append("edge[%d] is not an object" % index)
            continue
        a = str(edge.get("a", ""))
        b = str(edge.get("b", ""))
        if a not in uid_set:
            errors.append("edge[%d].a is unknown: %s" % (index, a))
        if b not in uid_set:
            errors.append("edge[%d].b is unknown: %s" % (index, b))
    if errors:
        raise ValueError("; ".join(errors))


def validate_shape(node, obj):
    uid = str(node["uid"])
    shape = obj.Shape
    if shape.isNull() or not shape.isValid() or not shape.Solids:
        raise AssertionError("%s has an invalid or non-solid shape" % uid)

    bbox = node["bbox"]
    original_volume = bbox["w"] * bbox["d"] * bbox["h"]
    volume = float(shape.Volume)
    if not math.isfinite(volume) or volume <= 0:
        raise AssertionError("%s has invalid volume: %r" % (uid, volume))

    cuts = node.get("cuts", [])
    expected_removed = math.fsum(
        cut["w"] * cut["d"] * cut["h"] for cut in cuts
    )
    removed = original_volume - volume

    # 显式抛出 AssertionError，确保 python -O 下仍执行自检。
    if cuts and removed <= 0:
        raise AssertionError("%s: cuts did not reduce board volume" % uid)
    if not math.isfinite(expected_removed) or not math.isclose(
        removed, expected_removed, rel_tol=0.0, abs_tol=VOLUME_TOLERANCE
    ):
        raise AssertionError(
            "%s: removed volume %.9f mm3 != expected %.9f mm3 "
            "(tolerance +/- %.1f mm3); "
            "check cuts for overlap or extension outside the board"
            % (uid, removed, expected_removed, VOLUME_TOLERANCE)
        )
    return volume


def make_row(index, node, obj, volume):
    bbox = node["bbox"]
    row = {
        "index": index,
        "uid": str(node["uid"]),
        "object_name": obj.Name,
        "label": obj.Label,
        "role": node["role"],
        "space_id": node["space_id"],
        "operator_id": node["operator_id"],
        "material": node.get("material", "panel"),
        "grain": node.get("grain", "none"),
        "volume_mm3": volume,
        "source_json": json_text(node.get("source", {})),
        # JSON 数组兼作槽标记及加工信息；无槽为 []。
        "cuts": json_text(node.get("cuts", [])),
    }
    for field in ("x", "y", "z", "w", "d", "h"):
        row[field] = bbox[field]
    return row


def write_csv(path, rows):
    fields = [
        "index",
        "uid",
        "object_name",
        "label",
        "role",
        "space_id",
        "operator_id",
        "material",
        "grain",
        "x",
        "y",
        "z",
        "w",
        "d",
        "h",
        "volume_mm3",
        "source_json",
        "cuts",
    ]

    with open(path, "w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def close_existing_document(name):
    documents = App.listDocuments()
    if name in documents:
        App.closeDocument(name)


def export_graph(graph_path, output_dir):
    graph = load_graph(graph_path)
    nodes = [normalized_node(node) for node in graph["nodes"]]

    uids = [str(node["uid"]) for node in nodes]
    uid_set = set(uids)
    if len(uids) != len(uid_set):
        raise ValueError("Board graph contains duplicate node UIDs")
    validate_edges(graph, uid_set)

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 原源码未提供输出文件名；此处使用文档名及 BOM 常规命名。
    fcstd_path = output_dir / (DOC_NAME + ".FCStd")
    csv_path = output_dir / "board_bom.csv"

    close_existing_document(DOC_NAME)
    doc = App.newDocument(DOC_NAME)
    try:
        used_names = set()
        objects = [create_object(doc, node, used_names) for node in nodes]
        doc.recompute()

        rows = []
        for index, (node, obj) in enumerate(zip(nodes, objects), start=1):
            volume = validate_shape(node, obj)
            rows.append(make_row(index, node, obj, volume))

        doc.saveAs(str(fcstd_path.resolve()))
        write_csv(csv_path, rows)
    except Exception:
        App.closeDocument(doc.Name)
        raise

    print("Exported %d boards" % len(nodes))
    print("FCStd: %s" % fcstd_path)
    print("CSV/BOM: %s" % csv_path)
    return fcstd_path, csv_path


def main(argv=None):
    graph_path, output_dir = parse_args(sys.argv if argv is None else argv)
    try:
        export_graph(graph_path, output_dir)
    except Exception:
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    exit_code = main()
    if exit_code:
        raise SystemExit(exit_code)
