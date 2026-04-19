/**
 * DeepStream WebRTC KLV Dashboard — Frontend Application
 *
 * Handles:
 *  - Stream management (add/remove via REST API)
 *  - WebRTC peer connections per stream (signaling over WebSocket)
 *  - KLV telemetry subscription and display
 *  - Leaflet map with real-time drone position
 *  - Sparkline chart for KLV packet rate
 */

"use strict";

// ── Config ────────────────────────────────────────────────────────────────────
const API_BASE  = `${location.origin}`;
const WS_BASE   = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}`;

// ── State ─────────────────────────────────────────────────────────────────────
const state = {
  streams: {},         // stream_id → { config, pc, ws, videoEl, klvWs, status }
  activeTelemetry: "", // stream_id currently shown in telemetry panel
  followMap: true,
  mapInitialized: false,
};

// ── Map ───────────────────────────────────────────────────────────────────────
let map, droneMarkers = {}, droneTracks = {}, frameCenters = {};

function initMap() {
  map = L.map("map", {
    center: [32.08, 34.78],
    zoom: 13,
    zoomControl: true,
  });

  // Dark tile layer
  L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
    attribution: "© OpenStreetMap, © CartoDB",
    maxZoom: 20,
  }).addTo(map);

  state.mapInitialized = true;
}

function updateDroneOnMap(streamId, klv) {
  const lat = klv.lat;
  const lon = klv.lon;
  if (lat == null || lon == null) return;

  // Create or update marker
  if (!droneMarkers[streamId]) {
    const el = document.createElement("div");
    el.innerHTML = `<div class="drone-icon" id="drone-icon-${streamId}">✈</div>`;
    el.style.textAlign = "center";

    const icon = L.divIcon({
      className: "",
      html: el.innerHTML,
      iconSize: [32, 32],
      iconAnchor: [16, 16],
    });

    droneMarkers[streamId] = L.marker([lat, lon], { icon })
      .addTo(map)
      .bindPopup(`
        <b>Stream: ${streamId}</b><br>
        Lat: ${lat.toFixed(6)}<br>
        Lon: ${lon.toFixed(6)}<br>
        Alt: ${klv.alt != null ? klv.alt.toFixed(1) + " m" : "—"}
      `);

    // Polyline track
    droneTracks[streamId] = L.polyline([[lat, lon]], {
      color: getStreamColor(streamId),
      weight: 2,
      opacity: 0.7,
      dashArray: "4 4",
    }).addTo(map);
  } else {
    const marker = droneMarkers[streamId];
    marker.setLatLng([lat, lon]);
    marker.setPopupContent(`
      <b>Stream: ${streamId}</b><br>
      Lat: ${lat.toFixed(6)}<br>
      Lon: ${lon.toFixed(6)}<br>
      Alt: ${klv.alt != null ? klv.alt.toFixed(1) + " m" : "—"}<br>
      Heading: ${klv.heading != null ? klv.heading.toFixed(1) + "°" : "—"}<br>
      Speed: ${klv.airspeed != null ? klv.airspeed.toFixed(1) + " m/s" : "—"}
    `);
    droneTracks[streamId].addLatLng([lat, lon]);
  }

  // Rotate icon based on heading
  if (klv.heading != null) {
    const iconEl = document.getElementById(`drone-icon-${streamId}`);
    if (iconEl) iconEl.style.transform = `rotate(${klv.heading}deg)`;
  }

  // Frame center marker
  if (klv.frame_lat != null && klv.frame_lon != null) {
    if (!frameCenters[streamId]) {
      frameCenters[streamId] = L.circleMarker([klv.frame_lat, klv.frame_lon], {
        radius: 6, color: "#f59e0b", fillColor: "#f59e0b",
        fillOpacity: 0.4, weight: 1.5,
      }).addTo(map).bindTooltip("Frame center");

      // Draw line from drone to frame center
      frameCenters[streamId + "_line"] = L.polyline(
        [[lat, lon], [klv.frame_lat, klv.frame_lon]],
        { color: "#f59e0b", weight: 1, opacity: 0.5, dashArray: "3 5" }
      ).addTo(map);
    } else {
      frameCenters[streamId].setLatLng([klv.frame_lat, klv.frame_lon]);
      frameCenters[streamId + "_line"]?.setLatLngs([
        [lat, lon], [klv.frame_lat, klv.frame_lon]
      ]);
    }
  }

  // Auto-follow
  if (state.followMap && streamId === state.activeTelemetry) {
    map.panTo([lat, lon], { animate: true, duration: 0.5 });
  }
}

const STREAM_COLORS = ["#4f7ef8", "#22c55e", "#f59e0b", "#ef4444", "#14b8a6", "#a855f7"];
function getStreamColor(streamId) {
  const ids = Object.keys(state.streams);
  return STREAM_COLORS[ids.indexOf(streamId) % STREAM_COLORS.length];
}

// ── WebRTC ─────────────────────────────────────────────────────────────────────

const RTC_CONFIG = {
  // STUN is needed so Chrome sends real IP candidates instead of mDNS (.local)
  // addresses. GStreamer cannot resolve mDNS, so without STUN all ICE candidates
  // are filtered out and DTLS never completes.
  iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
};

/**
 * modify_sdp — ported from the working legacy client.
 * Patches the GStreamer H264 offer so Chrome/Firefox can decode:
 *  - forces profile-level-id=42001f (constrained-baseline)
 *  - forces level-asymmetry-allowed=1
 * Returns [modifiedOffer, revertFn] so we can un-patch the answer.
 */
function modify_sdp(data) {
  const REGEX_H264   = /H264\/90000/;
  const REGEX_level  = /level-asymmetry-allowed=([^;"\s\r\n]+)/;
  const REGEX_profile= /profile-level-id=([^;"\s\r\n]+)/;

  if (!data.sdp || !REGEX_H264.test(data.sdp)) {
    return [data, d => d];
  }

  let sdp = data.sdp;
  let origLevel, origProfile;

  const mLevel = sdp.match(REGEX_level);
  if (mLevel) {
    origLevel = mLevel[1];
    sdp = sdp.replace(REGEX_level, "level-asymmetry-allowed=1");
  }

  const mProfile = sdp.match(REGEX_profile);
  if (mProfile) {
    origProfile = mProfile[1];
    sdp = sdp.replace(REGEX_profile,
      `profile-level-id=42001f${origLevel ? "" : ";level-asymmetry-allowed=1"}`);
  }

  const modified = { type: data.type, sdp };

  const revert = (d) => {
    if (!d.sdp) return d;
    let s = d.sdp;
    if (origLevel) {
      s = s.replace("level-asymmetry-allowed=1", `level-asymmetry-allowed=${origLevel}`);
    } else {
      s = s.replace(/;?level-asymmetry-allowed=1/g, "");
    }
    if (origProfile) {
      s = s.replace("profile-level-id=42001f", `profile-level-id=${origProfile}`);
    }
    return { type: d.type, sdp: s };
  };

  return [modified, revert];
}

function createWebRTCConnection(streamId) {
  const stream = state.streams[streamId];
  if (!stream) return;

  stream.status = "connecting";
  updateStreamCard(streamId);

  // Signaling WebSocket
  const ws = new WebSocket(`${WS_BASE}/ws/signaling/${streamId}`);
  stream.ws = ws;

  // PeerConnection — no addTransceiver here.
  // Server sends offer first; adding a transceiver before setRemoteDescription
  // creates a duplicate m-line which breaks negotiation.
  const pc = new RTCPeerConnection(RTC_CONFIG);
  stream.pc = pc;

  // Receive video track — identical approach to working test_webrtc.py
  pc.ontrack = (event) => {
    console.log(`[${streamId}] ontrack kind=${event.track.kind} streams=${event.streams.length}`);
    if (event.track.kind !== "video") return;

    const videoEl = stream.videoEl;
    videoEl.muted = true;

    // Use streams[0] if available; otherwise wrap track in new MediaStream.
    // GStreamer may not send msid so streams[0] could be empty — check readyState.
    if (event.streams && event.streams[0] && event.streams[0].getTracks().length > 0) {
      videoEl.srcObject = event.streams[0];
    } else {
      // No stream attached to track (missing msid) — create one manually
      const ms = new MediaStream();
      ms.addTrack(event.track);
      videoEl.srcObject = ms;
    }

    // Also handle track mute/unmute events
    event.track.onunmute = () => {
      console.log(`[${streamId}] track unmuted — retrying play`);
      videoEl.play().catch(() => {});
    };

    const tryPlay = () => {
      videoEl.play().then(() => {
        console.log(`[${streamId}] ▶ video playing`);
        stream.status = "live";
        updateStreamCard(streamId);
        updateVideoTileBadge(streamId, "live");
        showToast(`Stream ${stream.config.name} — live ✓`, "success");
      }).catch(e => {
        if (e.name === "AbortError") {
          // Another load interrupted — retry once after short delay
          console.warn(`[${streamId}] play() AbortError — retrying in 200ms`);
          setTimeout(tryPlay, 200);
          return;
        }
        if (e.name === "NotAllowedError") {
          console.warn(`[${streamId}] play() blocked by autoplay — showing overlay`);
          _showPlayOverlay(streamId);
          return;
        }
        console.error(`[${streamId}] play() error:`, e.name, e.message);
      });
    };

    // Small delay so srcObject assignment fully propagates before play()
    setTimeout(tryPlay, 50);

    // Debug: log RTP stats after 4s — check framesDecoded in console
    setTimeout(async () => {
      try {
        const stats = await pc.getStats();
        stats.forEach(r => {
          if (r.type === "inbound-rtp" && r.kind === "video") {
            console.log(`[${streamId}] RTP stats — framesDecoded:${r.framesDecoded} bytesReceived:${r.bytesReceived} packetsLost:${r.packetsLost} jitter:${r.jitter?.toFixed(3)}`);
            if (r.framesDecoded === 0 && r.bytesReceived > 0) {
              console.warn(`[${streamId}] ⚠ bytes arriving but 0 frames decoded — likely codec/profile mismatch`);
            } else if (r.bytesReceived === 0) {
              console.warn(`[${streamId}] ⚠ 0 bytes received — RTP not reaching browser`);
            }
          }
        });
      } catch(_) {}
    }, 4000);
  };



  pc.onconnectionstatechange = () => {
    const cs = pc.connectionState;
    console.log(`[${streamId}] PC state: ${cs}`);
    if (cs === "connected") {
      console.log(`[${streamId}] ✅ WebRTC connected`);
      stream._reconnectAttempts = 0;
    }
    // "disconnected" is transient (network blip) — wait, don't reconnect yet.
    // Only reconnect on hard "failed". This stops the AbortError loop.
    if (cs === "failed") {
      stream.status = "error";
      updateStreamCard(streamId);
      updateVideoTileBadge(streamId, "error");
      const attempts = (stream._reconnectAttempts || 0) + 1;
      stream._reconnectAttempts = attempts;
      const delay = Math.min(1000 * attempts, 8000); // back-off: 1s, 2s, 4s, 8s max
      console.warn(`[${streamId}] PC failed — reconnect attempt ${attempts} in ${delay}ms`);
      setTimeout(() => reconnectStream(streamId), delay);
    }
  };

  pc.oniceconnectionstatechange = () => {
    console.log(`[${streamId}] ICE: ${pc.iceConnectionState}`);
    if (pc.iceConnectionState === "failed") {
      console.warn(`[${streamId}] ICE failed — restarting`);
      pc.restartIce();
    }
  };

  pc.onicegatheringstatechange = () => {
    console.log(`[${streamId}] ICE gathering: ${pc.iceGatheringState}`);
  };

  pc.onicecandidate = (event) => {
    if (event.candidate) {
      console.log(`[${streamId}] ICE candidate: ${event.candidate.candidate.substring(0,60)}`);
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({
          type: "ice",
          data: {
            candidate: event.candidate.candidate,
            sdpMLineIndex: event.candidate.sdpMLineIndex,
          },
        }));
      }
    } else {
      console.log(`[${streamId}] ICE gathering complete`);
    }
  };

  // Signaling messages from server
  ws.onopen = () => {
    ws.send(JSON.stringify({ type: "join" }));
  };

  ws.onmessage = async (event) => {
    const msg = JSON.parse(event.data);

    // Support both formats:
    //   new pipeline: {type:"offer", sdp:"..."}
    //   old pipeline: {type:"sdp", data:{type:"offer", sdp:"..."}}
    const isOffer = (msg.type === "offer") ||
                    (msg.type === "sdp" && msg.data?.type === "offer");
    const offerSdp = msg.sdp || msg.data?.sdp;

    if (isOffer && offerSdp) {
      try {
        console.log(`[${streamId}] SDP offer received`);
        const offerObj = { type: "offer", sdp: offerSdp };
        const dirMatch = offerSdp.match(/a=(sendonly|recvonly|sendrecv|inactive)/);
        if (dirMatch) console.log(`[${streamId}] Offer direction: ${dirMatch[1]}`);

        const [offerPatched] = modify_sdp(offerObj);

        await pc.setRemoteDescription(new RTCSessionDescription(offerPatched));
        const answer = await pc.createAnswer();
        await pc.setLocalDescription(answer);

        const answerSetup = answer.sdp.match(/a=setup:(\S+)/)?.[1];
        const answerDir   = answer.sdp.match(/a=(sendonly|recvonly|sendrecv|inactive)/)?.[1];
        console.log(`[${streamId}] Answer setup: ${answerSetup}, direction: ${answerDir}`);

        // Send answer as-is — do NOT revert SDP, it can corrupt a=setup
        ws.send(JSON.stringify({ type: "sdp", data: { type: "answer", sdp: answer.sdp } }));
        console.log(`[${streamId}] SDP answer sent`);
      } catch(e) {
        console.error(`[${streamId}] SDP negotiation error:`, e.name, e.message);
      }

    } else if (msg.type === "ice") {
      // Support both formats: {data:{candidate,sdpMLineIndex}} and {candidate,sdpMLineIndex}
      const iceData = msg.data || msg;
      if (!iceData.candidate) return;
      try {
        await pc.addIceCandidate(new RTCIceCandidate({
          candidate: iceData.candidate,
          sdpMLineIndex: iceData.sdpMLineIndex,
        }));
      } catch (e) {
        console.warn("ICE candidate error:", e);
      }

    } else if (msg.type === "error") {
      showToast(`Stream error: ${msg.message}`, "error");
      stream.status = "error";
      updateStreamCard(streamId);
    }
  };

  ws.onerror = (e) => {
    console.error(`[${streamId}] Signaling WS error`, e);
    stream.status = "error";
    updateStreamCard(streamId);
  };

  ws.onclose = () => {
    console.log(`[${streamId}] Signaling WS closed`);
  };
}

function reconnectStream(streamId) {
  const stream = state.streams[streamId];
  if (!stream) return;
  // Guard: if a reconnect is already in flight, skip
  if (stream._reconnecting) return;
  stream._reconnecting = true;

  console.log(`[${streamId}] reconnecting (attempt ${stream._reconnectAttempts || 1})...`);
  showToast(`Reconnecting ${stream.config?.name || streamId}...`, "info", 2000);

  // Stop video element cleanly before closing PC
  // This prevents AbortError from a pending play() call
  const vid = stream.videoEl;
  if (vid) {
    vid.pause();
    vid.srcObject = null;
  }

  stream.ws?.close();
  try { stream.pc?.close(); } catch(_) {}
  stream.pc = null;
  stream.ws = null;
  stream.status = "connecting";
  updateStreamCard(streamId);
  updateVideoTileBadge(streamId, "connecting");

  setTimeout(() => {
    stream._reconnecting = false;
    if (state.streams[streamId]) {
      createWebRTCConnection(streamId);
    }
  }, 500);
}

function _showPlayOverlay(streamId) {
  const tile = document.getElementById(`tile-${streamId}`);
  if (!tile || tile.querySelector(".play-overlay")) return;
  const ov = document.createElement("div");
  ov.className = "play-overlay";
  ov.style.cssText = "position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;background:rgba(0,0,0,0.7);cursor:pointer;z-index:10;border-radius:8px";
  ov.innerHTML = `
    <div style="font-size:48px;margin-bottom:12px">▶</div>
    <button style="padding:14px 32px;font-size:16px;border-radius:8px;border:none;background:#4f7ef8;color:#fff;cursor:pointer;font-weight:600">Click to Play</button>
    <div style="margin-top:8px;font-size:11px;color:#aaa">Browser blocked autoplay</div>
  `;
  ov.onclick = () => {
    const stream = state.streams[streamId];
    if (!stream?.videoEl) return;
    stream.videoEl.muted = true;
    stream.videoEl.play()
      .then(() => { ov.remove(); console.log(`[${streamId}] ▶ playing after click`); })
      .catch(e => console.error(`[${streamId}] click play failed:`, e));
  };
  tile.appendChild(ov);
}

function destroyWebRTCConnection(streamId) {
  const stream = state.streams[streamId];
  if (!stream) return;
  stream.ws?.close();
  stream.pc?.close();
  stream.klvWs?.close();
}

// ── KLV Telemetry WebSocket ────────────────────────────────────────────────────

// Sparkline data
const klvRateHistory = Array(30).fill(0);
let klvCountThisSec = 0;
setInterval(() => {
  klvRateHistory.shift();
  klvRateHistory.push(klvCountThisSec);
  klvCountThisSec = 0;
  drawSparkline();
}, 1000);

function subscribeKLV(streamId) {
  const stream = state.streams[streamId];
  if (!stream) return;

  const ws = new WebSocket(`${WS_BASE}/ws/telemetry/${streamId}`);
  stream.klvWs = ws;

  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === "klv") {
      klvCountThisSec++;
      updateDroneOnMap(streamId, msg.data);
      if (streamId === state.activeTelemetry) {
        updateTelemetryPanel(msg.data);
      }
    }
  };

  ws.onerror = () => console.warn(`[${streamId}] KLV WS error`);
}

// ── Telemetry Panel ────────────────────────────────────────────────────────────

function updateTelemetryPanel(klv) {
  const fmt = (v, unit = "", dp = 4) =>
    v != null ? `${typeof v === "number" ? v.toFixed(dp) : v}${unit}` : "—";

  const set = (id, val, flash = true) => {
    const el = document.getElementById(id);
    if (!el) return;
    const txt = typeof val === "string" ? val : fmt(val);
    if (el.textContent === txt) return;
    el.textContent = txt;
    if (flash) {
      el.classList.add("updated");
      setTimeout(() => el.classList.remove("updated"), 400);
    }
  };

  set("t-lat",  klv.lat,       false);
  set("t-lon",  klv.lon,       false);
  set("t-alt",  fmt(klv.alt, " m", 1));
  set("t-heading", fmt(klv.heading, "°", 1));
  set("t-pitch",   fmt(klv.pitch, "°", 2));
  set("t-roll",    fmt(klv.roll, "°", 2));
  set("t-airspeed", fmt(klv.airspeed, " m/s", 1));
  set("t-slant",    fmt(klv.slant_range, " m", 0));
  set("t-platform", klv.platform || "—");
  set("t-mission",  klv.mission_id || "—");
  set("t-flat",  klv.frame_lat, false);
  set("t-flon",  klv.frame_lon, false);
  set("t-hfov",  fmt(klv.hfov, "°", 1));

  // Timestamp
  if (klv.timestamp) {
    const d = new Date(klv.timestamp / 1000);
    set("t-ts", d.toISOString().substring(11, 23));
  }

  // Compass needle
  if (klv.heading != null) {
    const needle = document.getElementById("compass-needle");
    if (needle) needle.style.transform = `translateX(-50%) translateY(-100%) rotate(${klv.heading}deg)`;
  }

  // Raw JSON
  const raw = document.getElementById("raw-json");
  if (raw && !raw.closest(".collapsible.collapsed")) {
    raw.textContent = JSON.stringify(klv, null, 2);
  }
}

function drawSparkline() {
  const canvas = document.getElementById("klv-sparkline");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const w = canvas.clientWidth || 200;
  const h = canvas.clientHeight || 50;
  canvas.width = w;
  canvas.height = h;

  ctx.clearRect(0, 0, w, h);

  const max = Math.max(...klvRateHistory, 1);
  const step = w / (klvRateHistory.length - 1);

  ctx.beginPath();
  ctx.strokeStyle = "#14b8a6";
  ctx.lineWidth = 1.5;
  ctx.lineJoin = "round";

  klvRateHistory.forEach((v, i) => {
    const x = i * step;
    const y = h - (v / max) * (h - 4) - 2;
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  });
  ctx.stroke();

  // Fill
  ctx.lineTo((klvRateHistory.length - 1) * step, h);
  ctx.lineTo(0, h);
  ctx.closePath();
  ctx.fillStyle = "rgba(20,184,166,0.12)";
  ctx.fill();

  // Current rate label
  const cur = klvRateHistory[klvRateHistory.length - 1];
  ctx.fillStyle = "#14b8a6";
  ctx.font = "10px monospace";
  ctx.fillText(`${cur} pkt/s`, 4, 12);
}

// ── Stream Management ─────────────────────────────────────────────────────────

async function fetchStreams() {
  try {
    const res = await fetch(`${API_BASE}/api/streams`);
    const data = await res.json();
    return data.streams || [];
  } catch (e) {
    return [];
  }
}

async function addStreamToServer(uri, name, width, height) {
  const res = await fetch(`${API_BASE}/api/streams`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ uri, name, width: +width, height: +height }),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

async function removeStreamFromServer(streamId) {
  await fetch(`${API_BASE}/api/streams/${streamId}`, { method: "DELETE" });
}

function addStreamToUI(config) {
  const { stream_id, uri, name } = config;
  if (state.streams[stream_id]) return;

  // Create video element
  const videoEl = document.createElement("video");
  videoEl.autoplay    = true;
  videoEl.muted       = true;
  videoEl.playsInline = true;
  videoEl.id          = `video-${stream_id}`;
  videoEl.style.cssText = "width:100%;height:100%;object-fit:contain;display:block;background:#000";

  state.streams[stream_id] = { config, videoEl, status: "connecting", pc: null, ws: null, klvWs: null };

  // Add to video grid
  addVideoTile(stream_id, name, videoEl);

  // Add to sidebar
  addStreamCard(stream_id, config);

  // Add to telemetry selector
  const sel = document.getElementById("telemetry-stream-select");
  const opt = document.createElement("option");
  opt.value = stream_id;
  opt.textContent = name || stream_id;
  sel.appendChild(opt);

  // Auto-select if first stream
  if (Object.keys(state.streams).length === 1) {
    state.activeTelemetry = stream_id;
    sel.value = stream_id;
  }

  // Start WebRTC + KLV
  createWebRTCConnection(stream_id);
  subscribeKLV(stream_id);

  // Update stream count badge
  document.getElementById("stream-count").textContent = Object.keys(state.streams).length;

  // Remove empty state
  document.querySelector(".no-video")?.remove();
}

function removeStreamFromUI(streamId) {
  destroyWebRTCConnection(streamId);

  // Remove video tile
  document.getElementById(`tile-${streamId}`)?.remove();

  // Remove sidebar card
  document.getElementById(`card-${streamId}`)?.remove();

  // Remove from telemetry selector
  document.querySelector(`#telemetry-stream-select option[value="${streamId}"]`)?.remove();

  // Remove map markers
  if (droneMarkers[streamId]) { map.removeLayer(droneMarkers[streamId]); delete droneMarkers[streamId]; }
  if (droneTracks[streamId])  { map.removeLayer(droneTracks[streamId]);  delete droneTracks[streamId]; }
  if (frameCenters[streamId]) { map.removeLayer(frameCenters[streamId]); delete frameCenters[streamId]; }
  if (frameCenters[streamId + "_line"]) { map.removeLayer(frameCenters[streamId + "_line"]); delete frameCenters[streamId + "_line"]; }

  delete state.streams[streamId];

  if (state.activeTelemetry === streamId) {
    const remaining = Object.keys(state.streams)[0];
    state.activeTelemetry = remaining || "";
    document.getElementById("telemetry-stream-select").value = remaining || "";
  }

  document.getElementById("stream-count").textContent = Object.keys(state.streams).length;

  // Show empty state if no streams
  if (Object.keys(state.streams).length === 0) {
    document.getElementById("video-grid").innerHTML = `
      <div class="no-video">
        <div class="no-video-inner">
          <div class="no-video-icon">▶</div>
          <p>Add a stream to start watching</p>
        </div>
      </div>`;
    document.getElementById("stream-list").innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">📡</div>
        <p>No streams yet.<br>Add an RTSP or UDP stream.</p>
      </div>`;
  }
}

// ── DOM helpers ────────────────────────────────────────────────────────────────

function addVideoTile(streamId, name, videoEl) {
  const grid = document.getElementById("video-grid");
  const tile = document.createElement("div");
  tile.className = "video-tile";
  tile.id = `tile-${streamId}`;

  // Build overlay and button WITHOUT touching videoEl via innerHTML.
  // innerHTML+= destroys existing DOM nodes including the video element,
  // which means srcObject is lost and the video never shows.
  const overlay = document.createElement("div");
  overlay.className = "video-tile-overlay";
  overlay.innerHTML = `<span class="video-tile-name">${name || streamId}</span><span class="video-tile-badge connecting" id="badge-${streamId}">Connecting</span>`;

  const fsBtn = document.createElement("button");
  fsBtn.className = "video-fullscreen-btn";
  fsBtn.textContent = "⛶";
  fsBtn.onclick = () => toggleFullscreen(streamId);

  tile.appendChild(videoEl);
  tile.appendChild(overlay);
  tile.appendChild(fsBtn);
  grid.appendChild(tile);
}

function updateVideoTileBadge(streamId, status) {
  const badge = document.getElementById(`badge-${streamId}`);
  if (!badge) return;
  badge.className = `video-tile-badge ${status}`;
  badge.textContent = status === "live" ? "LIVE" : status === "error" ? "Error" : "Connecting";
}

function addStreamCard(streamId, config) {
  const list = document.getElementById("stream-list");
  document.querySelector(".empty-state")?.remove();

  const card = document.createElement("div");
  card.className = "stream-card";
  card.id = `card-${streamId}`;
  card.innerHTML = `
    <div class="stream-card-name">${config.name || streamId}</div>
    <div class="stream-card-uri">${config.uri}</div>
    <div class="stream-card-footer">
      <div class="stream-status">
        <div class="stream-status-dot connecting" id="sdot-${streamId}"></div>
        <span id="stext-${streamId}">Connecting</span>
      </div>
      <button class="stream-remove-btn" onclick="UI.removeStream('${streamId}')" title="Remove stream">✕</button>
    </div>
  `;
  card.onclick = (e) => {
    if (e.target.classList.contains("stream-remove-btn")) return;
    state.activeTelemetry = streamId;
    document.getElementById("telemetry-stream-select").value = streamId;
    document.querySelectorAll(".stream-card").forEach(c => c.classList.remove("active"));
    card.classList.add("active");
  };
  list.appendChild(card);
}

function updateStreamCard(streamId) {
  const stream = state.streams[streamId];
  if (!stream) return;
  const dot = document.getElementById(`sdot-${streamId}`);
  const txt = document.getElementById(`stext-${streamId}`);
  if (!dot || !txt) return;
  const { status } = stream;
  dot.className = `stream-status-dot ${status === "live" ? "" : status}`;
  txt.textContent = status === "live" ? "Live" : status === "error" ? "Error" : "Connecting";
}

function toggleFullscreen(streamId) {
  const video = document.getElementById(`video-${streamId}`);
  if (video) {
    video.requestFullscreen?.() || video.webkitRequestFullscreen?.();
  }
}

// ── Toast ──────────────────────────────────────────────────────────────────────

function showToast(message, type = "info", duration = 3500) {
  const container = document.getElementById("toast-container");
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(() => toast.remove(), duration);
}

// ── UI Object (exposed to HTML) ────────────────────────────────────────────────

const UI = {
  openAddStreamModal() {
    document.getElementById("add-stream-modal").classList.add("open");
    document.getElementById("stream-uri").focus();
  },
  closeAddStreamModal() {
    document.getElementById("add-stream-modal").classList.remove("open");
  },
  closeModal(e) {
    if (e.target.id === "add-stream-modal") this.closeAddStreamModal();
  },

  async addStream() {
    const uri = document.getElementById("stream-uri").value.trim();
    if (!uri) { showToast("Please enter a stream URL", "error"); return; }

    const name   = document.getElementById("stream-name").value.trim() || uri.split("/").pop();
    const width  = document.getElementById("stream-width").value;
    const height = document.getElementById("stream-height").value;

    try {
      showToast("Adding stream...", "info", 2000);
      const config = await addStreamToServer(uri, name, width, height);
      addStreamToUI(config);
      this.closeAddStreamModal();
      showToast(`Stream "${name}" added`, "success");
      document.getElementById("global-status").classList.add("connected");
      document.getElementById("global-status-text").textContent = "Connected";
    } catch (e) {
      showToast(`Failed to add stream: ${e.message}`, "error");
    }
  },

  async removeStream(streamId) {
    removeStreamFromUI(streamId);
    try { await removeStreamFromServer(streamId); } catch (_) {}
    showToast("Stream removed", "info");
  },

  setPreset(uri) {
    document.getElementById("stream-uri").value = uri;
  },

  setVideoColumns(n) {
    const grid = document.getElementById("video-grid");
    grid.className = `video-grid${n > 1 ? ` cols-${n}` : ""}`;
    document.querySelectorAll(".view-btn").forEach(b => {
      b.classList.toggle("active", +b.dataset.cols === n);
    });
  },

  centerMap() {
    const markers = Object.values(droneMarkers);
    if (markers.length === 0) return;
    if (markers.length === 1) {
      map.setView(markers[0].getLatLng(), 14);
    } else {
      const group = L.featureGroup(markers);
      map.fitBounds(group.getBounds().pad(0.2));
    }
  },

  clearTracks() {
    Object.values(droneTracks).forEach(t => t.setLatLngs([]));
    showToast("Tracks cleared", "info");
  },

  toggleFollow(on) {
    state.followMap = on;
  },

  switchTelemetryStream(streamId) {
    state.activeTelemetry = streamId;
    document.querySelectorAll(".stream-card").forEach(c => {
      c.classList.toggle("active", c.id === `card-${streamId}`);
    });
  },

  toggleLayout() {
    const layout = document.getElementById("main-layout");
    layout.classList.toggle("compact");
  },

  toggleRaw() {
    const group = document.getElementById("raw-group");
    group.classList.toggle("collapsed");
    document.getElementById("raw-arrow").textContent =
      group.classList.contains("collapsed") ? "▶" : "▼";
  },
};

// ── Keyboard shortcuts ─────────────────────────────────────────────────────────
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") UI.closeAddStreamModal();
  if ((e.ctrlKey || e.metaKey) && e.key === "k") { e.preventDefault(); UI.openAddStreamModal(); }
});

// ── Enter in modal to submit ───────────────────────────────────────────────────
document.getElementById("stream-uri").addEventListener("keydown", (e) => {
  if (e.key === "Enter") UI.addStream();
});

// ── Init ──────────────────────────────────────────────────────────────────────
(async function init() {
  initMap();

  // Load existing streams from server
  const existing = await fetchStreams();
  for (const config of existing) {
    addStreamToUI(config);
  }

  if (existing.length > 0) {
    document.getElementById("global-status").classList.add("connected");
    document.getElementById("global-status-text").textContent = "Connected";
  }

  // Draw initial sparkline
  drawSparkline();

  console.log("DeepStream WebRTC Dashboard initialized");
  console.log("Keyboard shortcuts: Ctrl+K = Add stream, Esc = Close modal");
})();
