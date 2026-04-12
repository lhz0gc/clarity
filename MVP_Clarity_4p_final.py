
import json
import os
import re
import socket
import threading
import time
import uuid
import webbrowser
from collections import defaultdict
from contextlib import closing
from typing import Optional

from aiohttp import web, WSMsgType

HOST = "0.0.0.0"
PORT = 8000
MAX_PEERS = 4
MAX_WS_MESSAGE_SIZE = 12 * 1024 * 1024  # enough for frozen board snapshots
ROOM_CODE_RE = re.compile(r"^[A-Z0-9]{4,8}$")

# ============================================================
# TURN / SIGNALING CONFIG
# ============================================================
TURN_URLS_FALLBACK = ",".join(
    [
        "stun:stun.relay.metered.ca:80",
        "turn:global.relay.metered.ca:80",
        "turn:global.relay.metered.ca:80?transport=tcp",
        "turn:global.relay.metered.ca:443",
        "turns:global.relay.metered.ca:443?transport=tcp",
    ]
)
TURN_USERNAME_FALLBACK = "a52e41d24a1bb7f6c1186141"
TURN_CREDENTIAL_FALLBACK = "+c/FMeGCii0VVH/C"

# room_code -> { peer_id: ws }
ROOMS: dict[str, dict[str, web.WebSocketResponse]] = {}
ROOM_STATES: dict[str, dict[str, Optional[str]]] = {}

PUBLIC_BASE_URL: Optional[str] = None
NGROK_TUNNEL = None


# ============================================================
# PWA ASSETS
# ============================================================
ICON_SVG = r'''
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#00E676"/>
      <stop offset="100%" stop-color="#00C853"/>
    </linearGradient>
  </defs>
  <rect width="512" height="512" rx="128" fill="#111111"/>
  <circle cx="256" cy="256" r="170" fill="url(#g)" opacity="0.12"/>
  <path d="M162 196c0-17.7 14.3-32 32-32h92c17.7 0 32 14.3 32 32v14l48-30c21.3-13.3 48 2 48 27.2v97.6c0 25.2-26.7 40.5-48 27.2l-48-30v14c0 17.7-14.3 32-32 32h-92c-17.7 0-32-14.3-32-32V196z" fill="#FFFFFF"/>
  <path d="M165 79l138 93-66 19-28 73z" fill="#00E676"/>
</svg>
'''.strip()

MANIFEST_JSON = json.dumps(
    {
        "name": "Clarity",
        "short_name": "Clarity",
        "description": "See Together. Guide Better.",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#FFFFFF",
        "theme_color": "#00C853",
        "icons": [
            {
                "src": "/icon.svg",
                "sizes": "any",
                "type": "image/svg+xml",
                "purpose": "any maskable",
            }
        ],
    }
)

SERVICE_WORKER_JS = r'''
const CACHE_NAME = 'clarity-shell-v7';
const APP_SHELL = ['/manifest.json', '/icon.svg'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(APP_SHELL))
      .then(() => self.skipWaiting())
      .catch(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;
  const url = new URL(event.request.url);
  if (url.origin !== location.origin) return;
  if (url.pathname === '/' || url.pathname === '/sw.js') return;

  event.respondWith(
    caches.match(event.request).then((cached) => {
      if (cached) return cached;
      return fetch(event.request)
        .then((response) => {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy)).catch(() => {});
          return response;
        })
        .catch(() => cached);
    })
  );
});
'''.strip()


