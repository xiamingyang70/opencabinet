import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from opencabinet.spec_builder import solve_simple


def test_three_bay_seam_aligned():
    graph, check = solve_simple(1800, 2200, bays=3, bay_functions=["fold"] * 3)
    assert check["passed"]
    assert check["board_count"] == len(graph["nodes"])


def test_two_bay_reference_matches_example():
    graph, check = solve_simple(1200, 2400, bays=2, bay_functions=["hang", "fold"])
    assert check["passed"]
    assert check["board_count"] == 19
