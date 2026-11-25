"""Phase E regression guards for the measurement harness and its honesty."""
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(name):
    with open(os.path.join(PROJECT_ROOT, name), "r", encoding="utf-8") as handle:
        return handle.read()


def test_E_harness_measures_real_process_tree_and_packets():
    src = _read(os.path.join("tools", "benchmark_multicam.py"))
    assert "children(recursive=True)" in src, "must include Python child process"
    assert "cpu_times()" in src, "CPU must be measured, not assumed"
    assert "websocket" in src.lower() and "config" in src
    assert "_mjpeg_fps" in src and "--frame" in src


def test_E_performance_doc_reports_actual_hardware_and_measurements():
    doc = _read(os.path.join("docs", "PERFORMANCE.md"))
    assert "AMD Ryzen 7 7435HS" in doc
    assert "24,261 MB" in doc
    for count in (1, 2, 4, 6):
        assert f"| {count} |" in doc
    assert "NOT measured" in doc
    assert "not estimated" in doc


def test_E_no_unmeasured_speed_claim():
    doc = _read(os.path.join("docs", "PERFORMANCE.md"))
    assert "22.5" in doc and "~22.5 fps" in doc
    assert "25 fps sleep target" in doc