# ============================================================
# MOBILE-FIRST HTML — 4-PERSON MESH + GPT REFINEMENTS
# ============================================================
INDEX_HTML = r'''
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover" />
  <meta name="apple-mobile-web-app-capable" content="yes" />
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
  <meta name="theme-color" content="#00C853" />
  <title>Clarity</title>
  <link rel="manifest" href="/manifest.json" />
  <style>
    :root {
      --green: #00C853;
      --green-light: #69F0AE;
      --green-dark: #00A844;
      --green-faded: rgba(0,200,83,0.12);
      --red: #FF3B30;
      --yellow: #FFCC00;
      --blue: #007AFF;
      --white: #FFFFFF;
      --off-white: #F5F5F5;
      --gray-light: #E0E0E0;
      --gray: #9E9E9E;
      --gray-dark: #424242;
      --charcoal: #1E1E1E;
      --black: #111111;
      --safe-top: env(safe-area-inset-top, 0px);
      --safe-bottom: env(safe-area-inset-bottom, 0px);
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }

    html, body {
      width: 100%;
      height: 100%;
      overflow: hidden;
    }

    body {
      font-family: -apple-system, BlinkMacSystemFont, 'SF Pro', 'Segoe UI', Roboto, 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', sans-serif;
      background: var(--white);
      color: var(--black);
      height: 100dvh;
      width: 100vw;
      -webkit-user-select: none;
      user-select: none;
      -webkit-touch-callout: none;
    }

    button, input, textarea { font: inherit; }
    .hidden { display: none !important; }

    /* ══════ HOME — elderly-friendly ══════ */
    #homeScreen {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      height: 100dvh;
      padding: 32px 24px;
      padding-top: calc(32px + var(--safe-top));
      background: var(--white);
      gap: 0;
    }
    .logo-row { display: flex; align-items: flex-start; gap: 4px; margin-bottom: 4px; }
    .logo-text { font-size: 52px; font-weight: 800; color: var(--black); letter-spacing: -1px; }
    .logo-cursor { width: 20px; height: 20px; margin-top: 10px; }
    .logo-cursor svg { fill: var(--green); }
    .tagline { font-size: 20px; color: var(--gray-dark); font-weight: 500; margin-bottom: 32px; text-align: center; }

    .quick-call-btn {
      width: 92%; max-width: 400px; height: 100px; border-radius: 28px;
      background: var(--green); border: none;
      display: flex; align-items: center; justify-content: center; gap: 14px;
      cursor: pointer;
      box-shadow: 0 10px 40px rgba(0,200,83,0.4);
      transition: transform 0.15s;
      -webkit-tap-highlight-color: transparent;
      animation: pulse 3s ease-in-out infinite;
    }
    .quick-call-btn:active { transform: scale(0.96); animation: none; }
    @keyframes pulse { 0%,100%{transform:scale(1)} 50%{transform:scale(1.02)} }
    .quick-call-btn .icon { font-size: 40px; }
    .quick-call-btn .label { font-size: 32px; font-weight: 800; color: white; }
    .quick-call-btn .sub { display: none; }

    .or-divider {
      display: flex; align-items: center; gap: 16px;
      width: 80%; max-width: 360px; margin: 28px 0 20px;
    }
    .or-divider::before, .or-divider::after {
      content: ''; flex: 1; height: 1px; background: var(--gray-light);
    }
    .or-divider span { font-size: 18px; color: var(--gray); font-weight: 500; }

    .join-section { width: 92%; max-width: 400px; }
    .join-label { display: none; }
    .join-row { display: flex; gap: 12px; }
    .join-input {
      flex: 1; height: 72px; background: var(--off-white);
      border: 3px solid var(--gray-light); border-radius: 20px;
      text-align: center; font-size: 32px; font-weight: 800;
      letter-spacing: 5px; color: var(--black); outline: none;
      -webkit-appearance: none; text-transform: uppercase;
    }
    .join-input::placeholder { font-size: 22px; letter-spacing: 2px; font-weight: 500; color: var(--gray); }
    .join-input:focus { border-color: var(--green); background: white; }
    .join-btn {
      height: 72px; min-width: 100px; padding: 0 24px; background: var(--charcoal);
      color: white; border: none; border-radius: 20px;
      font-size: 26px; font-weight: 800; cursor: pointer;
      -webkit-tap-highlight-color: transparent;
    }
    .join-btn:active { opacity: 0.8; }

    .badge-4p { display: none; }

    .lang-btn {
      margin-top: 16px; padding: 8px 28px; font-size: 18px; font-weight: 700;
      border-radius: 20px; border: 2px solid var(--gray);
      background: transparent; color: var(--gray); cursor: pointer;
      -webkit-tap-highlight-color: transparent;
      transition: all 0.2s;
    }
    .lang-btn:active { background: var(--off-white); }

    .server-toggle {
      margin-top: 24px; font-size: 15px; color: var(--gray);
      cursor: pointer; background: none; border: none;
      -webkit-tap-highlight-color: transparent;
    }
    .server-panel {
      margin-top: 10px; width: 90%; max-width: 400px;
      background: var(--off-white); border-radius: 18px; padding: 16px;
    }
    .server-input {
      width: 100%; height: 52px; background: white;
      border: 2px solid var(--gray-light); border-radius: 14px;
      padding: 0 14px; font-size: 16px; color: var(--black); outline: none;
      -webkit-appearance: none;
    }
    .server-hint {
      margin-top: 8px; color: var(--gray); font-size: 14px; line-height: 1.4;
    }

    .features { display: none; }

    /* ══════ CALL SCREEN ══════ */
    #callScreen {
      display: none; position: relative;
      width: 100vw; height: 100dvh;
      background: var(--black); flex-direction: column;
    }
    #callScreen.active { display: flex; }

    /* Status bar */
    .call-status-bar {
      position: absolute; top: 0; left: 0; right: 0; z-index: 20;
      padding: calc(10px + var(--safe-top)) 16px 10px;
      background: linear-gradient(to bottom, rgba(0,0,0,0.75), transparent);
      display: flex; align-items: center; gap: 8px;
    }
    .status-dot { width: 12px; height: 12px; border-radius: 50%; background: #FF9500; flex-shrink: 0; }
    .status-dot.connected { background: var(--green); }
    .call-status-text { flex: 1; font-size: 20px; color: white; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-weight: 700; }
    .room-badge { background: rgba(255,255,255,0.2); padding: 8px 16px; border-radius: 12px; font-size: 20px; font-weight: 800; color: white; letter-spacing: 3px; min-width: 70px; text-align: center; }
    .peer-count { background: rgba(0,200,83,0.35); padding: 8px 12px; border-radius: 12px; font-size: 18px; font-weight: 800; color: var(--green-light); }

    /* Video grid */
    .video-area { flex: 1; position: relative; overflow: hidden; }

    .video-grid {
      position: absolute; inset: 0;
      display: grid;
      gap: 2px;
      background: var(--black);
    }
    .video-grid[data-count="0"],
    .video-grid[data-count="1"] {
      grid-template-columns: 1fr;
      grid-template-rows: 1fr;
    }
    .video-grid[data-count="2"] {
      grid-template-columns: 1fr;
      grid-template-rows: 1fr 1fr;
    }
    .video-grid[data-count="3"] {
      grid-template-columns: 1fr 1fr;
      grid-template-rows: 1fr 1fr;
    }
    .video-grid[data-count="3"] .video-cell:first-child {
      grid-column: 1 / -1;
    }
    .video-grid[data-count="4"] {
      grid-template-columns: 1fr 1fr;
      grid-template-rows: 1fr 1fr;
    }

    .video-cell {
      position: relative;
      overflow: hidden;
      background: var(--charcoal);
      border-radius: 4px;
    }
    .video-cell video {
      position: absolute; inset: 0;
      width: 100%; height: 100%;
      object-fit: cover;
      background: var(--charcoal);
    }
    .video-cell .name-tag {
      position: absolute; bottom: 10px; left: 10px;
      background: rgba(0,0,0,0.65);
      padding: 4px 10px; border-radius: 8px;
      font-size: 15px; font-weight: 700; color: white; z-index: 5;
    }

    /* Frozen canvas overlay */
    #frozenCanvas {
      position: absolute; inset: 0;
      width: 100%; height: 100%;
      z-index: 10;
      touch-action: none;
      cursor: crosshair;
      background: var(--charcoal);
    }

    /* Waiting overlay */
    .waiting-view {
      position: absolute; inset: 0;
      display: flex; flex-direction: column;
      align-items: center; justify-content: center;
      background: var(--charcoal); z-index: 5;
    }
    .waiting-view .emoji { font-size: 80px; margin-bottom: 20px; }
    .waiting-view .text { font-size: 28px; color: white; font-weight: 700; }
    .waiting-view .code { font-size: 52px; color: var(--green); font-weight: 900; letter-spacing: 8px; margin-top: 16px; }
    .waiting-view .hint { font-size: 20px; color: #bbb; margin-top: 20px; text-align: center; padding: 0 24px; line-height: 1.5; }
    .copy-link-btn {
      margin-top: 28px; padding: 20px 48px;
      background: var(--green); color: white; border: none;
      border-radius: 22px; font-size: 26px; font-weight: 800;
      cursor: pointer; -webkit-tap-highlight-color: transparent;
      box-shadow: 0 6px 24px rgba(0,200,83,0.4);
    }
    .copy-link-btn:active { opacity: 0.85; transform: scale(0.97); }

    /* PiP self-view */
    .pip {
      position: absolute;
      top: calc(60px + var(--safe-top)); right: 10px;
      width: 100px; height: 130px;
      border-radius: 14px; overflow: hidden;
      border: 3px solid white; z-index: 12;
      box-shadow: 0 4px 16px rgba(0,0,0,0.4);
      background: #000;
    }
    .pip video { width: 100%; height: 100%; object-fit: cover; background: #000; }
    .pip .pip-label {
      position: absolute; bottom: 4px; left: 4px;
      background: rgba(0,0,0,0.65);
      padding: 3px 7px; border-radius: 5px;
      font-size: 12px; color: white; font-weight: 800;
    }

    /* Annotation bar */
    .annotation-bar {
      display: none; align-items: center; gap: 10px;
      padding: 12px 16px; background: rgba(30,30,30,0.95); z-index: 15;
      overflow-x: auto;
    }
    .annotation-bar.active { display: flex; }
    .color-dot {
      width: 50px; height: 50px; border-radius: 50%;
      border: 3px solid transparent; cursor: pointer;
      -webkit-tap-highlight-color: transparent;
      transition: transform 0.15s; flex-shrink: 0;
    }
    .color-dot.selected { border-color: white; transform: scale(1.15); }
    .ann-divider { width: 1px; height: 36px; background: rgba(255,255,255,0.2); margin: 0 6px; flex-shrink: 0; }
    .ann-action {
      padding: 14px 24px; background: rgba(255,255,255,0.1); border: none;
      border-radius: 14px; color: white; font-size: 20px; font-weight: 800;
      cursor: pointer; -webkit-tap-highlight-color: transparent; flex-shrink: 0;
    }
    .ann-action:active { opacity: 0.7; }
    .ann-action.done { background: var(--green); }

    /* Main toolbar — BIG for elderly */
    .call-toolbar {
      display: flex; align-items: center; justify-content: space-evenly;
      padding: 12px 4px calc(12px + var(--safe-bottom));
      background: rgba(30,30,30,0.95); z-index: 15;
      gap: 2px;
    }
    .tool-btn {
      display: flex; flex-direction: column; align-items: center; justify-content: center;
      min-width: 68px; height: 68px; border-radius: 18px;
      background: rgba(255,255,255,0.1); border: none;
      cursor: pointer; -webkit-tap-highlight-color: transparent; color: white;
      padding: 4px 6px;
    }
    .tool-btn:active { background: rgba(255,255,255,0.3); }
    .tool-btn.active-state { background: rgba(255,255,255,0.3); }
    .tool-btn .ti { font-size: 28px; }
    .tool-btn .tl { font-size: 13px; color: white; margin-top: 2px; font-weight: 700; }

    .freeze-btn {
      min-width: 80px; height: 80px; border-radius: 22px;
      background: var(--green); border: none;
      display: flex; flex-direction: column; align-items: center; justify-content: center;
      cursor: pointer; box-shadow: 0 4px 20px rgba(0,200,83,0.5);
      -webkit-tap-highlight-color: transparent; color: white;
      padding: 4px 8px;
    }
    .freeze-btn:active { transform: scale(0.94); }
    .freeze-btn.frozen { background: var(--green-dark); }
    .freeze-btn .ti { font-size: 32px; }
    .freeze-btn .tl { font-size: 14px; color: white; font-weight: 800; margin-top: 2px; }

    .end-btn {
      min-width: 68px; height: 68px; border-radius: 18px;
      background: var(--red); border: none;
      display: flex; flex-direction: column; align-items: center; justify-content: center;
      cursor: pointer; -webkit-tap-highlight-color: transparent; color: white;
      padding: 4px 6px;
    }
    .end-btn:active { opacity: 0.8; }
    .end-btn .ti { font-size: 28px; transform: rotate(135deg); }
    .end-btn .tl { font-size: 13px; color: white; margin-top: 2px; font-weight: 700; }

    .source-video { position: absolute; width: 1px; height: 1px; opacity: 0; pointer-events: none; }

    @media (max-height: 700px) {
      .quick-call-btn { height: 80px; }
      .quick-call-btn .label { font-size: 28px; }
      .or-divider { margin: 16px 0 12px; }
      .join-input { height: 60px; font-size: 28px; }
      .join-btn { height: 60px; font-size: 22px; }
      .tagline { margin-bottom: 20px; }
    }
  </style>
</head>
<body>

<!-- ══════ HOME SCREEN ══════ -->
<div id="homeScreen">
  <div class="logo-row">
    <span class="logo-text">Clarity</span>
    <div class="logo-cursor">
      <svg viewBox="0 0 24 24" width="20" height="20"><path d="M5 2l14 10-7 2-3 7z"/></svg>
    </div>
  </div>
  <div class="tagline" data-i18n="tagline">See Together. Guide Better.</div>

  <button class="lang-btn" id="langBtn" onclick="toggleLang()">中文</button>

  <button class="quick-call-btn" id="quickCallBtn">
    <span class="icon">📹</span>
    <span class="label" data-i18n="startCall">Start Call</span>
  </button>

  <div class="or-divider"><span data-i18n="or">or</span></div>

  <div class="join-section">
    <div class="join-row">
      <input class="join-input" id="homeRoomInput" data-i18n="roomPlaceholder" placeholder="Room Code" maxlength="8" autocapitalize="characters" autocomplete="off" />
      <button class="join-btn" id="homeJoinBtn" data-i18n="join">Join</button>
    </div>
  </div>

  <button class="server-toggle" id="serverToggle">&#9656; Server</button>
  <div class="server-panel hidden" id="serverPanel">
    <input class="server-input" id="serverUrlInput" placeholder="https://your-server.example" autocomplete="off" />
    <div class="server-hint" data-i18n="serverHint">Leave blank to use this page's server.</div>
  </div>
</div>

<!-- ══════ CALL SCREEN ══════ -->
<div id="callScreen">
  <div class="call-status-bar">
    <div class="status-dot" id="statusDot"></div>
    <div class="call-status-text" id="callStatusText" data-i18n="connecting">Connecting...</div>
    <div class="peer-count" id="peerCount">1/4</div>
    <div class="room-badge" id="roomBadge"></div>
  </div>

  <div class="video-area" id="videoArea">
    <div class="waiting-view" id="waitingView">
      <div class="emoji">📹</div>
      <div class="text" data-i18n="waitingOthers">Waiting for others...</div>
      <div class="code" id="waitingRoomCode"></div>
      <div class="hint" data-i18n="waitingHint">Tap below to invite someone</div>
      <button class="copy-link-btn" id="copyLinkBtn2" data-i18n="shareInvite">📤 Share Invite Link</button>
    </div>

    <div class="video-grid" id="videoGrid" data-count="0"></div>
    <canvas id="frozenCanvas" class="hidden"></canvas>

    <div class="pip" id="pipContainer">
      <video id="pipVideo" autoplay playsinline muted></video>
      <div class="pip-label" data-i18n="you">You</div>
    </div>
  </div>

  <div class="annotation-bar" id="annotationBar">
    <div class="color-dot selected" style="background:#FF3B30" data-color="#FF3B30"></div>
    <div class="color-dot" style="background:#34C759" data-color="#34C759"></div>
    <div class="color-dot" style="background:#FFCC00" data-color="#FFCC00"></div>
    <div class="color-dot" style="background:#007AFF" data-color="#007AFF"></div>
    <div class="ann-divider"></div>
    <button class="ann-action" id="clearAnnBtn" data-i18n="clearAll">Clear All</button>
    <button class="ann-action done" id="unfreezeBtn" data-i18n="resume">▶ Resume</button>
  </div>

  <div class="call-toolbar" id="callToolbar">
    <button class="tool-btn" id="muteBtn">
      <span class="ti">🎤</span><span class="tl" data-i18n="mic">Mic</span>
    </button>
    <button class="tool-btn" id="flipBtn">
      <span class="ti">🔄</span><span class="tl" data-i18n="flip">Flip</span>
    </button>
    <button class="freeze-btn" id="freezeBtn">
      <span class="ti">⏸</span><span class="tl" data-i18n="freeze">Freeze</span>
    </button>
    <button class="tool-btn" id="shareBtn2">
      <span class="ti">📤</span><span class="tl" data-i18n="share">Share</span>
    </button>
    <button class="end-btn" id="endBtn">
      <span class="ti">📞</span><span class="tl" data-i18n="end">End</span>
    </button>
  </div>
</div>

<video id="localVideoSrc" class="source-video" autoplay playsinline muted></video>

<script>
// ═════════════════════════════════
// CONFIG
// ═════════════════════════════════
const SERVER_PUBLIC_BASE_URL = "__PUBLIC_BASE_URL__";
const TURN_URLS = "__TURN_URLS__";
const TURN_USERNAME = "__TURN_USERNAME__";
const TURN_CREDENTIAL = "__TURN_CREDENTIAL__";
const SERVER_STORAGE_KEY = 'clarity.serverBase';
const ROOM_CODE_RE = /^[A-Z0-9]{4,8}$/;

// ═════════════════════════════════
// i18n — LANGUAGE SYSTEM
// ═════════════════════════════════
const LANG_KEY = 'clarity.lang';
const I18N = {
  en: {
    tagline: 'See Together. Guide Better.',
    startCall: 'Start Call',
    or: 'or',
    roomPlaceholder: 'Room Code',
    join: 'Join',
    serverToggleOpen: '▾ Server Settings',
    serverToggleClosed: '▸ Server',
    serverHint: "Leave blank to use this page's server.",
    connecting: 'Connecting...',
    waitingOthers: 'Waiting for others...',
    waitingHint: 'Tap below to invite someone',
    shareInvite: '📤 Share Invite Link',
    linkCopied: '✅ Link Copied!',
    clearAll: 'Clear All',
    resume: '▶ Resume',
    mic: 'Mic',
    unmute: 'Unmute',
    noMic: 'No Mic',
    flip: 'Flip',
    freeze: 'Freeze',
    resumeBtn: 'Resume',
    share: 'Share',
    end: 'End',
    you: 'You',
    peer: 'Peer',
    ready: 'Ready',
    preparing: 'Preparing...',
    joiningRoom: 'Joining room...',
    disconnected: 'Disconnected',
    requestingCam: 'Requesting camera/mic…',
    watchMode: 'Camera/mic unavailable. Joining in watch mode.',
    noFlipCam: 'No camera available to flip.',
    flipFailed: 'Could not flip camera on this device/browser.',
    noVideoFreeze: 'No video to freeze yet',
    frozenShared: 'Frozen — shared board',
    frozenDraw: 'Frozen — draw to annotate',
    live: 'Live',
    allLeft: 'All peers left',
    networkIssue: 'Network issue. Retrying...',
    newPeer: 'New peer joining...',
    badCode: 'Room code must be 4–8 letters/numbers',
    autoJoinFail: 'Unable to auto-join room.',
    peersConnected: (n) => `${n} peer(s) connected`,
    peerLeft: (n) => `Peer left. ${n} remaining.`,
    waitingPeers: (room) => `Room ${room} — Waiting for others...`,
  },
  zh: {
    tagline: '一起看，更好地指导',
    startCall: '开始通话',
    or: '或',
    roomPlaceholder: '房间号',
    join: '加入',
    serverToggleOpen: '▾ 服务器设置',
    serverToggleClosed: '▸ 服务器',
    serverHint: '留空则使用当前页面的服务器。',
    connecting: '连接中...',
    waitingOthers: '等待其他人加入...',
    waitingHint: '点击下方按钮邀请他人',
    shareInvite: '📤 分享邀请链接',
    linkCopied: '✅ 链接已复制！',
    clearAll: '清除标注',
    resume: '▶ 恢复',
    mic: '麦克风',
    unmute: '取消静音',
    noMic: '无麦克风',
    flip: '翻转',
    freeze: '冻结',
    resumeBtn: '恢复',
    share: '分享',
    end: '挂断',
    you: '我',
    peer: '参与者',
    ready: '就绪',
    preparing: '准备中...',
    joiningRoom: '正在加入房间...',
    disconnected: '已断开',
    requestingCam: '正在请求摄像头/麦克风…',
    watchMode: '摄像头/麦克风不可用，以观看模式加入。',
    noFlipCam: '没有可翻转的摄像头。',
    flipFailed: '此设备/浏览器无法翻转摄像头。',
    noVideoFreeze: '还没有视频可冻结',
    frozenShared: '已冻结 — 共享画板',
    frozenDraw: '已冻结 — 在画面上标注',
    live: '通话中',
    allLeft: '所有人已离开',
    networkIssue: '网络问题，重试中...',
    newPeer: '新成员加入中...',
    badCode: '房间号须为4-8位字母或数字',
    autoJoinFail: '无法自动加入房间。',
    peersConnected: (n) => `${n} 人已连接`,
    peerLeft: (n) => `有人离开，剩余 ${n} 人。`,
    waitingPeers: (room) => `房间 ${room} — 等待其他人...`,
  }
};

let currentLang = localStorage.getItem(LANG_KEY) || (navigator.language.startsWith('zh') ? 'zh' : 'en');

function t(key, ...args) {
  const val = I18N[currentLang]?.[key] || I18N.en[key] || key;
  return typeof val === 'function' ? val(...args) : val;
}

function applyLang() {
  localStorage.setItem(LANG_KEY, currentLang);
  // Static elements with data-i18n
  document.querySelectorAll('[data-i18n]').forEach(el => {
    const key = el.getAttribute('data-i18n');
    if (el.tagName === 'INPUT') el.placeholder = t(key);
    else el.textContent = t(key);
  });
  // Language toggle button
  const langBtn = document.getElementById('langBtn');
  if (langBtn) langBtn.textContent = currentLang === 'en' ? '中文' : 'EN';
  // Toolbar labels (only update if not dynamically changed)
  syncToolbarLabels();
}

function toggleLang() {
  currentLang = currentLang === 'en' ? 'zh' : 'en';
  applyLang();
}

function syncToolbarLabels() {
  // Freeze button
  const fti = freezeBtn?.querySelector('.ti');
  const ftl = freezeBtn?.querySelector('.tl');
  if (fti && ftl) {
    if (fti.textContent === '▶️') ftl.textContent = t('resumeBtn');
    else ftl.textContent = t('freeze');
  }
  // Mic button
  const mti = muteBtn?.querySelector('.tl');
  if (mti) {
    if (mti.textContent === 'Mic' || mti.textContent === '麦克风') mti.textContent = t('mic');
    if (mti.textContent === 'Unmute' || mti.textContent === '取消静音') mti.textContent = t('unmute');
    if (mti.textContent === 'No Mic' || mti.textContent === '无麦克风') mti.textContent = t('noMic');
  }
  // Other toolbar
  flipBtn?.querySelector('.tl') && (flipBtn.querySelector('.tl').textContent = t('flip'));
  shareBtn2?.querySelector('.tl') && (shareBtn2.querySelector('.tl').textContent = t('share'));
  endBtn?.querySelector('.tl') && (endBtn.querySelector('.tl').textContent = t('end'));
  // Annotation
  clearAnnBtn && (clearAnnBtn.textContent = t('clearAll'));
  unfreezeBtn && (unfreezeBtn.innerHTML = t('resume'));
  // PIP label
  const pipLabel = document.querySelector('.pip-label');
  if (pipLabel) pipLabel.textContent = t('you');
}

// ═════════════════════════════════
// ELEMENTS
// ═════════════════════════════════
const homeScreen = document.getElementById('homeScreen');
const callScreen = document.getElementById('callScreen');
const quickCallBtn = document.getElementById('quickCallBtn');
const homeRoomInput = document.getElementById('homeRoomInput');
const homeJoinBtn = document.getElementById('homeJoinBtn');
const serverToggle = document.getElementById('serverToggle');
const serverPanel = document.getElementById('serverPanel');
const serverUrlInput = document.getElementById('serverUrlInput');
const callStatusText = document.getElementById('callStatusText');
const statusDot = document.getElementById('statusDot');
const roomBadge = document.getElementById('roomBadge');
const peerCountEl = document.getElementById('peerCount');
const waitingView = document.getElementById('waitingView');
const waitingRoomCode = document.getElementById('waitingRoomCode');
const copyLinkBtn2 = document.getElementById('copyLinkBtn2');
const videoGrid = document.getElementById('videoGrid');
const frozenCanvas = document.getElementById('frozenCanvas');
const ctx = frozenCanvas.getContext('2d');
const pipVideo = document.getElementById('pipVideo');
const pipContainer = document.getElementById('pipContainer');
const annotationBar = document.getElementById('annotationBar');
const clearAnnBtn = document.getElementById('clearAnnBtn');
const unfreezeBtn = document.getElementById('unfreezeBtn');
const muteBtn = document.getElementById('muteBtn');
const freezeBtn = document.getElementById('freezeBtn');
const flipBtn = document.getElementById('flipBtn');
const shareBtn2 = document.getElementById('shareBtn2');
const endBtn = document.getElementById('endBtn');
const localVideoSrc = document.getElementById('localVideoSrc');

// ═════════════════════════════════
// STATE
// ═════════════════════════════════
let localStream = null;
let ws = null;
let currentRoom = null;
let myPeerId = null;
let wakeLock = null;
let currentFacingMode = 'user';
let isAutoJoin = false;

// Multi-peer: { peerId: { pc, remoteStream, videoEl, nameTag } }
const peers = {};

let isMuted = false;
let isVideoOff = false;
let isFrozen = false;
let frozenBaseDataUrl = null;
let penColor = '#FF3B30';
let penWidth = 4;
let isDrawing = false;
let lastX = null, lastY = null;

// Offscreen board canvas (GPT refinement — no more Image re-encoding)
const boardCanvas = document.createElement('canvas');
const boardCtx = boardCanvas.getContext('2d');

// ═════════════════════════════════
// HELPERS
// ═════════════════════════════════
function randomCode() { return Math.random().toString(36).slice(2,8).toUpperCase(); }

function sanitizeRoomCode(value) {
  return String(value || '').toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 8);
}

function normalizeBase(raw) {
  const value = String(raw || '').trim();
  if (!value) return '';
  const withProtocol = /^[a-z]+:\/\//i.test(value) ? value : `${location.protocol}//${value}`;
  try {
    const url = new URL(withProtocol);
    return url.origin.replace(/\/+$/, '');
  } catch (_) {
    return '';
  }
}

function getSelectedServerBase() {
  return normalizeBase(serverUrlInput.value);
}

function getShareBase() {
  // If we're already on a public URL (ngrok etc), use it directly
  if (location.hostname !== 'localhost' && location.hostname !== '127.0.0.1') {
    return location.origin;
  }
  return getSelectedServerBase() || normalizeBase(SERVER_PUBLIC_BASE_URL) || location.origin;
}

function getSignalingBase() {
  return getSelectedServerBase() || location.origin;
}

function resolveWsUrl() {
  const url = new URL(getSignalingBase());
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  url.pathname = '/ws';
  url.search = '';
  url.hash = '';
  return url.toString();
}

function getJoinLink() {
  if (!currentRoom) return '';
  const url = new URL(getShareBase());
  url.searchParams.set('room', currentRoom);
  return url.toString();
}

function setStatus(text, connected) {
  callStatusText.textContent = text;
  if (connected !== undefined) statusDot.classList.toggle('connected', !!connected);
}

function persistServerBase() {
  const base = getSelectedServerBase();
  if (base) localStorage.setItem(SERVER_STORAGE_KEY, base);
  else localStorage.removeItem(SERVER_STORAGE_KEY);
  serverUrlInput.value = base;
}

function hydrateServerBase() {
  const params = new URLSearchParams(location.search);
  const queryBase = normalizeBase(params.get('server'));
  const storedBase = normalizeBase(localStorage.getItem(SERVER_STORAGE_KEY));
  const base = queryBase || storedBase || '';
  if (base) {
    serverUrlInput.value = base;
    serverPanel.classList.remove('hidden');
    serverToggle.textContent = t('serverToggleOpen');
  }
}

function updatePeerCount() {
  const n = Object.keys(peers).length + 1; // +1 for self
  peerCountEl.textContent = `${n}/4`;
  if (n > 1) waitingView.classList.add('hidden');
}

// ═════════════════════════════════
// ICE / TURN
// ═════════════════════════════════
function parseTurnUrls(raw) {
  return String(raw || '').split(/[\s,;]+/).map((p) => p.trim()).filter(Boolean);
}

function buildIceServers() {
  const parsedUrls = parseTurnUrls(TURN_URLS);
  const defaultUrls = [
    'stun:stun.l.google.com:19302',
    'stun:stun.relay.metered.ca:80',
    'turn:global.relay.metered.ca:80',
    'turn:global.relay.metered.ca:80?transport=tcp',
    'turn:global.relay.metered.ca:443',
    'turns:global.relay.metered.ca:443?transport=tcp',
  ];
  const urls = parsedUrls.length ? parsedUrls : defaultUrls;
  const servers = [];
  const seen = new Set();

  for (const url of urls) {
    const isTurn = url.startsWith('turn:') || url.startsWith('turns:');
    const entry = isTurn
      ? { urls: url, username: TURN_USERNAME, credential: TURN_CREDENTIAL }
      : { urls: url };
    const key = JSON.stringify(entry);
    if (seen.has(key)) continue;
    seen.add(key);
    servers.push(entry);
  }

  if (!servers.some((s) => String(s.urls).startsWith('stun:'))) {
    servers.unshift({ urls: 'stun:stun.l.google.com:19302' });
  }

  return servers;
}

function buildRtcConfig() {
  return {
    iceServers: buildIceServers(),
    iceCandidatePoolSize: 6,
  };
}

function sendWs(payload) {
  if (!ws || ws.readyState !== WebSocket.OPEN || !currentRoom) return;
  ws.send(JSON.stringify({ ...payload, room: currentRoom, from: myPeerId }));
}

async function safePlay(el, forceMuted = false) {
  if (!el) return false;
  if (forceMuted) el.muted = true;
  try { await el.play(); return true; } catch (_) { return false; }
}

async function nudgePlayback() {
  // Nudge all peer videos + local
  for (const p of Object.values(peers)) {
    if (p.videoEl) await safePlay(p.videoEl, false);
  }
  await safePlay(localVideoSrc, true);
  await safePlay(pipVideo, true);
}

document.addEventListener('pointerdown', () => { nudgePlayback(); }, { passive: true });

// ═════════════════════════════════
// WAKE LOCK
// ═════════════════════════════════
async function requestWakeLock() {
  if (!('wakeLock' in navigator) || wakeLock) return;
  try {
    wakeLock = await navigator.wakeLock.request('screen');
    wakeLock.addEventListener('release', () => { wakeLock = null; }, { once: true });
  } catch (_) {}
}

async function releaseWakeLock() {
  try { await wakeLock?.release(); } catch (_) {} finally { wakeLock = null; }
}

document.addEventListener('visibilitychange', async () => {
  if (document.visibilityState === 'visible' && callScreen.classList.contains('active')) {
    await requestWakeLock();
    await nudgePlayback();
  }
});

// ═════════════════════════════════
// BOARD CANVAS (offscreen)
// ═════════════════════════════════
function hasBoard() { return boardCanvas.width > 0 && boardCanvas.height > 0; }

function clearBoard() {
  boardCanvas.width = 0;
  boardCanvas.height = 0;
  frozenBaseDataUrl = null;
}

function exportBoardDataUrl() {
  if (!hasBoard()) return null;
  return boardCanvas.toDataURL('image/png');
}

// ═════════════════════════════════
// SYNC BUTTON STATES
// ═════════════════════════════════
function syncLocalButtonStates() {
  const audioTrack = localStream?.getAudioTracks?.()[0] || null;
  const videoTrack = localStream?.getVideoTracks?.()[0] || null;

  isMuted = audioTrack ? !audioTrack.enabled : true;
  isVideoOff = videoTrack ? !videoTrack.enabled : true;

  muteBtn.classList.toggle('active-state', !!audioTrack && isMuted);
  muteBtn.querySelector('.ti').textContent = audioTrack ? (isMuted ? '🔇' : '🎤') : '🚫';
  muteBtn.querySelector('.tl').textContent = audioTrack ? (isMuted ? t('unmute') : t('mic')) : t('noMic');

  const showPip = !!videoTrack && !isFrozen;
  pipContainer.classList.toggle('hidden', !showPip);
}

// ═════════════════════════════════
// VIDEO GRID
// ═════════════════════════════════
function rebuildVideoGrid() {
  const peerIds = Object.keys(peers);
  videoGrid.innerHTML = '';
  videoGrid.setAttribute('data-count', String(peerIds.length));

  peerIds.forEach(pid => {
    const p = peers[pid];
    const cell = document.createElement('div');
    cell.className = 'video-cell';

    const video = document.createElement('video');
    video.autoplay = true;
    video.playsInline = true;
    video.muted = false;
    if (p.remoteStream) video.srcObject = p.remoteStream;
    p.videoEl = video;
    cell.appendChild(video);

    const tag = document.createElement('div');
    tag.className = 'name-tag';
    tag.textContent = p.label || `${t('peer')} ${pid.slice(0,4)}`;
    cell.appendChild(tag);

    videoGrid.appendChild(cell);
    safePlay(video, false);
  });

  updatePeerCount();
}

// ═════════════════════════════════
// MULTI-PEER WebRTC (MESH)
// ═════════════════════════════════
function createPeerConnection(peerId) {
  if (peers[peerId]?.pc) return peers[peerId].pc;

  if (!peers[peerId]) {
    peers[peerId] = { pc: null, remoteStream: null, videoEl: null, label: `${t('peer')} ${peerId.slice(0,4)}` };
  }

  const pc = new RTCPeerConnection(buildRtcConfig());
  peers[peerId].pc = pc;

  // Add local tracks
  if (localStream) {
    localStream.getTracks().forEach(t => pc.addTrack(t, localStream));
  }

  // Remote tracks
  pc.ontrack = async (ev) => {
    console.log(`[RTC:${peerId.slice(0,4)}] ontrack`, ev.track.kind);
    const stream = ev.streams?.[0] || new MediaStream([ev.track]);
    peers[peerId].remoteStream = stream;
    rebuildVideoGrid();
    if (!isFrozen) setStatus(t('peersConnected', Object.keys(peers).length), true);
  };

  // ICE candidates
  pc.onicecandidate = (ev) => {
    if (ev.candidate) {
      sendWs({ type: 'candidate', to: peerId, candidate: ev.candidate });
    }
  };

  // Connection state
  pc.onconnectionstatechange = () => {
    const s = pc.connectionState;
    console.log(`[RTC:${peerId.slice(0,4)}] state: ${s}`);
    if (s === 'connected') {
      if (!isFrozen) setStatus(t('peersConnected', Object.keys(peers).length), true);
    } else if (s === 'failed') {
      try { pc.restartIce?.(); } catch (_) {}
    } else if (s === 'closed') {
      removePeer(peerId);
    }
  };

  pc.oniceconnectionstatechange = () => {
    const state = pc.iceConnectionState;
    if (state === 'checking') setStatus(t('connecting'), false);
    else if (state === 'connected' || state === 'completed') {
      if (!isFrozen) setStatus(t('peersConnected', Object.keys(peers).length), true);
    } else if (state === 'failed') {
      setStatus(t('networkIssue'), false);
    }
  };

  return pc;
}

function removePeer(peerId) {
  const p = peers[peerId];
  if (!p) return;
  if (p.pc) {
    try {
      p.pc.ontrack = null;
      p.pc.onicecandidate = null;
      p.pc.onconnectionstatechange = null;
      p.pc.oniceconnectionstatechange = null;
      p.pc.close();
    } catch(_) {}
  }
  delete peers[peerId];
  rebuildVideoGrid();
  updatePeerCount();
  if (Object.keys(peers).length === 0) {
    waitingView.classList.remove('hidden');
    setStatus(t('allLeft'), false);
  }
}

async function makeOfferTo(peerId) {
  const pc = createPeerConnection(peerId);
  try {
    const offer = await pc.createOffer({ offerToReceiveAudio: true, offerToReceiveVideo: true });
    await pc.setLocalDescription(offer);
    sendWs({ type: 'offer', to: peerId, sdp: pc.localDescription });
  } catch(e) { console.error('offer error', e); }
}

async function handleOffer(msg) {
  const peerId = msg.from;
  const pc = createPeerConnection(peerId);
  try {
    await pc.setRemoteDescription(new RTCSessionDescription(msg.sdp));
    if (localStream) {
      const senders = pc.getSenders();
      const kinds = new Set(senders.map(s => s.track?.kind).filter(Boolean));
      localStream.getTracks().forEach(t => {
        if (!kinds.has(t.kind)) pc.addTrack(t, localStream);
      });
    }
    const answer = await pc.createAnswer();
    await pc.setLocalDescription(answer);
    sendWs({ type: 'answer', to: peerId, sdp: pc.localDescription });
  } catch(e) { console.error('handleOffer error', e); }
}

async function handleAnswer(msg) {
  const peerId = msg.from;
  const p = peers[peerId];
  if (!p?.pc) return;
  try {
    await p.pc.setRemoteDescription(new RTCSessionDescription(msg.sdp));
    // Flush pending candidates
    if (p.pendingCandidates) {
      while (p.pendingCandidates.length) {
        try { await p.pc.addIceCandidate(p.pendingCandidates.shift()); } catch(_) {}
      }
    }
  } catch(e) { console.error('handleAnswer error', e); }
}

async function handleCandidate(msg) {
  const peerId = msg.from;
  const p = peers[peerId];
  if (!p) return;
  const candidate = new RTCIceCandidate(msg.candidate);
  if (p.pc?.remoteDescription?.type) {
    try { await p.pc.addIceCandidate(candidate); } catch(_) {}
  } else {
    if (!p.pendingCandidates) p.pendingCandidates = [];
    p.pendingCandidates.push(candidate);
  }
}

// ═════════════════════════════════
// MEDIA (with fallback chain)
// ═════════════════════════════════
function cleanupLocalStream() {
  if (localStream) {
    localStream.getTracks().forEach((track) => {
      try { track.stop(); } catch (_) {}
    });
  }
  localStream = null;
  localVideoSrc.srcObject = null;
  pipVideo.srcObject = null;
}

async function setupMedia() {
  cleanupLocalStream();
  setStatus(t('requestingCam'), false);

  const attempts = [
    {
      video: { facingMode: currentFacingMode, width: { ideal: 1280 }, height: { ideal: 720 } },
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    },
    { video: { facingMode: currentFacingMode }, audio: true },
    { video: true, audio: false },
    { video: false, audio: true },
  ];

  let lastError = null;
  for (const constraints of attempts) {
    try {
      localStream = await navigator.mediaDevices.getUserMedia(constraints);
      localVideoSrc.srcObject = localStream;
      pipVideo.srcObject = localStream;
      await safePlay(localVideoSrc, true);
      await safePlay(pipVideo, true);
      syncLocalButtonStates();
      return true;
    } catch (error) {
      lastError = error;
    }
  }

  console.warn('Media setup failed', lastError);
  localVideoSrc.srcObject = null;
  pipVideo.srcObject = null;
  syncLocalButtonStates();
  setStatus(t('watchMode'), false);
  return false;
}

// ═════════════════════════════════
// CAMERA FLIP (proper replaceTrack)
// ═════════════════════════════════
async function replaceVideoTrack(newTrack) {
  if (!newTrack) return;

  if (!localStream) {
    localStream = new MediaStream([newTrack]);
  } else {
    const oldTrack = localStream.getVideoTracks()[0];
    if (oldTrack) {
      localStream.removeTrack(oldTrack);
      try { oldTrack.stop(); } catch (_) {}
    }
    localStream.addTrack(newTrack);
  }

  localVideoSrc.srcObject = localStream;
  pipVideo.srcObject = localStream;
  await safePlay(localVideoSrc, true);
  await safePlay(pipVideo, true);

  // Replace track on all peer connections
  for (const p of Object.values(peers)) {
    if (!p.pc) continue;
    const sender = p.pc.getSenders().find((s) => s.track && s.track.kind === 'video');
    if (sender) {
      await sender.replaceTrack(newTrack);
    } else {
      p.pc.addTrack(newTrack, localStream);
    }
  }

  syncLocalButtonStates();
}

async function flipCamera() {
  const currentVideoTrack = localStream?.getVideoTracks?.()[0];
  if (!currentVideoTrack) {
    setStatus(t('noFlipCam'), false);
    return;
  }

  const nextFacingMode = currentFacingMode === 'user' ? 'environment' : 'user';
  try {
    const tempStream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: nextFacingMode } },
      audio: false,
    });
    const newTrack = tempStream.getVideoTracks()[0];
    if (!newTrack) {
      tempStream.getTracks().forEach((t) => t.stop());
      throw new Error('No replacement video track');
    }
    currentFacingMode = nextFacingMode;
    await replaceVideoTrack(newTrack);
    tempStream.getTracks().forEach((t) => {
      if (t !== newTrack) { try { t.stop(); } catch (_) {} }
    });
    flipBtn.classList.toggle('active-state', currentFacingMode === 'environment');
  } catch (error) {
    console.warn('Flip camera failed', error);
    setStatus(t('flipFailed'), false);
  }
}

// ═════════════════════════════════
// FREEZE & ANNOTATE (board canvas)
// ═════════════════════════════════
function getBoardRect() {
  if (!hasBoard()) return null;
  const scale = Math.min(frozenCanvas.width / boardCanvas.width, frozenCanvas.height / boardCanvas.height);
  const width = boardCanvas.width * scale;
  const height = boardCanvas.height * scale;
  return {
    x: (frozenCanvas.width - width) / 2,
    y: (frozenCanvas.height - height) / 2,
    width, height,
  };
}

function resizeCanvas() {
  const rect = frozenCanvas.parentElement.getBoundingClientRect();
  frozenCanvas.width = Math.floor(rect.width);
  frozenCanvas.height = Math.floor(rect.height);
  redrawFrozen();
}

function redrawFrozen() {
  ctx.clearRect(0, 0, frozenCanvas.width, frozenCanvas.height);
  ctx.fillStyle = '#1E1E1E';
  ctx.fillRect(0, 0, frozenCanvas.width, frozenCanvas.height);
  if (!hasBoard()) return;
  const rect = getBoardRect();
  if (!rect) return;
  ctx.drawImage(boardCanvas, rect.x, rect.y, rect.width, rect.height);
}

function canvasToBoard(cx, cy) {
  const rect = getBoardRect();
  if (!rect) return null;
  if (cx < rect.x || cy < rect.y || cx > rect.x + rect.width || cy > rect.y + rect.height) return null;
  return {
    x: ((cx - rect.x) / rect.width) * boardCanvas.width,
    y: ((cy - rect.y) / rect.height) * boardCanvas.height,
  };
}

function drawLineOnBoard(x1, y1, x2, y2, color, width) {
  if (!hasBoard()) return;
  boardCtx.strokeStyle = color || penColor;
  boardCtx.lineWidth = width || penWidth;
  boardCtx.lineCap = 'round';
  boardCtx.lineJoin = 'round';
  boardCtx.beginPath();
  boardCtx.moveTo(x1, y1);
  boardCtx.lineTo(x2, y2);
  boardCtx.stroke();
  redrawFrozen();
}

function loadImage(dataUrl) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = reject;
    img.src = dataUrl;
  });
}

async function loadFrozenImage(dataUrl, baseUrl, options = {}) {
  if (!dataUrl) return;
  const { remote = true } = options;
  try {
    const img = await loadImage(dataUrl);
    boardCanvas.width = img.width;
    boardCanvas.height = img.height;
    boardCtx.clearRect(0, 0, boardCanvas.width, boardCanvas.height);
    boardCtx.drawImage(img, 0, 0);
    frozenBaseDataUrl = baseUrl || dataUrl;
    enterFreezeMode({ remote });
    redrawFrozen();
  } catch (error) {
    console.warn('Load frozen image failed', error);
  }
}

function captureVideoFrame(videoEl) {
  const tmp = document.createElement('canvas');
  tmp.width = videoEl.videoWidth;
  tmp.height = videoEl.videoHeight;
  tmp.getContext('2d').drawImage(videoEl, 0, 0);
  return tmp.toDataURL('image/png');
}

function freezeFrame() {
  // Try to capture from the first remote peer's video, or local
  let sourceVideo = null;
  const firstPeer = Object.values(peers)[0];
  if (firstPeer?.videoEl && firstPeer.videoEl.readyState >= 2 && firstPeer.videoEl.videoWidth > 0) {
    sourceVideo = firstPeer.videoEl;
  } else if (localVideoSrc.readyState >= 2 && localVideoSrc.videoWidth > 0) {
    sourceVideo = localVideoSrc;
  }
  if (!sourceVideo) { setStatus(t('noVideoFreeze')); return; }

  const url = captureVideoFrame(sourceVideo);
  loadFrozenImage(url, url, { remote: false });
  sendWs({ type: 'freeze_frame', base_data_url: url, current_data_url: url });
}

function enterFreezeMode({ remote = false } = {}) {
  isFrozen = true;
  resizeCanvas();
  frozenCanvas.classList.remove('hidden');
  annotationBar.classList.add('active');
  pipContainer.classList.add('hidden');
  freezeBtn.classList.add('frozen');
  freezeBtn.querySelector('.ti').textContent = '▶️';
  freezeBtn.querySelector('.tl').textContent = t('resumeBtn');
  setStatus(remote ? t('frozenShared') : t('frozenDraw'));
}

function exitFreezeMode({ notify = true, silent = false } = {}) {
  isFrozen = false;
  isDrawing = false;
  lastX = null;
  lastY = null;
  frozenCanvas.classList.add('hidden');
  annotationBar.classList.remove('active');
  if (localStream?.getVideoTracks?.().length) {
    pipContainer.classList.remove('hidden');
  }
  freezeBtn.classList.remove('frozen');
  freezeBtn.querySelector('.ti').textContent = '⏸';
  freezeBtn.querySelector('.tl').textContent = t('freeze');
  if (!silent) {
    const connected = Object.keys(peers).length > 0;
    setStatus(connected ? t('live') : t('waitingPeers', currentRoom || ''), connected);
  }
  if (notify) sendWs({ type: 'resume_live' });
}

function clearAnnotations() {
  if (!frozenBaseDataUrl) return;
  loadFrozenImage(frozenBaseDataUrl, frozenBaseDataUrl, { remote: false });
  sendWs({ type: 'clear_annotations', base_data_url: frozenBaseDataUrl, current_data_url: frozenBaseDataUrl });
}

function sendBoardSnapshot() {
  const currentDataUrl = exportBoardDataUrl();
  if (!currentDataUrl) return;
  sendWs({
    type: 'board_snapshot',
    current_data_url: currentDataUrl,
    base_data_url: frozenBaseDataUrl || currentDataUrl,
  });
}

// Pointer events (unified touch/mouse)
function handlePointerStart(event) {
  if (!isFrozen || !hasBoard()) return;
  event.preventDefault();
  frozenCanvas.setPointerCapture?.(event.pointerId);
  const rect = frozenCanvas.getBoundingClientRect();
  const point = canvasToBoard(event.clientX - rect.left, event.clientY - rect.top);
  if (!point) return;
  isDrawing = true;
  lastX = point.x;
  lastY = point.y;
}

function handlePointerMove(event) {
  if (!isDrawing || !hasBoard()) return;
  event.preventDefault();
  const rect = frozenCanvas.getBoundingClientRect();
  const point = canvasToBoard(event.clientX - rect.left, event.clientY - rect.top);
  if (!point) return;
  drawLineOnBoard(lastX, lastY, point.x, point.y, penColor, penWidth);
  sendWs({ type: 'draw_line', x1: lastX, y1: lastY, x2: point.x, y2: point.y, color: penColor, width: penWidth });
  lastX = point.x;
  lastY = point.y;
}

function handlePointerEnd() {
  if (isDrawing && hasBoard()) sendBoardSnapshot();
  isDrawing = false;
  lastX = null;
  lastY = null;
}

frozenCanvas.addEventListener('pointerdown', handlePointerStart, { passive: false });
frozenCanvas.addEventListener('pointermove', handlePointerMove, { passive: false });
frozenCanvas.addEventListener('pointerup', handlePointerEnd);
frozenCanvas.addEventListener('pointercancel', handlePointerEnd);
frozenCanvas.addEventListener('pointerleave', handlePointerEnd);

// ═════════════════════════════════
// SIGNALING (4-PERSON MESH)
// ═════════════════════════════════
function connectWs(room) {
  const normalizedRoom = sanitizeRoomCode(room);
  if (!ROOM_CODE_RE.test(normalizedRoom)) {
    return Promise.reject(new Error('Room code must be 4–8 letters/numbers.'));
  }

  return new Promise((resolve, reject) => {
    const socket = new WebSocket(resolveWsUrl());
    let settled = false;

    const fail = (message) => {
      setStatus(message, false);
      if (!settled) { settled = true; reject(new Error(message)); }
    };

    socket.onopen = () => {
      ws = socket;
      setStatus(t('joiningRoom'), false);
      socket.send(JSON.stringify({ type: 'join', room: normalizedRoom }));
    };

    socket.onerror = (error) => {
      console.warn('WebSocket error', error);
      fail('Server error');
    };

    socket.onclose = () => {
      if (!settled) {
        fail('Could not join room');
      } else if (currentRoom) {
        setStatus(t('disconnected'), false);
      }
    };

    socket.onmessage = async (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (_) { return; }

      if (msg.type === 'joined') {
        myPeerId = msg.peer_id;
        setStatus(t('waitingPeers', normalizedRoom), false);
        updatePeerCount();
        if (!settled) { settled = true; resolve(); }
        return;
      }

      if (msg.type === 'room_full') {
        fail('Room full (max 4)');
        socket.close();
        return;
      }

      if (msg.type === 'invalid_room') {
        fail('Room code must be 4–8 letters/numbers');
        socket.close();
        return;
      }

      if (msg.type === 'peer_joined') {
        // New peer arrived — I should create an offer to them
        const newPeerId = msg.peer_id;
        console.log(`[Signal] peer_joined: ${newPeerId.slice(0,4)}`);
        setStatus(t('newPeer'));
        await makeOfferTo(newPeerId);
        return;
      }

      if (msg.type === 'peer_left') {
        removePeer(msg.peer_id);
        setStatus(t('peerLeft', Object.keys(peers).length));
        return;
      }

      if (msg.type === 'existing_peers') {
        // When I join, I get list of existing peers — they will send me offers
        console.log(`[Signal] existing peers:`, msg.peer_ids);
        return;
      }

      if (msg.type === 'offer') { await handleOffer(msg); return; }
      if (msg.type === 'answer') { await handleAnswer(msg); return; }
      if (msg.type === 'candidate') { await handleCandidate(msg); return; }

      // Freeze / annotation sync (broadcast to all)
      if (msg.type === 'freeze_frame' || msg.type === 'sync_state') {
        const url = msg.current_data_url || msg.base_data_url;
        if (url) await loadFrozenImage(url, msg.base_data_url || url);
        return;
      }
      if (msg.type === 'clear_annotations') {
        const url = msg.current_data_url || msg.base_data_url;
        if (url) await loadFrozenImage(url, url);
        return;
      }
      if (msg.type === 'draw_line') {
        drawLineOnBoard(msg.x1, msg.y1, msg.x2, msg.y2, msg.color, msg.width);
        return;
      }
      if (msg.type === 'board_snapshot') {
        const url = msg.current_data_url;
        if (url) await loadFrozenImage(url, msg.base_data_url || url);
        return;
      }
      if (msg.type === 'resume_live') {
        clearBoard();
        exitFreezeMode({ notify: false });
        return;
      }
    };
  });
}

// ═════════════════════════════════
// CLEANUP
// ═════════════════════════════════
function cleanupSocket() {
  if (!ws) return;
  const socket = ws;
  ws = null;
  socket.onclose = null;
  socket.onerror = null;
  socket.onmessage = null;
  try { socket.close(); } catch (_) {}
}

function cleanup() {
  Object.keys(peers).forEach(removePeer);
  cleanupLocalStream();
  cleanupSocket();
  currentRoom = null;
  myPeerId = null;
  exitFreezeMode({ notify: false, silent: true });
  clearBoard();
  waitingView.classList.remove('hidden');
  videoGrid.innerHTML = '';
  videoGrid.setAttribute('data-count', '0');
  pipVideo.srcObject = null;
  setStatus(t('ready'), false);
  releaseWakeLock();
}

// ═════════════════════════════════
// NAVIGATION
// ═════════════════════════════════
function showCallScreen() {
  homeScreen.style.display = 'none';
  callScreen.classList.add('active');
  setStatus(t('preparing'), false);
}

function showHomeScreen() {
  callScreen.classList.remove('active');
  homeScreen.style.display = 'flex';
  cleanup();
}

async function startRoom(roomCode) {
  const room = sanitizeRoomCode(roomCode);
  homeRoomInput.value = room;
  if (!ROOM_CODE_RE.test(room)) {
    setStatus(t('badCode'), false);
    homeRoomInput.focus();
    return;
  }

  persistServerBase();
  showCallScreen();
  await requestWakeLock();
  await setupMedia();

  currentRoom = room;
  roomBadge.textContent = room;
  waitingRoomCode.textContent = room;
  waitingView.classList.remove('hidden');

  try {
    await connectWs(room);
  } catch (error) {
    console.warn('Join failed', error);
    if (isAutoJoin) {
      setStatus(t('autoJoinFail'), false);
    }
    setTimeout(() => { showHomeScreen(); }, 500);
  }
}

// ═════════════════════════════════
// EVENT BINDINGS
// ═════════════════════════════════
quickCallBtn.addEventListener('click', async () => {
  isAutoJoin = false;
  await startRoom(randomCode());
  // Auto-trigger share after room is created
  setTimeout(autoShare, 600);
});

homeJoinBtn.addEventListener('click', async () => {
  isAutoJoin = false;
  await startRoom(homeRoomInput.value);
});

homeRoomInput.addEventListener('input', () => {
  homeRoomInput.value = sanitizeRoomCode(homeRoomInput.value);
});

homeRoomInput.addEventListener('keydown', async (event) => {
  if (event.key === 'Enter') {
    event.preventDefault();
    isAutoJoin = false;
    await startRoom(homeRoomInput.value);
  }
});

serverToggle.addEventListener('click', () => {
  serverPanel.classList.toggle('hidden');
  serverToggle.textContent = serverPanel.classList.contains('hidden') ? t('serverToggleClosed') : t('serverToggleOpen');
});

serverUrlInput.addEventListener('change', persistServerBase);
serverUrlInput.addEventListener('blur', persistServerBase);

async function autoShare() {
  const link = getJoinLink();
  if (!link) return;
  try {
    if (navigator.share) {
      await navigator.share({ title: 'Join Clarity', text: 'Join my video call', url: link });
      return;
    }
  } catch (_) {}
  // Fallback: copy to clipboard
  try {
    await navigator.clipboard.writeText(link);
    copyLinkBtn2.textContent = t('linkCopied');
    setTimeout(() => { copyLinkBtn2.textContent = t('shareInvite'); }, 3000);
  } catch (_) {
    prompt('Copy this link:', link);
  }
}

copyLinkBtn2.addEventListener('click', autoShare);

muteBtn.addEventListener('click', () => {
  const audioTrack = localStream?.getAudioTracks?.()[0];
  if (!audioTrack) return;
  audioTrack.enabled = !audioTrack.enabled;
  syncLocalButtonStates();
});

shareBtn2.addEventListener('click', autoShare);

freezeBtn.addEventListener('click', () => {
  if (isFrozen) {
    clearBoard();
    exitFreezeMode({ notify: true });
  } else {
    freezeFrame();
  }
});

flipBtn.addEventListener('click', async () => {
  await flipCamera();
});

endBtn.addEventListener('click', showHomeScreen);
clearAnnBtn.addEventListener('click', clearAnnotations);
unfreezeBtn.addEventListener('click', () => {
  clearBoard();
  exitFreezeMode({ notify: true });
});

document.querySelectorAll('.color-dot').forEach((dot) => {
  dot.addEventListener('click', () => {
    document.querySelectorAll('.color-dot').forEach((item) => item.classList.remove('selected'));
    dot.classList.add('selected');
    penColor = dot.dataset.color;
  });
});

window.addEventListener('resize', () => { if (isFrozen) resizeCanvas(); });
window.addEventListener('pagehide', () => { cleanup(); });

// Service Worker registration
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  });
}

// Auto-join from URL
(async function init() {
  applyLang();
  hydrateServerBase();
  syncLocalButtonStates();
  const params = new URLSearchParams(location.search);
  const room = sanitizeRoomCode(params.get('room'));
  if (room && ROOM_CODE_RE.test(room)) {
    isAutoJoin = true;
    await startRoom(room);
  }
})();
</script>
</body>
</html>
'''


