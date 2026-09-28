"""
SenseMaker AI - Powered by Jev
Streamlit prototype: live semantic sorting with physics, via the TypeSafe AI (Jev) API.

* You type -> after a short pause (no Enter needed) the search term goes to Python.
* Python makes ONE backend-to-backend request to Jev (no CORS problem) with one
  yes/no question per emoji and gets a probability (0..1) for each emoji.
* The browser part (custom Streamlit component, plain JavaScript, no downloads)
  lets matching emojis float up out of the heap. Better match = higher up.

No fake data: if the API fails, the exact HTTP status and body are shown.
"""

import os
import tempfile
import time
from pathlib import Path

import requests
import streamlit as st
import streamlit.components.v1 as components

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------


def _get_secret(name, default=None):
    """Read a value from Streamlit secrets (cloud), else env variable, else default."""
    try:
        return st.secrets[name]
    except Exception:
        return os.environ.get(name, default)


API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
# Local use: paste your key into the SECOND pair of quotes below.
# Cloud use: leave the placeholder and put JEV_API_KEY into the app's Secrets.
API_KEY = _get_secret("JEV_API_KEY", "YOUR_API_KEY_HERE")
# Optional: if APP_PASSWORD is set in the Secrets, visitors must enter it first.
APP_PASSWORD = _get_secret("APP_PASSWORD", None)
REQUEST_TIMEOUT_S = 30
POOL_COPIES = 3      # 60 unique emojis x 3 = 180 emojis in the heap
MIN_QUERY_CHARS = 2  # API is only called from this many characters on

# 60 unique emojis (keep <= 64: the API accepts a limited number of questions
# per request). Only the emojis themselves are sent to the API.
UNIQUE_EMOJIS = [
    "🍎", "🍌", "🍕", "🍔", "🍟", "🥦", "🍫", "🍺", "🍇", "🥕",
    "🐶", "🐱", "🦈", "🐍", "🐝", "🦁", "🐘", "🦋", "🐧", "🦉",
    "🚗", "🚑", "🚀", "🚲", "🚁", "🚂", "🚢", "🔥", "🔪", "💀",
    "💣", "⚡", "🌊", "🌹", "🌵", "🌲", "🌞", "🌈", "🌙", "🎸",
    "📱", "💻", "🔑", "💡", "📚", "⚽", "🎂", "💰", "🎁", "🏠",
    "⏰", "🎈", "🔔", "💊", "💉", "🦷", "👑", "💎", "🧲", "🎹",
]

