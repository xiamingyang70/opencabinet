#!/usr/bin/env python3
"""将 board_graph 中的 groove 边转换为背板槽切除体。"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import unittest
from pathlib import Path
from typing import Any


GROOVE_DEPTH = 10.0
EPSILON = 1e-9


def _nodes(graph: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = graph.get("nodes", [])
    if isinstance(nodes, list):
        return nodes
    if isinstance(nodes, dict):
        return list(nodes.values())
    raise ValueError("graph.nodes 必须是列表或字典")


def _node_map(graph: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result = {}
    for node in _nodes(graph):
        if not isinstance(node, dict) or "uid" not in node:
            raise ValueError("节点必须包含 uid")
        result[node["uid"]] = node
    return result


def _bbox(node: dict[str, Any]) -> dict[str, float]:
    value = node.get("bbox")
    if not isinstance(value, dict):
        raise ValueError(f"节点 {node.get('uid')} 缺少 bbox")

    required = ("x", "y", "z", "w", "d", "h")
    if any(key not in value for key in required):
        raise ValueError(f"节点 {node.get('uid')} 的 bbox 字段不完整")

    return {key: float(value[key]) for key in required}


def _overlap(start_a: float, size_a: float,
             start_b: float, size_b: float) -> tuple[float, float]:
    start = max(start_a, start_b)
    end = min(start_a + size_a, start_b + size_b)
    if end - start <= EPSILON:
        raise ValueError("背板与节点没有有效接触长度")
    return start, end


def _is_side(role: str) -> bool:
    role = role.lower()
    return ("side" in role or "left" in role or "right" in role
            or "partition" in role)


def _is_top_or_bottom(role: str) -> bool:
    role = role.lower()
    return ("top" in role or "bottom" in role or "shelf" in role
            or "horizontal" in role)


def _make_cut(node: dict[str, Any], back: dict[str, Any]) -> dict[str, float | str]:
    nb = _bbox(node)
    bb = _bbox(back)
    role = str(node.get("role", ""))

    bt = bb["d"]
    if bt <= 0:
        raise ValueError("背板厚度 bt 必须大于 0")
    if GROOVE_DEPTH > nb["d"] + EPSILON:
        raise ValueError(f"节点 {node.get('uid')} 的深度不足以容纳 10mm 槽深")

    # 嵌槽=背板占位: 槽体Y区间与背板Y区间一致,背板插入后填满槽
    y = bb["y"]

    if _is_side(role):
        # 侧板槽：X 方向为背板厚度，Z 方向为接触净长。
        z1, z2 = _overlap(nb["z"], nb["h"], bb["z"], bb["h"])

        if "partition" in role:
            # 立板: 背板贯穿,槽=立板全厚X × 背板厚Y
            x1 = nb["x"]
            x2 = x1 + nb["w"]
        else:
            node_center_x = nb["x"] + nb["w"] / 2.0
            back_center_x = bb["x"] + bb["w"] / 2.0
            if back_center_x >= node_center_x:
                x2 = nb["x"] + nb["w"]
                x1 = x2 - bt
            else:
                x1 = nb["x"]
                x2 = x1 + bt

        cut = {
            "x": x1,
            "y": y,
            "z": z1,
            "w": x2 - x1,
            "d": bt,
            "h": z2 - z1,
            "purpose": "back_groove",
        }

    elif _is_top_or_bottom(role):
        # 顶/底/层板槽：X=接触净长，Y=背板厚。Z向: 顶底板取背板厚侧,层板(shelf)全厚贯穿
        x1, x2 = _overlap(nb["x"], nb["w"], bb["x"], bb["w"])

        if "shelf" in role or "horizontal" in role:
            z1 = nb["z"]
            z2 = z1 + nb["h"]
        else:
            node_center_z = nb["z"] + nb["h"] / 2.0
            back_center_z = bb["z"] + bb["h"] / 2.0
            if back_center_z >= node_center_z:
                z2 = nb["z"] + nb["h"]
                z1 = z2 - bt
            else:
                z1 = nb["z"]
                z2 = z1 + bt

        cut = {
            "x": x1,
            "y": y,
            "z": z1,
            "w": x2 - x1,
            "d": bt,
            "h": z2 - z1,
            "purpose": "back_groove",
        }

    else:
        raise ValueError(
            f"无法确定节点 {node.get('uid')} ({role}) 的背板槽方向"
        )

    _validate_cut_inside(node, cut)
    return cut


def _validate_cut_inside(
    node: dict[str, Any],
    cut: dict[str, float | str],
) -> None:
    box = _bbox(node)

    checks = (
        ("x", "w"),
        ("y", "d"),
        ("z", "h"),
    )
    for start_key, size_key in checks:
        cut_start = float(cut[start_key])
        cut_end = cut_start + float(cut[size_key])
        box_start = box[start_key]
        box_end = box_start + box[size_key]

        if (
            cut_start < box_start - EPSILON
            or cut_end > box_end + EPSILON
            or float(cut[size_key]) < -EPSILON
        ):
            raise ValueError(
                f"节点 {node.get('uid')} 的槽越界："
                f"{start_key}={cut_start}, end={cut_end}, "
                f"允许范围 [{box_start}, {box_end}]"
            )


def enhance_board_graph(graph: dict[str, Any]) -> dict[str, Any]:
    """返回增加 cuts 后的 board_graph，不修改传入对象。"""
    result = copy.deepcopy(graph)
    edges = result.get("edges", [])

    groove_edges = [
        edge for edge in edges
        if isinstance(edge, dict) and edge.get("type") == "groove"
    ]
    if not groove_edges:
        return result

    explicit_back = result.get("back")
    if explicit_back is not None and str(explicit_back).lower() == "none":
        return result

    nodes = _node_map(result)

    if isinstance(explicit_back, str):
        back = nodes.get(explicit_back)
        if back is None:
            raise ValueError(f"找不到背板节点 {explicit_back}")
    else:
        back = next(
            (node for node in nodes.values()
             if str(node.get("role", "")).lower() == "back"),
            None,
        )

    if back is None:
        back = next(
            (node for node in nodes.values()
             if str(node.get("role", "")).lower() == "back"),
            None,
        )
    if back is None:
        return result

    back_uid = back["uid"]

    for edge in groove_edges:
        from_uid = edge.get("from") or edge.get("a")
        to_uid = edge.get("to") or edge.get("b")

        if from_uid == back_uid:
            node_uid = to_uid
        elif to_uid == back_uid:
            node_uid = from_uid
        else:
            raise ValueError("groove 边必须连接背板节点")

        node = nodes.get(node_uid)
        if node is None:
            raise ValueError(f"找不到 groove 边连接的节点 {node_uid}")
        if node_uid == back_uid:
            continue

        cuts = node.setdefault("cuts", [])
        if not isinstance(cuts, list):
            raise ValueError(f"节点 {node_uid} 的 cuts 必须是列表")
        cuts.append(_make_cut(node, back))

    return result


def process_file(
    input_path: str | Path,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    input_path = Path(input_path)
    with input_path.open("r", encoding="utf-8") as stream:
        graph = json.load(stream)

    enhanced = enhance_board_graph(graph)

    if output_path is None:
        json.dump(enhanced, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        output_path = Path(output_path)
        with output_path.open("w", encoding="utf-8") as stream:
            json.dump(enhanced, stream, ensure_ascii=False, indent=2)
            stream.write("\n")

    return enhanced


class GrooverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.graph = {
            "nodes": [
                {
                    "uid": "carcass.left",
                    "role": "side_left",
                    "bbox": {
                        "x": 0.0, "y": 20.0, "z": 0.0,
                        "w": 18.0, "d": 580.0, "h": 2400.0,
                    },
                },
                {
                    "uid": "carcass.back",
                    "role": "back",
                    "bbox": {
                        "x": 18.0, "y": 559.0, "z": 78.0,
                        "w": 564.0, "d": 5.0, "h": 2304.0,
                    },
                },
            ],
            "edges": [
                {
                    "from": "carcass.left",
                    "to": "carcass.back",
                    "type": "groove",
                    "value": 11600.0,
                },
            ],
        }

    def test_side_groove(self) -> None:
        result = enhance_board_graph(self.graph)
        self.assertEqual(
            result["nodes"][0]["cuts"],
            [{
                "x": 13.0,
                "y": 590.0,
                "z": 78.0,
                "w": 5.0,
                "d": 10.0,
                "h": 2304.0,
                "purpose": "back_groove",
            }],
        )

    def test_no_groove_returns_unchanged(self) -> None:
        graph = copy.deepcopy(self.graph)
        graph["edges"] = []
        self.assertEqual(enhance_board_graph(graph), graph)

    def test_back_none_returns_unchanged(self) -> None:
        graph = copy.deepcopy(self.graph)
        graph["back"] = "none"
        self.assertEqual(enhance_board_graph(graph), graph)

    def test_out_of_bounds_raises(self) -> None:
        graph = copy.deepcopy(self.graph)
        graph["nodes"][0]["bbox"]["w"] = 3.0
        with self.assertRaises(ValueError):
            enhance_board_graph(graph)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="将 board_graph 的 groove 边转换为背板槽 cuts"
    )
    parser.add_argument("input", help="输入 board_graph.json 路径")
    parser.add_argument(
        "output",
        nargs="?",
        help="输出路径；省略时写入标准输出",
    )
    args = parser.parse_args()
    process_file(args.input, args.output)


if __name__ == "__main__":
    if len(sys.argv) == 1:
        unittest.main()
    else:
        main()
