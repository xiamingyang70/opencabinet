# operators.py
from __future__ import annotations

import sys
from copy import deepcopy
from typing import Any, Callable, Mapping, Sequence


OPERATOR_REGISTRY: dict[str, dict[str, Any]] = {}
EMITTER_REGISTRY: dict[str, Callable[..., dict[str, Any]]] = {}


def register_operator(definition: Mapping[str, Any]):
    operator_id = str(definition["id"])

    def decorator(function):
        if operator_id in OPERATOR_REGISTRY:
            raise ValueError(f"Duplicate operator: {operator_id}")
        OPERATOR_REGISTRY[operator_id] = deepcopy(dict(definition))
        EMITTER_REGISTRY[operator_id] = function
        return function

    return decorator


def get_operator(operator_id: str) -> dict[str, Any]:
    if operator_id not in OPERATOR_REGISTRY:
        raise KeyError(f"Unknown operator: {operator_id}")
    return deepcopy(OPERATOR_REGISTRY[operator_id])


def list_operators() -> list[str]:
    return sorted(OPERATOR_REGISTRY)


def emit_operator(
    operator_id: str,
    spec: Mapping[str, Any],
    space: Mapping[str, Any],
    params: Mapping[str, Any] | None = None,
    instance_id: str | None = None,
) -> dict[str, Any]:
    if operator_id not in EMITTER_REGISTRY:
        raise KeyError(f"Unknown operator: {operator_id}")
    return EMITTER_REGISTRY[operator_id](
        spec,
        space,
        dict(params or {}),
        instance_id or operator_id.replace(".", "_"),
    )


def _box(x, y, z, w, d, h):
    values = tuple(float(v) for v in (x, y, z, w, d, h))
    if min(values[3:]) <= 0:
        raise ValueError(f"Non-positive board dimensions: {values}")
    return dict(zip(("x", "y", "z", "w", "d", "h"), values))


def _node(uid, role, bbox, space_id, operator_id, **extra):
    node = {
        "uid": uid,
        "name": extra.pop("name", uid),
        "role": role,
        "bbox": bbox,
        "space_id": space_id,
        "operator_id": operator_id,
        "material": extra.pop("material", "panel"),
        "grain": extra.pop("grain", "none"),
        "source": extra.pop("source", {"rule_id": operator_id}),
    }
    node.update(extra)
    return node


def _edge(
    a,
    b,
    kind,
    contact=0.0,
    overlap=0.0,
    tolerance=0.1,
    **extra,
):
    edge = {
        "a": a,
        "b": b,
        "type": kind,
        "expected_contact_mm2": max(0.0, float(contact)),
        "allowed_overlap_mm3": max(0.0, float(overlap)),
        "tolerance": float(tolerance),
    }
    edge.update(extra)
    return edge


def _space_box(space):
    return space["bbox"]


def _interval_overlap(a0, a1, b0, b1, tolerance=1e-9):
    return min(a1, b1) - max(a0, b0) > tolerance


def split_horizontal_for_drawers(
    board_bbox: Mapping[str, float],
    drawer_envelopes: Sequence[Mapping[str, float]],
    tolerance: float = 0.01,
) -> list[dict[str, float]]:
    z0 = float(board_bbox["z"])
    z1 = z0 + float(board_bbox["h"])
    cuts = []
    for envelope in drawer_envelopes:
        ez0 = float(envelope["z"])
        ez1 = ez0 + float(envelope["h"])
        if _interval_overlap(z0, z1, ez0, ez1, tolerance):
            cuts.append(
                (
                    float(envelope["x"]),
                    float(envelope["x"]) + float(envelope["w"]),
                )
            )

    x0 = float(board_bbox["x"])
    x1 = x0 + float(board_bbox["w"])
    clipped = sorted(
        (max(x0, a), min(x1, b))
        for a, b in cuts
        if min(x1, b) - max(x0, a) > tolerance
    )
    merged = []
    for a, b in clipped:
        if merged and a <= merged[-1][1] + tolerance:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))

    segments = []
    cursor = x0
    for a, b in merged:
        if a - cursor > tolerance:
            segment = dict(board_bbox)
            segment["x"] = cursor
            segment["w"] = a - cursor
            segments.append(segment)
        cursor = max(cursor, b)
    if x1 - cursor > tolerance:
        segment = dict(board_bbox)
        segment["x"] = cursor
        segment["w"] = x1 - cursor
        segments.append(segment)
    return segments