# ----------------------------------------------------------------------------
# Browser part: custom Streamlit component (written to a temp folder at start)
# ----------------------------------------------------------------------------
COMPONENT_HTML = r'''<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  html, body { margin: 0; padding: 0; background: transparent;
               font-family: "Source Sans Pro", "Segoe UI", system-ui, sans-serif; }
  #wrap { padding: 2px; }
  #q { width: 100%; box-sizing: border-box; font-size: 22px; padding: 14px 18px;
       border-radius: 14px; border: 2px solid #334155; background: #0f172a;
       color: #e2e8f0; outline: none; transition: border-color .2s, box-shadow .2s; }
  #q::placeholder { color: #64748b; }
  #q:focus { border-color: #38bdf8; box-shadow: 0 0 0 4px rgba(56,189,248,.22); }
  #stage { position: relative; margin-top: 12px; border-radius: 16px; overflow: hidden;
           background: linear-gradient(180deg, #0a1122 0%, #10193a 55%, #1a1538 100%);
           box-shadow: inset 0 0 0 1px rgba(148,163,184,.15); }
  canvas { display: block; width: 100%; }
</style>
</head>
<body>
<div id="wrap">
  <input id="q" type="text" autocomplete="off"
         placeholder="Tippe einen Begriff ... z. B. gef&auml;hrlich, s&uuml;ss, Sommer, schnell">
  <div id="stage"><canvas id="c"></canvas></div>
</div>

<script>
(function () {
  "use strict";

  // ---------------------------------------------------------------- setup
  var canvas = document.getElementById("c");
  var ctx = canvas.getContext("2d");
  var input = document.getElementById("q");
  var wrap = document.getElementById("wrap");

  var H = 560;                       // canvas height in px
  var ZONE_TOP = 46;                 // top of the "Treffer" zone
  var DIVIDER = Math.round(H * 0.47);// boundary between Treffer and Datenpool
  var G = 0.32;                      // gravity (px per step^2)
  var STEP_MS = 1000 / 60;

  var W = 800, R = 18, N = 0;
  var dpr = Math.min(window.devicePixelRatio || 1, 2);
  var bodies = [];
  var ready = false;
  var query = "";
  var glow = 0;
  var state = { scores: {}, threshold: 0.5, latency: null, doneQuery: "" };

  // ------------------------------------------------ Streamlit protocol
  function post(type, data) {
    var msg = { isStreamlitMessage: true, type: type };
    for (var k in (data || {})) { msg[k] = data[k]; }
    window.parent.postMessage(msg, "*");
  }
  function setHeight() { post("streamlit:setFrameHeight", { height: wrap.offsetHeight + 8 }); }

  window.addEventListener("message", function (ev) {
    var d = ev.data;
    if (d && d.type === "streamlit:render") { onRender(d.args || {}); }
  });
  post("streamlit:componentReady", { apiVersion: 1 });
  setHeight();

  function onRender(args) {
    state.scores = args.scores || {};
    state.threshold = (typeof args.threshold === "number") ? args.threshold : 0.5;
    state.latency = (typeof args.latency_ms === "number") ? args.latency_ms : null;
    state.doneQuery = args.done_query || "";
    if (!ready && args.pool && args.pool.length) { init(args.pool); }
    refreshTargets();
  }

  // ------------------------------------------------------- typing input
  var debounce = null, lastSent = null;
  function send() {
    var q = input.value.trim();
    if (q === lastSent) { return; }
    lastSent = q;
    post("streamlit:setComponentValue", { value: { query: q, t: Date.now() }, dataType: "json" });
  }
  input.addEventListener("input", function () {
    query = input.value;
    refreshTargets();                // typing < 2 chars empties the target zone at once
    clearTimeout(debounce);
    debounce = setTimeout(send, 450);
  });
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter") { clearTimeout(debounce); send(); }
  });

  function isWaiting() {
    var q = query.trim();
    return q.length >= 2 && state.doneQuery !== q;
  }

  // ------------------------------------------------------------- sizing
  function measure() {
    W = Math.max(320, Math.floor(wrap.clientWidth));
    R = Math.max(13, Math.min(22, Math.sqrt(W * H * 0.30 / (Math.max(N, 1) * Math.PI))));
  }
  function sizeCanvas() {
    canvas.width = Math.round(W * dpr);
    canvas.height = Math.round(H * dpr);
    canvas.style.height = H + "px";
  }
  if (window.ResizeObserver) {
    new ResizeObserver(function () {
      var w = Math.max(320, Math.floor(wrap.clientWidth));
      if (ready && Math.abs(w - W) > 2) { W = w; sizeCanvas(); }
      setHeight();
    }).observe(wrap);
  }

  // -------------------------------------------------------------- bodies
  function init(pool) {
    ready = true;
    N = pool.length;
    measure();
    sizeCanvas();
    bodies = pool.map(function (emo) {
      var x = R + Math.random() * (W - 2 * R);
      var y = -Math.random() * 1100 - R;           // rain in from above
      return { emoji: emo, x: x, y: y, px: x, py: y, vx: 0, vy: 0, pvy: 0,
               angle: (Math.random() - 0.5) * 6, isFloat: false, want: false,
               norm: 0, score: 0, phase: Math.random() * 6.283, tm: null, touch: 0,
               floorHit: false };
    });
    setHeight();
    requestAnimationFrame(loop);
  }

  // decide which bodies should float (called on every new data / keystroke)
  function refreshTargets() {
    if (!ready) { return; }
    var active = query.trim().length >= 2;
    var scores = active ? state.scores : {};
    var thr = state.threshold;
    bodies.forEach(function (b) {
      var s = scores[b.emoji];
      var match = (typeof s === "number") && s >= thr;
      b.score = (typeof s === "number") ? s : 0;
      b.norm = match ? Math.min(1, Math.max(0, (s - thr) / Math.max(1e-6, 1 - thr))) : 0;
      if (match !== b.want) {
        b.want = match;
        clearTimeout(b.tm);
        b.tm = setTimeout(function () { setFloat(b, match); }, Math.random() * 320);
      }
    });
  }
  function setFloat(b, on) {
    b.isFloat = on;
    if (on) {                                       // little pop upwards
      b.vx += (Math.random() - 0.5) * 3;
      b.vy = -4 - Math.random() * 4;
    }
  }

  // ------------------------------------------------------------- physics
  function solvePairs(list) {
    var D = 2 * R, D2 = D * D;
    for (var i = 0; i < list.length; i++) {
      var a = list[i];
      for (var j = i + 1; j < list.length; j++) {
        var b = list[j];
        var dx = b.x - a.x, dy = b.y - a.y;
        var d2 = dx * dx + dy * dy;
        if (d2 < D2) {
          var d = Math.sqrt(d2), nx, ny;
          if (d < 1e-6) { nx = 0; ny = 1; d = 0; } else { nx = dx / d; ny = dy / d; }
          var push = (D - d) * 0.5;
          a.x -= nx * push; a.y -= ny * push;
          b.x += nx * push; b.y += ny * push;
          a.touch++; b.touch++;
        }
      }
    }
  }

  function step(now) {
    var waiting = isWaiting();
    var pile = [], flo = [];
    var i, b;

    for (i = 0; i < bodies.length; i++) {
      b = bodies[i];
      b.px = b.x; b.py = b.y; b.pvy = b.vy; b.touch = 0; b.floorHit = false;

      if (b.isFloat) {
        var lo = ZONE_TOP + R, hi = DIVIDER - R - 14;
        var ty = lo + (1 - b.norm) * (hi - lo);       // better match = higher up
        b.vx += Math.sin(now * 0.0016 + b.phase) * 0.010 - b.vx * 0.02;
        b.vy += (ty - b.y) * 0.0016 - b.vy * 0.07 + Math.cos(now * 0.0021 + b.phase * 1.7) * 0.010;
        flo.push(b);
      } else {
        b.vy += G;
        if (waiting && Math.random() < 0.05) {        // pile trembles while Jev thinks
          b.vx += (Math.random() - 0.5) * 1.6;
          b.vy -= Math.random() * 1.3;
        }
        pile.push(b);
      }
      b.vx *= 0.999; b.vy *= 0.999;
      b.x += b.vx; b.y += b.vy;
    }

    for (var it = 0; it < 6; it++) {
      solvePairs(pile);
      solvePairs(flo);
      for (i = 0; i < bodies.length; i++) {
        b = bodies[i];
        if (b.x < R) { b.x = R; }
        if (b.x > W - R) { b.x = W - R; }
        if (b.y > H - R) { b.y = H - R; b.floorHit = true; b.touch++; }
        if (b.isFloat && b.y < R) { b.y = R; }
      }
    }

    for (i = 0; i < bodies.length; i++) {
      b = bodies[i];
      var dx = b.x - b.px, dy = b.y - b.py;
      if (!b.isFloat && b.touch > 0) {
        if (Math.sqrt(dx * dx + dy * dy) < 0.12) {     // static friction -> stays a heap
          b.x = b.px; b.y = b.py; dx = 0; dy = 0;
        } else {
          dx *= 0.93;                                   // sliding friction
        }
      }
      b.vx = dx; b.vy = dy;
      if (b.floorHit && b.pvy > 2.5) { b.vy = -b.pvy * 0.28; }   // small bounce on landing

      if (b.isFloat) {
        var a = b.angle;
        a = ((a + Math.PI) % (2 * Math.PI) + 2 * Math.PI) % (2 * Math.PI) - Math.PI;
        var target = Math.sin(now * 0.0018 + b.phase) * 0.16;
        b.angle = a + (target - a) * 0.08;
      } else {
        b.angle += dx / R * 0.8;                         // rolling
      }
    }
  }

  // ------------------------------------------------------------ drawing
  function draw(now) {
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);

    var hits = 0, i, b;
    for (i = 0; i < bodies.length; i++) { if (bodies[i].want) { hits++; } }
    glow += ((hits > 0 ? 1 : 0) - glow) * 0.05;

    // glowing target zone
    if (glow > 0.01) {
      var g = ctx.createLinearGradient(0, 0, 0, DIVIDER);
      g.addColorStop(0, "rgba(56,189,248," + (0.20 * glow) + ")");
      g.addColorStop(1, "rgba(56,189,248,0)");
      ctx.fillStyle = g;
      ctx.fillRect(0, 0, W, DIVIDER);
    }

    // divider
    ctx.save();
    ctx.setLineDash([6, 6]);
    ctx.strokeStyle = "rgba(148,163,184,0.28)";
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, DIVIDER); ctx.lineTo(W, DIVIDER); ctx.stroke();
    ctx.restore();

    // labels
    ctx.textBaseline = "alphabetic";
    ctx.textAlign = "left";
    ctx.font = "700 13px 'Segoe UI', system-ui, sans-serif";
    ctx.fillStyle = "#7dd3fc";
    ctx.fillText("SEMANTISCHE TREFFER  \u00B7  " + hits, 16, 28);
    ctx.fillStyle = "rgba(148,163,184,0.95)";
    ctx.fillText("DATENPOOL  \u00B7  " + (bodies.length - hits), 16, DIVIDER + 22);

    ctx.textAlign = "right";
    if (isWaiting()) {
      var pulse = 0.5 + 0.5 * Math.sin(now * 0.008);
      ctx.fillStyle = "rgba(250,204,21," + (0.35 + 0.65 * pulse) + ")";
      ctx.fillText("\u25CF Jev analysiert ...", W - 16, 28);
    } else if (state.latency !== null && query.trim().length >= 2) {
      ctx.fillStyle = "#a7f3d0";
      ctx.fillText("\u26A1 " + Math.round(state.latency) + " ms", W - 16, 28);
    }

    // emojis
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.font = Math.round(R * 1.7) + "px 'Apple Color Emoji','Segoe UI Emoji','Noto Color Emoji',sans-serif";
    for (i = 0; i < bodies.length; i++) {
      b = bodies[i];
      ctx.save();
      ctx.translate(b.x, b.y);
      ctx.rotate(b.angle);
      if (b.isFloat) { ctx.shadowColor = "rgba(96,200,255,0.95)"; ctx.shadowBlur = 16; }
      ctx.fillText(b.emoji, 0, R * 0.08);
      ctx.restore();
    }

    // probability labels under the floating emojis
    ctx.font = "600 " + Math.round(R * 0.55) + "px 'Segoe UI', system-ui, sans-serif";
    ctx.fillStyle = "rgba(226,232,240,0.78)";
    for (i = 0; i < bodies.length; i++) {
      b = bodies[i];
      if (b.isFloat) { ctx.fillText(Math.round(b.score * 100) + "%", b.x, b.y + R * 1.12); }
    }
  }

  // ---------------------------------------------------------- main loop
  var last = performance.now(), acc = 0;
  function loop(t) {
    acc += Math.min(t - last, 100);
    last = t;
    while (acc >= STEP_MS) { step(t); acc -= STEP_MS; }
    draw(t);
    requestAnimationFrame(loop);
  }

  // handle for automated tests
  window.__sm = { bodies: function () { return bodies; }, W: function () { return W; },
                  H: H, DIVIDER: DIVIDER, R: function () { return R; } };
})();
</script>
</body>
</html>
'''

