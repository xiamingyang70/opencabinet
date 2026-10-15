# export_fcstd.py
from __future__ import annotations

import csv
import json
import os
import re
import sys
import traceback
from pathlib import Path

import FreeCAD as App
import Part


DEFAULT_GRAPH = "/home/ming/unitcab/out/semantic/board_graph.json"
DEFAULT_OUTPUT = "/home/ming/unitcab/out"
DOC_NAME = "UnitCab"


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


def require_number(value, field, uid):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("%s.%s must be numeric" % (uid, field))
    return float(value)


def normalized_node(node):
    required = ("uid", "role", "bbox", "space_id", "operator_id")
    missing = [field for field in required if field not in node]
    if missing:
        raise ValueError("Node missing fields: %s" % ", ".join(missing))

    uid = str(node["uid"])
    bbox = node["bbox"]
    if not isinstance(bbox, dict):
        raise ValueError("%s.bbox must be an object" % uid)

    values = {}
    for field in ("x", "y", "z", "w", "d", "h"):
        if field not in bbox:
            raise ValueError("%s.bbox.%s is missing" % (uid, field))
        values[field] = require_number(bbox[field], field, uid)

    if values["w"] <= 0 or values["d"] <= 0 or values["h"] <= 0:
        raise ValueError("%s has non-positive dimensions" % uid)

    result = dict(node)
    result["bbox"] = values
    return result


def add_string_property(obj, name, value):
    obj.addProperty("App::PropertyString", name, "Semantic")
    setattr(obj, name, str(value))


def add_float_property(obj, name, value):
    obj.addProperty("App::PropertyLength", name, "Geometry")
    setattr(obj, name, float(value))


def create_object(doc, node, used_names):
    bbox = node["bbox"]
    internal_name = safe_name(node["uid"], used_names)
    obj = doc.addObject("Part::Feature", internal_name)

    obj.Label = str(node.get("name") or node["uid"])
    obj.Shape = Part.makeBox(
        bbox["w"],
        bbox["d"],
        bbox["h"],
        App.Vector(bbox["x"], bbox["y"], bbox["z"]),
    )

    add_string_property(obj, "SemanticUID", node["uid"])
    add_string_property(obj, "Role", node["role"])
    add_string_property(obj, "SpaceID", node["space_id"])
    add_string_property(obj, "OperatorID", node["operator_id"])
    add_string_property(obj, "MaterialName", node.get("material", "panel"))
    add_string_property(obj, "Grain", node.get("grain", "none"))
    add_string_property(
        obj,
        "SourceJSON",
        json.dumps(
            node.get("source", {}),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )

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

    uids = [node["uid"] for node in nodes]
    if len(uids) != len(set(uids)):
        raise ValueError("Duplicate node uid in board graph")

    validate_edges(graph, set(uids))

    output_dir.mkdir(parents=True, exist_ok=True)
    fcstd_path = output_dir / "cab.FCStd"
    csv_path = output_dir / "boards.csv"

    close_existing_document(DOC_NAME)
    doc = App.newDocument(DOC_NAME)
    used_names = set()
    rows = []

    try:
        for index, node in enumerate(nodes, 1):
            obj = create_object(doc, node, used_names)
            bbox = node["bbox"]

            if obj.Shape.isNull():
                raise ValueError("%s produced a null shape" % node["uid"])

            expected_volume = bbox["w"] * bbox["d"] * bbox["h"]
            if abs(float(obj.Shape.Volume) - expected_volume) > 0.01:
                raise ValueError(
                    "%s volume mismatch: %.6f versus %.6f"
                    % (node["uid"], obj.Shape.Volume, expected_volume)
                )

            rows.append(
                {
                    "index": index,
                    "uid": node["uid"],
                    "object_name": obj.Name,
                    "label": obj.Label,
                    "role": node["role"],
                    "space_id": node["space_id"],
                    "operator_id": node["operator_id"],
                    "material": node.get("material", "panel"),
                    "grain": node.get("grain", "none"),
                    "x": bbox["x"],
                    "y": bbox["y"],
                    "z": bbox["z"],
                    "w": bbox["w"],
                    "d": bbox["d"],
                    "h": bbox["h"],
                    "volume_mm3": expected_volume,
                    "source_json": json.dumps(
                        node.get("source", {}),
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                }
            )

        doc.addProperty("App::PropertyString", "GraphSource", "Export")
        doc.GraphSource = str(graph_path)
        doc.addProperty("App::PropertyInteger", "NodeCount", "Export")
        doc.NodeCount = len(nodes)
        doc.addProperty("App::PropertyInteger", "EdgeCount", "Export")
        doc.EdgeCount = len(graph["edges"])

        doc.recompute()
        doc.saveAs(str(fcstd_path))
        write_csv(csv_path, rows)

        if not fcstd_path.exists() or fcstd_path.stat().st_size <= 0:
            raise IOError("FCStd file was not created")
        if not csv_path.exists() or csv_path.stat().st_size <= 0:
            raise IOError("CSV file was not created")

        print("EXPORT_OK")
        print("GRAPH=%s" % graph_path)
        print("FCSTD=%s" % fcstd_path)
        print("CSV=%s" % csv_path)
        print("NODES=%d" % len(nodes))
        print("EDGES=%d" % len(graph["edges"]))
    finally:
        documents = App.listDocuments()
        if DOC_NAME in documents:
            App.closeDocument(DOC_NAME)


def main():
    graph_path, output_dir = parse_args(sys.argv)
    try:
        export_graph(graph_path, output_dir)
    except Exception:
        print("EXPORT_FAILED")
        traceback.print_exc()


main()
