"""Phase G regression guards: docs must match verified implementation."""
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(name):
    with open(os.path.join(PROJECT_ROOT, name), "r", encoding="utf-8") as handle:
        return handle.read()


def test_G_status_table_has_all_five_categories():
    doc = _read(os.path.join("docs", "VERIFICATION.md"))
    for category in ("IMPLEMENTED", "AUTOMATED TEST PASSED", "MANUALLY VERIFIED",
                     "NOT VERIFIED", "BLOCKED"):
        assert category in doc


def test_G_test_count_matches_final_suite_and_no_stale_claims():
    doc = _read(os.path.join("docs", "VERIFICATION.md"))
    assert "64 passed" in doc
    readme = _read("README.md")
    assert "currently 9 tests" not in readme
    assert "All 5 tests must pass" not in readme
    changelog = _read("CHANGELOG.md")
    assert "NOT VERIFIED" in changelog
    assert "ID-switch rate" in _read(os.path.join("docs", "ACCURACY_LIMITATIONS.md"))


def test_G_architecture_claim_corrections_exist():
    doc = _read(os.path.join("docs", "ARCHITECTURE.md"))
    assert "Phase G corrections" in doc
    assert "priority queue" in doc


def test_G_issue_register_has_no_open_rows():
    reg = _read(os.path.join("docs", "ISSUE_REGISTER.md"))
    rows = [line for line in reg.splitlines() if line.startswith("| I-")]
    assert rows
    for row in rows:
        assert "| OPEN |" not in row, row