_COMPONENT_DIR = Path(tempfile.gettempdir()) / "sensemaker_component"
_COMPONENT_DIR.mkdir(parents=True, exist_ok=True)
_index = _COMPONENT_DIR / "index.html"
if not _index.exists() or _index.read_text(encoding="utf-8") != COMPONENT_HTML:
    _index.write_text(COMPONENT_HTML, encoding="utf-8")

physics_component = components.declare_component(
    "sensemaker_physics", path=str(_COMPONENT_DIR)
)


# ----------------------------------------------------------------------------
# API call
# ----------------------------------------------------------------------------
def call_jev(query: str):
    """
    One request to Jev, one yes/no ("noul") question per emoji.

    Returns (scores, latency_ms, raw_json, error_text).
    scores maps emoji -> probability (0..1) that it matches the query.
    On failure scores/raw are None and error_text holds the real server reply.
    """
    questions = {
        f"e{i}": {
            "type": "noul",
            "instructions": (
                f"Ist das Emoji {emoji} ein passendes Beispiel für den "
                f"Suchbegriff? Bewerte die Bedeutung, nicht Wortüberschneidungen."
            ),
            "criteria": {
                "true": "Das Emoji steht klar für den Suchbegriff oder gehört eindeutig dazu.",
                "false": "Das Emoji hat keinen erkennbaren Bezug zum Suchbegriff.",
            },
        }
        for i, emoji in enumerate(UNIQUE_EMOJIS)
    }
    payload = {
        "state": f"Suchbegriff: '{query}'",
        "model": MODEL,
        "questions": questions,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}",
    }

    start = time.perf_counter()
    try:
        response = requests.post(
            API_URL, headers=headers, json=payload, timeout=REQUEST_TIMEOUT_S
        )
    except requests.exceptions.RequestException as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000
        return None, elapsed_ms, None, f"Request failed before a response was received: {exc!r}"
    elapsed_ms = (time.perf_counter() - start) * 1000

    if response.status_code != 200:
        return None, elapsed_ms, None, f"HTTP {response.status_code}\n\n{response.text}"

    try:
        raw = response.json()
        answers = raw["answers"]
        scores = {
            emoji: float(answers[f"e{i}"]["noul"])
            for i, emoji in enumerate(UNIQUE_EMOJIS)
        }
    except (ValueError, KeyError, TypeError) as exc:
        return None, elapsed_ms, None, (
            f"HTTP 200, but the response could not be parsed ({exc!r}).\n\n"
            f"Raw body:\n\n{response.text}"
        )
    return scores, elapsed_ms, raw, None


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------
st.set_page_config(page_title="SenseMaker AI", page_icon="🧠", layout="wide")
st.title("SenseMaker AI - Powered by Jev")

