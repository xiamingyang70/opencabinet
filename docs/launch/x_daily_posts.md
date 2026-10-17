# X daily post bank — opencabinet promotion (each ≤280 chars, verified)
# Rotate: feature / technical detail / use case / progress update

1 |
OpenCabinet refuses to give you a 3D wardrobe model until it PROVES it buildable: no panel interference (solid booleans), dimension chains exact, real back-panel grooves.

MIT, free: https://github.com/xiamingyang70/opencabinet

#FreeCAD #Woodworking

2 |
Technical detail: a groove cut doesn't change a board's BoundBox, so bbox-based interference checks false-alarm on every grooved assembly. OpenCabinet's verifier uses exact Part.common() volumes with groove exemptions.

#FreeCAD

3 |
Describing a wardrobe in OpenCabinet = ~10 lines of JSON: bays, shelves, hanging rods, drawers, doors. The solver outputs 19 boards with every dimension exact — or tells you exactly which rule your spec broke.

https://github.com/xiamingyang70/opencabinet

4 |
Why deterministic? Chat AI is great at filling forms and terrible at arithmetic. OpenCabinet's MCP server lets any chat app design a wardrobe — but every dimension comes from the engine, never from the model.

https://github.com/xiamingyang70/opencabinet

5 |
Dimension chains must close EXACTLY: sum(bay widths) + thickness*(bays+1) == total width, to the last decimal. 0.1mm of rounding drift gets rejected. Furniture CAD shouldn't ship "approximately correct".

https://github.com/xiamingyang70/opencabinet

6 |
OpenCabinet pipeline: JSON spec → solver → board graph → FreeCAD FCStd + cut list → verification that re-opens the file and re-checks it with solid booleans. Fail any check = no model, error message instead.

https://github.com/xiamingyang70/opencabinet

7 |
Wardrobe family only, for now. One cabinet type done properly beats eight done approximately. Roadmap: sideboards → shoe cabinets → kitchen base cabinets → sheet nesting.

What should verification cover next? Hardware? Hinge positions?

https://github.com/xiamingyang70/opencabinet

8 |
The verification report is the product: pass/fail per check, interference volumes, extent closure, groove coverage. You can audit every number the engine claims.

https://github.com/xiamingyang70/opencabinet

9 |
FreeCAD does the heavy lifting: freecadcmd headless export, grooves cut as real boolean ops into partitions/shelves/carcase, then the verifier re-opens the FCStd and checks the actual solids.

https://github.com/xiamingyang70/opencabinet

10 |
Day 2 of shipping in public: 0 stars, full pipeline working. solve → export → verify, all green on the reference wardrobe. Building in public means the numbers stay honest.

https://github.com/xiamingyang70/opencabinet
