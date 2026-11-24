"""Smoke test for analytics.py — imports the real module and exercises core logic.

This test must `from analytics import ...` the real module (not copy-reimplemented logic).
It uses synthetic data to verify:
  - compute_flow_signal returns valid values (2-tuple)
  - LiveAnalyzer level_for maps density to correct risk level
  - Density forced levels: 4/m² HIGH, 6/m² CRITICAL
  - combine_levels never downgrades when combined
"""
import sys
import os

# Ensure the project root is on the path so we import the real analytics module
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from analytics import (
    LiveAnalyzer,
    compute_flow_signal,
    level_for,
    density_forced_level,
    combine_levels,
)


def test_compute_flow_signal_returns_tuple():
    """compute_flow_signal should return a 2-tuple (turbulence, divergence)."""
    import numpy as np

    # Create two consecutive synthetic frames
    frame_prev = np.zeros((100, 100), dtype=np.uint8)
    frame_gray = np.zeros((100, 100), dtype=np.uint8)
    result = compute_flow_signal(frame_prev, frame_gray)
    assert isinstance(result, tuple), f"Expected tuple, got {type(result)}"
    # compute_flow_signal returns (turbulence, divergence) — 2 values
    assert len(result) == 2, f"Expected 2 elements, got {len(result)}: {result}"
    turbulence, divergence = result
    assert isinstance(turbulence, float), f"turbulence should be float, got {type(turbulence)}"
    assert isinstance(divergence, float), f"divergence should be float, got {type(divergence)}"
    # Values should be finite and in [0,1] family
    assert np.isfinite(turbulence), f"turbulence should be finite, got {turbulence}"
    assert np.isfinite(divergence), f"divergence should be finite, got {divergence}"
    print("  PASS: compute_flow_signal returns valid 2-tuple")


def test_level_for_formula():
    """level_for implements: CRITICAL if risk >= critical_at, HIGH if risk >= threshold, MODERATE if risk >= threshold*0.6, else SAFE."""
    critical_at = 0.85
    threshold = 0.55

    # Below all thresholds → SAFE
    assert level_for(0.0, threshold, critical_at) == "SAFE", \
        f"level_for(0.0, {threshold}, {critical_at}) should be SAFE"

    # Below threshold but above tail → MODERATE
    assert level_for(0.4, threshold, critical_at) == "MODERATE", \
        f"level_for(0.4, {threshold}, {critical_at}) should be MODERATE"

    # At or above threshold → HIGH
    assert level_for(0.6, threshold, critical_at) == "HIGH", \
        f"level_for(0.6, {threshold}, {critical_at}) should be HIGH"

    # At or above critical → CRITICAL
    assert level_for(0.9, threshold, critical_at) == "CRITICAL", \
        f"level_for(0.9, {threshold}, {critical_at}) should be CRITICAL"

    print("  PASS: level_for formula mapping")


def test_density_forced_level():
    """density_forced_level should return HIGH at 4/m² and CRITICAL at 6/m²."""
    # 4 people per m² → HIGH
    result_4 = density_forced_level(4, 1.0)  # 4 people in 1 m² = density 4/m²
    assert result_4 == "HIGH", f"density_forced_level(4, 1.0) should be HIGH, got {result_4}"
    # 6 people per m² → CRITICAL
    result_6 = density_forced_level(6, 1.0)  # 6 people in 1 m² = density 6/m²
    assert result_6 == "CRITICAL", f"density_forced_level(6, 1.0) should be CRITICAL, got {result_6}"
    # Fewer people should not trigger forced critical
    result_2 = density_forced_level(2, 1.0)  # 2 people in 1 m² = density 2/m²
    assert result_2 != "CRITICAL", f"density_forced_level(2, 1.0) should not be CRITICAL, got {result_2}"
    print("  PASS: density forced level thresholds")


def test_live_analyzer_initialization():
    """LiveAnalyzer should initialize without error."""
    analyzer = LiveAnalyzer()
    assert analyzer is not None, "LiveAnalyzer instantiation failed"
    # last_terms should be a dict (may be empty initially)
    last_terms = getattr(analyzer, "last_terms", {})
    assert isinstance(last_terms, dict), f"last_terms should be dict, got {type(last_terms)}"
    print("  PASS: LiveAnalyzer initialization")


def test_combine_levels_never_downgrades():
    """combine_levels should never downgrade - take the more severe level."""
    # HIGH combined with SAFE should remain HIGH
    result = combine_levels("HIGH", "SAFE")
    assert result == "HIGH", f"combine_levels('HIGH', 'SAFE') should be HIGH, got {result}"

    # MODERATE combined with HIGH should be HIGH
    result = combine_levels("MODERATE", "HIGH")
    assert result == "HIGH", f"combine_levels('MODERATE', 'HIGH') should be HIGH, got {result}"

    # CRITICAL combined with anything should be CRITICAL
    result = combine_levels("CRITICAL", "SAFE")
    assert result == "CRITICAL", f"combine_levels('CRITICAL', 'SAFE') should be CRITICAL, got {result}"

    # SAFE combined with SAFE should stay SAFE
    result = combine_levels("SAFE", "SAFE")
    assert result == "SAFE", f"combine_levels('SAFE', 'SAFE') should be SAFE, got {result}"

    # Density level should override formula level when more severe
    result = combine_levels("SAFE", "HIGH")
    assert result == "HIGH", f"combine_levels('SAFE', 'HIGH') should be HIGH, got {result}"

    print("  PASS: combine_levels never downgrades")


if __name__ == "__main__":
    print("Running analytics.py smoke tests...")
    test_compute_flow_signal_returns_tuple()
    test_level_for_formula()
    test_density_forced_level()
    test_live_analyzer_initialization()
    test_combine_levels_never_downgrades()
    print("\nAll smoke tests PASSED.")