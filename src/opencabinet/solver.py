from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contract import (
    dump_json,
    validate_board_graph,
    validate_semantic,
    validate_spec,
)
from .operators import emit_operator


EPS = 0.01
# 程总 2026-10-02 裁定：门缝与抽面缝固定 2.5mm；立板位置允许 1mm 误差。
# 仅用于「门缝中心 ↔ 立板中心」与「立板实际 x ↔ 规划 x」的对齐检查，
# 不放宽门宽、门缝、抽面缝、板厚、边界及实体干涉等任何其他检查。
PARTITION_POSITION_TOL = 1.0
OVERLAP_VOLUME_EPS = 1.0
STRUCT_Y = 20.0
PANEL_T = 18.0
BACK_T = 10.0
BACK_FRONT_OFFSET = 28.0


class SolveError(ValueError):
    pass


def _box(x, y, z, w, d, h):
    values = tuple(float(v) for v in (x, y, z, w, d, h))
    if min(values[3:]) <= 0:
        raise SolveError(f"Invalid box dimensions: {values}")
    return dict(zip(("x", "y", "z", "w", "d", "h"), values))


def _node(uid, role, bbox, space_id, operator_id, **extra):
    result = {
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
    result.update(extra)
    return result


def _edge(a, b, kind, contact=0.0, overlap=0.0, tolerance=0.1):
    return {
        "a": a,
        "b": b,
        "type": kind,
        "expected_contact_mm2": max(0.0, float(contact)),
        "allowed_overlap_mm3": max(0.0, float(overlap)),
        "tolerance": max(0.0, float(tolerance)),
    }


def _space(space_id, kind, bbox, parent_id=None, functions=None, status="available"):
    result = {
        "id": space_id,
        "kind": kind,
        "bbox": bbox,
        "parent_id": parent_id,
        "front": "y_min",
        "functions": list(functions or []),
        "constraints": [],
        "status": status,
    }
    return result


def _end(box, axis):
    size = {"x": "w", "y": "d", "z": "h"}[axis]
    return float(box[axis]) + float(box[size])


def _inside(inner, outer, tolerance=EPS):
    return all(
        float(inner[axis]) >= float(outer[axis]) - tolerance
        and _end(inner, axis) <= _end(outer, axis) + tolerance
        for axis in ("x", "y", "z")
    )


def _overlap_volume(a, b):
    lengths = []
    for axis in ("x", "y", "z"):
        overlap = min(_end(a, axis), _end(b, axis)) - max(
            float(a[axis]), float(b[axis])
        )
        if overlap <= EPS:
            return 0.0
        lengths.append(overlap)
    return lengths[0] * lengths[1] * lengths[2]


def _function_names(spec):
    names = []
    for value in spec.get("functions", []):
        if isinstance(value, str):
            names.append(value.lower())
        elif isinstance(value, Mapping):
            name = value.get("name") or value.get("type") or value.get("function")
            if name:
                names.append(str(name).lower())
    return names


def _function_config(spec, name):
    aliases = {
        "hang": {"hang", "hanging", "挂衣"},
        "drawer": {"drawer", "drawers", "抽屉"},
        "fold": {"fold", "folding", "shelf", "shelves", "叠放"},
        "door": {"door", "doors", "门"},
        "open": {"open", "open_bay", "开放格"},
    }
    accepted = aliases.get(name, {name})
    result = {}
    for value in spec.get("functions", []):
        if not isinstance(value, Mapping):
            continue
        key = str(
            value.get("name") or value.get("type") or value.get("function") or ""
        ).lower()
        if key in accepted:
            result.update(value)
    return result


def _has_function(spec, name):
    aliases = {
        "hang": {"hang", "hanging", "挂衣"},
        "drawer": {"drawer", "drawers", "抽屉"},
        "fold": {"fold", "folding", "shelf", "shelves", "叠放"},
        "door": {"door", "doors", "门"},
        "open": {"open", "open_bay", "开放格"},
    }
    return bool(set(_function_names(spec)) & aliases.get(name, {name}))


def _cabinet_type(spec):
    return str(spec.get("cabinet_type", "generic")).lower()


def _base_z(spec):
    # base_z is the BOTTOM face of the cabinet bottom board.
    plinth = float(spec.get("plinth_height", 60.0))
    return plinth if plinth > 0.0 else 0.0


def _facade_span(spec):
    """门列可用范围（r99）：不靠墙（见光）侧板**盖门板**，门列须让出该侧板厚度。"""
    width = float(spec["width"])
    t = float(spec.get("board_thickness", PANEL_T))
    # 只在**显式声明 walls** 时才判定见光侧（未声明＝沿用旧行为：门板盖侧板），避免影响既有柜
    exposed = ([v for v in ("left", "right") if v not in (spec.get("walls") or [])]
               if "walls" in spec else [])
    x0 = t if "left" in exposed else 0.0
    span = width - x0 - (t if "right" in exposed else 0.0)
    return x0, span


def _door_width_for_count(spec, count):
    _x0, width = _facade_span(spec)
    gap = float(spec.get("gap", 2.5))
    if count < 1:
        raise SolveError("Door count must be positive")
    return (width - gap * max(0, count - 1)) / count


def _solve_door_count(spec):
    """
    Door count is solved before the interior-column layout.

    Candidate count starts from round(inner_width / 415), then is clamped to
    a count whose emitted door width is in [400, 500]. The width calculation
    is identical to door_row.v1 with outer_gaps=False.
    """
    width = float(spec["width"])
    thickness = float(spec.get("board_thickness", PANEL_T))
    inner_width = width - 2.0 * thickness
    if inner_width <= 0:
        raise SolveError("Cabinet has no positive inner width")

    explicit = spec.get("door_count")
    valid_counts = []
    max_count = max(1, int(math.ceil(width / 400.0)) + 2)

    for count in range(1, max_count + 1):
        door_width = _door_width_for_count(spec, count)
        if 400.0 - EPS <= door_width <= 500.0 + EPS:
            valid_counts.append(count)

    if explicit is not None:
        count = int(explicit)
        if count < 1:
            raise SolveError("spec.door_count must be positive")
        door_width = _door_width_for_count(spec, count)
        if not 400.0 - EPS <= door_width <= 500.0 + EPS:
            # 样式优先（程总 2026-10-04）：门数由样式决定时，门宽越界只告警不拦截
            print("WARN_DOOR_WIDTH explicit door_count=%d -> %.3f mm outside 400..500 (style-first)"
                  % (count, door_width), file=sys.stderr)
        return count, door_width

    if not valid_counts:
        raise SolveError(
            "No door count can produce a door width in 400..500 mm "
            "for cabinet width %.3f mm" % width
        )

    target = max(1, int(math.floor(inner_width / 415.0 + 0.5)))
    count = min(
        valid_counts,
        key=lambda candidate: (
            abs(candidate - target),
            abs(_door_width_for_count(spec, candidate) - 415.0),
            candidate,
        ),
    )
    return count, _door_width_for_count(spec, count)


def _drawer_column_indices(spec, columns, warnings):
    """Allocate independent drawer modules to functional bays.

    Explicit indices refer to layout["columns"], not individual doors.

    drawer_min_width is measured in mm and retains its existing
    aggregate-clear-width semantics. A positive value may expand the
    selection to adjacent functional bays. Each selected bay receives
    an independent drawer module; widths are not merged into one drawer.

    No implicit minimum clear width is imposed on an individual bay.
    """
    count = len(columns)
    if count == 0 or not _has_function(spec, "drawer"):
        return []

    if "drawer_columns" in spec:
        raw = spec["drawer_columns"]
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise SolveError("spec.drawer_columns must be an array")
        selected = sorted(set(int(value) for value in raw))
    elif "drawer_column" in spec:
        selected = [int(spec["drawer_column"])]
    else:
        selected = [count - 1]

    if not selected:
        raise SolveError(
            "Drawer function requires at least one functional bay"
        )

    for index in selected:
        if index < 0 or index >= count:
            raise SolveError(
                "Drawer functional-bay index %d is outside 0..%d"
                % (index, count - 1)
            )

    minimum_width = float(spec.get("drawer_min_width", 0.0))
    if minimum_width <= 0:
        return selected

    def allocated_width(indices):
        return sum(float(columns[index][1]) for index in indices)

    while allocated_width(selected) < minimum_width - EPS:
        left = min(selected) - 1
        right = max(selected) + 1
        candidates = []
        if left >= 0:
            candidates.append(left)
        if right < count:
            candidates.append(right)
        if not candidates:
            break

        chosen = min(
            candidates,
            key=lambda index: (
                abs(
                    allocated_width(selected)
                    + float(columns[index][1])
                    - minimum_width
                ),
                index,
            ),
        )
        selected = sorted(set(selected + [chosen]))

    if allocated_width(selected) < minimum_width - EPS:
        raise SolveError(
            "Drawer allocation cannot reach requested minimum width %.3f mm"
            % minimum_width
        )

    if len(selected) > 1:
        warnings.append({
            "code": "DRAWER_ALLOCATION_EXPANDED",
            "message": (
                "Drawer allocation spans multiple functional bays; "
                "each bay remains an independent drawer module."
            ),
            "columns": list(selected),
            "combined_clear_width": allocated_width(selected),
        })

    return selected


def _column_layout(spec):
    """Solve independent functional bays and explicit facade openings.

    New inputs:
      column_count: positive integer; never inferred from doors/functions.
      column_widths: optional positive clear widths, left-to-right.
      facade_openings: for cabinets with doors, a list of:
        {"bay_indices": [0, 1], "door_count": 2}

    Openings must cover functional bays once, consecutively, left-to-right.
    An opening may span multiple bays. Its internal door seams do NOT
    create partitions.

    Existing equal-width facade generator is retained. Opening boundaries
    must agree with its seams; neither subsystem silently changes geometry.

    Return status:
      solved: usable layout, possibly with warnings
      pending: missing required input; must not generate
      infeasible: contradictory/invalid input; must not generate
    """
    import math

    warnings = []
    errors = []
    missing = []
    result = {
        "status": None,
        "can_generate": False,
        "warnings": warnings,
        "errors": errors,
        "missing_parameters": missing,
        "door_count": 0,
        "door_width": 0.0,
        "gap": 0.0,
        "seam_centers": [],
        # Compatibility key only: no longer describes functional bays.
        "bay_groups": None,
        "partitions": [],
        "columns": [],
        "drawer_columns": [],
        "facade_openings": [],
        "boundary_bindings": [],
        "layout_contract_version": 2,
    }

    def issue(code, message):
        errors.append({"code": code, "message": message})

    def finish():
        result["status"] = (
            "infeasible" if errors else "pending" if missing else "solved"
        )
        result["can_generate"] = result["status"] == "solved"
        return result

    def number(value, path, positive=False):
        if isinstance(value, bool):
            issue("INVALID_PARAMETER", "%s must be numeric" % path)
            return None
        try:
            value = float(value)
        except (TypeError, ValueError, OverflowError):
            issue("INVALID_PARAMETER", "%s must be numeric" % path)
            return None
        if not math.isfinite(value) or (positive and value <= 0):
            issue("INVALID_PARAMETER", "%s has an invalid value" % path)
            return None
        return value

    def integer(value, path, minimum=0):
        value = number(value, path)
        if value is None:
            return None
        if value != int(value) or value < minimum:
            issue("INVALID_PARAMETER", "%s must be an integer >= %d"
                  % (path, minimum))
            return None
        return int(value)

    if "width" not in spec:
        missing.append("width")
        width = None
    else:
        width = number(spec["width"], "width", positive=True)

    thickness = number(
        spec.get("board_thickness", PANEL_T),
        "board_thickness", positive=True,
    )
    gap = number(spec.get("gap", 2.5), "gap")
    if gap is not None:
        result["gap"] = gap
        if gap < 0:
            issue("NEGATIVE_GAP", "gap must not be negative")

    if "column_count" not in spec:
        missing.append("column_count")
        count = None
    else:
        count = integer(spec["column_count"], "column_count", 1)

    has_facade = (
        _cabinet_type(spec) == "wardrobe" or _has_function(spec, "door")
    )
    raw_openings = spec.get("facade_openings")
    if has_facade and raw_openings is None:
        missing.append("facade_openings")
    elif has_facade and (
        not isinstance(raw_openings, list) or not raw_openings
    ):
        issue("INVALID_OPENINGS", "facade_openings must be a nonempty list")

    # Detect a known dimensional contradiction even if other input is missing.
    if width is not None and thickness is not None:
        if width <= 2.0 * thickness:
            issue("NO_INTERIOR", "No positive cabinet interior width")

    if errors or missing:
        return finish()

    available = width - (count + 1) * thickness
    if available <= EPS:
        issue("NO_BAY_SPACE", "Partitions leave no positive clear bay width")
        return finish()

    raw_widths = spec.get("column_widths")
    if raw_widths is None:
        widths = [available / count] * count
    else:
        if not isinstance(raw_widths, list) or len(raw_widths) != count:
            issue("INVALID_BAY_WIDTHS",
                  "column_widths must contain column_count clear widths")
            return finish()
        widths = [
            number(value, "column_widths[%d]" % index, positive=True)
            for index, value in enumerate(raw_widths)
        ]
        if errors:
            return finish()
        if abs(sum(widths) - available) > EPS:
            issue("BAY_WIDTH_SUM",
                  "Clear widths plus side/partition thicknesses must equal width")
            return finish()

    if any(value <= EPS for value in widths):
        issue("NO_BAY_SPACE", "Every functional bay must have positive clear width")
        return finish()

    cursor = thickness
    for index, clear_width in enumerate(widths):
        result["columns"].append((cursor, clear_width))
        cursor += clear_width
        if index < count - 1:
            result["partitions"].append({
                "index": index,
                "x": cursor,
                "w": thickness,
                # These keys are retained for old node creation code.
                # A non-boundary partition need not coincide with any seam.
                "seam_index": None,
                "seam_center": None,
            })
            cursor += thickness

    # Normalize existing function selectors without deriving bay count.
    normalized = dict(spec)
    style = spec.get("style", {})
    if not isinstance(style, dict):
        issue("INVALID_STYLE", "style must be an object")
        return finish()

    def bind_alias(key, value):
        if key in normalized and normalized[key] != value:
            issue("CONFLICTING_PARAMETER", "Conflicting declarations for " + key)
        else:
            normalized[key] = value

    if "hang_columns" in style:
        bind_alias("hang_columns", style["hang_columns"])

    drawer = style.get("drawer", {})
    fold = style.get("fold", {})
    if not isinstance(drawer, dict) or not isinstance(fold, dict):
        issue("INVALID_STYLE", "style.drawer and style.fold must be objects")
        return finish()

    if "column" in drawer:
        bind_alias("drawer_column", drawer["column"])

    selectors = []
    for key in ("hang_columns", "drawer_columns"):
        if key in normalized:
            values = normalized[key]
            if not isinstance(values, (list, tuple)):
                issue("INVALID_SELECTOR", key + " must be an array")
            else:
                selectors.extend((key, value) for value in values)
    if "drawer_column" in normalized:
        selectors.append(("drawer_column", normalized["drawer_column"]))
    if "column" in fold:
        selectors.append(("style.fold.column", fold["column"]))

    for path, value in selectors:
        index = integer(value, path)
        if index is not None and index >= count:
            issue("FUNCTION_INDEX_OUT_OF_RANGE",
                  "%s=%d is outside functional bays 0..%d"
                  % (path, index, count - 1))

    if "drawer_columns" in normalized and "drawer_column" in normalized:
        if not errors and set(normalized["drawer_columns"]) != {
            normalized["drawer_column"]
        }:
            issue("CONFLICTING_PARAMETER",
                  "drawer_column and drawer_columns disagree")

    if errors:
        return finish()

    try:
        result["drawer_columns"] = _drawer_column_indices(
            normalized, result["columns"], warnings
        )
    except SolveError as exc:
        issue("DRAWER_ALLOCATION", str(exc))
        return finish()

    if not has_facade:
        if raw_openings:
            issue("UNEXPECTED_OPENINGS",
                  "facade_openings supplied for a cabinet without a facade")
        return finish()

    # Explicit openings reference existing bays; they never construct bays.
    next_bay = 0
    total_doors = 0
    openings = []
    for index, raw in enumerate(raw_openings):
        if not isinstance(raw, dict):
            issue("INVALID_OPENING", "Each opening must be an object")
            continue
        bays = raw.get("bay_indices")
        if not isinstance(bays, list) or not bays:
            issue("INVALID_OPENING", "bay_indices must be a nonempty list")
            continue
        parsed = [
            integer(value, "facade_openings[%d].bay_indices" % index)
            for value in bays
        ]
        doors = integer(
            raw.get("door_count"),
            "facade_openings[%d].door_count" % index, 1,
        )
        if any(value is None for value in parsed) or doors is None:
            continue
        expected = list(range(next_bay, next_bay + len(parsed)))
        if parsed != expected or parsed[-1] >= count:
            issue("OPENING_COVERAGE",
                  "Openings must cover bays once in contiguous left-to-right order")
            continue
        openings.append({
            "index": index,
            "bay_indices": parsed,
            "door_count": doors,
            "first_door_index": total_doors,
        })
        next_bay += len(parsed)
        total_doors += doors

    if next_bay != count:
        issue("OPENING_COVERAGE", "Facade openings do not cover all functional bays")
    if errors:
        return finish()

    doors_style = style.get("doors", {})
    if not isinstance(doors_style, dict):
        issue("INVALID_STYLE", "style.doors must be an object")
        return finish()

    for path, value in (
        ("door_count", spec.get("door_count")),
        ("style.doors.count", doors_style.get("count")),
    ):
        if value is not None:
            declared = integer(value, path, 1)
            if declared is not None and declared != total_doors:
                issue("DOOR_COUNT_CONFLICT",
                      path + " disagrees with facade_openings")

    if "gap" in doors_style:
        nested_gap = number(doors_style["gap"], "style.doors.gap")
        if nested_gap is not None and abs(nested_gap - gap) > EPS:
            issue("GAP_CONFLICT", "gap and style.doors.gap disagree")

    if errors:
        return finish()

    facade_spec = dict(normalized)
    facade_spec["door_count"] = total_doors
    try:
        door_count, door_width = _solve_door_count(facade_spec)
    except SolveError as exc:
        issue("FACADE_DIMENSIONS", str(exc))
        return finish()

    if door_count != total_doors:
        issue("DOOR_COUNT_CHANGED", "Door solver changed the explicit opening count")
        return finish()

    _fx0, _fspan = _facade_span(spec)
    seams = [
        _fx0 + index * (door_width + gap) - gap / 2.0
        for index in range(1, door_count)
    ]
    result.update({
        "door_count": door_count,
        "door_width": door_width,
        "facade_span": [_fx0, _fx0 + _fspan],
        "seam_centers": seams,
        "facade_openings": openings,
    })

    for opening in openings[:-1]:
        partition_index = opening["bay_indices"][-1]
        partition = result["partitions"][partition_index]
        seam_index = (
            opening["first_door_index"] + opening["door_count"] - 1
        )
        center = partition["x"] + partition["w"] / 2.0
        seam_center = seams[seam_index]
        result["boundary_bindings"].append({
            "partition_index": partition_index,
            "seam_index": seam_index,
        })
        if abs(center - seam_center) > PARTITION_POSITION_TOL:
            issue(
                "OPENING_BOUNDARY_CONFLICT",
                "Opening boundary at %.6f does not match facade seam %.6f; "
                "explicit dimensions/counts must be revised"
                % (center, seam_center),
            )
        else:
            partition["seam_index"] = seam_index
            partition["seam_center"] = seam_center

    if errors:
        return finish()

    if any(len(opening["bay_indices"]) > 1 for opening in openings):
        warnings.append({
            "code": "OPENING_SPANS_FUNCTIONAL_BAYS",
            "message": (
                "An opening spans multiple functional bays. "
                "This layout result does not establish hinge support "
                "or drawer opening clearance."
            ),
        })

    return finish()


def _resolve_door_z(spec):
    """按目标开口、安装方式、盖量和边缝求解门高，并记录参数来源。"""
    import math

    sources = {}

    def parameter(name, default):
        explicit = name in spec
        value = spec[name] if explicit else default
        sources[name] = {
            "source": "explicit" if explicit else "default",
            "value": value,
        }
        return value

    def number(name, default, nonnegative=False):
        raw = parameter(name, default)
        if isinstance(raw, bool):
            raise SolveError(f"{name} must be numeric")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise SolveError(f"Invalid {name}: {raw!r}") from None
        if not math.isfinite(value) or (nonnegative and value < 0):
            raise SolveError(f"Invalid {name}: {value}")
        sources[name]["value"] = value
        return value

    height = float(spec["height"])
    thickness = float(spec.get("board_thickness", PANEL_T))
    # 与 build_space_tree 使用同一支承基准，不另行推导底板位置。
    base_z = float(_base_z(spec))
    if not all(math.isfinite(v) for v in (height, thickness, base_z)):
        raise SolveError("Non-finite cabinet dimensions")
    if height <= 0 or thickness <= 0:
        raise SolveError("Cabinet height and board thickness must be positive")

    installation = parameter("door_installation", "overlay")
    if installation not in ("overlay", "inset"):
        raise SolveError("door_installation must be 'overlay' or 'inset'")

    cover_plinth = parameter("cover_plinth", installation == "overlay")
    if not isinstance(cover_plinth, bool):
        raise SolveError("cover_plinth must be boolean")
    if installation == "inset" and cover_plinth:
        raise SolveError("Inset doors cannot cover the plinth")

    # 覆盖地脚时缺省采用柜体外轮廓；否则采用底板顶面至顶板底面。
    # 显式开口参数可覆盖这些布局缺省值。
    opening_bottom = number(
        "door_opening_bottom_z",
        0.0 if cover_plinth else base_z + thickness,
    )
    opening_top = number(
        "door_opening_top_z",
        (height if cover_plinth else height - thickness)
        - float(spec.get("top_trim") or 0.0),        # r99：顶收口条下沿
    )
    if not 0 <= opening_bottom < opening_top <= height:
        raise SolveError("Invalid door target opening")

    # 保留上一版可覆盖的布局缺省；不将其视为生产工艺或联合统计结论。
    use_cover_defaults = installation == "overlay" and cover_plinth
    bottom_gap = number(
        "door_bottom_gap",
        20.0 if use_cover_defaults else spec.get("gap", 2.5),
        nonnegative=True,
    )
    top_gap = number(
        "door_top_gap",
        10.0 if use_cover_defaults else spec.get("gap", 2.5),
        nonnegative=True,
    )
    bottom_overlay = number(
        "door_bottom_overlay", 0.0, nonnegative=True
    )
    top_overlay = number(
        "door_top_overlay", 0.0, nonnegative=True
    )

    if installation == "inset" and (bottom_overlay or top_overlay):
        raise SolveError("Inset installation requires zero overlay allowances")

    solved_bottom = opening_bottom - bottom_overlay + bottom_gap
    solved_top = opening_top + top_overlay - top_gap

    # 保留旧显式端点入口；显式端点覆盖计算值，来源单独记录。
    bottom = number("door_bottom_z", solved_bottom)
    top = number("door_top_z", solved_top)
    for name in ("door_bottom_z", "door_top_z"):
        if name not in spec:
            sources[name]["source"] = "derived"

    if not 0 <= bottom < top <= height:
        raise SolveError(f"Invalid door-front Z interval: [{bottom}, {top}]")

    return {
        "bottom": bottom,
        "top": top,
        "installation": installation,
        "cover_plinth": cover_plinth,
        "target_opening": [opening_bottom, opening_top],
        "computed_interval": [solved_bottom, solved_top],
        "parameter_sources": sources,
    }


def build_space_tree(spec, layout=None):
    validate_spec(spec)
    layout = layout or _column_layout(spec)

    width = float(spec["width"])
    depth = float(spec["depth"])
    height = float(spec["height"])
    thickness = float(spec.get("board_thickness", PANEL_T))
    base_z = _base_z(spec)
    top_z = height - thickness - float(spec.get("top_trim") or 0.0)   # r99：顶收口
    interior_z = base_z + thickness
    interior_h = top_z - interior_z
    interior_y = STRUCT_Y
    interior_d = depth - STRUCT_Y
    columns = layout["columns"]
    drawer_columns = set(layout.get("drawer_columns", []))

    spaces = [
        _space(
            "cabinet",
            "cabinet",
            _box(0, 0, 0, width, depth, height),
            None,
            spec.get("functions", []),
        )
    ]

    for index, (column_x, column_w) in enumerate(columns):
        functions = []

        if _cabinet_type(spec) == "wardrobe":
            if _has_function(spec, "hang"):
                functions.append("hang")
            if _has_function(spec, "fold") and index == 1:
                functions.append("fold")
            if _has_function(spec, "drawer") and index in drawer_columns:
                functions.append("drawer")
        else:
            if _has_function(spec, "drawer") and index in drawer_columns:
                functions.append("drawer")
            if _has_function(spec, "open"):
                functions.append("open")

        column_id = "column.%d" % index
        spaces.append(
            _space(
                column_id,
                "interior",
                _box(
                    column_x,
                    interior_y,
                    interior_z,
                    column_w,
                    interior_d,
                    interior_h,
                ),
                "cabinet",
                functions,
            )
        )

        if "drawer" in functions:
            drawer_cfg = _function_config(spec, "drawer")
            heights = drawer_cfg.get("heights")
            if heights is None:
                heights = spec.get("drawer_heights")
            if heights is None:
                heights = [147.0, 187.0]

            heights = [float(value) for value in heights]
            gap = float(spec.get("gap", 2.5))
            drawer_h = (
                sum(heights)
                + gap * max(0, len(heights) - 1)
            )

            spaces.append(
                _space(
                    "%s.drawer" % column_id,
                    "drawer_column",
                    _box(
                        column_x,
                        interior_y,
                        interior_z,
                        column_w,
                        interior_d,
                        drawer_h,
                    ),
                    column_id,
                    ["drawer"],
                )
            )

            remaining_z = interior_z + drawer_h
            remaining_h = top_z - remaining_z
            if remaining_h > EPS:
                upper_functions = []
                upper_kind = "open_bay"

                if "hang" in functions:
                    upper_functions.append("hang")
                    upper_kind = "hanging_bay"
                elif "fold" in functions:
                    upper_functions.append("fold")
                else:
                    upper_functions.append("open")

                spaces.append(
                    _space(
                        "%s.upper" % column_id,
                        upper_kind,
                        _box(
                            column_x,
                            interior_y,
                            remaining_z,
                            column_w,
                            interior_d,
                            remaining_h,
                        ),
                        column_id,
                        upper_functions,
                    )
                )

        elif "hang" in functions:
            spaces.append(
                _space(
                    "%s.hang" % column_id,
                    "hanging_bay",
                    _box(
                        column_x,
                        interior_y,
                        interior_z,
                        column_w,
                        interior_d,
                        interior_h,
                    ),
                    column_id,
                    ["hang"],
                )
            )

        elif "fold" in functions:
            spaces.append(
                _space(
                    "%s.fold" % column_id,
                    "open_bay",
                    _box(
                        column_x,
                        interior_y,
                        interior_z,
                        column_w,
                        interior_d,
                        interior_h,
                    ),
                    column_id,
                    ["fold"],
                )
            )

    if layout["door_count"] > 0:
        door_coverage = _resolve_door_z(spec)
        door_bottom = door_coverage["bottom"]
        door_top = door_coverage["top"]

        _exp = ([s for s in ("left", "right") if s not in (spec.get("walls") or [])]
                if "walls" in spec else [])
        _dx = thickness if "left" in _exp else 0.0
        _dw = width - thickness * (1 if "right" in _exp else 0) - _dx
        door_space = _space(
            "front.doors",
            "door_front",
            _box(
                _dx,
                0,
                door_bottom,
                _dw,
                20.0,
                door_top - door_bottom,
            ),
            "cabinet",
            ["door"],
        )
        door_space.setdefault("metadata", {})["door_coverage"] = door_coverage
        spaces.append(door_space)

    return spaces


def _add_plinth(spec, graph, space_id="cabinet"):
    """
    R3-compatible closed plinth without geometric exemptions.

    Full-height carcass sides remain at x=[0,t] and x=[W-t,W].
    Front/rear plinth rails span only the clear interval x=[t,W-t].
    Left/right return rails occupy the inner side of the carcass panels:
        left  x=[t,2t]
        right x=[W-2t,W-t]
    The four plinth members meet face-to-face but never overlap.
    """
    width = float(spec["width"])
    depth = float(spec["depth"])
    plinth_h = float(spec.get("plinth_height", 60.0))
    if plinth_h <= 0:
        return

    thickness = float(spec.get("board_thickness", PANEL_T))
    clear_width = width - 2.0 * thickness
    return_depth = depth - STRUCT_Y - 2.0 * thickness
    if clear_width <= 0 or return_depth <= 0:
        raise SolveError("Cabinet is too small for closed plinth")

    operator_id = "plinth.closed.v1"
    front = "plinth.front"
    rear = "plinth.rear"
    left = "plinth.left"
    right = "plinth.right"

    graph["nodes"].extend(
        [
            _node(
                front,
                "plinth_front",
                _box(
                    thickness,
                    STRUCT_Y,
                    0,
                    clear_width,
                    thickness,
                    plinth_h,
                ),
                space_id,
                operator_id,
                grain="x",
            ),
            _node(
                rear,
                "plinth_rear",
                _box(
                    thickness,
                    depth - thickness,
                    0,
                    clear_width,
                    thickness,
                    plinth_h,
                ),
                space_id,
                operator_id,
                grain="x",
            ),
            _node(
                left,
                "plinth_side_left",
                _box(
                    thickness,
                    STRUCT_Y + thickness,
                    0,
                    thickness,
                    return_depth,
                    plinth_h,
                ),
                space_id,
                operator_id,
                grain="y",
            ),
            _node(
                right,
                "plinth_side_right",
                _box(
                    width - 2.0 * thickness,
                    STRUCT_Y + thickness,
                    0,
                    thickness,
                    return_depth,
                    plinth_h,
                ),
                space_id,
                operator_id,
                grain="y",
            ),
        ]
    )

    end_contact = thickness * plinth_h
    graph["edges"].extend(
        [
            _edge(front, left, "butt", end_contact),
            _edge(front, right, "butt", end_contact),
            _edge(rear, left, "butt", end_contact),
            _edge(rear, right, "butt", end_contact),
        ]
    )


def _add_vertical_partitions(spec, layout, graph):
    partitions = layout["partitions"]
    columns = layout["columns"]
    seams = layout["seam_centers"]
    door_count = int(layout["door_count"])

    thickness = float(spec.get("board_thickness", PANEL_T))
    depth = float(spec["depth"])
    height = float(spec["height"])
    base_z = _base_z(spec)
    top_z = height - thickness - float(spec.get("top_trim") or 0.0)   # r99：顶收口
    partition_z = base_z + thickness
    partition_h = top_z - partition_z
    back_enabled = spec.get("back", "grooved_10") != "none"
    back_id = "carcass.back"

    # S1 契约：立板数 = 功能格数 − 1，不再由门数/门缝反推
    if not columns:
        raise SolveError("I8: No functional bay declared")
    if len(partitions) != len(columns) - 1:
        raise SolveError(
            "I8: Partition count %d must equal functional bay count %d - 1"
            % (len(partitions), len(columns))
        )

    if not partitions:
        return
    if partition_h <= 0:
        raise SolveError("Vertical partition height is not positive")
    if depth <= STRUCT_Y:
        raise SolveError("Vertical partition depth is not positive")

    # Validate every partition before mutating graph.
    for position, partition in enumerate(partitions):
        x = float(partition["x"])
        w = float(partition["w"])
        center = x + w / 2.0

        if abs(w - thickness) > EPS:
            raise SolveError("I8: Partition thickness mismatch")
        # S1 契约：立板与门缝的绑定由 _check_facade_partition_alignment 校验，
        # 此处不再按门组反推、也不要求每块立板都落在门缝上。
        seam_center_meta = partition.get("seam_center")
        if seam_center_meta is not None and abs(center - float(seam_center_meta)) > 0.5:
            raise SolveError("I8: Partition center metadata mismatch")

        left_x, left_w = columns[position]
        right_x, _ = columns[position + 1]
        if (
            abs(float(left_x) + float(left_w) - x) > EPS
            or abs(x + w - float(right_x)) > EPS
        ):
            raise SolveError(
                "I8: Partition %d does not bound adjacent clear bays"
                % position
            )

    for partition in partitions:
        index = int(partition["index"])
        x = float(partition["x"])
        seam_center = partition.get("seam_center")
        uid = "partition.vertical.%d" % index

        # Use -1 for a structural boundary without a facade seam.
        seam_index = partition.get("seam_index")
        if seam_index is None:
            seam_index = -1

        graph["nodes"].append(
            _node(
                uid,
                "vertical_partition",
                _box(
                    x,
                    STRUCT_Y,
                    partition_z,
                    thickness,
                    depth - STRUCT_Y,
                    partition_h,
                ),
                "cabinet",
                "partition.vertical.v1",
                grain="z",
                seam_center=seam_center,
                seam_index=seam_index,
            )
        )

        graph["edges"].extend([
            _edge(
                "carcass.bottom",
                uid,
                "support",
                thickness * (depth - STRUCT_Y),
            ),
            _edge(
                uid,
                "carcass.top",
                "butt",
                thickness * (depth - STRUCT_Y),
            ),
        ])

        if back_enabled:
            overlap = thickness * BACK_T * partition_h
            graph["edges"].append(
                _edge(uid, back_id, "groove", overlap=overlap)
            )


def _add_shelf(graph, uid, role, box, space_id, contacts=()):
    graph["nodes"].append(
        _node(
            uid,
            role,
            box,
            space_id,
            "shelf.layout.v1",
            grain="x",
        )
    )
    for target in contacts:
        graph["edges"].append(
            _edge(
                target,
                uid,
                "support",
                float(box["h"]) * float(box["d"]),
            )
        )
    if any(node["uid"] == "carcass.back" for node in graph["nodes"]):
        overlap = float(box["w"]) * min(BACK_T, float(box["d"])) * float(box["h"])
        graph["edges"].append(
            _edge(uid, "carcass.back", "groove", overlap=overlap)
        )


def _add_layout_boards(spec, columns, graph, drawer_result=None):
    if _dwg_explicit_shelves(spec, columns, graph):
        return

    thickness = float(spec.get("board_thickness", PANEL_T))
    depth = float(spec["depth"])
    height = float(spec["height"])
    base_z = _base_z(spec)
    inner_bottom = base_z + thickness
    top_z = height - thickness - float(spec.get("top_trim") or 0.0)   # r99：顶收口
    cabinet_type = _cabinet_type(spec)

    if cabinet_type == "bedside":
        drawer_heights = spec.get("drawer_heights", [147.0, 187.0])
        gap = float(spec.get("gap", 2.5))
        drawer_top = inner_bottom + sum(drawer_heights) + gap * (
            len(drawer_heights) - 1
        )
        shelf_zs = [
            drawer_top,
            float(spec.get("open_shelf_z", top_z - 100.0)),
        ]
        for index, z in enumerate(shelf_zs):
            if z + thickness > top_z + EPS:
                continue
            _add_shelf(
                graph,
                f"layout.horizontal.{index}",
                "drawer_separator" if index == 0 else "open_shelf",
                _box(
                    thickness,
                    STRUCT_Y,
                    z,
                    float(spec["width"]) - 2.0 * thickness,
                    depth - STRUCT_Y,
                    thickness,
                ),
                "column.0",
                ("carcass.left", "carcass.right"),
            )
        return

    if cabinet_type != "wardrobe":
        return

    shelves = []
    for index, (column_x, column_w) in enumerate(columns):
        column_id = f"column.{index}"
        if index == len(columns) - 1 and _has_function(spec, "drawer"):
            drawer_cfg = _function_config(spec, "drawer")
            heights = drawer_cfg.get(
                "heights",
                spec.get("drawer_heights", [147.0, 187.0]),
            )
            gap = float(spec.get("gap", 2.5))
            drawer_top = inner_bottom + sum(float(v) for v in heights)
            drawer_top += gap * max(0, len(heights) - 1)
            shelves.append((column_id, column_x, column_w, drawer_top, "drawer_separator"))

        if index == 1 and _has_function(spec, "fold"):
            available_bottom = inner_bottom
            available_top = top_z
            count = int(spec.get("fold_shelf_count", 4))
            pitch = (available_top - available_bottom) / (count + 1)
            for shelf_index in range(count):
                shelves.append(
                    (
                        column_id,
                        column_x,
                        column_w,
                        available_bottom + pitch * (shelf_index + 1),
                        "fold_shelf",
                    )
                )

    desired = int(spec.get("wardrobe_horizontal_count", 7))
    if len(shelves) < desired:
        candidates = []
        for index, (column_x, column_w) in enumerate(columns):
            if index == 1 and _has_function(spec, "fold"):
                continue
            candidates.append(
                (
                    f"column.{index}",
                    column_x,
                    column_w,
                    top_z - 500.0,
                    "upper_shelf",
                )
            )
        while len(shelves) < desired and candidates:
            shelves.append(candidates[(len(shelves) - 1) % len(candidates)])
    shelves = shelves[:desired]

    seen = {}
    for index, (space_id, x, width, z, role) in enumerate(shelves):
        key = (round(x, 3), round(z, 3))
        occurrence = seen.get(key, 0)
        seen[key] = occurrence + 1
        if occurrence:
            z -= occurrence * (thickness + 2.5)
        _add_shelf(
            graph,
            f"layout.horizontal.{index}",
            role,
            _box(x, STRUCT_Y, z, width, depth - STRUCT_Y, thickness),
            space_id,
        )


def _merge_result(graph, result):
    graph["nodes"].extend(result.get("nodes", []))
    graph["edges"].extend(result.get("edges", []))


def _space_by_id(spaces):
    return {space["id"]: space for space in spaces}


def _operator_use(use_id, operator_id, space_ids, params, score=1.0):
    return {
        "id": use_id,
        "operator_id": operator_id,
        "space_ids": list(space_ids),
        "params": dict(params),
        "score": float(score),
    }


# solver.py
def _drawer_project_constraints(spec, space):
    """
    依据 R6/C7：不设置柜型相关的0.6硬阈值。
    未声明上限时返回旧接口的0.0禁用哨兵。
    声明的高度上限须在最终生成后按真实Z范围检查。

    R5/C4/C5：未来背板前界由 _drawer_emit_params 作为硬几何
    边界参与求解，不登记为待定参数；保留最终实体与五金检查。
    """
    import math

    constraints = spec.get("constraints", {})
    if not isinstance(constraints, dict):
        raise SolveError("constraints must be an object")

    key = "drawer_height_ratio_cap"
    values = []
    if key in spec:
        values.append(("spec", spec[key]))
    if key in constraints:
        values.append(("constraints", constraints[key]))

    cap = 0.0
    source = "undeclared"
    if values:
        parsed = []
        for location, raw in values:
            if isinstance(raw, bool):
                raise SolveError(f"{location}.{key} must be numeric")
            try:
                value = float(raw)
            except (TypeError, ValueError):
                raise SolveError(
                    f"{location}.{key} must be numeric"
                ) from None
            if not math.isfinite(value) or not 0 < value <= 1:
                raise SolveError(f"{location}.{key} must be in (0, 1]")
            parsed.append((location, value))
        if len(parsed) == 2 and abs(parsed[0][1] - parsed[1][1]) > 1e-9:
            raise SolveError("Conflicting drawer height ratio constraints")
        source, cap = parsed[-1]

    raw_height = spec["height"]
    if isinstance(raw_height, bool):
        raise SolveError("Cabinet height must be positive")
    try:
        height = float(raw_height)
    except (TypeError, ValueError):
        raise SolveError("Cabinet height must be positive") from None
    if not math.isfinite(height) or height <= 0:
        raise SolveError("Cabinet height must be positive")

    return {
        "drawer_height_ratio_cap": cap,
        "drawer_height_constraint": {
            "enabled": bool(values),
            "source": source,
            "max_z": height * cap if values else None,
            "applies_to": ["drawer_front", "drawer_box"],
        },
        # 声明检查不等于检查已执行，保留现有发布门禁。
        "required_checks": [
            {
                "kind": "final_static_bounds",
                "components": ["drawer_front", "drawer_box", "slide"],
                "space_id": space["id"],
                "status": "pending",
            },
            {
                "kind": "project_drawer_height_limit",
                "max_z": height * cap if values else None,
                "status": "pending" if values else "not_applicable",
            },
            {
                "kind": "slide_mounting_fit",
                "status": "pending",
            },
            {
                "kind": "drawer_motion_collision",
                "components": ["drawer_front", "drawer_box", "slide"],
                "status": "pending",
            },
        ],
    }


INNER_DRAWER_HINGE_INSET_MM = 80.0     # 程总 2026-10-02：内抽在铰链侧多让 80
DOOR_LAMINATE_T = 18.0                # 门厚（用于换算门叶伸入量）


def _covering_doors_for_bay(openings, columns, bay_index, gap, spec=None):
    """返回覆盖该格的门的铰链侧与几何伸入量（供内抽让位使用）。

    门 X 位置与 door_row.v1（outer_gaps=False）**同式**：
      dw = (facade_span - gap*(N-1)) / N ；door_i_x = facade_x0 + i*(dw+gap)
    其中 facade_span/facade_x0 由 _facade_span(spec) 给出（不靠墙侧板盖门时让出其厚度）。
    """
    if spec is None:
        return None
    fx0, fspan = _facade_span(spec)
    total = 0
    for op in openings:
        total = max(total, int(op.get("first_door_index") or 0) + int(op.get("door_count") or 0))
    if total < 1:
        return None
    dw = (fspan - gap * (total - 1)) / float(total)
    width = float(spec["width"])
    t = DOOR_LAMINATE_T
    for op in openings:
        idxs = op.get("bay_indices") or []
        if bay_index not in idxs:
            continue
        bx0, bw = columns[bay_index]
        bx1 = bx0 + bw
        count = int(op.get("door_count") or 1)
        out = []
        for i in range(count):
            gi = int(op.get("first_door_index") or 0) + i
            dx = fx0 + gi * (dw + gap)
            if count >= 2:                       # 对开门：左扇左开、右扇右开
                hinge = "left" if i == 0 else "right"
            else:                                # 单开门
                if dx <= t + 1e-6:               # 左侧即柜体侧板（往侧板方向开）
                    hinge = "left"
                elif dx + dw >= width - t - 1e-6:  # 右侧即柜体侧板
                    hinge = "right"
                else:                            # 中间单门：按门序确定性派生（程总：随机；记录一次）
                    hinge = "left" if gi % 2 == 0 else "right"
            edge = dx if hinge == "left" else dx + dw
            zone = (edge, edge + t) if hinge == "left" else (edge - t, edge)
            ov = max(0.0, min(zone[1], bx1) - max(zone[0], bx0))
            out.append({"door_index": gi, "hinge_side": hinge, "door_x": round(dx, 4),
                        "door_w": round(dw, 4),
                        "hinge_edge_x": round(edge, 4), "leaf_zone_x": [round(zone[0], 4), round(zone[1], 4)],
                        "intrusion_into_bay_mm": round(ov, 4)})
        return {"opening_index": op["index"], "bay_index": bay_index, "doors": out, "gap": gap,
                "facade_span": [fx0, fx0 + fspan], "door_w": round(dw, 4)}
    return None


def _drawer_emit_params(spec, space, heights):
    """
    依据 R5/C4/C5：
    - 抽面区分 exposed / behind_door，前后界统一用全局Y求解。
    - back="none" 时，future_back 缺省为 "grooved_10"：
      预留未来背板Y=[D-28,D-18]，硬后界不晚于D-28。
    - future_back="none" 表示不预留未来背板；仍受当前空间、
      当前背板及显式后障碍边界约束。
    - future_back 仅参与抽盒深度求解，不修改spec，不生成实体，
      不改变背板/后拉条生成器对back的判断。
    - 默认后净空为10mm预览策略，未显式声明时进入pending；
      预留背板前界是硬几何，不进入pending。
    - 保留现有局部 front_y / box_y / box_depth 接口和返回结构。

    依赖：
      SolveError、PANEL_T、_cabinet_type、_drawer_project_constraints。
    """
    import math

    sources = {}
    pending = []
    eps = 0.01

    def finite(name, raw):
        if isinstance(raw, bool):
            raise SolveError(f"{name} must be numeric")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise SolveError(f"Invalid {name}: {raw!r}") from None
        if not math.isfinite(value):
            raise SolveError(f"Non-finite {name}: {value}")
        return value

    def number(name, default, positive=False):
        explicit = name in spec
        value = finite(name, spec[name] if explicit else default)
        if value < 0 or (positive and value <= 0):
            raise SolveError(f"Invalid {name}: {value}")
        sources[name] = {
            "source": "explicit" if explicit else "layout_default",
            "value": value,
        }
        return value

    thickness = number("board_thickness", PANEL_T, positive=True)
    cabinet_depth = finite("depth", spec["depth"])
    bbox = space["bbox"]
    space_y = finite("space.bbox.y", bbox["y"])
    space_depth = finite("space.bbox.d", bbox["d"])
    if cabinet_depth <= 0 or space_y < 0 or space_depth <= 0:
        raise SolveError("Invalid cabinet or drawer-space depth")

    metadata = space.get("metadata", {})
    if not isinstance(metadata, dict):
        raise SolveError("space.metadata must be an object")

    if "drawer_front_mode" in metadata:
        mode = metadata["drawer_front_mode"]
        mode_source = "space.metadata.drawer_front_mode"
    elif "drawer_front_mode" in spec:
        mode = spec["drawer_front_mode"]
        mode_source = "spec.drawer_front_mode"
    else:
        door_count = spec.get("door_count")
        has_doors = (
            isinstance(door_count, (int, float))
            and not isinstance(door_count, bool)
            and door_count > 0
        )
        mode = (
            "behind_door"
            if _cabinet_type(spec) == "wardrobe" and has_doors
            else "exposed"
        )
        mode_source = "inferred_layout_semantics"
        pending.append("confirmed_drawer_front_mode")

    if mode not in ("exposed", "behind_door"):
        raise SolveError(
            "drawer_front_mode must be 'exposed' or 'behind_door'"
        )
    sources["drawer_front_mode"] = {
        "source": mode_source,
        "value": mode,
    }

    front_thickness = number(
        "drawer_front_thickness", 18.0, positive=True
    )

    if mode == "exposed":
        # R5/C5：外露抽面使用全局Y。
        front_global_y = number("drawer_front_y", 2.0)
    else:
        # R5/C5：门后抽面使用所属空间前界加退距。
        if "drawer_front_y" in spec:
            raise SolveError(
                "behind_door drawers use drawer_hidden_front_gap relative "
                "to space.bbox.y; remove the global drawer_front_y parameter"
            )
        hidden_gap = number("drawer_hidden_front_gap", 2.0)
        front_global_y = space_y + hidden_gap
        if "drawer_hidden_front_gap" not in spec:
            pending.append("drawer_hidden_front_gap")
        pending.append("serving_door_to_drawer_front_clearance")

    connection_offset = number("drawer_connection_offset", 0.0)
    hardware_front_clearance = number(
        "drawer_hardware_front_clearance", 0.0
    )
    for key in (
        "drawer_connection_offset",
        "drawer_hardware_front_clearance",
    ):
        if key not in spec:
            pending.append(key)

    front_back_global_y = front_global_y + front_thickness
    box_front_global_y = (
        front_back_global_y
        + connection_offset
        + hardware_front_clearance
    )

    # R5/C4：后界是硬障碍前表面，尚未扣除净空。
    rear_limit = min(cabinet_depth, space_y + space_depth)
    # R5/C4/C5 修订：真实空间后界（未叠加未来背板/实际背板硬边界）。
    space_rear_limit = rear_limit
    back = spec.get("back", "grooved_10")
    future_back = spec.get("future_back", "grooved_10")
    if future_back not in ("none", "grooved_10"):
        raise SolveError(
            "future_back must be 'none' or 'grooved_10'"
        )
    sources["future_back"] = {
        "source": "explicit" if "future_back" in spec else "layout_default",
        "value": future_back,
    }

    if back == "none":
        if future_back == "grooved_10":
            # R5/C4/C5：未来槽背板固定占位[D-28,D-18]。
            # 这是预留硬几何，不沿用可变背板参数，不登记pending，
            # 不把back改为grooved_10，也不触发背板/后拉条生成。
            future_back_front_y = cabinet_depth - 28.0
            rear_limit = min(rear_limit, future_back_front_y)
            sources["future_back_front_y"] = {
                "source": "derived_hard_geometry",
                "value": future_back_front_y,
            }
        # future_back="none"时不增加未来背板边界。
    elif back == "grooved_10":
        # 保留当前实际背板参数的原有语义；
        # future_back="none"不能取消实际存在的背板障碍。
        back_thickness = number(
            "back_thickness", 10.0, positive=True
        )
        back_setback = number("back_rear_setback", 18.0)
        rear_limit = min(
            rear_limit,
            cabinet_depth - back_setback - back_thickness,
        )
    elif "drawer_rear_limit_y" not in spec:
        pending.append("resolved_back_front_y")

    if "drawer_rear_limit_y" in spec:
        # 显式边界只能进一步收紧，不能放宽未来背板硬边界。
        rear_limit = min(
            rear_limit,
            number("drawer_rear_limit_y", rear_limit),
        )

    # R5/C4/C5 修订：先确定是否进入“无背板量化缺省分支”，
    # 由同一个布尔量同时决定净空取值与长度分支，避免先按不适用净空判错。
    quantized_default = (
        back == "none"
        and "drawer_box_length" not in spec
        and not (
            "drawer_slide_length" in spec
            and "drawer_slide_box_length_delta" in spec
        )
        and "drawer_layout_box_length" not in spec
    )

    explicit_rear_clearance = "drawer_rear_clearance" in spec
    if explicit_rear_clearance:
        rear_clearance = number("drawer_rear_clearance", 0.0)
        if not math.isfinite(rear_clearance) or rear_clearance < 0:
            raise SolveError(
                "drawer_rear_clearance must be finite and non-negative"
            )
        sources["drawer_rear_clearance"] = {
            "source": "explicit",
            "value": rear_clearance,
        }
    elif quantized_default:
        rear_clearance = 0.0
        sources["drawer_rear_clearance"] = {
            "source": "quantized_default_no_implicit_clearance",
            "value": rear_clearance,
        }
    else:
        rear_clearance = 10.0
        pending.append("drawer_rear_clearance")

    available_length = (
        rear_limit - rear_clearance - box_front_global_y
    )
    # 缺省分支实际执行的硬上限（显式取消未来背板时不等于 available_length）。
    enforced_length_limit = available_length
    if available_length <= 0:
        raise SolveError("No depth available for drawer box")

    # R5/C5：轨长与盒长不假定相等；适配差值允许正负。
    slide_length = None
    slide_delta = None
    if "drawer_slide_length" in spec:
        slide_length = number(
            "drawer_slide_length", 0.0, positive=True
        )
    if "drawer_slide_box_length_delta" in spec:
        slide_delta = finite(
            "drawer_slide_box_length_delta",
            spec["drawer_slide_box_length_delta"],
        )
        sources["drawer_slide_box_length_delta"] = {
            "source": "explicit",
            "value": slide_delta,
        }

    if "drawer_box_length" in spec:
        box_length = number("drawer_box_length", 0.0, positive=True)
        if slide_length is None:
            pending.append("drawer_slide_length")
        if slide_delta is None:
            pending.append("drawer_slide_box_length_delta")
    elif slide_length is not None and slide_delta is not None:
        box_length = slide_length + slide_delta
        sources["drawer_box_length"] = {
            "source": "derived_from_explicit_hardware",
            "value": box_length,
        }
    elif quantized_default:
        # R5/C4/C5 修订：无背板缺省分支按未来背板策略二选一，
        # 长度向 50mm 取整（向下）。
        if future_back == "grooved_10":
            length_limit = available_length
            quantized_source = (
                "derived_from_back_none_reserving_future_back_quantized_50"
            )
        else:
            # D-28 是长度上限；available_length 已包含真实空间边界、
            # 显式后界与净空（rear_limit 已被显式后界收紧）。
            length_limit = min(
                cabinet_depth - 28.0 - rear_clearance,
                available_length,
            )
            quantized_source = "derived_from_back_none_quantized_50"
        box_length = 50.0 * math.floor(length_limit / 50.0)
        if box_length <= 0:
            raise SolveError(
                "No quantized drawer box length available: "
                f"limit={length_limit}"
            )
        enforced_length_limit = length_limit
        sources["drawer_box_length"] = {
            "source": quantized_source,
            "value": box_length,
        }
        sources["future_back_policy"] = {
            "source": sources["future_back"]["source"],
            "value": future_back,
        }
        sources["future_back_reservation_required"] = {
            "source": "derived_from_back_policy",
            "value": future_back == "grooved_10",
        }
    else:
        # 仅预览布局长度允许裁短；显式盒长及五金推导盒长
        # 超出可用长度时必须报错，不能静默改变五金尺寸。
        preferred_length = number(
            "drawer_layout_box_length",
            available_length,
            positive=True,
        )
        box_length = min(preferred_length, available_length)
        sources["drawer_box_length"] = {
            "source": (
                "explicit_layout_capped_to_available"
                if "drawer_layout_box_length" in spec
                else "layout_default_from_available_depth"
            ),
            "value": box_length,
        }
        pending.append("hardware_compatible_drawer_box_length")

    if not math.isfinite(box_length) or box_length < 100.0:
        raise SolveError(f"Drawer box length too small: {box_length}")

    # R5/C4：深度是硬边界，不使用eps允许盒体越过预留净空。
    if box_length > enforced_length_limit:
        raise SolveError(
            f"Drawer box requires {box_length}mm; available depth "
            f"with rear clearance is {enforced_length_limit}mm"
        )

    if slide_length is not None:
        if slide_length > available_length + eps:
            raise SolveError(
                "Slide installation length exceeds available depth"
            )
        if slide_delta is not None:
            if abs(box_length - (slide_length + slide_delta)) > eps:
                raise SolveError(
                    "Drawer box length is incompatible with slide"
                )

    # R5/C5：唯一的全局->局部转换点，保持operator接口不变。
    front_y_local = front_global_y - space_y
    box_y_local = box_front_global_y - space_y

    if box_y_local < -eps:
        raise SolveError(
            "Drawer space begins behind box front: "
            f"space_y={space_y}, box_front_y={box_front_global_y}"
        )
    if (
        front_global_y < 0
        or front_back_global_y > cabinet_depth
    ):
        raise SolveError("Drawer front is outside cabinet depth")

    resolved_heights = [
        finite(f"heights[{index}]", value)
        for index, value in enumerate(heights)
    ]
    if any(value <= 0 for value in resolved_heights):
        raise SolveError("Drawer heights must be positive")

    # —— r90：轨型由**抽侧板高度**逐抽几何推导（程总规则），间隙随之确定 ——
    RAIL_CLEARANCE_BY_TYPE = {"three_section": 13.0, "undermount": 5.0}
    UM_MIN_SIDE_H = 100.0
    heights_for_rail = [float(h) for h in resolved_heights]
    derived = []
    for i, fh in enumerate(heights_for_rail):
        if not (fh > 0) or fh != fh:
            raise SolveError("drawer %d: 抽面高 %.6g 非法（需有限且为正）——判据不可靠时拒绝，不落默认轨型" % (i, fh))
        # 判据量＝**让位前**标准侧板高（现有标准构造公式，非程总原话新增公式）
        side_h_std = max(40.0, fh - 25.0)
        t = "undermount" if side_h_std >= UM_MIN_SIDE_H else "three_section"
        clr = RAIL_CLEARANCE_BY_TYPE[t]
        gap = 42.0 if t == "undermount" else 12.0
        side_h_after = max(40.0, fh - 25.0 - (30.0 if t == "undermount" else 0.0))
        # ③ max(40,…) 下限触发时，盒顶会超过抽面顶 → 明确拒绝（不静默生成矛盾几何）
        if gap + side_h_after > fh + 1e-9:
            raise SolveError(
                "drawer %d: front height %.3f too small for the %s construction — box top would exceed the front top "
                "(bottom_gap %.1f + side_h_after %.3f > front %.3f); refusing to emit contradictory geometry"
                % (i, fh, t, gap, side_h_after, fh))
        derived.append({"index": i, "front_h": fh,
                        "side_h_before_clearance": side_h_std,
                        "side_h_before_clearance_source": "标准构造公式 max(40, 抽面高 - 25)（现有构造，非本次新规则新增）",
                        "side_h_after_clearance": side_h_after,
                        "slide_type": t, "slide_clearance": clr, "bottom_gap": gap,
                        "rail_vertical_region": {
                            "z_datum": "所属抽屉格空间底面(space bbox z)",
                            "z_from_mm_above_datum": 0.0, "z_to_mm_above_datum": gap, "height_mm": gap,
                            "rail_occupancy_mm": 30.0 if t == "undermount" else 0.0,
                            "pre_existing_clearance_mm": 12.0,
                            "measured_surface": "抽底板下表面（＝盒侧板下缘）"}})
    types = sorted({d["slide_type"] for d in derived})
    if "drawer_slide_type" in spec:
        if spec["drawer_slide_type"] not in RAIL_CLEARANCE_BY_TYPE:
            raise SolveError("spec.drawer_slide_type must be one of %s; got %r"
                             % (sorted(RAIL_CLEARANCE_BY_TYPE), spec["drawer_slide_type"]))
        if spec["drawer_slide_type"] not in types:
            raise SolveError("spec.drawer_slide_type %r contradicts geometry-derived type(s) %s "
                             "(side heights %s)" % (spec["drawer_slide_type"], types,
                                                    [round(d["side_h_before_clearance"], 3) for d in derived]))
    for d in derived:
        space_width = float(space["bbox"]["w"])
        if space_width - 2.0 * d["slide_clearance"] - 2.0 * thickness <= eps:
            raise SolveError("drawer %d: bay clear width %.4f leaves no positive width for bottom/back with %s"
                             % (d["index"], space_width, d["slide_type"]))
    # astra r89 §3：推导值、判据、最终有效值、来源、规则版本**分开记录**
    sources["drawer_slide_type"] = {
        "rule_id": "rail_selection.v1",
        "rule_version": "2026-10-02",
        "rule_text": "程总：抽侧板的高度大于等于100就用托底轨，其它用三节轨；托底轨在抽屉底板下面需要占用30的高度位置（修正：在既有 12 之外再占 30 ⇒ 底距 42，抽侧板高 −30）",
        "derived_slide_type_per_drawer": [d["slide_type"] for d in derived],
        "derivation_basis": {
            "quantity": "side_h_before_clearance（判据量＝让位前标准侧板高）",
            "formula": "max(40, 抽面高 - 25)（现有标准构造公式，非程总原话新增）",
            "per_drawer_value": [d["side_h_before_clearance"] for d in derived],
            "threshold_mm": UM_MIN_SIDE_H,
            "threshold_branch": ">=100 → undermount（180 口径已作废，见 rule_revision 记录）",
        },
        "effective_slide_type_per_drawer": [d["slide_type"] for d in derived],
        "effective_source": "geometry_derived（本轮无显式覆盖）",
        "explicit_spec_value": spec.get("drawer_slide_type"),
        "explicit_conflict": None if spec.get("drawer_slide_type") in (None, *types) else "conflict",
        "override_authorization": "无（特殊订单覆盖自动规则需单独授权）",
    }
    sources["slide_clearance"] = {"source": "derived_from_drawer_slide_type",
                                  "values": sorted({d["slide_clearance"] for d in derived})}
    result = {
        "heights": resolved_heights,
        "gap": number("gap", 2.5),
        "thickness": thickness,
        "front_y": front_y_local,
        "front_depth": front_thickness,
        "box_y": box_y_local,
        "box_depth": box_length,
        "drawer_slide_type_per_index": [d["slide_type"] for d in derived],
        "slide_clearance_per_index": [d["slide_clearance"] for d in derived],
        "bottom_gap_per_index": [d["bottom_gap"] for d in derived],
        "side_h_before_clearance_per_index": [d["side_h_before_clearance"] for d in derived],
        "side_h_after_clearance_per_index": [d["side_h_after_clearance"] for d in derived],
        "rail_vertical_region_per_index": [d["rail_vertical_region"] for d in derived],
        "rail_derivation": derived,
    }
    result.update(_drawer_project_constraints(spec, space))
    result["parameter_sources"] = sources
    result["pending_parameters"] = sorted(set(pending))

    result["future_back_policy"] = (
        future_back if back == "none" else "not_applicable"
    )
    result["future_back_policy_source"] = (
        sources.get("future_back", {}).get("source")
        if back == "none"
        else "not_applicable"
    )

    result["coordinate_audit"] = {
        "drawer_front_mode": mode,
        "space_y_global": space_y,
        "front_y_global": front_global_y,
        "front_y_local": front_y_local,
        "front_back_y_global": front_back_global_y,
        "box_y_global": box_front_global_y,
        "box_y_local": box_y_local,
        "box_end_y_global": box_front_global_y + box_length,
        "rear_limit_y_global": rear_limit,
        "rear_clearance": rear_clearance,
        "space_rear_limit_y_global": space_rear_limit,
        "enforced_length_limit": enforced_length_limit,
        "future_back_reservation_required": (
            future_back == "grooved_10" if back == "none" else None
        ),
        "future_back_reservation_check": (
            "not_applicable"
            if back != "none" or future_back != "grooved_10"
            else (
                "passed"
                if box_front_global_y + box_length <= rear_limit + eps
                else "failed"
            )
        ),
    }

    result["production_ready"] = False
    result["validation_status"] = {
        "depth_envelope": "passed",
        "hardware_parameters": "pending" if pending else "provided",
        "front_placement_and_serving_door": "pending",
        "final_drawer_front_box_z": "pending",
        "slide_mounting_geometry": "pending",
        "motion": "pending",
    }
    return result



def _dwg_number(value, label):
    import math
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SolveError(label + " must be numeric")
    value = float(value)
    if not math.isfinite(value):
        raise SolveError(label + " must be finite")
    return value


def _dwg_explicit_shelves(spec, columns, graph):
    """Return False only when explicit z_positions is absent."""
    cfg = spec.get("shelves")
    if not isinstance(cfg, dict) or "z_positions" not in cfg:
        return False

    positions = cfg["z_positions"]
    if not isinstance(positions, list):
        raise SolveError("shelves.z_positions must be an array")

    # Drawer separators are structural, not silently added to an exact list.
    # Their explicit integration requires a separate drawer-aware solution.
    if _has_function(spec, "drawer"):
        raise SolveError(
            "Explicit shelves with drawers require a declared separator layout"
        )

    t = _dwg_number(spec.get("board_thickness", PANEL_T), "thickness")
    depth = _dwg_number(spec["depth"], "depth")
    bottom = _base_z(spec) + t
    top = float(spec["height"]) - t - float(spec.get("top_trim") or 0)
    zs = sorted(
        _dwg_number(v, "shelves.z_positions[%d]" % i)
        for i, v in enumerate(positions)
    )
    if t <= 0:
        raise SolveError("Shelf thickness must be positive")
    for i, z in enumerate(zs):
        if z < bottom - EPS or z + t > top + EPS:
            raise SolveError("Explicit shelf outside clear body: %.6f" % z)
        if i and z - zs[i - 1] < t - EPS:
            raise SolveError("Duplicate or overlapping explicit shelf levels")

    # Exact, declared Z levels apply to every interior column.
    # Boards remain split at structural vertical partitions.
    for col_index, (x, width) in enumerate(columns):
        if width <= 0:
            raise SolveError("Explicit shelf column must have positive width")
        for level, z in enumerate(zs):
            uid = "layout.explicit_shelf.%d.%d" % (col_index, level)
            # Door front occupies Y=2..20. Shelves begin behind the
            # door plane and run to the nominal cabinet rear.
            # This is the revised effective-depth interpretation of
            # "full depth" for a closed front.
            shelf_y = float(spec.get("struct_y", 20.0))
            if abs(shelf_y - 20.0) > EPS:
                raise SolveError(
                    "Explicit shelf front clearance requires STRUCT_Y=20"
                )
            shelf_depth = depth - shelf_y
            if shelf_depth <= 0:
                raise SolveError("Explicit shelf effective depth is nonpositive")

            _add_shelf(
                graph, uid, "explicit_shelf",
                _box(x, shelf_y, z, width, shelf_depth, t),
                "column.%d" % col_index,
            )
            node = next(n for n in graph["nodes"] if n["uid"] == uid)
            node.setdefault("source", {}).update({
                "rule_id": "explicit_shelf_front_clearance.v1",
                "z_reference": "cabinet_bottom",
                "z_min": z,
                "y_min": shelf_y,
                "y_max": shelf_y + shelf_depth,
                "input_field": "shelves.z_positions",
                "depth_policy": "effective_depth_behind_door",
                "front_clearance_mm": 20.0,
            })
            node = next(n for n in graph["nodes"] if n["uid"] == uid)
            node.setdefault("source", {}).update({
                "rule_id": "explicit_shelf_z.v1",
                "z_reference": "cabinet_bottom",
                "z_min": z,
                "input_field": "shelves.z_positions",
                "depth_policy": "full_depth_y0",
            })
    return True


def _dwg_vertical_config(spec):
    openings = spec.get("facade_openings", [])
    if not isinstance(openings, list):
        raise SolveError("facade_openings must be an array")
    configured = [
        o for o in openings
        if isinstance(o, dict) and "vertical_segments" in o
    ]
    if not configured:
        return None
    if len(openings) != 1 or len(configured) != 1:
        raise SolveError(
            "vertical_segments currently requires exactly one facade opening"
        )

    raw = configured[0]["vertical_segments"]
    if not isinstance(raw, list) or not raw:
        raise SolveError("vertical_segments must be a nonempty array")
    gap = _dwg_number(spec.get("gap", 2.5), "gap")
    if abs(gap - 2.5) > EPS:
        raise SolveError("Explicit vertical door gap must be 2.5 mm")
    height = float(spec["height"])
    segments = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict) or set(item) != {"z_min", "z_max"}:
            raise SolveError("Each segment requires only z_min and z_max")
        lo = _dwg_number(item["z_min"], "segment.z_min")
        hi = _dwg_number(item["z_max"], "segment.z_max")
        if lo < 0 or hi > height or hi <= lo:
            raise SolveError("Invalid vertical door segment bounds")
        if i and abs(lo - segments[-1][1] - gap) > EPS:
            raise SolveError(
                "Segments must be Z ordered with exactly 2.5 mm gaps"
            )
        segments.append((lo, hi))

    # A front header may touch the upper door, but may not cover it.
    header = spec.get("top_trim_front")
    if isinstance(header, dict):
        header_bottom = height - _dwg_number(
            header.get("height"), "top_trim_front.height"
        )
        if segments[-1][1] > header_bottom + EPS:
            raise SolveError("Vertical doors intersect front header")
    return segments


