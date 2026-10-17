# -*- coding: utf-8 -*-
"""Workbench integration: adds a toolbar button to the CabinetDesign WB (or standalone)."""
import os
import sys

_MODDIR = os.path.dirname(os.path.abspath(__file__))
if _MODDIR not in sys.path:
    sys.path.insert(0, _MODDIR)


class OpenCabinetEngineWB(Workbench):
    MenuText = "OpenCabinet Engine"
    ToolTip = "Verified wardrobe generation (opencabinet solver)"
    Icon = os.path.join(_MODDIR, "icon.svg")

    def Initialize(self):
        import FreeCADGui
        import oc_panel  # noqa: F401  (registers command below)
        FreeCADGui.addCommand("OpenCabinetEngine_Generate", {
            "Label": "Generate Wardrobe",
            "ToolTip": "Simplified inputs -> opencabinet solver -> verified boards",
            "Command": "import oc_panel; oc_panel.open_panel()",
        })
        self.appendToolbar("OpenCabinet", ["OpenCabinetEngine_Generate"])

    def GetClassName(self):
        return "Gui::PythonWorkbench"


FreeCADGui.addWorkbench(OpenCabinetEngineWB)