ss = st.session_state

# Optional password gate (protects your API credit when the link is shared)
if APP_PASSWORD and not ss.get("authed"):
    pw = st.text_input("Passwort", type="password")
    if pw and pw == str(APP_PASSWORD):
        ss.authed = True
        st.rerun()
    if pw:
        st.error("Falsches Passwort.")
    st.stop()

if "pool" not in ss:
    ss.pool = UNIQUE_EMOJIS * POOL_COPIES
    ss.scores = {}
    ss.latency_ms = None
    ss.raw = None
    ss.error = None
    ss.last_query = None
    ss.done_query = ""

threshold = st.sidebar.slider(
    "Schwellenwert (Treffer ab Wahrscheinlichkeit)", 0.05, 0.95, 0.50, 0.05
)

# Containers reserve the layout order; they are filled further below.
metric_box = st.container()
error_box = st.container()

# The component holds the text field AND the physics animation.
result = physics_component(
    pool=ss.pool,
    scores=ss.scores,
    threshold=threshold,
    latency_ms=ss.latency_ms,
    done_query=ss.done_query,
    key="physics",
    default=None,
)

# New search term from the browser? -> (maybe) call the API, then rerun.
query = ((result or {}).get("query") or "").strip()
if result is not None and query != ss.last_query:
    if len(query) < MIN_QUERY_CHARS:
        ss.scores, ss.error = {}, None
    elif API_KEY == "YOUR_API_KEY_HERE":
        ss.scores = {}
        ss.error = (
            "No API key configured. Set the environment variable JEV_API_KEY "
            "or edit API_KEY in app.py."
        )
    else:
        scores, latency, raw, error = call_jev(query)
        ss.scores = scores or {}
        ss.latency_ms = latency
        ss.raw = raw
        ss.error = error
    ss.done_query = query
    ss.last_query = query
    st.rerun()