def _dwg_emit_vertical_doors(
    spec, layout, door_space, spaces, spaces_by_id, graph, operator_uses
):
    import copy
    segments = _dwg_vertical_config(spec)
    if segments is None:
        return False
    if door_space is None:
        raise SolveError("vertical_segments requires front.doors space")
    if _has_function(spec, "drawer"):
        raise SolveError(
            "Segmented doors with drawers require Z-aware covering-door analysis"
        )

    count = int(layout["door_count"])
    if count < 1:
        raise SolveError("Segmented doors require positive horizontal count")
    x = float(door_space["bbox"]["x"])
    width = float(door_space["bbox"]["w"])
    gap = float(spec.get("gap", 2.5))
    leaf_width = (width - gap * (count - 1)) / count
    if leaf_width <= 0:
        raise SolveError("Segmented door leaf has nonpositive width")

    # The existing front.doors object becomes the enclosing semantic space.
    door_space["bbox"]["z"] = segments[0][0]
    door_space["bbox"]["h"] = segments[-1][1] - segments[0][0]

    for index, (lo, hi) in enumerate(segments):
        sid = "front.doors.segment.%d" % index
        if sid in spaces_by_id:
            raise SolveError("Duplicate segmented door space: " + sid)
        space = copy.deepcopy(door_space)
        space.update({
            "id": sid,
            "kind": "door_front",
            "parent_id": door_space["id"],
        })
        space["bbox"]["z"] = lo
        space["bbox"]["h"] = hi - lo
        spaces.append(space)
        spaces_by_id[sid] = space

        params = {
            "count": count,
            "gap": gap,
            "front_y": 2.0,
            "depth": 18.0,
            "outer_gaps": False,
            "enforce_width": _cabinet_type(spec) == "wardrobe",
        }
        result = emit_operator(
            "door_row.v1", spec, space, params,
            "front_segment_%d" % index,
        )
        # door_row.v1 is expected to emit exactly one panel per horizontal leaf.
        emitted = sorted(
            result["nodes"], key=lambda n: n["bbox"]["x"]
        )
        if len(emitted) != count:
            raise SolveError("door_row emitted an unexpected node count")
        for leaf, node in enumerate(emitted):
            expected = {
                "x": x + leaf * (leaf_width + gap),
                "y": 2.0, "z": lo,
                "w": leaf_width, "d": 18.0, "h": hi - lo,
            }
            for key, value in expected.items():
                if abs(float(node["bbox"][key]) - value) > EPS:
                    raise SolveError(
                        "door_row changed final segment geometry: "
                        + node["uid"] + "." + key
                    )
            node.setdefault("source", {}).update({
                "vertical_segment_index": index,
                "horizontal_leaf_index": leaf,
                "z_min": lo,
                "z_max": hi,
                "input_field": "facade_openings[0].vertical_segments",
            })
        _merge_result(graph, result)
        operator_uses.append(_operator_use(
            "use." + sid, "door_row.v1", [sid], params,
        ))
    return True


