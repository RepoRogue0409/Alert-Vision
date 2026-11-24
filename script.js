/* Alert Vision AI - Multi-Camera Control Room Telemetry Engine */

(function () {
  "use strict";

  var WS_URL = "ws://127.0.0.1:8765";
  var VIDEO_BASE = "http://127.0.0.1:8766";
  var STREAM_URL = VIDEO_BASE + "/video";
  var RECONNECT_DELAY_MS = 2000;
  var VIDEO_WATCHDOG_MS = 5000;
  var STREAM_RETRY_BASE_MS = 1000;
  var STREAM_RETRY_MAX_MS = 10000;
  var LINK_STALE_MS = 3000;
  var LINK_LOST_MS = 8000;
  var LINK_TICK_MS = 1000;
  var ALARM_MIN_RANK = 2;
  var CONFIG_FLASH_MS = 4000;
  var CONFIG_DEBOUNCE_MS = 300;
  var CAMERA_DEBOUNCE_MS = 800;
  var CONNECT_TIMEOUT_MS = 10000;
  var MAX_CAMERAS = 6;
  var MAX_LEDGER_ROWS = 100;
  var STORAGE_KEY = "alertVision.cameraConfig.v1";

  var ui = {
    linkStatus: document.getElementById("ui-link-status"),
    focusLabel: document.getElementById("ui-focus-label"),
    viewportGrid: document.getElementById("viewport-grid"),
    channelList: document.getElementById("channel-list"),
    addChannel: document.getElementById("add-channel"),
    strideSlider: document.getElementById("stride-slider"),
    strideValue: document.getElementById("stride-val"),
    weightDensity: document.getElementById("weight-density"),
    weightDensityValue: document.getElementById("weight-density-val"),
    weightRate: document.getElementById("weight-rate"),
    weightRateValue: document.getElementById("weight-rate-val"),
    weightConvergence: document.getElementById("weight-convergence"),
    weightConvergenceValue: document.getElementById("weight-convergence-val"),
    weightTurbulence: document.getElementById("weight-turbulence"),
    weightTurbulenceValue: document.getElementById("weight-turbulence-val"),
    thresholdSlider: document.getElementById("threshold-slider"),
    thresholdValue: document.getElementById("threshold-val"),
    headcount: document.getElementById("ui-headcount"),
    risk: document.getElementById("ui-risk"),
    latency: document.getElementById("ui-latency"),
    advice: document.getElementById("ui-advice"),
    ledger: document.getElementById("ui-ledger-box"),
    ledgerCount: document.getElementById("ui-ledger-count"),
    ledgerClear: document.getElementById("ui-ledger-clear"),
    alarm: document.getElementById("ui-alarm"),
    alarmTitle: document.getElementById("ui-alarm-title"),
    alarmMeta: document.getElementById("ui-alarm-meta"),
    alarmAck: document.getElementById("ui-alarm-ack"),
    barDensity: document.getElementById("bar-density"),
    barDensityValue: document.getElementById("bar-density-val"),
    barRate: document.getElementById("bar-rate"),
    barRateValue: document.getElementById("bar-rate-val"),
    barConvergence: document.getElementById("bar-convergence"),
    barConvergenceValue: document.getElementById("bar-convergence-val"),
    barTurbulence: document.getElementById("bar-turbulence"),
    barTurbulenceValue: document.getElementById("bar-turbulence-val"),
    infoDensityPsm: document.getElementById("info-density-psm"),
    infoLevelFormula: document.getElementById("info-level-formula"),
    infoLevelDensity: document.getElementById("info-level-density"),
    infoEta: document.getElementById("info-eta")
  };

  var RISK_COLORS = {
    SAFE: "#10b981",
    MODERATE: "#eab308",
    HIGH: "#f97316",
    CRITICAL: "#ef4444"
  };

  var RISK_RANK = { SAFE: 0, MODERATE: 1, HIGH: 2, CRITICAL: 3 };

  var ACTION_GUIDELINES = {
    SAFE: [
      "\ud83d\udc4d Continue routine monitoring",
      "\ud83d\udc40 Keep the feed on wider screen",
      "\ud83d\udccb Standby \u2014 no action required"
    ],
    MODERATE: [
      "\ud83d\udc41 Increase monitoring frequency",
      "\ud83d\udeb6 Alert staff to watch bottleneck area",
      "\ud83d\udde3 Prepare announcement PA system"
    ],
    HIGH: [
      "\ud83d\udd00 DIVERT the crowd to another area",
      "\ud83d\udce2 Start crowd announcement now",
      "\ud83e\uddd1\u200d\u2708\ufe0f Deploy security personnel on spot"
    ],
    CRITICAL: [
      "\u26d4 IMMEDIATE action in affected area",
      "\ud83d\udce2 Broadcast emergency evacuation warning",
      "\ud83e\uddd1\u200d\ud83d\ude92 Deploy crowd marshals immediately"
    ]
  };

  var ACTION_LABELS = {
    DISPATCH: "DISPATCH MARSHALS",
    ESCALATE: "ESCALATE RESPONSE",
    NONE: "MONITOR ONLY"
  };

  /* Camera-type presets. `path` is the default stream path for each preset. */
  var PRESETS = {
    webcam: { label: "Webcam (this PC)", kind: "webcam", scheme: "http", port: 0, path: "" },
    droidcam: { label: "DroidCam (phone)", kind: "network", scheme: "http", port: 4747, path: "/video" },
    ipwebcam: { label: "IP Webcam (phone)", kind: "network", scheme: "http", port: 8080, path: "/video" },
    mjpeg: { label: "Generic MJPEG", kind: "network", scheme: "http", port: 80, path: "/video" },
    rtsp: { label: "Generic RTSP", kind: "network", scheme: "rtsp", port: 554, path: "/stream1" },
    hikvision: { label: "Hikvision", kind: "network", scheme: "rtsp", port: 554, path: "/Streaming/Channels/101" },
    dahua: { label: "Dahua", kind: "network", scheme: "rtsp", port: 554, path: "/cam/realmonitor?channel=1&subtype=0" },
    tapo: { label: "TP-Link Tapo", kind: "network", scheme: "rtsp", port: 554, path: "/stream1" },
    custom: { label: "Custom URL", kind: "network", scheme: "http", port: 80, path: "/" }
  };

  var socket = null;
  var reconnectTimer = null;
  var cameraDebounce = null;
  var controlDebounce = null;
  var currentAdviceKey = null;
  var backendAdopted = false;
  var camerasEdited = false;
  var controlsFromStorage = false;
  var serverConfigAdopted = false;
  var controlsEdited = false;
  var viewRefs = {};
  var channelRefs = {};
  var seenAlertIds = {};
  var ledgerEventCount = 0;
  var lastTelemetryAt = 0;
  var linkTimer = null;
  var configFlashTimer = null;
  var latencyEma = null;
  var alarmState = { alertId: 0, cameraId: "", level: "SAFE" };
  var audioCtx = null;

  var state = {
    cameras: [makeCamera("cam1", { kind: "webcam", preset: "webcam" })],
    activeId: "cam1"
  };

  /* ------------------------------------------------------------------ */
  /* Small helpers                                                      */
  /* ------------------------------------------------------------------ */
  function toNumber(value, fallback) {
    var parsed = parseFloat(value);
    return isFinite(parsed) ? parsed : fallback;
  }

  function toInt(value, fallback) {
    var parsed = parseInt(value, 10);
    return isFinite(parsed) ? parsed : fallback;
  }

  function weightFraction(slider) {
    return Math.round(toNumber(slider.value, 0)) / 100;
  }

  function thresholdFraction(slider) {
    return Math.round(toNumber(slider.value, 55)) / 100;
  }

  function riskColor(level) {
    return RISK_COLORS[level] || RISK_COLORS.SAFE;
  }

  function normalizeLevel(level) {
    var upper = typeof level === "string" ? level.toUpperCase() : "SAFE";
    return RISK_COLORS[upper] ? upper : "SAFE";
  }

  function cameraById(id) {
    for (var i = 0; i < state.cameras.length; i += 1) {
      if (state.cameras[i].id === id) return state.cameras[i];
    }
    return null;
  }

  function makeCamera(id, opts) {
    var base = {
      id: id,
      kind: "network",
      preset: "custom",
      index: 0,
      scheme: "http",
      host: "",
      port: 4747,
      path: "/video",
      username: "",
      password: "",
      status: "IDLE",
      connectAt: 0,
      error: ""
    };
    if (opts) {
      Object.keys(opts).forEach(function (key) {
        base[key] = opts[key];
      });
    }
    return base;
  }

  function defaultPortFor(scheme) {
    return scheme === "https" ? 443 : scheme === "rtsp" ? 554 : 80;
  }

  /* ------------------------------------------------------------------ */
  /* Source building / masking / validation                             */
  /* ------------------------------------------------------------------ */
  function buildUrl(cam) {
    var scheme = (cam.scheme || "http").toLowerCase();
    var host = String(cam.host || "").trim();
    if (!host) return "";
    var port = toInt(cam.port, defaultPortFor(scheme));
    var auth = "";
    if (scheme === "rtsp" && cam.username) {
      auth = encodeURIComponent(cam.username);
      if (cam.password) auth += ":" + encodeURIComponent(cam.password);
      auth += "@";
    }
    var path = String(cam.path || "/");
    if (path.charAt(0) !== "/") path = "/" + path;
    return scheme + "://" + auth + host + (port ? ":" + port : "") + path;
  }

  function maskUrl(url) {
    if (!url) return "";
    return String(url).replace(/(:\/\/)([^/@\s]+)@/, "$1***@");
  }

  function buildSource(cam) {
    if (cam.kind === "webcam") {
      return Math.max(0, Math.round(toNumber(cam.index, 0)));
    }
    return buildUrl(cam);
  }

  function sourceKey(source) {
    if (typeof source === "number") return "idx:" + source;
    var text = String(source || "").trim();
    if (/^\d+$/.test(text)) return "idx:" + parseInt(text, 10);
    // Credentials do not make two cameras distinct.
    text = text.replace(/^([a-z][a-z0-9+.-]*:\/\/)[^/@]*@/i, "$1");
    return text.toLowerCase();
  }

  function isValidHost(host) {
    if (!host) return false;
    var value = String(host).trim();
    var ipv4 = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/;
    if (ipv4.test(value)) {
      return value.split(".").every(function (part) {
        return parseInt(part, 10) <= 255;
      });
    }
    if (value.length > 253) return false;
    return /^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$/.test(value);
  }

  function classifyHost(host) {
    var value = String(host || "").trim();
    var ipv4 = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/;
    if (!ipv4.test(value)) {
      return { valid: isValidHost(value), kind: "hostname" };
    }
    var parts = value.split(".").map(function (part) { return parseInt(part, 10); });
    if (parts.some(function (part) { return part > 255; })) {
      return { valid: false, kind: "ipv4" };
    }
    if (parts[0] === 100 && parts[1] >= 64 && parts[1] <= 127) {
      return { valid: true, kind: "tailscale" };
    }
    if (parts[0] === 10 || (parts[0] === 192 && parts[1] === 168) || (parts[0] === 172 && parts[1] >= 16 && parts[1] <= 31)) {
      return { valid: true, kind: "private" };
    }
    return { valid: true, kind: "public" };
  }

  function validateCamera(cam) {
    if (!cam) return "Unknown camera.";
    if (cam.kind === "webcam") {
      var index = toInt(cam.index, -1);
      if (!isFinite(index) || index < 0) return "Webcam index must be 0 or higher.";
      return "";
    }
    if (!cam.host) return "Enter the camera IP address or hostname.";
    var hostInfo = classifyHost(cam.host);
    if (!hostInfo.valid) return "That IP/hostname is not valid.";
    var port = toInt(cam.port, -1);
    if (!isFinite(port) || port < 1 || port > 65535) return "Port must be between 1 and 65535.";
    var scheme = String(cam.scheme || "").toLowerCase();
    if (["http", "https", "rtsp"].indexOf(scheme) < 0) return "Use an http, https or rtsp stream.";
    if (!cam.path) return "Enter the stream path (for example /video).";
    return "";
  }

  function duplicateSourceIds() {
    var seen = {};
    var dupes = {};
    state.cameras.forEach(function (cam) {
      if (validateCamera(cam)) return;
      var key = sourceKey(buildSource(cam));
      if (seen[key]) dupes[cam.id] = true;
      else seen[key] = true;
    });
    return dupes;
  }

  function cameraSource(cam) {
    return buildSource(cam);
  }

  function cameraLabel(cam, index) {
    var title = "CAM " + (index + 1);
    if (cam.kind === "webcam") {
      return title + " \u00b7 Webcam " + Math.max(0, Math.round(toNumber(cam.index, 0)));
    }
    var preset = PRESETS[cam.preset];
    return title + " \u00b7 " + (preset ? preset.label.replace(" (phone)", "") : "Network");
  }

  /* ------------------------------------------------------------------ */
  /* URL parsing / preset detection                                     */
  /* ------------------------------------------------------------------ */
  function detectPreset(url) {
    var text = String(url || "");
    var lower = text.toLowerCase();
    if (lower.indexOf("4747") >= 0) return "droidcam";
    if (lower.indexOf("8080") >= 0 && lower.indexOf("/video") >= 0) return "ipwebcam";
    if (lower.indexOf("streaming/channels") >= 0) return "hikvision";
    if (lower.indexOf("realmonitor") >= 0) return "dahua";
    if (lower.indexOf("rtsp://") === 0) return "rtsp";
    if (lower.indexOf("http://") === 0 || lower.indexOf("https://") === 0) return "mjpeg";
    return "custom";
  }

  function parseUrlInto(cam, url) {
    try {
      var parsed = new URL(url);
      cam.scheme = (parsed.protocol || "http:").replace(":", "").toLowerCase();
      cam.host = parsed.hostname || "";
      cam.port = parsed.port ? parseInt(parsed.port, 10) : defaultPortFor(cam.scheme);
      cam.path = (parsed.pathname || "/") + (parsed.search || "");
      if (parsed.username) cam.username = decodeURIComponent(parsed.username);
      if (parsed.password) cam.password = decodeURIComponent(parsed.password);
      cam.preset = detectPreset(url);
      return true;
    } catch (error) {
      return false;
    }
  }

  function maybeSplitPastedUrl(cam, text, refs) {
    var value = String(text || "").trim();
    if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(value)) return false;
    if (!parseUrlInto(cam, value)) return false;
    if (refs && refs.hostInput) refs.hostInput.value = cam.host;
    syncCardFields(cam, refs);
    return true;
  }

  /* ------------------------------------------------------------------ */
  /* Persistence                                                        */
  /* ------------------------------------------------------------------ */
  function currentControls() {
    return {
      stride: ui.strideSlider ? Math.round(toNumber(ui.strideSlider.value, 1)) : 1,
      threshold: ui.thresholdSlider ? thresholdFraction(ui.thresholdSlider) : 0.55,
      weights: {
        density: ui.weightDensity ? weightFraction(ui.weightDensity) : 0.4,
        rate_of_change: ui.weightRate ? weightFraction(ui.weightRate) : 0.2,
        convergence: ui.weightConvergence ? weightFraction(ui.weightConvergence) : 0.2,
        turbulence: ui.weightTurbulence ? weightFraction(ui.weightTurbulence) : 0.2
      }
    };
  }

  function savePersisted() {
    try {
      var cameras = state.cameras.map(function (cam) {
        return {
          id: cam.id,
          kind: cam.kind,
          preset: cam.preset,
          index: cam.index,
          scheme: cam.scheme,
          host: cam.host,
          port: cam.port,
          path: cam.path,
          username: cam.username
          /* password intentionally never persisted */
        };
      });
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify({ cameras: cameras, controls: currentControls() }));
    } catch (error) {
      /* storage may be unavailable (private mode); ignore */
    }
  }

  function loadPersisted() {
    try {
      var raw = window.localStorage.getItem(STORAGE_KEY);
      if (!raw) return null;
      var parsed = JSON.parse(raw);
      if (!parsed || !Array.isArray(parsed.cameras) || parsed.cameras.length === 0) return null;
      return parsed;
    } catch (error) {
      return null;
    }
  }

  function restoreCamera(stored) {
    var cam = makeCamera(stored.id || "cam1", {
      kind: stored.kind === "webcam" ? "webcam" : "network",
      preset: PRESETS[stored.preset] ? stored.preset : "custom",
      index: Math.max(0, toInt(stored.index, 0)),
      scheme: stored.scheme || "http",
      host: stored.host || "",
      port: toInt(stored.port, 4747),
      path: stored.path || "/",
      username: stored.username || "",
      password: ""
    });
    cam.status = "IDLE";
    return cam;
  }

  function applyControls(controls) {
    if (!controls) return;
    if (controls.stride && ui.strideSlider) {
      ui.strideSlider.value = String(controls.stride);
      if (ui.strideValue) ui.strideValue.textContent = String(controls.stride);
    }
    if (ui.thresholdSlider && typeof controls.threshold === "number") {
      ui.thresholdSlider.value = String(Math.round(controls.threshold * 100));
      if (ui.thresholdValue) ui.thresholdValue.textContent = toNumber(controls.threshold, 0.55).toFixed(2);
    }
    var weights = controls.weights || {};
    if (ui.weightDensity && typeof weights.density === "number") {
      ui.weightDensity.value = String(Math.round(weights.density * 100));
      if (ui.weightDensityValue) ui.weightDensityValue.textContent = toNumber(weights.density, 0.4).toFixed(2);
    }
    if (ui.weightRate && typeof weights.rate_of_change === "number") {
      ui.weightRate.value = String(Math.round(weights.rate_of_change * 100));
      if (ui.weightRateValue) ui.weightRateValue.textContent = toNumber(weights.rate_of_change, 0.2).toFixed(2);
    }
    if (ui.weightConvergence && typeof weights.convergence === "number") {
      ui.weightConvergence.value = String(Math.round(weights.convergence * 100));
      if (ui.weightConvergenceValue) ui.weightConvergenceValue.textContent = toNumber(weights.convergence, 0.2).toFixed(2);
    }
    if (ui.weightTurbulence && typeof weights.turbulence === "number") {
      ui.weightTurbulence.value = String(Math.round(weights.turbulence * 100));
      if (ui.weightTurbulenceValue) ui.weightTurbulenceValue.textContent = toNumber(weights.turbulence, 0.2).toFixed(2);
    }
  }

  /* ------------------------------------------------------------------ */
  /* Annotated MJPEG viewports                                          */
  /* ------------------------------------------------------------------ */
  function clearViewTimers() {
    Object.keys(viewRefs).forEach(function (id) {
      clearStreamTimers(viewRefs[id]);
    });
    viewRefs = {};
  }

  function clearStreamTimers(refs) {
    if (!refs) return;
    if (refs.watchTimer) {
      clearTimeout(refs.watchTimer);
      refs.watchTimer = null;
    }
    if (refs.retryTimer) {
      clearTimeout(refs.retryTimer);
      refs.retryTimer = null;
    }
  }

  function setNoSignal(refs, visible, message) {
    if (!refs || !refs.noSignal) return;
    refs.noSignal.classList.toggle("visible", !!visible);
    if (message && refs.noSignalText) {
      refs.noSignalText.textContent = message;
    }
  }

  function attemptStream(camId, refs) {
    if (!refs) return;
    clearStreamTimers(refs);
    refs.img.src = STREAM_URL + "?cam=" + encodeURIComponent(camId) + "&t=" + Date.now();
    refs.watchTimer = setTimeout(function () {
      refs.watchTimer = null;
      if (!refs.img.naturalWidth) {
        scheduleStreamRetry(camId, refs);
      }
    }, VIDEO_WATCHDOG_MS);
  }

  /* F1: exponential backoff + explicit NO SIGNAL state instead of a dead frame. */
  function scheduleStreamRetry(camId, refs) {
    if (!refs || refs.retryTimer) return;
    refs.failedAttempts = (refs.failedAttempts || 0) + 1;
    var delay = Math.min(
      STREAM_RETRY_BASE_MS * Math.pow(2, refs.failedAttempts - 1),
      STREAM_RETRY_MAX_MS
    );
    setNoSignal(refs, true, "NO SIGNAL \u2014 retrying in " + Math.round(delay / 1000) + "s");
    refs.retryTimer = setTimeout(function () {
      refs.retryTimer = null;
      attemptStream(camId, refs);
    }, delay);
  }

  function attachStream(camId, refs) {
    refs.failedAttempts = 0;
    refs.img.addEventListener("load", function () {
      refs.failedAttempts = 0;
      clearStreamTimers(refs);
      setNoSignal(refs, false, "");
    });
    refs.img.addEventListener("error", function () {
      scheduleStreamRetry(camId, refs);
    });
    attemptStream(camId, refs);
  }

  function renderViewports() {
    if (!ui.viewportGrid) return;
    clearViewTimers();
    ui.viewportGrid.innerHTML = "";

    state.cameras.forEach(function (cam, index) {
      var tile = document.createElement("div");
      tile.className = "cam-tile" + (cam.id === state.activeId ? " focused" : "");
      tile.dataset.cam = cam.id;
      tile.tabIndex = 0;
      tile.setAttribute("role", "button");
      tile.setAttribute("aria-pressed", cam.id === state.activeId ? "true" : "false");
      tile.setAttribute("aria-label", cameraLabel(cam, index) + " \u2014 activate to focus this camera");

      var head = document.createElement("div");
      head.className = "cam-head";
      var title = document.createElement("span");
      title.className = "cam-title";
      title.textContent = cameraLabel(cam, index);
      var badge = document.createElement("span");
      badge.className = "cam-badge";
      badge.textContent = "IDLE";
      head.appendChild(title);
      head.appendChild(badge);

      var videoBox = document.createElement("div");
      videoBox.className = "video-box";
      var img = document.createElement("img");
      img.className = "cam-stream";
      img.alt = "Live stream for " + cameraLabel(cam, index);
      videoBox.appendChild(img);

      var noSignal = document.createElement("div");
      noSignal.className = "no-signal visible";
      var noSignalTitle = document.createElement("strong");
      noSignalTitle.textContent = "NO SIGNAL";
      var noSignalText = document.createElement("span");
      noSignalText.textContent = "Connecting to camera stream\u2026";
      noSignal.appendChild(noSignalTitle);
      noSignal.appendChild(noSignalText);
      videoBox.appendChild(noSignal);

      var foot = document.createElement("div");
      foot.className = "cam-foot";
      var people = document.createElement("span");
      people.className = "cam-people";
      people.textContent = "People: 0";
      var score = document.createElement("span");
      score.className = "cam-score";
      score.textContent = "Score 0.00";
      foot.appendChild(people);
      foot.appendChild(score);

      tile.appendChild(head);
      tile.appendChild(videoBox);
      tile.appendChild(foot);

      tile.addEventListener("click", function () {
        selectCamera(cam.id);
      });
      tile.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " " || event.key === "Spacebar") {
          event.preventDefault();
          selectCamera(cam.id);
        }
      });

      ui.viewportGrid.appendChild(tile);

      var refs = {
        tile: tile,
        img: img,
        title: title,
        badge: badge,
        people: people,
        score: score,
        noSignal: noSignal,
        noSignalText: noSignalText,
        watchTimer: null,
        retryTimer: null,
        failedAttempts: 0
      };
      viewRefs[cam.id] = refs;
      attachStream(cam.id, refs);
    });

    updateFocusLabel();
  }

  function selectCamera(camId) {
    state.activeId = camId;
    currentAdviceKey = null;
    latencyEma = null;
    Object.keys(viewRefs).forEach(function (id) {
      var focused = id === camId;
      viewRefs[id].tile.classList.toggle("focused", focused);
      viewRefs[id].tile.setAttribute("aria-pressed", focused ? "true" : "false");
    });
    updateFocusLabel();
  }

  function updateFocusLabel() {
    if (!ui.focusLabel) return;
    var index = state.cameras.findIndex(function (cam) {
      return cam.id === state.activeId;
    });
    if (index < 0) index = 0;
    ui.focusLabel.textContent = "FOCUS: " + cameraLabel(state.cameras[index], index);
  }

  /* ------------------------------------------------------------------ */
  /* Link indicator (F2: real stale/lost detection)                      */
  /* ------------------------------------------------------------------ */
  function setLinkState(mode) {
    if (!ui.linkStatus) return;
    var lost = mode === "lost";
    var stale = mode === "stale";
    ui.linkStatus.classList.toggle("offline", lost);
    ui.linkStatus.classList.toggle("stale", stale);
    if (mode === "online") {
      ui.linkStatus.textContent = "\u25cf CONTROL DESK SECURE LINK ONLINE";
    } else if (stale) {
      ui.linkStatus.textContent = "\u25cf LINK STALE \u2014 NO TELEMETRY";
    } else {
      ui.linkStatus.textContent = "\u25cf LINK LOST \u2014 RECONNECTING";
    }
  }

  function flashLink(message) {
    if (!ui.linkStatus) return;
    ui.linkStatus.textContent = message;
    ui.linkStatus.classList.remove("offline", "stale");
    if (configFlashTimer) clearTimeout(configFlashTimer);
    configFlashTimer = setTimeout(function () {
      configFlashTimer = null;
      refreshLinkState();
    }, CONFIG_FLASH_MS);
  }

  function refreshLinkState() {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      setLinkState("lost");
      return;
    }
    if (!lastTelemetryAt) {
      setLinkState("online");
      return;
    }
    var age = Date.now() - lastTelemetryAt;
    if (age > LINK_LOST_MS) {
      setLinkState("lost");
    } else if (age > LINK_STALE_MS) {
      setLinkState("stale");
    } else {
      setLinkState("online");
    }
  }

  function startLinkWatchdog() {
    if (linkTimer) return;
    linkTimer = setInterval(refreshLinkState, LINK_TICK_MS);
  }

  /* ------------------------------------------------------------------ */
  /* WebSocket transport                                                */
  /* ------------------------------------------------------------------ */
  function connect() {
    if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
      return;
    }

    try {
      socket = new WebSocket(WS_URL);
    } catch (error) {
      setLinkState("lost");
      scheduleReconnect();
      return;
    }

    socket.addEventListener("open", function () {
      lastTelemetryAt = Date.now();
      setLinkState("online");
      startLinkWatchdog();
      onControlDeskReady();
    });

    socket.addEventListener("message", function (event) {
      var packet;
      try {
        packet = JSON.parse(event.data);
      } catch (error) {
        return;
      }
      handlePacket(packet);
    });

    socket.addEventListener("close", function () {
      lastTelemetryAt = 0;
      setLinkState("lost");
      scheduleReconnect();
    });

    socket.addEventListener("error", function () {
      setLinkState("lost");
    });
  }

  function scheduleReconnect() {
    if (reconnectTimer) return;
    reconnectTimer = setTimeout(function () {
      reconnectTimer = null;
      connect();
    }, RECONNECT_DELAY_MS);
  }

  function onControlDeskReady() {
    if (!controlsFromStorage && !serverConfigAdopted && !controlsEdited) {
      /* F4: ask the backend for its live config before overriding anything. */
      if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: "config" }));
      }
    } else {
      sendConfig(false);
    }
    if (camerasEdited || backendAdopted) {
      markCamerasConnecting();
      sendConfig(true);
    }
  }

  /* ------------------------------------------------------------------ */
  /* Packet handling                                                    */
  /* ------------------------------------------------------------------ */
  function handlePacket(packet) {
    if (!packet || typeof packet !== "object") return;
    if (packet.type === "history") {
      (packet.alerts || []).forEach(function (event) {
        appendLedgerFromEvent(event);
      });
      return;
    }
    if (packet.type === "alert") {
      appendLedgerFromEvent(packet);
      return;
    }
    if (packet.type === "config_ack") {
      lastTelemetryAt = lastTelemetryAt || Date.now();
      if (!controlsFromStorage && !serverConfigAdopted && !controlsEdited && packet.config) {
        serverConfigAdopted = true;
        applyControls({
          stride: packet.config.stride,
          threshold: packet.config.alert_threshold,
          weights: packet.config.weights
        });
        savePersisted();
      }
      refreshLinkState();
      return;
    }
    if (packet.type === "config_error") {
      var reason = typeof packet.error === "string" ? packet.error : "check the input values";
      flashLink("\u25cf CONFIG REJECTED \u2014 " + reason.slice(0, 70));
      return;
    }
    handleTelemetry(packet);
  }

  function handleTelemetry(packet) {
    if (!packet || !Array.isArray(packet.cameras)) return;
    lastTelemetryAt = Date.now();

    if (!backendAdopted && !camerasEdited && packet.cameras.length > 0) {
      backendAdopted = true;
      adoptBackendCameras(packet.cameras);
      return;
    }

    packet.cameras.forEach(function (cam) {
      updateTile(cam);
      updateCameraStatusFromTelemetry(cam, true);
    });

    state.cameras.forEach(function (cam) {
      var present = packet.cameras.some(function (item) { return item.id === cam.id; });
      if (!present) updateCameraStatusFromTelemetry(cam, false);
    });

    var active = null;
    for (var i = 0; i < packet.cameras.length; i += 1) {
      if (packet.cameras[i].id === state.activeId) {
        active = packet.cameras[i];
        break;
      }
    }
    if (!active && packet.cameras.length > 0) {
      active = packet.cameras[0];
    }
    if (active) {
      updateFocus(active);
    }

    packet.cameras.forEach(function (cam) {
      if (cam.alert === true) {
        appendLedgerFromEvent(cam);
      }
    });
  }

  function adoptBackendCameras(cameras) {
    state.cameras = cameras.map(function (cam, index) {
      var source = cam.source_value;
      if (source === undefined || source === null) {
        source = cam.source;
      }
      var isIndex = typeof source === "number" || /^\d+$/.test(String(source || ""));
      var restored = makeCamera(cam.id || "cam" + (index + 1), {});
      if (isIndex) {
        restored.kind = "webcam";
        restored.preset = "webcam";
        restored.index = Math.max(0, parseInt(source, 10) || 0);
      } else if (source) {
        restored.kind = "network";
        if (!parseUrlInto(restored, String(source))) {
          restored.preset = "custom";
          restored.host = "";
          restored.path = "/";
        }
      } else {
        restored.kind = "network";
        restored.preset = "custom";
      }
      restored.status = cam.online ? "CONNECTED" : "IDLE";
      return restored;
    });
    if (state.cameras.length === 0) {
      state.cameras = [makeCamera("cam1", { kind: "webcam", preset: "webcam" })];
    }
    var exists = state.cameras.some(function (cam) {
      return cam.id === state.activeId;
    });
    if (!exists) state.activeId = state.cameras[0].id;
    renderChannels();
    renderViewports();
    savePersisted();
  }

  function updateTile(cam) {
    var refs = viewRefs[cam.id];
    if (!refs) return;
    var level = normalizeLevel(cam.risk_level);
    var color = riskColor(level);

    refs.badge.textContent = cam.online ? level : "OFFLINE";
    refs.badge.style.color = cam.online ? color : "#64748b";
    refs.badge.style.borderColor = cam.online ? color : "rgba(100, 116, 139, 0.35)";
    refs.badge.style.background = cam.online ? "rgba(255, 255, 255, 0.04)" : "rgba(100, 116, 139, 0.15)";

    refs.title.textContent = cam.label || cam.id;
    refs.people.textContent = "People: " + Math.max(0, Math.round(toNumber(cam.people_now, 0)));
    refs.score.textContent = "Score " + toNumber(cam.risk_score, 0).toFixed(2);

    if (!cam.online) {
      setNoSignal(refs, true, "CAMERA OFFLINE \u2014 CHECK SOURCE");
    }
    refs.tile.classList.toggle(
      "alarm",
      alarmState.cameraId === cam.id && RISK_RANK[alarmState.level] >= ALARM_MIN_RANK
    );
  }

  function barColor(value) {
    if (value >= 0.75) return RISK_COLORS.CRITICAL;
    if (value >= 0.5) return RISK_COLORS.HIGH;
    if (value >= 0.25) return RISK_COLORS.MODERATE;
    return RISK_COLORS.SAFE;
  }

  function updateBar(fill, label, value) {
    var clamped = Math.max(0, Math.min(1, toNumber(value, 0)));
    if (fill) {
      fill.style.width = (clamped * 100).toFixed(1) + "%";
      fill.style.background = barColor(clamped);
    }
    if (label) label.textContent = clamped.toFixed(2);
  }

  function formatEta(seconds) {
    var eta = toNumber(seconds, NaN);
    if (!isFinite(eta) || eta < 0) return "\u2014";
    if (eta < 60) return Math.round(eta) + "s";
    return Math.floor(eta / 60) + "m " + Math.round(eta % 60) + "s";
  }

  function updateFocus(cam) {
    var level = normalizeLevel(cam.risk_level);
    var color = riskColor(level);

    ui.headcount.textContent = String(Math.max(0, Math.round(toNumber(cam.people_now, 0))));
    ui.risk.textContent = level;
    ui.risk.style.color = color;
    ui.risk.style.textShadow = "0 0 18px " + color;

    /* F2: real measured latency from the server timestamp, never a fake constant. */
    var stamp = toNumber(cam.timestamp, 0);
    if (stamp > 0) {
      var sample = Date.now() - stamp;
      if (isFinite(sample) && sample >= 0 && sample <= 60000) {
        latencyEma = latencyEma === null ? sample : latencyEma * 0.7 + sample * 0.3;
      }
    }
    var frameAge = Math.max(0, toNumber(cam.last_frame_age_ms, 0));
    if (latencyEma === null) {
      ui.latency.textContent = "\u2014";
      ui.latency.style.color = "#64748b";
      ui.latency.title = "No telemetry timing data yet";
    } else {
      var shown = Math.max(1, Math.round(latencyEma));
      ui.latency.textContent = shown + "ms";
      ui.latency.style.color = shown > 150 ? RISK_COLORS.CRITICAL : shown > 60 ? RISK_COLORS.MODERATE : "#00f2fe";
      ui.latency.title = "Network " + shown + "ms \u00b7 frame age " + Math.round(frameAge) + "ms";
    }

    /* F7: live signal breakdown + density gate + forecast ETA. */
    updateBar(ui.barDensity, ui.barDensityValue, cam.risk_density);
    updateBar(ui.barRate, ui.barRateValue, cam.risk_rate_of_change);
    updateBar(ui.barConvergence, ui.barConvergenceValue, cam.risk_convergence);
    updateBar(ui.barTurbulence, ui.barTurbulenceValue, cam.risk_turbulence);

    if (ui.infoDensityPsm) {
      var psm = toNumber(cam.density_per_sqm, 0);
      ui.infoDensityPsm.textContent = psm.toFixed(2);
    }
    var formulaLevel = normalizeLevel(cam.risk_level_formula);
    if (ui.infoLevelFormula) {
      ui.infoLevelFormula.textContent = formulaLevel;
      ui.infoLevelFormula.style.color = riskColor(formulaLevel);
    }
    var densityLevel = normalizeLevel(cam.risk_level_density);
    if (ui.infoLevelDensity) {
      ui.infoLevelDensity.textContent = densityLevel;
      ui.infoLevelDensity.style.color = riskColor(densityLevel);
    }
    if (ui.infoEta) {
      ui.infoEta.textContent = formatEta(cam.forecast_eta_s);
    }

    var adviceKey = cam.id + ":" + level;
    if (adviceKey !== currentAdviceKey) {
      renderAdvice(level);
      currentAdviceKey = adviceKey;
    }

    if (ui.focusLabel) {
      ui.focusLabel.textContent = "FOCUS: " + (cam.label || cam.id);
    }
  }

  function renderAdvice(level) {
    if (!ui.advice) return;
    var lines = ACTION_GUIDELINES[level] || ACTION_GUIDELINES.SAFE;
    var list = document.createElement("ul");

    lines.forEach(function (line) {
      var item = document.createElement("li");
      item.textContent = line;
      list.appendChild(item);
    });

    ui.advice.innerHTML = "";
    ui.advice.appendChild(list);
    ui.advice.style.borderLeftColor = riskColor(level);
  }

  function appendLedgerFromEvent(event) {
    if (!ui.ledger || !event) return;
    var alertId = toInt(event.alert_id, 0);
    if (alertId > 0) {
      if (seenAlertIds[alertId]) return;
      seenAlertIds[alertId] = true;
    }

    var level = normalizeLevel(event.risk_level);
    var header = ui.ledger.firstElementChild;
    var row = document.createElement("div");
    row.className = "log-row";

    var timeCell = document.createElement("div");
    var stamp = toNumber(event.timestamp, Date.now());
    timeCell.textContent = new Date(stamp).toLocaleTimeString();

    var camCell = document.createElement("div");
    camCell.textContent = event.camera_label || event.label || event.id || event.camera_id || "Camera";
    camCell.style.color = "#00f2fe";

    var levelCell = document.createElement("div");
    levelCell.textContent = level;
    levelCell.style.color = riskColor(level);
    levelCell.style.fontWeight = "bold";

    var actionCell = document.createElement("div");
    var actionKey = typeof event.action === "string" ? event.action.toUpperCase() : "NONE";
    actionCell.textContent = ACTION_LABELS[actionKey] || ACTION_LABELS.NONE;

    row.appendChild(timeCell);
    row.appendChild(camCell);
    row.appendChild(levelCell);
    row.appendChild(actionCell);

    if (header) {
      header.insertAdjacentElement("afterend", row);
    } else {
      ui.ledger.appendChild(row);
    }

    while (ui.ledger.children.length > MAX_LEDGER_ROWS + 1) {
      ui.ledger.removeChild(ui.ledger.lastElementChild);
    }
    ui.ledger.scrollTop = 0;
  }

  /* ------------------------------------------------------------------ */
  /* Camera status                                                      */
  /* ------------------------------------------------------------------ */
  function statusLabel(status) {
    if (status === "CONNECTING") return "\u25cf CONNECTING";
    if (status === "CONNECTED") return "\u25cf CONNECTED";
    if (status === "FAILED") return "\u25cf FAILED";
    return "\u25cf IDLE";
  }

  function setCameraStatus(cam, status) {
    cam.status = status;
    var refs = channelRefs[cam.id];
    if (!refs || !refs.statusChip) return;
    refs.statusChip.textContent = statusLabel(status);
    refs.statusChip.className = "status-chip " + status.toLowerCase();
  }

  function markCamerasConnecting() {
    state.cameras.forEach(function (cam) {
      if (cam.kind !== "webcam") {
        setCameraStatus(cam, "CONNECTING");
        cam.connectAt = Date.now();
      }
    });
  }

  function updateCameraStatusFromTelemetry(telemetry, present) {
    var cam = cameraById(telemetry.id);
    if (!cam) return;
    if (telemetry.online) {
      if (cam.status !== "CONNECTED") {
        setCameraStatus(cam, "CONNECTED");
        cam.error = "";
        updateCardRefs(cam);
      }
      return;
    }
    if (!present) {
      if (cam.status === "CONNECTING" && Date.now() - cam.connectAt > CONNECT_TIMEOUT_MS) {
        setCameraStatus(cam, "FAILED");
        cam.error = "Camera was not registered by the backend. Check the URL and try again.";
        updateCardRefs(cam);
      }
      return;
    }
    if (cam.status === "CONNECTING" && Date.now() - cam.connectAt > CONNECT_TIMEOUT_MS) {
      setCameraStatus(cam, "FAILED");
      cam.error = "No frames received. Check the IP, port, path, or that the phone is on the same Wi-Fi.";
      updateCardRefs(cam);
    }
  }

  /* ------------------------------------------------------------------ */
  /* Camera channel editor                                              */
  /* ------------------------------------------------------------------ */
  function nextCameraId() {
    var index = 1;
    while (cameraById("cam" + index)) index += 1;
    return "cam" + index;
  }

  function scheduleCameras(delay) {
    camerasEdited = true;
    if (cameraDebounce) clearTimeout(cameraDebounce);
    cameraDebounce = setTimeout(function () {
      cameraDebounce = null;
      sendConfig(true);
    }, typeof delay === "number" ? delay : CAMERA_DEBOUNCE_MS);
  }

  function commitCamera(cam, immediate) {
    var error = validateCamera(cam);
    if (!error) {
      var dupes = duplicateSourceIds();
      if (dupes[cam.id]) {
        error = "Duplicate source \u2014 another channel already uses this stream.";
      }
    }
    cam.error = error;
    if (error) {
      setCameraStatus(cam, "FAILED");
      updateCardRefs(cam);
      return false;
    }
    setCameraStatus(cam, "CONNECTING");
    cam.connectAt = Date.now();
    updateCardRefs(cam);
    savePersisted();
    scheduleCameras(immediate ? 0 : CAMERA_DEBOUNCE_MS);
    return true;
  }

  function addCamera() {
    if (state.cameras.length >= MAX_CAMERAS) return;
    var id = nextCameraId();
    state.cameras.push(makeCamera(id, {
      kind: "network",
      preset: "droidcam",
      scheme: "http",
      port: 4747,
      path: "/video"
    }));
    renderChannels();
    renderViewports();
    selectCamera(id);
    savePersisted();
  }

  function removeCamera(camId) {
    if (state.cameras.length <= 1) return;
    var removedView = viewRefs[camId];
    if (removedView && removedView.snapTimer) clearInterval(removedView.snapTimer);
    state.cameras = state.cameras.filter(function (cam) {
      return cam.id !== camId;
    });
    delete channelRefs[camId];
    if (state.activeId === camId) {
      state.activeId = state.cameras[0].id;
    }
    renderChannels();
    renderViewports();
    savePersisted();
    scheduleCameras(CAMERA_DEBOUNCE_MS);
  }

  function createField(labelText) {
    var wrapper = document.createElement("div");
    var label = document.createElement("div");
    label.className = "field-label";
    label.textContent = labelText;
    wrapper.appendChild(label);
    return wrapper;
  }

  function syncCardFields(cam, refs) {
    if (!refs) return;
    var isWebcam = cam.kind === "webcam";
    if (refs.webcamBlock) refs.webcamBlock.style.display = isWebcam ? "block" : "none";
    if (refs.networkBlock) refs.networkBlock.style.display = isWebcam ? "none" : "flex";
    if (refs.presetSelect) refs.presetSelect.value = cam.preset;
    if (refs.indexInput && document.activeElement !== refs.indexInput) {
      refs.indexInput.value = String(cam.index);
    }
    if (refs.hostInput && document.activeElement !== refs.hostInput) {
      refs.hostInput.value = cam.host;
    }
    if (refs.portInput && document.activeElement !== refs.portInput) {
      refs.portInput.value = String(cam.port);
    }
    if (refs.pathInput && document.activeElement !== refs.pathInput) {
      refs.pathInput.value = cam.path;
    }
    if (refs.userInput && document.activeElement !== refs.userInput) {
      refs.userInput.value = cam.username;
    }
    if (refs.passInput && document.activeElement !== refs.passInput) {
      refs.passInput.value = cam.password;
    }
    if (refs.credRow) {
      refs.credRow.classList.toggle("visible", !isWebcam && cam.scheme === "rtsp");
    }
    if (refs.pathLabel) {
      refs.pathLabel.textContent = cam.scheme === "rtsp" ? "Stream Path" : "URL Path";
    }
    updateCardRefs(cam);
  }

  function updateCardRefs(cam) {
    var refs = channelRefs[cam.id];
    if (!refs) return;
    if (refs.urlPreview) {
      var url = cam.kind === "webcam" ? "Webcam index " + cam.index : buildUrl(cam);
      refs.urlPreview.textContent = url ? maskUrl(url) : "Enter camera details to generate the stream URL";
    }
    if (refs.error) refs.error.textContent = cam.error || "";
    if (refs.row) refs.row.classList.toggle("invalid", !!cam.error);
    if (refs.connectBtn) refs.connectBtn.disabled = cam.kind === "network" && !cam.host;
    if (refs.hint) {
      if (cam.kind === "webcam") {
        refs.hint.textContent = "Uses a webcam attached to this PC.";
      } else {
        var info = classifyHost(cam.host);
        if (info.kind === "tailscale") {
          refs.hint.textContent = "Tailscale IP detected \u2014 make sure this PC is on the same tailnet.";
        } else if (info.kind === "private") {
          refs.hint.textContent = "Private LAN IP \u2014 phone and PC must be on the same Wi-Fi.";
        } else {
          refs.hint.textContent = "Phone/camera and PC must share Wi-Fi, or use a Tailscale (100.x.x.x) IP.";
        }
      }
    }
  }

  function buildChannelCard(cam, index) {
    var row = document.createElement("div");
    row.className = "channel-row";
    row.dataset.cam = cam.id;

    var head = document.createElement("div");
    head.className = "channel-head";
    var name = document.createElement("span");
    name.textContent = "CHANNEL " + (index + 1);

    var actions = document.createElement("div");
    actions.className = "channel-head-actions";
    var statusChip = document.createElement("span");
    statusChip.className = "status-chip " + (cam.status || "idle").toLowerCase();
    statusChip.textContent = statusLabel(cam.status || "IDLE");
    var remove = document.createElement("button");
    remove.type = "button";
    remove.className = "channel-remove";
    remove.textContent = "\u2715";
    remove.setAttribute("aria-label", "Remove channel " + (index + 1));
    remove.addEventListener("click", function () {
      removeCamera(cam.id);
    });
    actions.appendChild(statusChip);
    actions.appendChild(remove);
    head.appendChild(name);
    head.appendChild(actions);

    var controls = document.createElement("div");
    controls.className = "channel-controls";

    var presetSelect = document.createElement("select");
    presetSelect.className = "channel-preset";
    presetSelect.setAttribute("aria-label", "Camera type preset");
    Object.keys(PRESETS).forEach(function (key) {
      var option = document.createElement("option");
      option.value = key;
      option.textContent = PRESETS[key].label;
      presetSelect.appendChild(option);
    });
    presetSelect.value = cam.preset;
    controls.appendChild(presetSelect);

    var webcamBlock = document.createElement("div");
    webcamBlock.className = "field-row";
    var indexField = createField("Webcam Index");
    var indexInput = document.createElement("input");
    indexInput.type = "number";
    indexInput.min = "0";
    indexInput.step = "1";
    indexInput.setAttribute("aria-label", "Webcam index");
    indexField.appendChild(indexInput);
    webcamBlock.appendChild(indexField);
    controls.appendChild(webcamBlock);

    var networkBlock = document.createElement("div");
    networkBlock.className = "channel-controls";
    networkBlock.style.display = "flex";

    var hostPortRow = document.createElement("div");
    hostPortRow.className = "field-row";
    var hostField = createField("IP Address / Hostname");
    var hostInput = document.createElement("input");
    hostInput.type = "text";
    hostInput.placeholder = "192.168.1.50";
    hostInput.autocomplete = "off";
    hostInput.spellcheck = false;
    hostInput.setAttribute("aria-label", "Camera IP address or hostname");
    hostField.appendChild(hostInput);
    var portField = createField("Port");
    portField.className = "port-field";
    var portInput = document.createElement("input");
    portInput.type = "number";
    portInput.min = "1";
    portInput.max = "65535";
    portInput.setAttribute("aria-label", "Camera port");
    portField.appendChild(portInput);
    hostPortRow.appendChild(hostField);
    hostPortRow.appendChild(portField);
    networkBlock.appendChild(hostPortRow);

    var pathRow = document.createElement("div");
    pathRow.className = "field-row";
    var pathField = createField("URL Path");
    var pathLabel = pathField.querySelector(".field-label");
    var pathInput = document.createElement("input");
    pathInput.type = "text";
    pathInput.autocomplete = "off";
    pathInput.spellcheck = false;
    pathInput.setAttribute("aria-label", "Stream path");
    pathField.appendChild(pathInput);
    pathRow.appendChild(pathField);
    networkBlock.appendChild(pathRow);

    var credRow = document.createElement("div");
    credRow.className = "field-row cred-row";
    var userField = createField("Username (RTSP only)");
    var userInput = document.createElement("input");
    userInput.type = "text";
    userInput.autocomplete = "username";
    userInput.setAttribute("aria-label", "Camera username");
    userField.appendChild(userInput);
    var passField = createField("Password (RTSP only)");
    var passInput = document.createElement("input");
    passInput.type = "password";
    passInput.autocomplete = "new-password";
    passInput.setAttribute("aria-label", "Camera password");
    passField.appendChild(passInput);
    credRow.appendChild(userField);
    credRow.appendChild(passField);
    networkBlock.appendChild(credRow);

    var urlPreview = document.createElement("div");
    urlPreview.className = "url-preview";
    urlPreview.setAttribute("aria-live", "polite");
    networkBlock.appendChild(urlPreview);

    var hint = document.createElement("div");
    hint.className = "channel-hint";
    networkBlock.appendChild(hint);

    controls.appendChild(networkBlock);

    var error = document.createElement("div");
    error.className = "channel-error";
    controls.appendChild(error);

    var connectBtn = document.createElement("button");
    connectBtn.type = "button";
    connectBtn.className = "connect-btn";
    connectBtn.textContent = "CONNECT / APPLY";
    controls.appendChild(connectBtn);

    row.appendChild(head);
    row.appendChild(controls);

    var refs = {
      row: row,
      statusChip: statusChip,
      presetSelect: presetSelect,
      webcamBlock: webcamBlock,
      indexInput: indexInput,
      networkBlock: networkBlock,
      hostInput: hostInput,
      portInput: portInput,
      pathInput: pathInput,
      pathLabel: pathLabel,
      userInput: userInput,
      passInput: passInput,
      credRow: credRow,
      urlPreview: urlPreview,
      hint: hint,
      error: error,
      connectBtn: connectBtn
    };
    channelRefs[cam.id] = refs;

    presetSelect.addEventListener("change", function () {
      var key = presetSelect.value;
      var preset = PRESETS[key] || PRESETS.custom;
      cam.preset = key;
      cam.kind = preset.kind;
      if (preset.kind === "network" && key !== "custom") {
        cam.scheme = preset.scheme;
        cam.port = preset.port;
        cam.path = preset.path;
      }
      syncCardFields(cam, refs);
      if (cam.kind === "webcam" || cam.host) {
        commitCamera(cam, true);
      } else {
        cam.error = "";
        updateCardRefs(cam);
      }
    });

    indexInput.addEventListener("input", function () {
      cam.index = Math.max(0, toInt(indexInput.value, 0));
      updateCardRefs(cam);
    });
    indexInput.addEventListener("change", function () {
      cam.index = Math.max(0, toInt(indexInput.value, 0));
      commitCamera(cam);
    });
    indexInput.addEventListener("keydown", function (event) {
      if (event.key === "Enter") commitCamera(cam, true);
    });

    hostInput.addEventListener("input", function () {
      if (maybeSplitPastedUrl(cam, hostInput.value, refs)) {
        cam.error = "";
        updateCardRefs(cam);
        return;
      }
      cam.host = hostInput.value.trim();
      cam.error = "";
      updateCardRefs(cam);
    });
    hostInput.addEventListener("change", function () {
      cam.host = hostInput.value.trim();
      commitCamera(cam);
    });
    hostInput.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        cam.host = hostInput.value.trim();
        commitCamera(cam, true);
      }
    });

    portInput.addEventListener("input", function () {
      cam.port = toInt(portInput.value, cam.port);
      cam.error = "";
      updateCardRefs(cam);
    });
    portInput.addEventListener("change", function () {
      cam.port = toInt(portInput.value, cam.port);
      commitCamera(cam);
    });
    portInput.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        cam.port = toInt(portInput.value, cam.port);
        commitCamera(cam, true);
      }
    });

    pathInput.addEventListener("input", function () {
      cam.path = pathInput.value;
      cam.error = "";
      updateCardRefs(cam);
    });
    pathInput.addEventListener("change", function () {
      cam.path = pathInput.value;
      commitCamera(cam);
    });
    pathInput.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        cam.path = pathInput.value;
        commitCamera(cam, true);
      }
    });

    userInput.addEventListener("input", function () {
      cam.username = userInput.value;
      cam.error = "";
      updateCardRefs(cam);
    });
    userInput.addEventListener("change", function () {
      cam.username = userInput.value;
      commitCamera(cam);
    });

    passInput.addEventListener("input", function () {
      cam.password = passInput.value;
      cam.error = "";
    });
    passInput.addEventListener("change", function () {
      cam.password = passInput.value;
      commitCamera(cam);
    });
    passInput.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        cam.password = passInput.value;
        commitCamera(cam, true);
      }
    });

    connectBtn.addEventListener("click", function () {
      commitCamera(cam, true);
    });

    syncCardFields(cam, refs);
    return row;
  }

  function renderChannels() {
    if (!ui.channelList) return;
    ui.channelList.innerHTML = "";
    channelRefs = {};
    state.cameras.forEach(function (cam, index) {
      ui.channelList.appendChild(buildChannelCard(cam, index));
    });
  }

  /* ------------------------------------------------------------------ */
  /* Configuration emission                                             */
  /* ------------------------------------------------------------------ */
  function camerasPayload() {
    var used = {};
    var payload = [];
    state.cameras.forEach(function (cam, index) {
      if (validateCamera(cam)) return;
      var source = buildSource(cam);
      var key = sourceKey(source);
      if (used[key]) return;
      used[key] = true;
      payload.push({
        id: cam.id,
        label: cameraLabel(cam, index),
        source: source
      });
    });
    return payload;
  }

  function buildConfig(includeCameras) {
    var config = {
      type: "config",
      stride: Math.max(1, Math.round(toNumber(ui.strideSlider.value, 1))),
      weights: {
        density: weightFraction(ui.weightDensity),
        rate_of_change: weightFraction(ui.weightRate),
        convergence: weightFraction(ui.weightConvergence),
        turbulence: weightFraction(ui.weightTurbulence)
      },
      alert_threshold: thresholdFraction(ui.thresholdSlider)
    };
    if (includeCameras) {
      config.cameras = camerasPayload();
    }
    return config;
  }

  function sendConfig(includeCameras) {
    if (!socket || socket.readyState !== WebSocket.OPEN) return;
    var config = buildConfig(includeCameras);
    if (includeCameras && (!config.cameras || config.cameras.length === 0)) {
      return;
    }
    socket.send(JSON.stringify(config));
  }

  function scheduleControls() {
    savePersisted();
    if (controlDebounce) clearTimeout(controlDebounce);
    controlDebounce = setTimeout(function () {
      controlDebounce = null;
      sendConfig(false);
    }, CONFIG_DEBOUNCE_MS);
  }

  function bindWeight(slider, label) {
    if (!slider || !label) return;
    slider.addEventListener("input", function () {
      label.textContent = weightFraction(slider).toFixed(2);
      scheduleControls();
    });
  }

  function bindControls() {
    if (ui.addChannel) {
      ui.addChannel.addEventListener("click", addCamera);
    }

    if (ui.strideSlider) {
      ui.strideSlider.addEventListener("input", function () {
        if (ui.strideValue) {
          ui.strideValue.textContent = String(ui.strideSlider.value);
        }
        scheduleControls();
      });
    }

    bindWeight(ui.weightDensity, ui.weightDensityValue);
    bindWeight(ui.weightRate, ui.weightRateValue);
    bindWeight(ui.weightConvergence, ui.weightConvergenceValue);
    bindWeight(ui.weightTurbulence, ui.weightTurbulenceValue);

    if (ui.thresholdSlider) {
      ui.thresholdSlider.addEventListener("input", function () {
        if (ui.thresholdValue) {
          ui.thresholdValue.textContent = thresholdFraction(ui.thresholdSlider).toFixed(2);
        }
        scheduleControls();
      });
    }

    bindAlarmAck();
    bindLedgerClear();
  }

  function bindAlarmAck() {
    if (!ui.alarmAck) return;
    ui.alarmAck.addEventListener("click", function () {
      alarmState = { alertId: 0, cameraId: "", level: "SAFE" };
      setNoSignal(null, false, "");
      ui.alarm.classList.remove("alarm");
      ui.alarmTitle.textContent = "⚠ CROWD ALERT";
      ui.alarmMeta.textContent = "No active alerts";
      ui.alarm.style.display = "none";
    });
  }

  /* ------------------------------------------------------------------ */
  /* Boot                                                               */
  /* ------------------------------------------------------------------ */
  function init() {
    var persisted = loadPersisted();
    if (persisted) {
      state.cameras = persisted.cameras.slice(0, MAX_CAMERAS).map(restoreCamera);
      if (state.cameras.length === 0) {
        state.cameras = [makeCamera("cam1", { kind: "webcam", preset: "webcam" })];
      }
      state.activeId = state.cameras[0].id;
      camerasEdited = true;
      applyControls(persisted.controls);
    }

    renderChannels();
    renderViewports();
    bindControls();
    renderAdvice("SAFE");
    setLinkState(false);
    if (persisted) {
      markCamerasConnecting();
    }
    connect();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
