# -*- coding: utf-8 -*-
"""OpenCabinetEnginePanel — 简化参数 -> opencabinet引擎 -> 实体板件

用户只填: 宽/高/深/格数/每格功能。列宽门缝对齐由spec_builder自动解算。
生成失败时弹门禁报告(哪条规则没过)。
FreeCAD 1.1 exec()限制: 方法体内重新import, 类体只用字符串字面量。
"""
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import FreeCAD
import FreeCADGui
from PySide import QtGui, QtCore


class OpenCabinetEnginePanel:
    def __init__(self):
        self.form = QtGui.QWidget()
        self.form.setWindowTitle("OpenCabinet Engine - Wardrobe")
        self._build_ui()

    def _build_ui(self):
        lay = QtGui.QFormLayout(self.form)
        self.w_width = QtGui.QSpinBox()
        self.w_width.setRange(300, 6000)
        self.w_width.setValue(1200)
        self.w_width.setSuffix(" mm")
        lay.addRow("Width", self.w_width)

        self.w_height = QtGui.QSpinBox()
        self.w_height.setRange(300, 3200)
        self.w_height.setValue(2400)
        self.w_height.setSuffix(" mm")
        lay.addRow("Height", self.w_height)

        self.w_depth = QtGui.QSpinBox()
        self.w_depth.setRange(150, 1200)
        self.w_depth.setValue(580)
        self.w_depth.setSuffix(" mm")
        lay.addRow("Depth", self.w_depth)

        self.w_bays = QtGui.QSpinBox()
        self.w_bays.setRange(1, 6)
        self.w_bays.setValue(2)
        lay.addRow("Bays", self.w_bays)

        # 每格功能行(动态)
        self._bay_rows = []
        self._bay_box = QtGui.QGroupBox("Bay functions")
        self._bay_lay = QtGui.QFormLayout(self._bay_box)
        lay.addRow(self._bay_box)
        self._rebuild_bay_rows()

        self.w_bays.valueChanged.connect(self._rebuild_bay_rows)

        self.w_info = QtGui.QLabel("Seam-aligned column widths are solved automatically.")
        self.w_info.setWordWrap(True)
        lay.addRow(self.w_info)

        self.btn = QtGui.QPushButton("Generate")
        self.btn.clicked.connect(self.on_generate)
        lay.addRow(self.btn)

    def _rebuild_bay_rows(self):
        while self._bay_lay.count():
            item = self._bay_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._bay_rows = []
        opts = ["hang", "fold (shelf top)", "fold (open)"]
        for i in range(self.w_bays.value()):
            combo = QtGui.QComboBox()
            combo.addItems(opts)
            combo.setCurrentIndex(0 if i == 0 else 1)
            self._bay_lay.addRow("Bay %d" % i, combo)
            self._bay_rows.append(combo)

    def _functions(self):
        out = []
        for combo in self._bay_rows:
            txt = combo.currentText()
            if txt.startswith("hang"):
                out.append("hang")
            else:
                out.append("fold")
        return out

    def on_generate(self):
        from opencabinet.spec_builder import solve_simple
        from opencabinet.oc_build import build_boards
        try:
            graph, check = solve_simple(
                float(self.w_width.value()),
                float(self.w_height.value()),
                depth=float(self.w_depth.value()),
                bays=int(self.w_bays.value()),
                bay_functions=self._functions())
        except Exception as exc:
            QtGui.QMessageBox.critical(self.form, "Spec error", str(exc))
            return
        if not check.get("passed"):
            feas = check.get("layout_feasibility") or {}
            errs = feas.get("errors") or []
            report = "\n".join("%s: %s" % (e.get("code"), e.get("message")) for e in errs) or "unknown gate failure"
            QtGui.QMessageBox.warning(self.form, "Gate rejected", report)
            return
        doc = FreeCAD.ActiveDocument or FreeCAD.newDocument("OpenCabinet")
        used = set(o.Name for o in doc.Objects)
        objects, errors = build_boards(doc, graph, used_names=used)
        doc.recompute()
        FreeCADGui.ActiveDocument.ActiveView.viewIsometric()
        FreeCADGui.SendMsgToActiveView("ViewFit")
        cuts_failed = len(errors)
        QtGui.QMessageBox.information(
            self.form, "Generated",
            "%d boards created. Gate: PASS. Cut errors: %d"
            % (len(objects), cuts_failed))

    def accept(self):
        FreeCADGui.Control.closeDialog()

    def reject(self):
        FreeCADGui.Control.closeDialog()


def open_panel():
    FreeCADGui.Control.showDialog(OpenCabinetEnginePanel())