def solve(spec):
    # —— r98：靠墙侧收口条让位（程总 2026-06-02 靠墙规则）——
    WALL_TRIM_W = 18.0
    WALL_TRIM_D = 60.0
    _walls = [w for w in (spec.get("walls") or []) if w in ("left", "right")]
    _nominal_width = float(spec["width"]) if spec.get("width") is not None else None
    if _walls and _nominal_width:
        spec = dict(spec)
        spec["width"] = _nominal_width - WALL_TRIM_W * len(_walls)
        spec["_wall_trim"] = {"nominal_width": _nominal_width, "walls": _walls,
                              "trim_w": WALL_TRIM_W, "trim_d": WALL_TRIM_D,
                              "rule_id": "wall_trim.v1",
                              "rule_source": "程总2026-06-02靠墙规则（技能 trim-construction.md）"}
    validate_spec(spec)
    _dwg_vertical_config(spec)
    layout = _column_layout(spec)
    if not layout.get("can_generate", False):
        # S1 契约：缺失/矛盾的功能格与开口声明不得生成模型
        empty_graph = {
            "nodes": [],
            "edges": [],
            "assembly_bbox": _box(
                0, 0, 0, spec["width"], spec["depth"], spec["height"]
            ),
        }
        return {
            "spec": dict(spec),
            "solve_status": layout["status"],
            "solve_errors": layout["errors"],
            "solve_warnings": layout["warnings"],
            "missing_parameters": layout["missing_parameters"],
            "layout_contract_version": layout["layout_contract_version"],
            "semantic_assembly": {
                "spec_id": spec["id"],
                "spaces": [],
                "operators": [],
                "relations": [],
                "candidate_score": {
                    "total": 0.0,
                    "structure": 0.0,
                    "function": 0.0,
                    "facade": 0.0,
                },
                "metadata": {
                    "solve_status": layout["status"],
                    "solve_errors": layout["errors"],
                    "solve_warnings": layout["warnings"],
                    "missing_parameters": layout["missing_parameters"],
                    "layout_contract_version": layout["layout_contract_version"],
                    "boundary_bindings": layout["boundary_bindings"],
                },
            },
            "board_graph": empty_graph,
            "self_check": {
                "passed": False,
                "board_count": 0,
                "node_count": 0,
                "edge_count": 0,
                "invariants": [],
                "not_run": True,
                "layout_feasibility": {
                    "status": layout["status"],
                    "passed": False,
                    "errors": layout["errors"],
                    "missing_parameters": layout["missing_parameters"],
                },
            },
        }
    spaces = build_space_tree(spec, layout)
    columns = layout["columns"]
    spaces_by_id = _space_by_id(spaces)
    graph = {
        "nodes": [],
        "edges": [],
        "assembly_bbox": _box(
            0,
            0,
            0,
            spec["width"],
            spec["depth"],
            spec["height"],
        ),
    }
    operator_uses = []

    carcass_params = {
        "thickness": spec.get("board_thickness", PANEL_T),
        "struct_y": STRUCT_Y,
        "plinth_height": spec.get("plinth_height", 60.0),
        "back": spec.get("back", "grooved_10"),
        "back_thickness": (5.0 if spec.get("back") == "grooved_5" else BACK_T),
        "back_front_offset": (23.0 if spec.get("back") == "grooved_5" else BACK_FRONT_OFFSET),
        "top_trim_height": float(spec.get("top_trim") or 0.0),
        "exposed_sides": ([s for s in ("left", "right") if s not in (spec.get("walls") or [])]
                            if "walls" in spec else []),
    }
    carcass_result = emit_operator(
        "carcass.v1",
        spec,
        spaces_by_id["cabinet"],
        carcass_params,
        "carcass",
    )
    _merge_result(graph, carcass_result)
    operator_uses.append(
        _operator_use(
            "use.carcass",
            "carcass.v1",
            ["cabinet"],
            carcass_params,
        )
    )

    _add_plinth(spec, graph)
    _add_vertical_partitions(spec, layout, graph)

    drawer_results = []
    for space in spaces:
        if space["kind"] != "drawer_column":
            continue
        cfg = _function_config(spec, "drawer")
        heights = cfg.get(
            "heights",
            spec.get("drawer_heights", [147.0, 187.0]),
        )
        drawer_params = _drawer_emit_params(spec, space, heights)
        # —— r96：内抽在铰链侧让 80mm（程总规则）——
        try:
            _bay_i = int(space["id"].split(".", 1)[0].split("_")[-1] if space["id"].startswith("column.") is False else space["id"].split(".")[1])
        except Exception:
            _bay_i = None
        cov = (_covering_doors_for_bay(layout.get("facade_openings") or [], columns, _bay_i,
                                       float(spec.get("gap", 2.5)), spec)
               if isinstance(_bay_i, int) else None)
        cov = cov if (cov and cov.get("doors")) else None
        # astra r96 要求：内抽/外抽身份与"规则是否施加"分列记录，不得混写
        drawer_params["drawer_kind"] = "inner" if cov else "outer"
        drawer_params.setdefault("parameter_sources", {})["drawer_kind"] = {
            "rule_id": "inner_drawer_kind.v1", "rule_version": "2026-10-02",
            "value": drawer_params["drawer_kind"],
            "basis": "抽面之前有覆盖门 → 内抽；否则外抽（程总：露在外面是外抽，在门板背后是内抽）",
        }
        if cov:
            intruding = [d for d in cov["doors"] if d["intrusion_into_bay_mm"] > 1e-6]
            hinges = sorted({d["hinge_side"] for d in cov["doors"]})
            # 让位侧：**每一扇实际伸入格内的门**的铰链侧（两侧都有门叶伸入＝两侧都让）；
            # 若无门叶伸入，则按程总的产品规则取覆盖门的铰链侧（不因"没检测到"而不施加）
            sides = sorted({d["hinge_side"] for d in intruding}) if intruding else hinges
            drawer_params["hinge_inset"] = {"sides": sides, "mm": INNER_DRAWER_HINGE_INSET_MM}
            drawer_params.setdefault("parameter_sources", {})["hinge_inset"] = {
                "rule_id": "inner_drawer_hinge_inset.v1",
                "rule_version": "2026-10-02",
                "rule_text": "程总：内抽在铰链侧多让80",
                "drawer_kind": "inner",
                "hinge_sides": sides,
                "hinge_inset_mm": INNER_DRAWER_HINGE_INSET_MM,
                "covering_opening": cov["opening_index"],
                "bay_index": cov["bay_index"],
                "doors": cov["doors"],
                # astra r96 口径：三条分开写，禁止把"没检测到干涉"写成"没执行规则"
                "clearance_policy": "inner_drawer_hinge_side_80",
                "geometric_intrusion_detected": bool(intruding),
                "geometric_intruding_doors": [d["door_index"] for d in intruding],
                "clearance_applied": True,
                "clearance_reason": "product_rule",
                "note": "门叶伸入量按『铰链侧门前棱为转轴』的声明模型估算，仅作原因与验证信息，"
                        "**不作为是否执行规则的开关**（程总规则＝产品规则）",
            }
        result = emit_operator(
            "drawer_module.v1",
            spec,
            space,
            drawer_params,
            space["id"].replace(".", "_"),
        )
        drawer_results.append(result)
        _merge_result(graph, result)
        operator_uses.append(
            _operator_use(
                f"use.{space['id']}.drawer",
                "drawer_module.v1",
                [space["id"]],
                drawer_params,
            )
        )

    _add_layout_boards(
        spec,
        columns,
        graph,
        drawer_results[0] if drawer_results else None,
    )

    if _has_function(spec, "hang"):
        for space in spaces:
            if space["kind"] != "hanging_bay":
                continue
            if float(space["bbox"]["h"]) < 1350.0:
                continue
            rod_params = {
                "diameter": 25.0,
                "top_offset": 60.0,
                "min_clear_height": 1350.0,
            }
            result = emit_operator(
                "hang_rod.v1",
                spec,
                space,
                rod_params,
                space["id"].replace(".", "_"),
            )
            _merge_result(graph, result)
            operator_uses.append(
                _operator_use(
                    f"use.{space['id']}.rod",
                    "hang_rod.v1",
                    [space["id"]],
                    rod_params,
                )
            )

    door_space = spaces_by_id.get("front.doors")
    segmented = _dwg_emit_vertical_doors(
        spec, layout, door_space, spaces, spaces_by_id,
        graph, operator_uses,
    ) if _dwg_vertical_config(spec) is not None else False
    if door_space is not None and not segmented:
        default_count = 3 if _cabinet_type(spec) == "wardrobe" else 1
        door_params = {
            "count": int(layout["door_count"]),
            "gap": spec.get("gap", 2.5),
            "front_y": 2.0,
            "depth": 18.0,
            "outer_gaps": False,
            "enforce_width": _cabinet_type(spec) == "wardrobe",
        }
        result = emit_operator(
            "door_row.v1",
            spec,
            door_space,
            door_params,
            "front",
        )
        _merge_result(graph, result)
        operator_uses.append(
            _operator_use(
                "use.front.doors",
                "door_row.v1",
                ["front.doors"],
                door_params,
            )
        )

    assembly = {
        "spec_id": spec["id"],
        "metadata": {
            "solve_status": layout["status"],
            "solve_errors": layout["errors"],
            "solve_warnings": layout["warnings"],
            "missing_parameters": layout["missing_parameters"],
            "layout_contract_version": layout["layout_contract_version"],
            "boundary_bindings": layout["boundary_bindings"],
        },
        "spaces": spaces,
        "operators": operator_uses,
        "relations": _build_relations(spaces),
        "candidate_score": {
            "total": 1.0,
            "structure": 1.0,
            "function": 1.0,
            "facade": 1.0,
        },
    }

    validate_semantic(assembly)
    validate_board_graph(graph)
    # —— r98：靠墙侧收口条（让位后追加，先缩宽再平移＝右缘锚定）——
    _wt = spec.get("_wall_trim") if isinstance(spec, dict) else None
    if _wt:
        _tw, _td = _wt["trim_w"], _wt["trim_d"]
        _shift = _tw if "left" in _wt["walls"] else 0.0
        if _shift:
            for _n in graph["nodes"]:
                _n["bbox"]["x"] += _shift
        _trims = []
        _nom = _wt["nominal_width"]
        _h = float(spec["height"])
        for _side in _wt["walls"]:
            _x0 = 0.0 if _side == "left" else _nom - _tw
            _uid = "trim.side_%s" % _side
            _nn = {"uid": _uid, "name": _uid,
                   "role": "side_trim_left" if _side == "left" else "side_trim_right",
                   "bbox": {"x": _x0, "y": 2.0, "z": 0.0, "w": _tw, "d": _td, "h": _h},
                   "space_id": "cabinet", "operator_id": "wall_trim.v1",
                   "material": "panel", "grain": "z",
                   "source": {"rule_id": "wall_trim.v1",
                              "rule_source": "程总2026-06-02靠墙规则（技能 trim-construction.md）",
                              "note": "靠墙侧收口条：18 宽 x 60 深 x 全高，前脸与门板齐平(Y=2)；总宽固定、柜体让位"}}
            graph["nodes"].append(_nn)
            _pu = "carcass.left" if _side == "left" else "carcass.right"
            _pn = next((n for n in graph["nodes"] if n.get("uid") == _pu), None)
            if _pn:
                _ov = max(0.0, min(2.0 + _td, _pn["bbox"]["y"] + _pn["bbox"]["d"]) - max(2.0, _pn["bbox"]["y"]))
                graph["edges"].append({"a": _uid, "b": _pu, "type": "butt",
                                       "expected_contact_mm2": _ov * _h,
                                       "allowed_overlap_mm3": 0.0, "tolerance": 0.1})
            _trims.append({"side": _side, "uid": _uid, "x0": _x0, "w": _tw, "d": _td, "y0": 2.0,
                           "body_width": float(spec["width"]), "nominal_width": _nom, "rule_id": "wall_trim.v1"})
        # 让位后：柜体包络回到总宽（收口条并入柜体包络）；并登记 wall_trim.v1 算子归属
        for _sp in assembly.get("spaces", []):
            if _sp.get("id") == "cabinet" and isinstance(_sp.get("bbox"), dict):
                _sp["bbox"]["x"] = 0.0
                _sp["bbox"]["w"] = _nom
        if not any(o.get("operator_id") == "wall_trim.v1" for o in assembly.get("operators", [])):
            assembly.setdefault("operators", []).append({
                "id": "use.wall_trim", "operator_id": "wall_trim.v1", "space_ids": ["cabinet"],
                "params": {"trim_w": _tw, "trim_d": _td, "walls": list(_wt["walls"]),
                           "nominal_width": _nom, "body_width": float(spec["width"])},
                "score": 1.0,
            })
        # 图形 schema 只允许 nodes/edges/assembly_bbox（键集合固定）→ 收口条记录写在**节点 source** 与 spec 里
        graph["assembly_bbox"] = {"x": 0.0, "y": 0.0, "z": 0.0,
                                  "w": _nom, "d": float(spec["depth"]), "h": float(spec["height"])}
        spec["wall_trim_applied"] = _trims

    report = self_check(spec, assembly, graph, layout=layout)
    if not report["passed"]:
        failures = [
            item["name"]
            for item in report["invariants"]
            if not item["passed"]
        ]
        raise SolveError("Self-check failed: " + ", ".join(failures))

    return {
        "spec": dict(spec),
        "semantic_assembly": assembly,
        "board_graph": graph,
        "self_check": report,
    }


