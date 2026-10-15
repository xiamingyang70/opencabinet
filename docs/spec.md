# Spec reference (wardrobe)

A spec is a single JSON file. The solver is deterministic: the same spec always
produces the same board graph. Anything ambiguous or contradictory is **rejected**
with a measurable reason — the solver never guesses.

## Top-level fields

| Field | Type | Required | Rules |
|---|---|---|---|
| `id` | string | yes | ASCII identifier, e.g. `wardrobe_1200` |
| `cabinet_type` | string | yes | `wardrobe` |
| `width` | number | yes | total exterior width, mm |
| `depth` | number | yes | 350–600 recommended; 580 for reach-in wardrobes |
| `height` | number | yes | ≤ 2800 |
| `board_thickness` | number | yes | 18 |
| `gap` | number | no | door gap, default 2.5 |
| `plinth_height` | number | no | default 60 |
| `back` | string | no | `grooved_10` (default) or `none` |
| `column_count` | int | yes | number of bays |
| `column_widths` | number[] | yes | clear width per bay, left to right |
| `functions` | string[] | yes | subset of `hang`, `fold`, `drawer`, `door` |
| `style` | object | yes | see below |
| `facade_openings` | list | yes* | see below (*required when doors exist) |

## Hard constraints (violations are rejected, not corrected)

1. **Exact width closure** — `sum(column_widths) + board_thickness × (bays + 1) == width`
   to the last decimal. Distribute the rounding remainder into the last bay.
2. **Openings cover bays once** — each bay index appears in exactly one opening,
   openings are consecutive and ordered left-to-right.
3. **Opening boundaries align with facade seams** — an opening spanning several bays
   may have internal seams that ignore bay boundaries, but its outer boundaries must
   match the equal-width facade generator's seam positions.
4. **Hanging clearance** — a bay with hanging needs clear height ≥ 900 mm (short
   garments) or ≥ 1400 mm (long garments).
5. **Drawers at the bottom** of their bay; drawer face heights come from the
   147/187 stack system.

## `style`

```json
{
  "doors":  {"mode": "full_height", "gap": 2.5},
  "hang_columns": [0],
  "fold":   {"column": 1, "top": true},
  "drawer": {"count": 2, "column": 2, "face_heights": [147, 187]}
}
```

- `hang_columns`: which bay indices get a hanging rod (needs `functions: ["hang"]`).
- `fold.column`: which bay gets folding shelves; `top: true` reserves its upper
  zone for luggage/blankets.
- Door count per opening comes from `facade_openings[].door_count`.

## What you get back

`opencabinet solve spec.json --output out/` writes:

- `board_graph.json` — every board with exact bbox `{x,y,z,w,d,h}` plus groove cuts
- `semantic.json` — spaces/operators/relations (the derivation record)
- `self_check.json` — the gate report: `passed`, `board_count`, layout errors

`opencabinet export out/board_graph.json --output out/fc/` then produces
`UnitCab.FCStd` (grooves cut into the solids) and `board_bom.csv`.

`opencabinet verify` re-checks the produced solid model: exact-solid interference
(cut-exempted), board count, and extent closure. The FCStd ships only if `pass: true`.
