# -*- coding: utf-8 -*-
"""spec_builder.v1 — 简化输入 -> 完整CabinetSpec

FreeCAD插件/外部用户共用的入口。用户只给 宽/高/深/格数/每格功能,
这里负责: 列宽解算(门缝对齐分板中心) + facade_openings构造。
门禁忠实映射: 每格1门(引擎要求门缝=分板中心); 门宽越界只告警不拦。
"""
from __future__ import annotations

import sys


def build_spec(width, height, depth=580, bays=2, bay_functions=None,
               plinth=60, back="grooved_10", board_thickness=18, gap=2.5,
               cabinet_type="wardrobe"):
    """bay_functions: 每格功能, 如 ['hang','fold','fold']。
    支持 hang/fold/shelf。None默认 = 首格挂衣, 其余叠放。"""
    t, g = float(board_thickness), float(gap)
    n = int(bays)
    if n < 1:
        raise ValueError("bays must be >= 1")
    bay_functions = list(bay_functions) if bay_functions else (
        ["hang"] + ["fold"] * (n - 1))
    if len(bay_functions) != n:
        raise ValueError("bay_functions length != bays")

    functions, hang_cols, fold_cfg = [], [], None
    for i, f in enumerate(bay_functions):
        if f == "hang":
            functions.append("hang"); hang_cols.append(i)
        elif f == "fold":
            functions.append("fold")
            if fold_cfg is None:
                fold_cfg = {"column": i, "top": True}
        elif f == "shelf":
            functions.append("fold")
            if fold_cfg is None:
                fold_cfg = {"column": i, "top": False}
        else:
            raise ValueError("unknown bay function: %r" % f)
    if not functions:
        functions = ["fold"]; fold_cfg = {"column": 0, "top": True}

    # 列宽解算: 门缝中心 s_i = i*(dw+g) - g/2 必须落在分板i中心 (i=1..n-1)
    # 门均分整宽(无walls声明: 门板盖侧板, span=width)
    dw = (width - g * (n - 1)) / n
    centers = [i * (dw + g) - g / 2.0 for i in range(1, n)]
    colw = [centers[0] - 1.5 * t]
    for i in range(1, n - 1):
        colw.append(centers[i] - centers[i - 1] - t)
    used = sum(colw) + t * (n + 1)
    colw.append(width - used)
    if min(colw) <= 50:
        raise ValueError(
            "columns too thin after seam alignment: %s" %
            [round(v, 1) for v in colw])

    spec = {
        "board_thickness": t, "gap": g, "plinth_height": plinth,
        "back": back, "id": "wb", "cabinet_type": cabinet_type,
        "width": width, "depth": depth, "height": height,
        "functions": functions, "style": {
            "doors": {"mode": "full_height", "count": n, "gap": g},
            "drawer": {"count": 0},
            "hang_columns": hang_cols, "fold": fold_cfg},
        "column_count": n, "column_widths": colw,
        "facade_openings": [
            {"bay_indices": [i], "door_count": 1} for i in range(n)],
        "door_count": n,
    }
    return spec


def solve_simple(width, height, depth=580, bays=2, bay_functions=None,
                 plinth=60, back="grooved_10"):
    """build_spec + solve + groover。返回 (graph_with_cuts, self_check)。"""
    from opencabinet.solver import solve
    from opencabinet.groover import enhance_board_graph
    spec = build_spec(width, height, depth=depth, bays=bays,
                      bay_functions=bay_functions, plinth=plinth, back=back)
    result = solve(spec)
    graph = enhance_board_graph(result["board_graph"])
    return graph, result["self_check"]


if __name__ == "__main__":
    for w, b in [(1200, 2), (1800, 3), (2400, 4)]:
        graph, sc = solve_simple(w, 2200, bays=b, bay_functions=["fold"] * b)
        print("%dx%d: %d boards pass=%s" % (w, b, sc["board_count"], sc["passed"]))