CARCASS = {
    "id": "carcass.v1",
    "version": "1.0.0",
    "level": "construction",
    "input_spaces": [{"kind": "cabinet", "min_w": 100, "min_d": 100, "min_h": 100}],
    "occupancy": [],
    "interfaces": [],
    "clearance_rules": [{
        "when": "horizontal Z interval intersects drawer envelope",
        "action": "split",
        "target_role": "horizontal_partition",
        "expression": "interval_overlap(board.z, board.h, drawer.z, drawer.h)",
    }],
    "cabinet_types": ["bedside", "wardrobe", "bookcase", "sideboard", "generic"],
    "parameters": {
        "thickness": 18,
        "struct_y": 20,
        "back_thickness": 10,
        "back_front_offset": 28,
    },
    "produces": ["side_left", "side_right", "bottom", "top", "back", "horizontal_partition"],
    "evidence": [{"source": "golden_case", "metric": "geometry", "query_or_case": "R1-R3"}],
}


@register_operator(CARCASS)
def emit_carcass(spec, space, params, instance_id):
    b = _space_box(space)
    x, y, z = float(b["x"]), float(b["y"]), float(b["z"])
    w, d, h = float(b["w"]), float(b["d"]), float(b["h"])
    t = float(params.get("thickness", spec.get("board_thickness", 18)))
    sy = float(params.get("struct_y", 20))
    plinth = float(params.get("plinth_height", spec.get("plinth_height", 60)))
    # base_z is the bottom face of the bottom board (top of plinth)
    base_z = float(params.get("base_z", z + (plinth if plinth > 0 else 0.0)))
    trim_h = float(params.get("top_trim_height", 0.0))
    exposed = [v for v in (params.get("exposed_sides") or []) if v in ("left", "right")]
    top_z = z + h - t - trim_h           # r99：顶收口条占高 60，顶板下移
    if w <= 2 * t or d <= sy or base_z + t > top_z:
        raise ValueError("Cabinet is too small for carcass.v1")

    sid = space["id"]
    op = "carcass.v1"
    ids = {name: f"{instance_id}.{name}" for name in ("left", "right", "bottom", "top", "back")}
    nodes = [
        # r99：不靠墙（见光）侧板满深 Y=0 —— 前脸在门板之前，盖住门板
        _node(ids["left"], "side_left",
              _box(x, y, z, t, d, h) if "left" in exposed else _box(x, y + sy, z, t, d - sy, h),
              sid, op, grain="z"),
        _node(ids["right"], "side_right",
              _box(x + w - t, y, z, t, d, h) if "right" in exposed else _box(x + w - t, y + sy, z, t, d - sy, h),
              sid, op, grain="z"),
        _node(ids["bottom"], "bottom", _box(x + t, y + sy, base_z, w - 2*t, d - sy, t), sid, op, grain="x"),
        _node(ids["top"], "top", _box(x + t, y + sy, top_z, w - 2*t, d - sy, t), sid, op, grain="x"),
    ]
    edges = [
        _edge(ids["left"], ids["bottom"], "support", t*(d-sy)),
        _edge(ids["right"], ids["bottom"], "support", t*(d-sy)),
        _edge(ids["left"], ids["top"], "butt", t*(d-sy)),
        _edge(ids["right"], ids["top"], "butt", t*(d-sy)),
    ]

    if trim_h > 0:
        _t1, _t2 = f"{instance_id}.top_trim_front", f"{instance_id}.top_trim_rear"
        nodes.extend([
            _node(_t1, "top_trim_front", _box(x + t, y + sy, z + h - trim_h, w - 2 * t, t, trim_h), sid, op, grain="x"),
            _node(_t2, "top_trim_rear", _box(x + t, y + sy + t, z + h - trim_h, w - 2 * t, 60.0, t), sid, op, grain="x"),
        ])
        edges.extend([
            _edge(ids["left"], _t1, "butt", t * t),
            _edge(ids["right"], _t1, "butt", t * t),
            _edge(_t1, _t2, "butt", (w - 2 * t) * t),
        ])

    back_mode = params.get("back", spec.get("back", "grooved_10"))
    if back_mode != "none":
        # Explicit new mode; legacy solver still supplies 10/28.
        if back_mode == "grooved_5":
            bt = 5.0
            by = y + d - 23.0
        else:
            bt = float(params.get("back_thickness", 10))
            by = y + d - float(params.get("back_front_offset", 28))
        bz = base_z + t
        bh = top_z - bz
        nodes.append(_node(
            ids["back"], "back",
            _box(x + t, by, bz, w - 2*t, bt, bh),
            sid, op, grain="z",
        ))
        edges.extend([
            _edge(ids["left"], ids["back"], "groove", bt*bh),
            _edge(ids["right"], ids["back"], "groove", bt*bh),
            _edge(ids["bottom"], ids["back"], "groove", bt*(w-2*t)),
            _edge(ids["top"], ids["back"], "groove", bt*(w-2*t)),
        ])

    drawer_envelopes = params.get("drawer_envelopes", [])
    for index, hz in enumerate(params.get("horizontal_z", [])):
        original = _box(x + t, y + sy, float(hz), w - 2*t, d - sy, t)
        pieces = split_horizontal_for_drawers(original, drawer_envelopes)
        for part_index, piece in enumerate(pieces):
            uid = f"{instance_id}.horizontal_{index}_{part_index}"
            nodes.append(_node(uid, "horizontal_partition", piece, sid, op, grain="x"))
            edges.extend([
                _edge(ids["left"], uid, "support", t*(d-sy)),
                _edge(ids["right"], uid, "support", t*(d-sy)),
            ])
            if back_mode != "none":
                connection_bt = 5.0 if back_mode == "grooved_5" else 10.0
                overlap = t * float(piece["w"]) * min(connection_bt, float(piece["d"]))
                edges.append(_edge(
                    uid, ids["back"], "groove",
                    contact=connection_bt*float(piece["w"]),
                    overlap=overlap,
                ))


    if "top_trim_front" in spec:
        import math
        cfg = spec["top_trim_front"]
        if not isinstance(cfg, dict) or set(cfg) != {"height"}:
            raise ValueError("top_trim_front must contain only height")
        raw_height = cfg["height"]
        if isinstance(raw_height, bool) or not isinstance(raw_height, (int, float)):
            raise ValueError("top_trim_front.height must be numeric")
        front_h = float(raw_height)
        if not math.isfinite(front_h) or not t <= front_h <= h:
            raise ValueError("top_trim_front.height must be finite and in [t,H]")
        if abs(t - 18.0) > 1e-8 or abs(sy - 20.0) > 1e-8:
            raise ValueError("Front header requires panel=18 and STRUCT_Y=20")
        if trim_h != 0.0:
            raise ValueError("top_trim_front conflicts with legacy top_trim")
        if exposed:
            raise ValueError("Full-width front header intersects exposed sides")

        header_id = f"{instance_id}.front_header"
        nodes.append(_node(
            header_id, "top_trim_front",
            _box(x, y + 2.0, z + h - front_h, w, t, front_h),
            sid, op, grain="x",
        ))
        edges.extend([
            _edge(ids["left"], header_id, "butt", t * front_h),
            _edge(ids["right"], header_id, "butt", t * front_h),
            _edge(ids["top"], header_id, "butt", (w - 2*t) * t),
        ])

    return {"nodes": nodes, "edges": edges}