def _build_relations(spaces):
    relations = []
    for space in spaces:
        parent = space.get("parent_id")
        if parent is not None:
            relations.append(
                {"from": parent, "type": "contains", "to": space["id"]}
            )
    return relations


def _check_facade_partition_alignment(spec, layout, graph):
    """Check functional partitions and only explicitly bound facade seams.

    Does not require a partition at every door seam.
    Does not treat layout feasibility as complete cabinet acceptance.
    """
    errors = []
    status = layout.get("status")
    if status != "solved":
        return {
            "passed": False,
            "status": status or "pending",
            "door_count": int(layout.get("door_count", 0)),
            "door_width": float(layout.get("door_width", 0.0)),
            "seam_centers": list(layout.get("seam_centers", [])),
            "partition_centers": [],
            "errors": [{
                "reason": "layout_not_solved",
                "status": status,
            }],
        }

    partitions = sorted(layout["partitions"], key=lambda item: item["x"])
    nodes = sorted(
        (
            node for node in graph["nodes"]
            if node["role"] == "vertical_partition"
        ),
        key=lambda node: float(node["bbox"]["x"]),
    )
    expected_count = len(layout["columns"]) - 1

    if len(partitions) != expected_count:
        errors.append({
            "reason": "layout_partition_count",
            "expected": expected_count,
            "actual": len(partitions),
        })
    if len(nodes) != expected_count:
        errors.append({
            "reason": "count_mismatch",
            "expected_partition_count": expected_count,
            "actual_partition_count": len(nodes),
        })

    thickness = float(spec.get("board_thickness", PANEL_T))
    for index, (partition, node) in enumerate(zip(partitions, nodes)):
        bbox = node["bbox"]
        for field, expected in (("x", partition["x"]), ("w", thickness)):
            actual = float(bbox[field])
            tolerance = PARTITION_POSITION_TOL if field == "x" else EPS
            if abs(actual - expected) > tolerance:
                errors.append({
                    "reason": "functional_partition_" + field,
                    "partition_index": index,
                    "uid": node.get("uid"),
                    "expected": expected,
                    "actual": actual,
                })

    seams = layout["seam_centers"]
    for binding in layout["boundary_bindings"]:
        index = binding["partition_index"]
        seam_index = binding["seam_index"]
        if not (0 <= index < len(partitions) and 0 <= seam_index < len(seams)):
            errors.append({"reason": "invalid_boundary_binding", **binding})
            continue

        expected_center = (
            partitions[index]["x"] + partitions[index]["w"] / 2.0
        )
        if abs(expected_center - seams[seam_index]) > PARTITION_POSITION_TOL:
            errors.append({
                "reason": "layout_opening_boundary_mismatch",
                **binding,
            })

        if index < len(nodes):
            bbox = nodes[index]["bbox"]
            actual_center = float(bbox["x"]) + float(bbox["w"]) / 2.0
            if abs(actual_center - seams[seam_index]) > PARTITION_POSITION_TOL:
                errors.append({
                    "reason": "opening_boundary_offset",
                    "uid": nodes[index].get("uid"),
                    "expected": seams[seam_index],
                    "actual": actual_center,
                    **binding,
                })

    return {
        "passed": not errors,
        "status": "passed" if not errors else "failed",
        "door_count": layout["door_count"],
        "door_width": layout["door_width"],
        "seam_centers": list(seams),
        "partition_centers": [
            float(node["bbox"]["x"]) + float(node["bbox"]["w"]) / 2.0
            for node in nodes
        ],
        "boundary_bindings": list(layout["boundary_bindings"]),
        "errors": errors,
    }


