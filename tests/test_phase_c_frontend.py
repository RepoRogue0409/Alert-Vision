"""Phase C frontend regression guards.

Node is not installed on this machine, so these tests assert the *source
contract* of script.js/index.html. Live browser QA is recorded separately in
docs/VERIFICATION.md; these guards prevent silent regressions of the fixes.
"""
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(name):
    with open(os.path.join(PROJECT_ROOT, name), "r", encoding="utf-8") as handle:
        return handle.read()


JS = _read("script.js")
HTML = _read("index.html")


def _between(source, start, end):
    a = source.index(start)
    b = source.index(end, a)
    return source[a:b]


def test_C1_lower_priority_alerts_are_queued_not_overwritten():
    assert "function queueAlert" in JS
    assert "alertQueue.push" in JS
    assert "function nextQueuedAlert" in JS
    show = _between(JS, "function showAlarm(", "function hideAlarm(")
    assert "alertQueue.push(previous)" in show, "displaced alert must be preserved"
    hide = _between(JS, "function hideAlarm(", "function renderAdvice(")
    assert "nextQueuedAlert()" in hide, "next queued alert must surface after clear"


def test_C2_alert_comes_from_alert_packet_not_10hz_telemetry():
    handle_packet = _between(JS, "function handlePacket(", "function handleTelemetry(")
    assert 'packet.type === "alert"' in handle_packet
    assert "queueAlert(packet)" in handle_packet
    telemetry = _between(JS, "function handleTelemetry(", "function adoptBackendCameras(")
    assert "showAlarm(cam)" not in telemetry, "telemetry must not re-trigger the alarm"
    show = _between(JS, "function showAlarm(", "function hideAlarm(")
    assert "duplicate" in show.lower() and "alarmState.alertId" in show


def test_C3_viewports_are_synced_incrementally():
    render = _between(JS, "function renderViewports()", "function selectCamera(")
    assert "clearViewTimers()" not in render
    assert 'innerHTML = ""' not in render
    assert "createViewportTile" in render
    assert "restartStream(cam.id, refs)" in render, "only changed sources re-attach"


def test_C4_uncalibrated_density_is_not_shown_as_zero():
    focus = _between(JS, "function updateFocus(", "/* -- A2:")
    assert "not calibrated" in focus
    assert "density_calibrated === false" in focus


def test_C5_csv_is_quoted_and_formula_safe():
    assert "function csvCell" in JS
    helper = _between(JS, "function csvCell", "function bindLedgerDownload")
    assert "replace(/\"/g, '\"\"')" in helper
    assert "test(text)" in helper
    download = _between(JS, "function bindLedgerDownload", "function bindControls")
    assert "cells.map(csvCell)" in download
    assert "header.map(csvCell)" in download


def test_C6_ledger_counter_increments():
    assert "ledgerEventCount += 1" in JS
    assert "ledgerEventCount + \" events\"" in JS


def test_C7_C8_adopt_does_not_drop_packet_and_is_incremental():
    telemetry = _between(JS, "function handleTelemetry(", "function adoptBackendCameras(")
    adopt_block = telemetry[telemetry.index("adoptBackendCameras(packet.cameras)"):]
    assert "return;" not in adopt_block.split("packet.cameras.forEach")[0], \
        "adopt must not return before updating this packet's tiles"


def test_C9_unknown_level_is_not_displayed_as_safe():
    assert "UNKNOWN: -1" in JS
    normalize = _between(JS, "function normalizeLevel", "function cameraById")
    assert 'upper === "UNKNOWN"' in normalize
    focus = _between(JS, "function updateFocus(", "/* -- A2:")
    assert 'level === "UNKNOWN"' in focus
    assert "ui.risk.textContent = \"UNKNOWN\"" in focus
    marker = "ui.headcount.textContent = " + '"' + chr(92) + "u2014" + '"'
    assert marker in focus


def test_frontend_ack_is_browser_receipt_not_queue_acceptance():
    show = _between(JS, "function showAlarm(", "function hideAlarm(")
    assert 'type: "alert_ack"' in show
    handle = _between(JS, "function handlePacket(", "function handleTelemetry(")
    assert 'type: "alert_ack"' not in handle, "ack must wait until the alert is displayed"


def test_eta_label_says_estimate():
    assert "Linear ETA (estimate)" in HTML
    assert '"~" + text + " (est)"' in JS