# ============================================================
# BACKEND HELPERS
# ============================================================
def get_turn_value(env_name: str, fallback: str) -> str:
    return os.getenv(env_name, "").strip() or fallback.strip()


def normalize_room(raw: object) -> Optional[str]:
    room = re.sub(r"[^A-Za-z0-9]", "", str(raw or "").upper())[:8]
    if ROOM_CODE_RE.fullmatch(room):
        return room
    return None


def find_free_port(preferred_port: int) -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        try:
            sock.bind(("127.0.0.1", preferred_port))
            return preferred_port
        except OSError:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]


def _read_ngrok_token_from_config() -> str:
    """Try to read authtoken from system ngrok config files."""
    import pathlib
    paths = [
        pathlib.Path.home() / "Library" / "Application Support" / "ngrok" / "ngrok.yml",
        pathlib.Path.home() / ".config" / "ngrok" / "ngrok.yml",
        pathlib.Path.home() / ".ngrok2" / "ngrok.yml",
    ]
    for p in paths:
        try:
            text = p.read_text()
            for line in text.splitlines():
                if line.strip().startswith("authtoken:"):
                    return line.split(":", 1)[1].strip()
        except Exception:
            continue
    return ""


def try_start_ngrok(port: int):
    global NGROK_TUNNEL
    try:
        from pyngrok import ngrok

        # 1) env var  2) system ngrok config file
        token = os.getenv("NGROK_AUTHTOKEN", "").strip()
        if not token:
            token = _read_ngrok_token_from_config()
        if token:
            try:
                ngrok.set_auth_token(token)
                print(f"  [ngrok] Auth token found ({token[:8]}...)")
            except Exception:
                pass
        NGROK_TUNNEL = ngrok.connect(addr=port, proto="http")
        return NGROK_TUNNEL.public_url.rstrip("/")
    except Exception as exc:
        print(f"  [ngrok] Auto-start failed: {exc}")
        return None


