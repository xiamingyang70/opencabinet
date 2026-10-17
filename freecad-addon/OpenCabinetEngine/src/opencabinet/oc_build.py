# -*- coding: utf-8 -*-
"""oc_build.py — board_graph -> FreeCAD solids (in current document)

与 export_fcstd.py 同源的几何逻辑(Part::Feature + makeBox), 增:
- node["cuts"] 嵌槽实体切割(匹配铁律: Part::Feature做Shape赋值)
- 语义属性(SemanticUID/Role/尺寸链) 同引擎导出
"""
import json
import re

import FreeCAD as App
import Part


def safe_name(value, used):
    name = re.sub(r"[^A-Za-z0-9_]", "_", str(value))
    name = re.sub(r"_+", "_", name).strip("_") or "Board"
    if name[0].isdigit():
        name = "B_" + name
    base, name, index = name[:60], name[:60], 1
    while name in used:
        suffix = "_%d" % index
        name = base[: 60 - len(suffix)] + suffix
        index += 1
    used.add(name)
    return name


def build_boards(doc, graph, used_names=None):
    """在doc中生成所有板件。返回 (objects, errors)。cuts失败不致命,记入errors。"""
    used_names = used_names if used_names is not None else set()
    objects, errors = [], []
    for node in graph["nodes"]:
        bbox = node["bbox"]
        internal = safe_name(node["uid"], used_names)
        obj = doc.addObject("Part::Feature", internal)
        obj.Label = str(node.get("name") or node["uid"])
        shape = Part.makeBox(
            float(bbox["w"]), float(bbox["d"]), float(bbox["h"]),
            App.Vector(float(bbox["x"]), float(bbox["y"]), float(bbox["z"])))
        # 嵌槽切割(cuts为全局坐标盒)
        for cut in node.get("cuts") or []:
            try:
                cutter = Part.makeBox(
                    float(cut["w"]), float(cut["d"]), float(cut["h"]),
                    App.Vector(float(cut["x"]), float(cut["y"]), float(cut["z"])))
                shape = shape.cut(cutter)
            except Exception as exc:  # 槽失败不拦整柜
                errors.append("%s cut failed: %s" % (node["uid"], exc))
        obj.Shape = shape

        # 语义属性(与引擎导出一致)
        for prop, key in (("SemanticUID", "uid"), ("Role", "role"),
                          ("SpaceID", "space_id"), ("OperatorID", "operator_id")):
            obj.addProperty("App::PropertyString", prop, "Semantic")
            setattr(obj, prop, str(node.get(key, "")))
        obj.addProperty("App::PropertyString", "MaterialName", "Semantic")
        obj.MaterialName = str(node.get("material", "panel"))
        for prop, key in (("BBoxX", "x"), ("BBoxY", "y"), ("BBoxZ", "z"),
                          ("WidthX", "w"), ("DepthY", "d"), ("HeightZ", "h")):
            obj.addProperty("App::PropertyFloat", prop, "Geometry")
            setattr(obj, prop, float(bbox[key]))
        objects.append(obj)
    return objects, errors


def build_from_graph(graph, doc_name="OpenCabinet"):
    """新建文档生成整柜。返回 (doc, objects, errors)。"""
    existing = App.listDocuments()
    if doc_name in existing:
        App.closeDocument(doc_name)
    doc = App.newDocument(doc_name)
    objects, errors = build_boards(doc, graph)
    doc.recompute()
    return doc, objects, errors