DRAWER = {
    "id": "drawer_module.v1",
    "version": "1.0.0",
    "level": "function",
    "input_spaces": [{"kind": "drawer_column", "min_w": 120, "min_d": 180, "min_h": 147}],
    "occupancy": [],
    "interfaces": [],
    "clearance_rules": [],
    "cabinet_types": ["bedside", "wardrobe", "sideboard", "generic"],
    "parameters": {"front_y": 2, "box_y": 20},
    # r86/DEF-1：原 `slide_clearance: 13` 为"已发现未消费的隐式默认"，已删除；
    # 单边间隙改由 solver 依据 spec.drawer_slide_type 派生后**显式传入并校验**（不在此注册表表达）。
    "produces": ["drawer_front", "drawer_side", "drawer_bottom", "drawer_back"],
    "evidence": [{"source": "golden_case", "metric": "drawer_heights", "query_or_case": "R1-R3"}],
}


@register_operator(DRAWER)
def emit_drawers(spec, space, params, instance_id):
    b = _space_box(space)
    x, y, z = float(b["x"]), float(b["y"]), float(b["z"])
    w, d, h = float(b["w"]), float(b["d"]), float(b["h"])
    t = float(params.get("thickness", spec.get("board_thickness", 18)))
    gap = float(params.get("gap", spec.get("gap", 2.5)))
    heights = [float(v) for v in params.get("heights", [147, 187])]
    if not heights:
        raise ValueError("drawer_module.v1 requires at least one drawer")
    total = sum(heights) + gap * (len(heights) - 1)
    if total > h + 0.01:
        raise ValueError("Drawer fronts exceed drawer-column height")
    max_ratio = float(params.get("drawer_height_ratio_cap", 0.6))
    if max_ratio > 0 and z + total > float(spec["height"]) * max_ratio + 0.01:
        raise ValueError("Drawers must remain below ratio cap of cabinet height")

    front_y = float(params.get("front_y", 2))
    front_depth = float(params.get("front_depth", 20))
    box_y = float(params.get("box_y", 20))
    # R5/C4/C5 修订：撤销 d - box_y - 46 的局部缺省。
    # 局部空间缺少柜深、背板策略与全局前缘上下文，无法复算求解器公式，
    # 独立调用缺参时明确拒绝，而不是复制一份局部公式。
    if "box_depth" not in params:
        raise ValueError(
            "drawer_module.v1 requires an explicit box_depth from the solver; "
            "refusing to derive a local default without cabinet depth, "
            "back policy and global drawer-front context"
        )
    import math

    raw_box_depth = params["box_depth"]
    if isinstance(raw_box_depth, bool):
        raise ValueError(
            "drawer_module.v1 box_depth must be finite and positive"
        )
    try:
        box_depth = float(raw_box_depth)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            "drawer_module.v1 box_depth must be finite and positive"
        ) from exc
    if not math.isfinite(box_depth) or box_depth <= 0:
        raise ValueError(
            "drawer_module.v1 box_depth must be finite and positive"
        )
    bottom_t = float(params.get("bottom_thickness", 12))
    side_t = float(params.get("side_thickness", 18))
    # —— r90：轨型由**抽侧板高度**逐抽推导；间隙随之确定（无无条件默认）——
    RAIL_BY_TYPE = {"three_section": 13.0, "undermount": 5.0}
    if "drawer_slide_type" in params and params.get("drawer_slide_type") not in RAIL_BY_TYPE:
        raise ValueError("drawer_module.v1 drawer_slide_type must be one of %s; got %r"
                         % (sorted(RAIL_BY_TYPE), params.get("drawer_slide_type")))
    _clr = params.get("slide_clearance_by_index")
    if _clr is not None and (not isinstance(_clr, (list, tuple)) or len(_clr) != len(heights)):
        raise ValueError("drawer_module.v1 slide_clearance_by_index must be a list matching drawer count")
    _typ = params.get("slide_type_by_index")
    if _typ is not None and (not isinstance(_typ, (list, tuple)) or len(_typ) != len(heights)):
        raise ValueError("drawer_module.v1 slide_type_by_index must be a list matching drawer count")
    sid, op = space["id"], "drawer_module.v1"
    nodes, edges, envelopes = [], [], []
    cursor = z

    for index, front_h in enumerate(heights):
        # 抽面高必须有限且为正：否则 max(40, NaN) 会静默取 40 并生成 NaN 高度的抽面
        if isinstance(front_h, bool) or not isinstance(front_h, (int, float)) or not math.isfinite(front_h) or front_h <= 0:
            raise ValueError("drawer_module.v1 heights[%d]=%r 非法（需有限且为正；布尔亦拒绝）" % (index, front_h))
        prefix = f"{instance_id}.drawer_{index}"
        # —— r90 逐抽：抽侧板高（标准构造）→ 轨型 → 单边间隙 → 底距 ——
        side_h_std = max(40.0, front_h - 25.0)
        derived_type = "undermount" if side_h_std >= 100.0 else "three_section"
        if _typ is not None and _typ[index] != derived_type:
            raise ValueError("drawer %d: passed slide type %r contradicts geometry-derived %r (侧板高 %.3f)"
                             % (index, _typ[index], derived_type, side_h_std))
        if params.get("drawer_slide_type") not in (None, derived_type):
            raise ValueError("drawer %d: spec drawer_slide_type %r contradicts geometry-derived %r"
                             % (index, params["drawer_slide_type"], derived_type))
        slide_clearance = RAIL_BY_TYPE[derived_type]
        if _clr is not None and abs(float(_clr[index]) - slide_clearance) > 1e-9:
            raise ValueError("drawer %d: passed clearance %.4f contradicts geometry-derived %.1f"
                             % (index, float(_clr[index]), slide_clearance))
        # 程总 2026-10-02 修正：托底轨在既有 12 之外**再占 30** ⇒ 底距 12+30=42；抽侧板高减 30
        bottom_gap = 42.0 if derived_type == "undermount" else 12.0
        bx = x + slide_clearance
        bw = w - 2.0 * slide_clearance
        # —— r96：内抽在铰链侧让位（程总 2026-10-02："内抽在铰链侧多让80"）——
        _hi = params.get("hinge_inset") or {}
        if _hi and "sides" in _hi and "side" not in _hi:
            _hi = dict(_hi)
            _hi["side"] = "both" if len(_hi["sides"]) > 1 else (_hi["sides"][0] if _hi["sides"] else None)
        _kind = params.get("drawer_kind")
        if _hi and _kind != "inner":
            raise ValueError("drawer_module.v1 hinge_inset may only apply to an inner drawer "
                             "(drawer_kind=%r) - inner_drawer_hinge_inset.v1 only applies to inner drawers" % (_kind,))
        if _kind == "inner" and not _hi:
            raise ValueError("drawer_module.v1 inner drawer requires hinge_inset "
                             "(inner_drawer_hinge_inset.v1)")
        _hs = _hi.get("side")
        _hmm = _hi.get("mm")
        if _hmm is None:
            _hmm = 0.0
        else:
            if _hs not in ("left", "right", "both"):
                raise ValueError("drawer_module.v1 hinge_inset.side must be 'left' or 'right'")
            if isinstance(_hmm, bool) or not isinstance(_hmm, (int, float)) or not math.isfinite(_hmm) or _hmm < 0:
                raise ValueError("drawer_module.v1 hinge_inset.mm must be a finite non-negative number")
            _hmm = float(_hmm)
            if w - _hmm <= 0 or bw - _hmm <= 0:
                raise ValueError("drawer_module.v1 hinge_inset %.3f leaves no positive drawer width (clear %.3f)"
                                 % (_hmm, w))
        if _hmm and _hs == "left":
            fx, fw = x + _hmm, w - _hmm
            bxx, bww = bx + _hmm, bw - _hmm
        elif _hmm and _hs == "both":
            fx, fw = x + _hmm, w - 2.0 * _hmm
            bxx, bww = bx + _hmm, bw - 2.0 * _hmm
        elif _hmm:
            fx, fw = x, w - _hmm
            bxx, bww = bx, bw - _hmm
        else:
            fx, fw = x, w
            bxx, bww = bx, bw
        if bww <= 0 or bww - 2.0 * side_t <= 0:
            raise ValueError("drawer %d: bay clear width %.4f leaves no positive width for bottom/back with %s"
                             % (index, w, derived_type))
        front = f"{prefix}.front"
        left = f"{prefix}.left"
        right = f"{prefix}.right"
        bottom = f"{prefix}.bottom"
        back = f"{prefix}.back"
        box_h = max(40.0, front_h - 25.0 - (30.0 if derived_type == "undermount" else 0.0))  # 盒顶保持抽面顶 −13
        box_z = cursor + bottom_gap
        # ③ 下限矛盾守卫：盒顶不得越过抽面顶（避免 max(40,…) 触底时静默生成矛盾几何）
        if box_z + box_h > cursor + front_h + 1e-9:
            raise ValueError("drawer %d: front %.3f too small — box top %.3f exceeds front top %.3f with %s "
                             "(bottom gap %.1f, side height %.3f)"
                             % (index, front_h, box_z + box_h, cursor + front_h, derived_type, bottom_gap, box_h))
        nodes.extend([
            _node(front, "drawer_front", _box(fx, y+front_y, cursor, fw, front_depth, front_h), sid, op, grain="x"),
            _node(left, "drawer_side_left", _box(bxx, y+box_y, box_z, side_t, box_depth, box_h), sid, op, grain="y"),
            _node(right, "drawer_side_right", _box(bxx+bww-side_t, y+box_y, box_z, side_t, box_depth, box_h), sid, op, grain="y"),
            _node(bottom, "drawer_bottom", _box(bxx+side_t, y+box_y, box_z, bww-2*side_t, box_depth, bottom_t), sid, op, grain="x"),
            _node(back, "drawer_back", _box(bxx+side_t, y+box_y+box_depth-side_t, box_z+bottom_t, bww-2*side_t, side_t, box_h-bottom_t), sid, op, grain="x"),
        ])
        edges.extend([
            _edge(left, bottom, "support", bottom_t*box_depth),
            _edge(right, bottom, "support", bottom_t*box_depth),
            _edge(left, back, "butt", side_t*(box_h-bottom_t)),
            _edge(right, back, "butt", side_t*(box_h-bottom_t)),
            _edge(bottom, back, "butt", side_t*(bww-2*side_t)),
            _edge(front, left, "fastener", side_t*box_h),
            _edge(front, right, "fastener", side_t*box_h),
        ])
            # r87 接口说明：本包络是**盒体（四块板）包络**，**不含保持原宽的抽面**；
        # 整个五件运动组的包围范围应由五件实际几何取得，不得直接使用本包络。
        envelopes.append(_box(bxx, y+box_y, box_z, bww, box_depth, box_h))
        cursor += front_h + gap

    return {"nodes": nodes, "edges": edges, "drawer_envelopes": envelopes}


