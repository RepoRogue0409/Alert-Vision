/* Alert Vision AI - Multi-Camera Control Room Telemetry Engine */

(function () {
  "use strict";

  var WS_URL = "ws://127.0.0.1:8765";
  var VIDEO_BASE = "http://127.0.0.1:8766";
  var STREAM_URL = VIDEO_BASE + "/video";
  var SNAPSHOT_URL = VIDEO_BASE + "/frame.jpg";
  var RECONNECT_DELAY_MS = 2000;
  var VIDEO_WATCHDOG_MS = 3000;
  var SNAPSHOT_INTERVAL_MS = 180;
  var CONFIG_DEBOUNCE_MS = 300;
  var MAX_CAMERAS = 6;
  var MAX_LEDGER_ROWS = 100;

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
    ledger: document.getElementById("ui-ledger-box")
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

  var socket = null;
  var reconnectTimer = null;
  var cameraDebounce = null;
  var currentAdviceKey = null;
  var backendAdopted = false;
  var camerasEdited = false;
  var camSeq = 1;
  var viewRefs = {};

  var state = {
    cameras: [{ id: "cam1", kind: "webcam", index: 0, url: "" }],
    activeId: "cam1"
  };

  /* ------------------------------------------------------------------ */
  /* Helpers                                                            */
  /* ------------------------------------------------------------------ */
  function toNumber(value, fallback) {
    var parsed = parseFloat(value);
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

  function cameraSource(cam) {
    if (cam.source_value !== undefined && cam.source_value !== null && cam.source_value !== "") {
      var sv = cam.source_value;
      if (typeof sv === "number") return Math.max(0, Math.round(sv));
      if (/^\d+$/.test(String(sv))) return Math.max(0, parseInt(sv, 10));
      return (sv || "").trim();
    }
    if (cam.kind === "webcam") {
      return Math.max(0, Math.round(toNumber(cam.index, 0)));
    }
    return (cam.url || "").trim();
  }

  function cameraLabel(cam, index) {
    if (cam.kind === "webcam") {
      return "CAM " + (index + 1) + " \u00b7 Webcam " + Math.max(0, Math.round(toNumber(cam.index, 0)));
    }
    return "CAM " + (index + 1) + " \u00b7 RTSP";
  }

  /* ------------------------------------------------------------------ */
  /* Annotated MJPEG viewports                                          */
  /* ------------------------------------------------------------------ */
  function clearViewTimers() {
    Object.keys(viewRefs).forEach(function (id) {
      var refs = viewRefs[id];
      if (refs.watchTimer) clearTimeout(refs.watchTimer);
      if (refs.snapTimer) clearInterval(refs.snapTimer);
    });
    viewRefs = {};
  }

  function startSnapshotMode(camId, refs) {
    if (refs.snapTimer) return;
    if (refs.watchTimer) {
      clearTimeout(refs.watchTimer);
      refs.watchTimer = null;
    }
    var tick = function () {
      refs.img.src = SNAPSHOT_URL + "?cam=" + encodeURIComponent(camId) + "&t=" + Date.now();
    };
    tick();
    refs.snapTimer = setInterval(tick, SNAPSHOT_INTERVAL_MS);
  }

  function attachStream(camId, refs) {
    refs.img.addEventListener("error", function () {
      startSnapshotMode(camId, refs);
    });
    refs.img.src = STREAM_URL + "?cam=" + encodeURIComponent(camId) + "&t=" + Date.now();
    refs.watchTimer = setTimeout(function () {
      refs.watchTimer = null;
      if (!refs.img.naturalWidth) {
        startSnapshotMode(camId, refs);
      }
    }, VIDEO_WATCHDOG_MS);
  }

  function renderViewports() {
    if (!ui.viewportGrid) return;
    clearViewTimers();
    ui.viewportGrid.innerHTML = "";

    state.cameras.forEach(function (cam, index) {
      var tile = document.createElement("div");
      tile.className = "cam-tile" + (cam.id === state.activeId ? " focused" : "");
      tile.dataset.cam = cam.id;

      var head = document.createElement("div");
      head.className = "cam-head";
      var title = document.createElement("span");
      title.className = "cam-title";
      title.textContent = cameraLabel(cam, index);
      var badge = document.createElement("span");
      badge.className = "cam-badge";
      badge.textContent = "OFFLINE";
      head.appendChild(title);
      head.appendChild(badge);

      var videoBox = document.createElement("div");
      videoBox.className = "video-box";
      var img = document.createElement("img");
      img.className = "cam-stream";
      img.alt = "Waiting for camera stream " + cam.id;
      videoBox.appendChild(img);

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

      ui.viewportGrid.appendChild(tile);

      var refs = {
        tile: tile,
        img: img,
        title: title,
        badge: badge,
        people: people,
        score: score,
        watchTimer: null,
        snapTimer: null
      };
      viewRefs[cam.id] = refs;
      attachStream(cam.id, refs);
    });

    updateFocusLabel();
  }

  function selectCamera(camId) {
    state.activeId = camId;
    currentAdviceKey = null;
    Object.keys(viewRefs).forEach(function (id) {
      viewRefs[id].tile.classList.toggle("focused", id === camId);
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
  /* Link indicator                                                     */
  /* ------------------------------------------------------------------ */
  function setLinkState(online) {
    if (!ui.linkStatus) return;
    ui.linkStatus.textContent = online
      ? "\u25cf CONTROL DESK SECURE LINK ONLINE"
      : "\u25cf LINK LOST \u2014 RECONNECTING";
    ui.linkStatus.classList.toggle("offline", !online);
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
      setLinkState(false);
      scheduleReconnect();
      return;
    }

    socket.addEventListener("open", function () {
      setLinkState(true);
      sendConfig(false);
    });

    socket.addEventListener("message", function (event) {
      var packet;
      try {
        packet = JSON.parse(event.data);
      } catch (error) {
        return;
      }
      handleTelemetry(packet);
    });

    socket.addEventListener("close", function () {
      setLinkState(false);
      scheduleReconnect();
    });

    socket.addEventListener("error", function () {
      setLinkState(false);
    });
  }

  function scheduleReconnect() {
    if (reconnectTimer) return;
    reconnectTimer = setTimeout(function () {
      reconnectTimer = null;
      connect();
    }, RECONNECT_DELAY_MS);
  }

  /* ------------------------------------------------------------------ */
  /* Multi-camera telemetry repaint                                     */
  /* ------------------------------------------------------------------ */
  function handleTelemetry(packet) {
    if (!packet || !Array.isArray(packet.cameras)) return;

    if (!backendAdopted && !camerasEdited && packet.cameras.length > 0) {
      backendAdopted = true;
      adoptBackendCameras(packet.cameras);
      return;
    }

    packet.cameras.forEach(function (cam) {
      updateTile(cam);
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
        appendLedgerRow(cam);
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
      return {
        id: cam.id || "cam" + (index + 1),
        kind: isIndex ? "webcam" : "rtsp",
        index: isIndex ? Math.max(0, parseInt(source, 10) || 0) : 0,
        url: isIndex ? "" : String(source || "")
      };
    });
    if (state.cameras.length === 0) {
      state.cameras = [{ id: "cam1", kind: "webcam", index: 0, url: "" }];
    }
    camSeq = Math.max(camSeq, state.cameras.length);
    var exists = state.cameras.some(function (cam) {
      return cam.id === state.activeId;
    });
    if (!exists) state.activeId = state.cameras[0].id;
    renderChannels();
    renderViewports();
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
  }

  function updateFocus(cam) {
    var level = normalizeLevel(cam.risk_level);
    var color = riskColor(level);

    ui.headcount.textContent = String(Math.max(0, Math.round(toNumber(cam.people_now, 0))));
    ui.risk.textContent = level;
    ui.risk.style.color = color;
    ui.risk.style.textShadow = "0 0 18px " + color;

    var timestamp = toNumber(cam.timestamp, 0);
    var latency = timestamp > 0 ? Date.now() - timestamp : 12;
    if (!isFinite(latency) || latency < 0 || latency > 1000) latency = 12;
    latency = Math.max(1, Math.round(latency));
    ui.latency.textContent = latency + "ms";
    ui.latency.style.color = latency > 45 ? RISK_COLORS.CRITICAL : "#00f2fe";

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

  function appendLedgerRow(cam) {
    if (!ui.ledger) return;

    var level = normalizeLevel(cam.risk_level);
    var header = ui.ledger.firstElementChild;
    var row = document.createElement("div");
    row.className = "log-row";

    var timeCell = document.createElement("div");
    var stamp = toNumber(cam.timestamp, Date.now());
    timeCell.textContent = new Date(stamp).toLocaleTimeString();

    var camCell = document.createElement("div");
    camCell.textContent = cam.label || cam.id;
    camCell.style.color = "#00f2fe";

    var levelCell = document.createElement("div");
    levelCell.textContent = level;
    levelCell.style.color = riskColor(level);
    levelCell.style.fontWeight = "bold";

    var actionCell = document.createElement("div");
    var actionKey = typeof cam.action === "string" ? cam.action.toUpperCase() : "NONE";
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
  /* Camera channel editor                                              */
  /* ------------------------------------------------------------------ */
  function scheduleCameras() {
    camerasEdited = true;
    if (cameraDebounce) clearTimeout(cameraDebounce);
    cameraDebounce = setTimeout(function () {
      cameraDebounce = null;
      sendConfig(true);
    }, CONFIG_DEBOUNCE_MS);
  }

  function addCamera() {
    if (state.cameras.length >= MAX_CAMERAS) return;
    camSeq += 1;
    var id = "cam" + camSeq;
    state.cameras.push({ id: id, kind: "rtsp", index: 0, url: "" });
    renderChannels();
    renderViewports();
    scheduleCameras();
  }

  function removeCamera(camId) {
    if (state.cameras.length <= 1) return;
    state.cameras = state.cameras.filter(function (cam) {
      return cam.id !== camId;
    });
    if (state.activeId === camId) {
      state.activeId = state.cameras[0].id;
    }
    renderChannels();
    renderViewports();
    scheduleCameras();
  }

  function renderChannels() {
    if (!ui.channelList) return;
    ui.channelList.innerHTML = "";

    state.cameras.forEach(function (cam, index) {
      var row = document.createElement("div");
      row.className = "channel-row";
      row.dataset.cam = cam.id;

      var head = document.createElement("div");
      head.className = "channel-head";
      var name = document.createElement("span");
      name.textContent = "CHANNEL " + (index + 1);
      var remove = document.createElement("button");
      remove.type = "button";
      remove.className = "channel-remove";
      remove.textContent = "\u2715";
      remove.addEventListener("click", function () {
        removeCamera(cam.id);
      });
      head.appendChild(name);
      head.appendChild(remove);

      var controls = document.createElement("div");
      controls.className = "channel-controls";

      var kind = document.createElement("select");
      kind.className = "channel-kind";
      kind.innerHTML = '<option value="webcam">Webcam</option><option value="rtsp">RTSP / HTTP</option>';
      kind.value = cam.kind;

      var indexInput = document.createElement("input");
      indexInput.type = "number";
      indexInput.min = "0";
      indexInput.step = "1";
      indexInput.className = "channel-index";
      indexInput.value = String(cam.index);

      var urlInput = document.createElement("input");
      urlInput.type = "text";
      urlInput.className = "channel-url";
      urlInput.placeholder = "rtsp://192.168.1.50:554/stream1";
      urlInput.value = cam.url;

      var syncVisibility = function () {
        var isWebcam = cam.kind === "webcam";
        indexInput.style.display = isWebcam ? "block" : "none";
        urlInput.style.display = isWebcam ? "none" : "block";
      };
      syncVisibility();

      kind.addEventListener("change", function () {
        cam.kind = kind.value;
        syncVisibility();
        scheduleCameras();
      });
      indexInput.addEventListener("input", function () {
        cam.index = Math.max(0, Math.round(toNumber(indexInput.value, 0)));
        scheduleCameras();
      });
      urlInput.addEventListener("input", function () {
        cam.url = urlInput.value;
        scheduleCameras();
      });

      controls.appendChild(kind);
      controls.appendChild(indexInput);
      controls.appendChild(urlInput);
      row.appendChild(head);
      row.appendChild(controls);
      ui.channelList.appendChild(row);
    });
  }

  /* ------------------------------------------------------------------ */
  /* Configuration emission                                             */
  /* ------------------------------------------------------------------ */
  function camerasPayload() {
    return state.cameras.map(function (cam, index) {
      return {
        id: cam.id,
        label: cameraLabel(cam, index),
        source: cameraSource(cam)
      };
    });
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
    socket.send(JSON.stringify(buildConfig(includeCameras)));
  }

  function bindWeight(slider, label) {
    if (!slider || !label) return;
    slider.addEventListener("input", function () {
      label.textContent = weightFraction(slider).toFixed(2);
      sendConfig(false);
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
        sendConfig(false);
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
        sendConfig(false);
      });
    }
  }

  /* ------------------------------------------------------------------ */
  /* Boot                                                               */
  /* ------------------------------------------------------------------ */
  function init() {
    renderChannels();
    renderViewports();
    bindControls();
    renderAdvice("SAFE");
    setLinkState(false);
    connect();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
