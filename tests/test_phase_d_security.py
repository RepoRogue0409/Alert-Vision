"""Phase D security + launcher regression tests (no browser/network needed)."""
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import alert_vision  # noqa: E402
import serve_static  # noqa: E402


def test_D2_safe_path_rejects_sibling_directory(tmp_path):
    root = tmp_path / "root"
    sibling = tmp_path / "root_evil"
    root.mkdir()
    sibling.mkdir()
    (sibling / "secret.txt").write_text("secret", encoding="utf-8")
    original = serve_static.ROOT
    serve_static.ROOT = str(root)
    try:
        safe_path = serve_static.WhitelistHandler._safe_path
        assert safe_path(None, "/../root_evil/secret.txt") is None, \
            "sibling directory with the ROOT prefix must be rejected"
        assert safe_path(None, "/index.html") is None  # file does not exist
    finally:
        serve_static.ROOT = original


def test_D3_origin_accepts_https_and_bare_host(monkeypatch):
    backend = alert_vision.AlertVisionBackend([], stride=1)
    assert backend._origin_allowed(None) is True  # non-browser probes
    assert backend._origin_allowed("http://127.0.0.1:8000") is True
    monkeypatch.setenv("ALERT_VISION_ALLOWED_ORIGINS", "https://example.invalid, localhost:9999")
    assert backend._origin_allowed("https://example.invalid") is True
    assert backend._origin_allowed("http://localhost:9999") is True
    assert backend._origin_allowed("https://evil.invalid") is False


def test_D5_rate_limit_sends_config_error():
    with open(os.path.join(PROJECT_ROOT, "alert_vision.py"), "r", encoding="utf-8") as handle:
        src = handle.read()
    block = src[src.index("if msg_count > MAX_WS_MESSAGES_PER_SEC:"):
                src.index("try:\n                        payload = json.loads(text)")]
    assert '"type": "config_error"' in block
    assert "message rate limit exceeded" in block


def test_D1_launcher_runs_helper_in_background_and_cleans_by_pid():
    with open(os.path.join(PROJECT_ROOT, "start.bat"), "r", encoding="utf-8") as handle:
        src = handle.read()
    assert 'taskkill /f /im powershell.exe' not in src
    assert 'taskkill /f /im python.exe' not in src
    assert 'start "" /b powershell' in src, "browser helper must NOT block backend boot"
    assert 'taskkill /f /pid !HELPER_PID!' in src
    assert 'taskkill /f /pid !STATIC_PID!' in src
    assert ':: Waiting for backend, browser will open automatically' in src
    assert 'if errorlevel 1 (' in src and 'IMPORT_CHECK_FAILED' in src
    assert 'PIP_INSTALL_FAILED' in src
    assert 'Start-Process "http://127.0.0.1:8000/"' in src
    assert 'echo   try { >> "%PS_SCRIPT%"' in src, "health probe must suppress timeout errors"
    assert '-lt 90' in src and '-Seconds 1' in src
    assert 'Write-Host ":: TIMEOUT opening browser.' in src
    # no duplicated step label
    assert src.count('echo :: [6/7]') == 1
    assert src.count('echo :: [7/7]') == 1


def test_D4_localhost_only_is_documented_as_unauthenticated():
    with open(os.path.join(PROJECT_ROOT, "alert_vision.py"), "r", encoding="utf-8") as handle:
        src = handle.read()
    assert 'HOST = "127.0.0.1"' in src
    docs_path = os.path.join(PROJECT_ROOT, "docs", "SECURITY.md")
    if os.path.exists(docs_path):
        with open(docs_path, "r", encoding="utf-8") as handle:
            docs = handle.read()
        assert "unauthenticated" in docs.lower()