# Metrics / errors
n_hits = sum(1 for e in ss.pool if ss.scores.get(e, 0.0) >= threshold)
with metric_box:
    c1, c2, c3 = st.columns(3)
    c1.metric(
        "JEV Latenz (ms)",
        f"{ss.latency_ms:,.0f}" if ss.latency_ms is not None else "—",
    )
    c2.metric("Semantische Treffer", n_hits)
    c3.metric("Datenpool (Unsortiert)", len(ss.pool) - n_hits)
    if ss.raw:
        usage = ss.raw.get("usage", {})
        parts = [
            f"Modell laut Server: {ss.raw.get('model', '?')}",
            f"Tokens ein/aus: {usage.get('input_tokens', '?')}/{usage.get('output_tokens', '?')}",
        ]
        if ss.raw.get("request_id"):
            parts.append(f"Request-ID: {ss.raw['request_id']}")
        st.caption(" · ".join(parts))
with error_box:
    if ss.error:
        st.error(ss.error)

if ss.scores:
    with st.expander("Debug: Wahrscheinlichkeit pro Emoji (von Jev)"):
        st.table(
            [
                {"Emoji": e, "P(passt)": round(s, 3)}
                for e, s in sorted(ss.scores.items(), key=lambda kv: -kv[1])
            ]
        )
        st.json(ss.raw)