def self_check(spec, assembly, graph, layout=None, warnings=None):
    spaces = assembly["spaces"]
    layout_feasibility = None
    alignment_result = None
    if layout is not None:
        layout_feasibility = {
            "status": layout.get("status"),
            "passed": bool(layout.get("can_generate", False)),
            "errors": layout.get("errors", []),
            "missing_parameters": layout.get("missing_parameters", []),
        }
        alignment_result = _check_facade_partition_alignment(
            spec, layout, graph
        )
    nodes = graph["nodes"]
    edges = graph["edges"]
    space_map = {space["id"]: space for space in spaces}
    node_map = {node["uid"]: node for node in nodes}

    all_ids = [space["id"] for space in spaces]
    all_ids += [use["id"] for use in assembly["operators"]]
    all_ids += [node["uid"] for node in nodes]
    unique_ok = len(all_ids) == len(set(all_ids))

    parent_errors = []
    for space in spaces:
        parent_id = space.get("parent_id")
        if parent_id is None:
            continue
        parent = space_map.get(parent_id)
        if parent is None or not _inside(space["bbox"], parent["bbox"]):
            parent_errors.append(space["id"])

    operator_ids = {use["operator_id"] for use in assembly["operators"]}
    operator_ids.update(
        {
            "plinth.closed.v1",
            "partition.vertical.v1",
            "shelf.layout.v1",
        }
    )
    ownership_errors = [
        node["uid"]
        for node in nodes
        if node["space_id"] not in space_map
        or node["operator_id"] not in operator_ids
    ]

    cabinet_box = space_map["cabinet"]["bbox"]
    boundary_errors = [
        node["uid"]
        for node in nodes
        if not _inside(node["bbox"], cabinet_box)
    ]

    connection_errors = []
    declared_pairs = set()
    for edge in edges:
        if edge["a"] not in node_map or edge["b"] not in node_map:
            connection_errors.append((edge["a"], edge["b"]))
        declared_pairs.add(frozenset((edge["a"], edge["b"])))

    duplicate_edges = []
    edge_keys = set()
    for edge in edges:
        key = (
            min(edge["a"], edge["b"]),
            max(edge["a"], edge["b"]),
            edge["type"],
        )
        if key in edge_keys:
            duplicate_edges.append(key)
        edge_keys.add(key)

    undeclared = []
    for index, left in enumerate(nodes):
        for right in nodes[index + 1 :]:
            volume = _overlap_volume(left["bbox"], right["bbox"])
            if volume <= OVERLAP_VOLUME_EPS:
                continue
            pair = frozenset((left["uid"], right["uid"]))
            if pair not in declared_pairs:
                undeclared.append(
                    {
                        "a": left["uid"],
                        "b": right["uid"],
                        "volume_mm3": round(volume, 6),
                    }
                )

    invariants = [
        {
            "name": "I1_unique_ids",
            "passed": unique_ok,
            "details": [] if unique_ok else ["duplicate ID"],
        },
        {
            "name": "I2_spaces_inside_parent",
            "passed": not parent_errors,
            "details": parent_errors,
        },
        {
            "name": "I3_node_space_operator_ownership",
            "passed": not ownership_errors,
            "details": ownership_errors,
        },
        {
            "name": "I4_nodes_inside_cabinet",
            "passed": not boundary_errors,
            "details": boundary_errors,
        },
        {
            "name": "I5_connection_references_and_uniqueness",
            "passed": not connection_errors and not duplicate_edges,
            "details": {
                "missing_references": connection_errors,
                "duplicate_edges": duplicate_edges,
            },
        },
        {
            "name": "I6_no_undeclared_aabb_overlap",
            "passed": not undeclared,
            "details": undeclared,
        },
    ]

    panel_nodes = [
        node for node in nodes if node.get("material", "panel") != "metal"
    ]
    rod_nodes = [node for node in nodes if node["role"] == "hang_rod"]
    role_counts = {}
    for node in nodes:
        role_counts[node["role"]] = role_counts.get(node["role"], 0) + 1

    return {
        "passed": all(item["passed"] for item in invariants),
        "board_count": len(panel_nodes),
        "node_count": len(nodes),
        "edge_count": len(edges),
        "rod_count": len(rod_nodes),
        "space_count": len(spaces),
        "operator_use_count": len(assembly["operators"]),
        "undeclared_aabb_overlap_count": len(undeclared),
        "role_counts": dict(sorted(role_counts.items())),
        "invariants": invariants,
        "layout_feasibility": layout_feasibility,
        "facade_partition_alignment": alignment_result,
    }


def solve_to_directory(spec, output_directory):
    result = solve(spec)
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)

    semantic_path = output / "semantic.json"
    graph_path = output / "board_graph.json"
    report_path = output / "self_check.json"
    spec_path = output / "cabinet_spec.json"

    dump_json(result["semantic_assembly"], semantic_path)
    dump_json(result["board_graph"], graph_path)
    dump_json(result["self_check"], report_path)
    dump_json(result["spec"], spec_path)

    validate_semantic(result["semantic_assembly"])
    validate_board_graph(result["board_graph"])

    return {
        "semantic_path": str(semantic_path),
        "board_graph_path": str(graph_path),
        "report_path": str(report_path),
        "spec_path": str(spec_path),
        **result,
    }


def _main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("spec")
    parser.add_argument(
        "--output",
        default="/home/ming/unitcab/out/semantic",
    )
    args = parser.parse_args()

    with Path(args.spec).open("r", encoding="utf-8") as stream:
        spec = json.load(stream)

    result = solve_to_directory(spec, args.output)
    print(json.dumps(result["self_check"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _main()

