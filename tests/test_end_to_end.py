"""End-to-end: solve a reference wardrobe spec, assert the gate report."""
import json
from pathlib import Path

from opencabinet.solver import solve_to_directory

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "wardrobe_1200.json"


def test_reference_wardrobe_passes(tmp_path):
    spec = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    result = solve_to_directory(spec, str(tmp_path))
    report = result["self_check"]
    assert report["passed"], report
    assert report["board_count"] == 19


def test_bay_width_must_sum_exactly(tmp_path):
    spec = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    spec["id"] = "bad_widths"
    spec["column_widths"] = [500.0, 500.0]  # 1000 + 54 != 1200
    result = solve_to_directory(spec, str(tmp_path))
    report = result["self_check"]
    assert not report["passed"]