HANG_ROD = {
    "id": "hang_rod.v1",
    "version": "1.0.0",
    "level": "function",
    "input_spaces": [{"kind": "hanging_bay", "min_w": 250, "min_d": 300, "min_h": 1350}],
    "occupancy": [],
    "interfaces": [],
    "clearance_rules": [],
    "cabinet_types": ["wardrobe"],
    "parameters": {"diameter": 25, "top_offset": 60},
    "produces": ["hang_rod", "rod_socket"],
    "evidence": [{"source": "golden_case", "metric": "rod_position", "query_or_case": "R3"}],
}


@register_operator(HANG_ROD)
def emit_hang_rod(spec, space, params, instance_id):
    b = _space_box(space)
    x, y, z = float(b["x"]), float(b["y"]), float(b["z"])
    w, d, h = float(b["w"]), float(b["d"]), float(b["h"])
    if h < float(params.get("min_clear_height", 1350)):
        raise ValueError("Hanging clear height must be at least 1350 mm")
    diameter = float(params.get("diameter", 25))
    socket_w = float(params.get("socket_width", 8))
    center_y = y + d / 2.0
    center_z = z + h - float(params.get("top_offset", 60))
    sid, op = space["id"], "hang_rod.v1"
    left = f"{instance_id}.socket_left"
    rod = f"{instance_id}.rod"
    right = f"{instance_id}.socket_right"
    nodes = [
        _node(left, "rod_socket_left", _box(x, center_y-diameter/2, center_z-diameter/2, socket_w, diameter, diameter), sid, op, material="metal"),
        _node(rod, "hang_rod", _box(x+socket_w, center_y-diameter/2, center_z-diameter/2, w-2*socket_w, diameter, diameter), sid, op, material="metal", shape="cylinder", axis="x"),
        _node(right, "rod_socket_right", _box(x+w-socket_w, center_y-diameter/2, center_z-diameter/2, socket_w, diameter, diameter), sid, op, material="metal"),
    ]
    edges = [
        _edge(left, rod, "fastener", 3.141592653589793*(diameter/2)**2),
        _edge(rod, right, "fastener", 3.141592653589793*(diameter/2)**2),
    ]
    return {"nodes": nodes, "edges": edges}


