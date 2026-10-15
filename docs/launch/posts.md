# Show HN post draft

## Title

Show HN: A wardrobe generator that refuses to emit structurally wrong models

## Text

I work in custom furniture manufacturing (China, wardrobe/cabinet production). The #1 problem with AI furniture generators isn't the looks — it's that the models are structurally wrong in ways nobody notices until the shop tries to build them: panels overlapping, back panels that can't physically fit, dimension chains that don't close by a few millimeters.

So I built OpenCabinet: a parametric wardrobe generator where structural correctness is a **gate, not a hope**.

How it works:

    spec.json → deterministic solver → board graph → FreeCAD FCStd + cut list → verification

The verification step is the product:

- Interference is checked with **exact solid boolean volumes** (FreeCAD `common()`), not bounding boxes. This matters: a board with a back-panel groove keeps its full bounding box after the cut, so AABB tests false-positive on every grooved assembly.
- Overlap regions fully covered by declared groove cuts are design intent (a back panel passing *through* a partition) and are exempted explicitly.
- Bay widths must close exactly: `sum(widths) + thickness*(bays+1) == width`, to the last decimal. Rounding drift of 0.1mm gets rejected.
- Door seams must agree with the facade generator's positions.
- Ambiguous specs are rejected with measurable reasons, never guessed.

Example spec (~10 lines of JSON) → 19 boards, 0 interference, dimension chain [0,1200]×[2,580]×[0,2400] exact, back-panel grooves cut into the actual solids.

It also ships an MCP server, so any MCP-capable chat app can generate wardrobes conversationally — but the chat model only ever *fills the spec*; every number comes from the deterministic engine.

Repo: https://github.com/xiamingyang70/opencabinet

Early and intentionally narrow: wardrobes only. One cabinet family done properly beats eight done approximately. Roadmap: sideboards → shoe cabinets → kitchen base cabinets, then sheet nesting.

Would love feedback on the spec format and on what "verified" should cover next (hardware? real hinge/part numbers?).

---

# Reddit r/woodworking post draft

## Title

I built a free tool that designs wardrobes you can actually build — it refuses to give you a model with hidden errors

## Body

Background: I work in custom furniture. The biggest problem with "AI designs your furniture" tools is that the results *look* fine but are structurally broken — panels that overlap, back panels that can't fit, parts that don't add up to the cabinet size. You only find out when you're holding a saw.

So I made an open-source tool that treats structural correctness as a hard gate. It generates the 3D model (FreeCAD file) + cut list, but only after it proves:

- **no two boards interfere** — checked with exact solid geometry, not the lazy bounding-box method that false-alarms on every board with a groove
- **the dimension chain closes exactly** — every part's position adds up to the cabinet's outside size, to the last decimal
- **the back panel actually fits** — its grooves are cut into the real solid model, and the volume removed is cross-checked against the math

If a check fails, the tool refuses to give you the model. You get the error instead.

It's parametric: you describe bays, hanging, shelves, drawers in a small JSON file. There's also a plugin so chat AI can fill the form for you — but the AI never computes a single dimension; it only picks options.

Free / MIT / no signup: https://github.com/xiamingyang70/opencabinet

Honest limitations: wardrobes only right now, English docs, needs FreeCAD installed for the 3D export. Cut-list nesting (fitting parts onto sheet goods) is next.

What would you want it to check next? Hinge positions? Real hardware part numbers?

---

# r/FreeCAD post draft

## Title

OpenCabinet: parametric wardrobe generator that outputs verified FreeCAD models (grooves actually cut into the solids)

## Body

I built an open-source parametric wardrobe generator on top of FreeCAD's Python API and wanted to share it here since FreeCAD does the heavy lifting.

Pipeline: JSON spec → pure-Python solver (board graph with exact bboxes) → FreeCAD export via freecadcmd (back-panel grooves cut as real boolean operations into partitions/shelves/carcase) → verification pass that re-opens the FCStd and checks it with exact solid `Part.common()` interference tests (groove-cut-exempted) and extent closure.

One detail that cost me some pain: after you cut a groove into a board, its BoundBox doesn't change — so any interference check based on bounding boxes will flag every grooved assembly as colliding. The verifier uses exact common() volumes and exempts regions covered by declared groove cuts instead.

MIT licensed: https://github.com/xiamingyang70/opencabinet — wardrobe family only for now (sideboards/shoe cabinets on the roadmap). Feedback on the spec schema welcome, especially from anyone doing CNC/cabinet work.
