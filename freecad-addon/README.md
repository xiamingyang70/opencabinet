# OpenCabinetEngine — FreeCAD Addon

Verified wardrobe generation inside FreeCAD, backed by the
[opencabinet](https://github.com/xiamingyang70/opencabinet) deterministic solver.

## Install

Copy the `OpenCabinetEngine/` folder into your FreeCAD user modules directory:

- Windows: `%APPDATA%/FreeCAD/Mod/`
- Linux: `~/.local/share/FreeCAD/Mod/`
- macOS: `~/Library/Application Support/FreeCAD/Mod/`

Restart FreeCAD.

## Use

1. Switch to the **OpenCabinet Engine** workbench (or any workbench — the toolbar follows).
2. Click **Generate Wardrobe**.
3. Fill in width / height / depth / bay count / per-bay function (hang or fold).
4. Click **Generate**.

Column widths are solved automatically so every door seam lands exactly on its
partition center — the part users get wrong when hand-writing specs. If a gate
check fails you get the rule report, not a broken model.

## Layout of this folder

- `Init.py` / `InitGui.py` — FreeCAD workbench bootstrap
- `oc_panel.py` — task panel (simplified inputs)
- `src/opencabinet/` — vendored opencabinet package (solver, groover, spec_builder, oc_build)