DOOR_ROW = {
    "id": "door_row.v1",
    "version": "1.0.0",
    "level": "function",
    "input_spaces": [{"kind": "door_front", "min_w": 400, "min_d": 18, "min_h": 200}],
    "occupancy": [],
    "interfaces": [],
    "clearance_rules": [],
    "cabinet_types": ["wardrobe", "bookcase", "sideboard", "generic"],
    "parameters": {"front_y": 2, "depth": 18, "gap": 2.5},
    "produces": ["door"],
    "evidence": [{"source": "golden_case", "metric": "door_width", "query_or_case": "R3"}],
}


@register_operator(DOOR_ROW)
def emit_door_row(spec, space, params, instance_id):
    b = _space_box(space)
    x, y, z = float(b["x"]), float(b["y"]), float(b["z"])
    w, h = float(b["w"]), float(b["h"])
    gap = float(params.get("gap", spec.get("gap", 2.5)))
    count = int(params.get("count", max(1, round(w / 415.0))))
    depth = float(params.get("depth", 18))
    front_y = float(params.get("front_y", 2))
    outer_gaps = bool(params.get("outer_gaps", True))
    gap_count = count + 1 if outer_gaps else max(0, count - 1)
    door_w = (w - gap_count * gap) / count
    if door_w <= 0:
        raise ValueError("Door-row width is invalid")
    if params.get("enforce_width", True) and not 400 <= door_w <= 500:
        # 样式优先（程总 2026-10-04）：越界只告警
        print("WARN_DOOR_WIDTH row door_w=%.3f outside 400..500 (style-first)" % door_w, file=sys.stderr)

    sid, op = space["id"], "door_row.v1"
    nodes, edges = [], []
    start = x + gap if outer_gaps else x
    previous = None
    for index in range(count):
        uid = f"{instance_id}.door_{index}"
        dx = start + index * (door_w + gap)
        nodes.append(_node(
            uid, "door", _box(dx, y+front_y, z, door_w, depth, h),
            sid, op, grain="z", door_index=index,
        ))
        if previous is not None:
            edges.append(_edge(
                previous, uid, "alignment", 0,
                tolerance=gap,
                interface_a="x_max",
                interface_b="x_min",
            ))
        previous = uid
    return {"nodes": nodes, "edges": edges}