def render_index_html() -> str:
    html = INDEX_HTML
    for key, value in {
        "__PUBLIC_BASE_URL__": PUBLIC_BASE_URL or "",
        "__TURN_URLS__": get_turn_value("TURN_URLS", TURN_URLS_FALLBACK),
        "__TURN_USERNAME__": get_turn_value("TURN_USERNAME", TURN_USERNAME_FALLBACK),
        "__TURN_CREDENTIAL__": get_turn_value("TURN_CREDENTIAL", TURN_CREDENTIAL_FALLBACK),
    }.items():
        html = html.replace(key, value.replace("\\", "\\\\").replace('"', '\\"'))
    return html


# ============================================================
# BACKEND — 4-PERSON ROOMS WITH PEER IDs
# ============================================================
async def index(request: web.Request) -> web.Response:
    return web.Response(
        text=render_index_html(),
        content_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


async def manifest_handler(request: web.Request) -> web.Response:
    return web.Response(
        text=MANIFEST_JSON,
        content_type="application/json",
        headers={"Cache-Control": "public, max-age=3600"},
    )


async def service_worker(request: web.Request) -> web.Response:
    return web.Response(
        text=SERVICE_WORKER_JS,
        content_type="application/javascript",
        headers={"Cache-Control": "no-store"},
    )


async def icon(request: web.Request) -> web.Response:
    return web.Response(
        text=ICON_SVG,
        content_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


async def healthz(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "ok": True,
            "rooms": len(ROOMS),
            "peers": sum(len(peers) for peers in ROOMS.values()),
            "public_base_url": PUBLIC_BASE_URL,
        }
    )


async def broadcast_to_room(room: str, sender_pid: str, payload: dict) -> None:
    """Broadcast a message to all peers in a room except the sender."""
    room_peers = ROOMS.get(room)
    if not room_peers:
        return
    for pid, peer_ws in list(room_peers.items()):
        if pid == sender_pid:
            continue
        try:
            await peer_ws.send_json(payload)
        except Exception:
            pass


async def leave_room(room: str, peer_id: str) -> None:
    """Remove a peer from a room and notify others."""
    room_peers = ROOMS.get(room)
    if not room_peers or peer_id not in room_peers:
        return

    del room_peers[peer_id]

    # Notify remaining peers
    for pid, peer_ws in list(room_peers.items()):
        try:
            await peer_ws.send_json({
                "type": "peer_left",
                "peer_id": peer_id,
            })
        except Exception:
            pass

    # Clean up empty room
    if not room_peers:
        ROOMS.pop(room, None)
        ROOM_STATES.pop(room, None)


async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    ws_conn = web.WebSocketResponse(heartbeat=25, max_msg_size=MAX_WS_MESSAGE_SIZE)
    await ws_conn.prepare(request)
    joined_room: Optional[str] = None
    peer_id: Optional[str] = None

    try:
        async for msg in ws_conn:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue

                msg_type = str(data.get("type", "")).strip()

                if msg_type == "join":
                    room = normalize_room(data.get("room"))
                    if not room:
                        await ws_conn.send_json({"type": "invalid_room"})
                        continue

                    # If already in another room, leave it first
                    if joined_room and joined_room != room:
                        await leave_room(joined_room, peer_id)
                        joined_room = None
                        peer_id = None

                    if room not in ROOMS:
                        ROOMS[room] = {}

                    if len(ROOMS[room]) >= MAX_PEERS:
                        await ws_conn.send_json({"type": "room_full"})
                        continue

                    peer_id = uuid.uuid4().hex[:8]
                    joined_room = room
                    ROOMS[room][peer_id] = ws_conn

                    # Tell the new peer their ID
                    existing = [pid for pid in ROOMS[room] if pid != peer_id]
                    await ws_conn.send_json({
                        "type": "joined",
                        "room": room,
                        "peer_id": peer_id,
                    })

                    if existing:
                        await ws_conn.send_json({
                            "type": "existing_peers",
                            "peer_ids": existing,
                        })

                    # Sync frozen state if any
                    state = ROOM_STATES.get(room)
                    if state and state.get("current_data_url"):
                        await ws_conn.send_json({
                            "type": "sync_state",
                            "base_data_url": state.get("base_data_url"),
                            "current_data_url": state.get("current_data_url"),
                        })

                    # Notify existing peers about the newcomer
                    for pid, peer_ws in list(ROOMS[room].items()):
                        if pid != peer_id:
                            try:
                                await peer_ws.send_json({
                                    "type": "peer_joined",
                                    "peer_id": peer_id,
                                })
                            except Exception:
                                pass

                    continue

                # Guard: must be in a room for all other messages
                if not joined_room or not peer_id:
                    continue

                # Cross-room guard
                msg_room = normalize_room(data.get("room"))
                if msg_room != joined_room:
                    continue

                # Targeted signaling: forward offer/answer/candidate to specific peer
                if msg_type in {"offer", "answer", "candidate"}:
                    target_id = data.get("to")
                    if not target_id:
                        continue

                    target_ws = ROOMS.get(joined_room, {}).get(target_id)
                    if target_ws:
                        data["from"] = peer_id
                        try:
                            await target_ws.send_json(data)
                        except Exception:
                            pass

                # Broadcast annotation/freeze messages
                elif msg_type in {"freeze_frame", "clear_annotations", "draw_line", "board_snapshot", "resume_live"}:
                    if msg_type in {"freeze_frame", "clear_annotations", "board_snapshot"}:
                        ROOM_STATES[joined_room] = {
                            "base_data_url": data.get("base_data_url"),
                            "current_data_url": data.get("current_data_url"),
                        }
                    elif msg_type == "resume_live":
                        ROOM_STATES.pop(joined_room, None)

                    await broadcast_to_room(joined_room, peer_id, data)

            elif msg.type == WSMsgType.ERROR:
                print(f"WebSocket error: {ws_conn.exception()}")

    finally:
        if joined_room and peer_id:
            await leave_room(joined_room, peer_id)

    return ws_conn


async def on_shutdown(app: web.Application) -> None:
    """Gracefully close all WebSocket connections on shutdown."""
    sockets: list[web.WebSocketResponse] = []
    for room_peers in ROOMS.values():
        sockets.extend(list(room_peers.values()))
    for ws_conn in sockets:
        try:
            await ws_conn.close()
        except Exception:
            pass
    ROOMS.clear()
    ROOM_STATES.clear()


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/manifest.json", manifest_handler)
    app.router.add_get("/sw.js", service_worker)
    app.router.add_get("/icon.svg", icon)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/ws", websocket_handler)
    app.on_shutdown.append(on_shutdown)
    return app


def open_browser_later(url: str, delay: float = 1.2) -> None:
    def _open() -> None:
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:
            pass
    threading.Thread(target=_open, daemon=True).start()


if __name__ == "__main__":
    # ---- Cloud vs Local detection ----
    # Render / Railway / fly.io set PORT env var
    IS_CLOUD = bool(os.getenv("PORT") or os.getenv("RENDER"))
    PORT = int(os.getenv("PORT", PORT))

    if not IS_CLOUD:
        PORT = find_free_port(PORT)

    local_url = f"http://localhost:{PORT}"

    # PUBLIC_BASE_URL: on cloud it comes from RENDER_EXTERNAL_URL or PUBLIC_URL
    PUBLIC_BASE_URL = (
        os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
        or os.getenv("PUBLIC_URL", "").strip().rstrip("/")
        or None
    )

    # Only try ngrok locally
    if not PUBLIC_BASE_URL and not IS_CLOUD:
        PUBLIC_BASE_URL = try_start_ngrok(PORT)

    turn_urls = get_turn_value("TURN_URLS", TURN_URLS_FALLBACK)
    turn_username = get_turn_value("TURN_USERNAME", TURN_USERNAME_FALLBACK)

    print("=" * 60)
    print("  Clarity — 4-Person Mobile MVP (Final)")
    print("  See Together. Guide Better.")
    print("=" * 60)
    mode = "Cloud" if IS_CLOUD else "Local"
    print(f"  Mode:    {mode}")
    print(f"  Port:    {PORT}")
    if PUBLIC_BASE_URL:
        print(f"  Public:  {PUBLIC_BASE_URL}")
    elif not IS_CLOUD:
        print(f"  Local:   {local_url}")
        print("  Public:  (set PUBLIC_URL env var or install pyngrok)")
    print(f"  TURN:    {'configured' if turn_urls and turn_username else 'fallback / partial'}")
    print(f"  Max:     {MAX_PEERS} participants per room")
    print(f"  Health:  /healthz")
    print("=" * 60)

    # Only open browser locally
    if not IS_CLOUD:
        open_browser_later(PUBLIC_BASE_URL or local_url)

    web.run_app(create_app(), host=HOST, port=PORT)
