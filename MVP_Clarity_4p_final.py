
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
    <radialGradient id="glow" cx="0.72" cy="0.22" r="0.9">
      <stop offset="0%" stop-color="#25D366" stop-opacity="0.28"></stop>
      <stop offset="60%" stop-color="#128C7E" stop-opacity="0.08"></stop>
      <stop offset="100%" stop-color="#075E54" stop-opacity="0"></stop>
    </radialGradient>
  </defs>
  <rect width="512" height="512" rx="128" fill="#075E54"></rect>
  <rect width="512" height="512" rx="128" fill="url(#glow)"></rect>
  <path d="M108 238c0-17.7 14.3-32 32-32h92c17.7 0 32 14.3 32 32v14l48-30c21.3-13.3 48 2 48 27.2v97.6c0 25.2-26.7 40.5-48 27.2l-48-30v14c0 17.7-14.3 32-32 32h-92c-17.7 0-32-14.3-32-32V238z" fill="#FFFFFF"></path>
  <path d="M444 68 L300 140 L348 156 L320 188 Z" fill="#25D366"></path>
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
        "theme_color": "#075E54",
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
const CACHE_NAME = 'clarity-shell-v28';
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
  <script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
  <meta name="apple-mobile-web-app-capable" content="yes" />
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
  <meta name="theme-color" content="#075E54" />
  <title>Clarity</title>
  <link rel="manifest" href="/manifest.json" />
  <link rel="icon" href="/icon.svg" type="image/svg+xml" />
  <link rel="apple-touch-icon" href="/icon.svg" />
  <style>
    :root {
      /* ── Brand ── */
      --primary: #075E54;
      --primary-light: #128C7E;
      --accent: #25D366;
      --accent-hover: #1DA855;
      --accent-faded: rgba(37,211,102,0.10);

      /* ── Semantic color ── */
      --red: #FF3B30;
      --red-end: #EA0038;
      --yellow: #FFCC00;
      --blue: #007AFF;
      --green-ann: #34C759;
      --status-warn: #FF9500;

      /* ── Neutrals — 9-step ramp ── */
      --n-0:   #FFFFFF;
      --n-50:  #F5F7F8;
      --n-100: #E1E5E8;
      --n-200: #C2C9CE;
      --n-300: #9BA4AB;
      --n-400: #6B757D;
      --n-500: #4A545C;
      --n-600: #2A333A;
      --n-700: #141B21;
      --n-900: #000000;

      /* ── Back-compat aliases ── */
      --white:      var(--n-0);
      --off-white:  var(--n-50);
      --gray-light: var(--n-100);
      --gray:       var(--n-400);
      --gray-dark:  var(--n-500);
      --charcoal:   var(--n-700);
      --black:      var(--n-700);

      /* ── Overlays (used over video) ── */
      --overlay-chrome:   rgba(22,30,34,0.94);
      --overlay-bar:      rgba(20,20,20,0.92);
      --overlay-scrim:    linear-gradient(to bottom, rgba(0,0,0,0.7) 0%, rgba(0,0,0,0.35) 70%, transparent 100%);
      --overlay-tag:      rgba(0,0,0,0.65);
      --overlay-soft:     rgba(255,255,255,0.12);
      --overlay-soft-hi:  rgba(255,255,255,0.30);

      /* ── Home-screen gradient ── */
      --home-bg: linear-gradient(168deg, #F0FFF4 0%, #FFFFFF 40%, #F0FDFA 100%);

      /* ── Radii ── */
      --r-xs:  4px;
      --r-sm:  8px;
      --r-md:  12px;
      --r-lg:  14px;
      --r-xl:  16px;
      --r-2xl: 24px;
      --r-3xl: 28px;
      --r-full: 9999px;

      /* ── Shadows / elevation ── */
      --shadow-btn-primary: 0 4px 14px rgba(7,94,84,0.25), 0 1px 3px rgba(0,0,0,0.08);
      --shadow-btn-accent:  0 2px 8px rgba(37,211,102,0.35);
      --shadow-pip:         0 4px 20px rgba(0,0,0,0.5);
      --shadow-toolbar:     0 4px 24px rgba(0,0,0,0.5);
      --shadow-freeze-ring: 0 0 0 4px rgba(37,211,102,0.2), 0 4px 16px rgba(7,94,84,0.35);

      /* ── Blur ── */
      --blur-chrome: 14px;
      --blur-bar:    8px;
      --blur-home:   10px;

      /* ── Safe areas ── */
      --safe-top: env(safe-area-inset-top, 0px);
      --safe-bottom: env(safe-area-inset-bottom, 0px);

      /* ── Accessibility scale ── */
      --scale: 1;

      /* ── Typography scale (modular 1.20, elderly-friendly baseline) ── */
      --fs-9:  calc(52px * var(--scale));
      --fs-8:  calc(44px * var(--scale));
      --fs-7:  calc(36px * var(--scale));
      --fs-6:  calc(30px * var(--scale));
      --fs-5:  calc(25px * var(--scale));
      --fs-4:  calc(21px * var(--scale));
      --fs-3:  calc(17px * var(--scale));
      --fs-2:  calc(14px * var(--scale));
      --fs-1:  calc(12px * var(--scale));

      /* ── Font weights ── */
      --fw-heavy:  800;
      --fw-bold:   700;
      --fw-semi:   600;
      --fw-medium: 500;

      /* ── Letter spacing ── */
      --ls-tight:   -0.5px;
      --ls-default: 0;
      --ls-code:    0.15em;

      /* ── Line heights ── */
      --lh-tight: 1.1;
      --lh-snug:  1.25;
      --lh-body:  1.4;
      --lh-loose: 1.5;

      /* ── Touch targets ── */
      --tap-min:    48px;
      --tap-comfy:  56px;
      --tap-freeze: 68px;
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

    /* ══════ HOME — WhatsApp-style clean design ══════ */
    #homeScreen {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      height: 100dvh;
      padding: 40px 24px calc(80px + var(--safe-bottom));
      background: var(--home-bg);
      position: relative;
      gap: 0;
    }
    .logo-row { display: flex; align-items: flex-start; gap: 6px; margin-bottom: 4px; }
    .logo-text { font-size: var(--fs-8); font-weight: var(--fw-heavy); color: var(--primary); letter-spacing: -0.5px; }
    .logo-cursor { width: 18px; height: 18px; margin-top: 10px; }
    .logo-cursor svg { fill: var(--accent); }
    .tagline { font-size: var(--fs-3); color: var(--gray); font-weight: var(--fw-medium); margin-bottom: 40px; text-align: center; }

    .quick-call-btn {
      width: 88%; max-width: 380px; height: calc(64px * var(--scale)); border-radius: var(--r-xl);
      background: var(--primary);
      border: none;
      display: flex; align-items: center; justify-content: center; gap: 12px;
      cursor: pointer;
      box-shadow: var(--shadow-btn-primary);
      transition: transform 0.15s, background 0.15s;
      -webkit-tap-highlight-color: transparent;
    }
    .quick-call-btn:active { transform: scale(0.97); background: var(--accent-hover); }
    .quick-call-btn .icon { font-size: 28px; display: flex; align-items: center; }
    .quick-call-btn .label { font-size: var(--fs-4); font-weight: 600; color: white; }
    .quick-call-btn .sub { display: none; }

    .or-divider {
      display: flex; align-items: center; gap: 16px;
      width: 80%; max-width: 360px; margin: 24px 0 18px;
    }
    .or-divider::before, .or-divider::after {
      content: ''; flex: 1; height: 1px; background: var(--gray-light);
    }
    .or-divider span { font-size: 14px; color: var(--gray); font-weight: 500; }

    .join-section { width: 88%; max-width: 380px; }
    .join-label { display: none; }
    .join-row { display: flex; gap: 10px; }
    .join-input {
      flex: 1; height: calc(56px * var(--scale)); background: var(--off-white);
      border: 1.5px solid var(--gray-light); border-radius: var(--r-lg);
      text-align: center; font-size: var(--fs-5); font-weight: 700;
      letter-spacing: 4px; color: var(--charcoal); outline: none;
      -webkit-appearance: none; text-transform: uppercase;
      padding: 0 20px;
    }
    .join-input::placeholder { font-size: calc(16px * var(--scale)); letter-spacing: 1px; font-weight: 400; color: var(--gray); }
    .join-input:focus { border-color: var(--accent); background: var(--white); }
    .join-btn {
      height: calc(56px * var(--scale)); min-width: calc(80px * var(--scale)); padding: 0 24px;
      background: var(--accent); color: white; border: none; border-radius: var(--r-lg);
      font-size: var(--fs-3); font-weight: 600; cursor: pointer;
      -webkit-tap-highlight-color: transparent;
    }
    .join-btn:active { opacity: 0.85; background: var(--accent-hover); }

    .join-hint {
      font-size: calc(14px * var(--scale)); color: var(--gray); text-align: center;
      margin-bottom: 8px;
    }

    .badge-4p { display: none; }

    /* Bottom settings row: lang + large text */
    .home-bottom-row {
      position: absolute; bottom: 0; left: 0; right: 0;
      display: flex; align-items: center; justify-content: center;
      gap: 12px; padding: 16px 24px calc(16px + var(--safe-bottom));
      background: rgba(255,255,255,0.85);
      backdrop-filter: blur(var(--blur-home));
      -webkit-backdrop-filter: blur(var(--blur-home));
    }
    .lang-btn {
      padding: 10px 28px; font-size: calc(16px * var(--scale)); font-weight: 600;
      border-radius: var(--r-2xl); border: 1.5px solid var(--gray-light);
      background: var(--off-white); color: var(--gray-dark); cursor: pointer;
      -webkit-tap-highlight-color: transparent;
      transition: all 0.2s;
    }
    .lang-btn:active { background: var(--gray-light); }
    .big-text-btn {
      padding: 10px 24px; font-size: calc(20px * var(--scale)); font-weight: 800;
      border-radius: var(--r-2xl); border: 1.5px solid var(--gray-light);
      background: var(--off-white); color: var(--gray-dark); cursor: pointer;
      -webkit-tap-highlight-color: transparent;
      transition: all 0.2s; line-height: 1;
    }
    .big-text-btn:active { background: var(--gray-light); }
    .big-text-btn.active { background: var(--primary); color: white; border-color: var(--primary); }

    .server-toggle {
      margin-top: 16px; font-size: 13px; color: var(--gray);
      cursor: pointer; background: none; border: none;
      -webkit-tap-highlight-color: transparent;
    }
    .server-panel {
      margin-top: 10px; width: 88%; max-width: 380px;
      background: var(--off-white); border-radius: var(--r-xl); padding: 14px;
    }
    .server-input {
      width: 100%; height: 48px; background: var(--white);
      border: 1.5px solid var(--gray-light); border-radius: var(--r-2xl);
      padding: 0 16px; font-size: 15px; color: var(--charcoal); outline: none;
      -webkit-appearance: none;
    }
    .server-hint {
      margin-top: 8px; color: var(--gray); font-size: 13px; line-height: 1.4;
    }

    .features { display: none; }

    /* ══════ LARGE TEXT MODE (大字版) ══════ */
    body.large-text { --scale: 1.5; }

    /* ══════ CALL SCREEN ══════ */
    #callScreen {
      display: none; position: relative;
      width: 100vw; height: 100dvh;
      background: #000; flex-direction: column;
    }
    #callScreen.active { display: flex; }

    /* Status bar — WhatsApp style top overlay */
    .call-status-bar {
      position: absolute; top: 0; left: 0; right: 0; z-index: 20;
      padding: calc(8px + var(--safe-top)) 14px 14px;
      background: var(--overlay-scrim);
      display: flex; align-items: center; gap: 8px;
    }
    .status-dot { width: 10px; height: 10px; border-radius: 50%; background: var(--status-warn); flex-shrink: 0; }
    .status-dot.connected { background: var(--accent); }
    .call-status-text { flex: 1; font-size: var(--fs-2); color: rgba(255,255,255,0.95); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-weight: 500; }
    .room-badge { background: rgba(255,255,255,0.18); padding: 5px 10px; border-radius: var(--r-sm); font-size: var(--fs-1); font-weight: 700; color: white; letter-spacing: 2px; min-width: 56px; text-align: center; }
    .peer-count { background: rgba(37,211,102,0.3); padding: 5px 10px; border-radius: var(--r-sm); font-size: var(--fs-1); font-weight: 700; color: var(--accent); }

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
      border-radius: var(--r-xs);
    }
    .video-cell video {
      position: absolute; inset: 0;
      width: 100%; height: 100%;
      object-fit: cover;
      background: var(--charcoal);
    }
    .video-cell .name-tag {
      position: absolute; bottom: 10px; left: 10px;
      background: var(--overlay-tag);
      padding: 4px 10px; border-radius: var(--r-sm);
      font-size: 15px; font-weight: 700; color: white; z-index: 5;
    }
    .video-cell.selected { border: 3px solid var(--accent); border-radius: 8px; }
    .pip.selected { border-color: var(--accent); }

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
    .waiting-view .emoji { font-size: 64px; margin-bottom: 16px; }
    .waiting-view .text { font-size: var(--fs-5); color: white; font-weight: 500; }
    .waiting-view .code { font-size: var(--fs-7); color: var(--accent); font-weight: 800; letter-spacing: 6px; margin-top: 12px; }
    .waiting-view .hint { font-size: var(--fs-3); color: #aaa; margin-top: 16px; text-align: center; padding: 0 24px; line-height: 1.5; }
    .copy-link-btn {
      margin-top: 24px; padding: calc(16px * var(--scale)) calc(40px * var(--scale));
      background: var(--accent); color: white; border: none;
      border-radius: var(--r-3xl); font-size: var(--fs-3); font-weight: 600;
      cursor: pointer; -webkit-tap-highlight-color: transparent;
      box-shadow: var(--shadow-btn-accent);
    }
    .copy-link-btn:active { opacity: 0.85; transform: scale(0.97); }

    /* PiP self-view — WhatsApp style rounded card */
    .pip {
      position: absolute;
      top: calc(56px + var(--safe-top)); right: 8px;
      width: calc(110px * var(--scale)); height: calc(150px * var(--scale));
      border-radius: var(--r-xl); overflow: hidden;
      border: 2px solid rgba(255,255,255,0.5); z-index: 12;
      box-shadow: var(--shadow-pip);
      background: #000;
    }
    .pip video { width: 100%; height: 100%; object-fit: cover; background: #000; }
    .pip .pip-label {
      position: absolute; bottom: 4px; left: 4px;
      background: rgba(0,0,0,0.55);
      padding: 2px 6px; border-radius: 4px;
      font-size: var(--fs-1); color: white; font-weight: 600;
    }

    /* Annotation bar — glass pill redesign */
    .annotation-bar {
      display: none; position: absolute; bottom: 0; left: 0; right: 0;
      flex-direction: column; gap: 10px;
      padding: 10px 12px calc(10px + var(--safe-bottom));
      z-index: 15;
    }
    .annotation-bar.active { display: flex; }
    .ann-pill {
      display: flex; align-items: center; gap: 10px;
      padding: 10px 12px;
      background: rgba(18,22,28,0.55);
      backdrop-filter: blur(18px) saturate(1.3);
      -webkit-backdrop-filter: blur(18px) saturate(1.3);
      border-radius: 999px;
      box-shadow: 0 10px 28px rgba(0,0,0,0.45), inset 0 0 0 1px rgba(255,255,255,0.12);
    }
    .color-dot {
      width: 44px; height: 44px; border-radius: 50%;
      border: 0; cursor: pointer; box-shadow: inset 0 0 0 3px rgba(0,0,0,0.15);
      -webkit-tap-highlight-color: transparent; transition: transform 0.12s; flex-shrink: 0;
    }
    .color-dot.selected {
      box-shadow: inset 0 0 0 3px rgba(0,0,0,0.15), 0 0 0 3px #fff, 0 0 0 5px rgba(255,255,255,0.15);
      transform: scale(1.05);
    }
    .ann-divider { width: 1px; height: 28px; background: rgba(255,255,255,0.18); margin: 0 2px; flex-shrink: 0; }
    .ann-util {
      width: 44px; height: 44px; border-radius: 50%;
      background: rgba(255,255,255,0.1); border: 0; cursor: pointer; color: #fff; padding: 0;
      display: flex; align-items: center; justify-content: center; transition: background 0.12s;
    }
    .ann-util:disabled { opacity: 0.35; cursor: default; }
    .ann-util svg { width: 22px; height: 22px; fill: currentColor; display: block; }
    .ann-resume {
      margin-left: auto; display: flex; align-items: center; gap: 6px;
      height: 44px; padding: 0 18px 0 14px; border-radius: 999px;
      background: var(--accent); color: #073B1F; border: 0; cursor: pointer;
      font-size: 15px; font-weight: 700; letter-spacing: 0.2px;
      box-shadow: 0 2px 10px rgba(37,211,102,0.35);
    }
    .ann-resume svg { width: 18px; height: 18px; fill: currentColor; }
    .ann-secondary {
      display: flex; gap: 8px; padding: 0 4px; align-items: center;
      font-size: 13px; color: rgba(255,255,255,0.7);
    }
    .ann-txtbtn {
      background: transparent; border: 0; color: rgba(255,255,255,0.85);
      font-size: 13.5px; font-weight: 600; cursor: pointer; padding: 6px 10px; border-radius: 8px;
      display: inline-flex; align-items: center; gap: 6px;
    }
    .ann-txtbtn:hover { background: rgba(255,255,255,0.08); }
    .ann-txtbtn svg { width: 16px; height: 16px; fill: currentColor; }
    .ann-txtbtn.danger { color: #FF9BA3; }

    /* Main toolbar — WhatsApp pill bar + elderly-friendly labels */
    .call-toolbar {
      position: absolute; bottom: 0; left: 0; right: 0;
      display: flex; align-items: center; justify-content: center;
      padding: 10px 8px calc(10px + var(--safe-bottom));
      z-index: 15;
    }
    .toolbar-pill {
      position: relative;
      display: flex; align-items: center; justify-content: center;
      gap: calc(6px * var(--scale)); padding: calc(8px * var(--scale)) calc(10px * var(--scale));
      background: rgba(18,22,28,0.55);
      border-radius: var(--r-3xl);
      backdrop-filter: blur(18px) saturate(1.3); -webkit-backdrop-filter: blur(18px) saturate(1.3);
      box-shadow: var(--shadow-toolbar), inset 0 0 0 1px rgba(255,255,255,0.12);
    }
    .tool-btn {
      display: flex; align-items: center; justify-content: center;
      width: calc(var(--tap-comfy) * var(--scale)); height: calc(var(--tap-comfy) * var(--scale)); border-radius: 50%;
      background: var(--overlay-soft); border: none;
      cursor: pointer; -webkit-tap-highlight-color: transparent; color: white;
      padding: 0; flex-shrink: 0;
    }
    .tool-btn:active { background: var(--overlay-soft-hi); }
    .tool-btn.active-state { background: white; color: var(--charcoal); }
    .tool-btn svg { width: 28px; height: 28px; fill: currentColor; flex-shrink: 0; }

    .freeze-btn {
      width: calc(var(--tap-freeze) * var(--scale)); height: calc(var(--tap-freeze) * var(--scale)); border-radius: 50%;
      background: var(--primary); border: 3px solid var(--accent);
      display: flex; align-items: center; justify-content: center;
      cursor: pointer; box-shadow: var(--shadow-freeze-ring);
      -webkit-tap-highlight-color: transparent; color: white;
      padding: 0; flex-shrink: 0;
      position: relative;
    }
    .freeze-btn:active { transform: scale(0.93); }
    .freeze-btn.frozen { background: var(--accent); border-color: var(--accent); }
    .freeze-btn svg { width: 34px; height: 34px; fill: currentColor; flex-shrink: 0; }

    .freeze-coach {
      position: absolute; bottom: calc(100% + 14px); left: 50%; transform: translateX(-50%);
      background: rgba(20,24,30,0.55);
      backdrop-filter: blur(16px) saturate(1.3);
      -webkit-backdrop-filter: blur(16px) saturate(1.3);
      color: #fff; padding: 10px 16px;
      border-radius: var(--r-md); font-size: var(--fs-2); font-weight: 600; white-space: nowrap;
      box-shadow: 0 8px 24px rgba(0,0,0,0.35), inset 0 0 0 1px rgba(255,255,255,0.16);
      animation: coachBounce 2.2s ease-in-out infinite;
      z-index: 30;
    }
    .freeze-coach::after {
      content: ''; position: absolute; top: 100%; left: 50%; transform: translateX(-50%);
      border: 7px solid transparent; border-top-color: rgba(20,24,30,0.55);
    }
    @keyframes coachBounce {
      0%, 100% { transform: translateX(-50%) translateY(0); }
      50% { transform: translateX(-50%) translateY(-6px); }
    }

    .end-btn {
      width: calc(var(--tap-comfy) * var(--scale)); height: calc(var(--tap-comfy) * var(--scale)); border-radius: 50%;
      background: var(--red-end); border: none;
      display: flex; align-items: center; justify-content: center;
      cursor: pointer; -webkit-tap-highlight-color: transparent; color: white;
      padding: 0; flex-shrink: 0;
    }
    .end-btn:active { opacity: 0.8; }
    .end-btn svg { width: 22px; height: 22px; fill: currentColor; flex-shrink: 0; }

    .source-video { position: absolute; width: 1px; height: 1px; opacity: 0; pointer-events: none; }

    /* Toast notification */
    .toast {
      position: fixed; bottom: calc(90px + var(--safe-bottom)); left: 50%; transform: translateX(-50%);
      background: rgba(20,24,30,0.96); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
      color: #fff; padding: 13px 16px 13px 14px; border-radius: 14px;
      display: flex; align-items: center; gap: 12px;
      box-shadow: 0 8px 24px rgba(0,0,0,0.5), inset 0 0 0 1px rgba(255,255,255,0.06);
      overflow: hidden; min-height: 48px; box-sizing: border-box;
      z-index: 999; opacity: 0; pointer-events: none;
      transition: opacity 0.3s ease;
      max-width: 90vw;
    }
    .toast.visible { opacity: 1; pointer-events: auto; }
    .toast::before {
      content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 4px;
      background: var(--toast-accent, #25D366);
    }
    .toast-icon {
      flex: none; width: 28px; height: 28px; border-radius: 50%;
      background: rgba(255,255,255,0.08);
      display: flex; align-items: center; justify-content: center;
      color: var(--toast-accent, #25D366);
    }
    .toast-icon svg { width: 18px; height: 18px; fill: currentColor; display: block; }
    .toast-msg { font-size: 15px; font-weight: 600; line-height: 1.3; }

    @media (max-height: 700px) {
      .quick-call-btn { height: 56px; }
      .quick-call-btn .label { font-size: 18px; }
      .or-divider { margin: 12px 0 10px; }
      .join-input { height: 48px; font-size: 22px; }
      .join-btn { height: 48px; font-size: 16px; }
      .tagline { margin-bottom: 20px; }
    }

    /* ══════ HANGUP SCREEN ══════ */
    #hangupScreen {
      display: none; position: fixed; inset: 0; z-index: 200;
      flex-direction: column; align-items: center; justify-content: center;
      background: var(--charcoal);
      animation: hangupFadeIn 0.3s ease-out;
    }
    #hangupScreen.active { display: flex; }
    @keyframes hangupFadeIn { from { opacity: 0; } to { opacity: 1; } }
    .hangup-tagline { font-size: var(--fs-3); color: rgba(255,255,255,0.5); margin-bottom: 32px; }
    .hangup-msg { font-size: var(--fs-4); color: var(--accent); font-weight: 600; margin-bottom: 8px; }
    .hangup-sub { font-size: var(--fs-2); color: rgba(255,255,255,0.6); }
    .hangup-back-btn {
      margin-top: 36px; padding: calc(14px * var(--scale)) calc(40px * var(--scale));
      background: var(--primary); color: white; border: none; border-radius: var(--r-lg);
      font-size: var(--fs-3); font-weight: 600; cursor: pointer;
    }
    .hangup-back-btn:active { background: var(--primary-light); }
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

  <button class="quick-call-btn" id="quickCallBtn">
    <span class="icon"><svg viewBox="0 0 24 24" width="26" height="26" fill="white"><path d="M17 10.5V7a1 1 0 0 0-1-1H4a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-3.5l4 4v-11l-4 4z"/></svg></span>
    <span class="label" data-i18n="startCall">Start Call</span>
  </button>

  <div class="or-divider"><span data-i18n="or">or</span></div>

  <div class="join-section">
    <div class="join-hint" data-i18n="joinHint">Got a code from someone? Enter it here</div>
    <div class="join-row">
      <input class="join-input" id="homeRoomInput" data-i18n="roomPlaceholder" placeholder="Room Code" maxlength="8" autocapitalize="characters" autocomplete="off" />
      <button class="join-btn" id="homeJoinBtn" data-i18n="join">Join</button>
    </div>
  </div>

  <div class="home-bottom-row">
    <button class="lang-btn" id="langBtn" onclick="toggleLang()">中文</button>
    <button class="big-text-btn" id="bigTextBtn" data-i18n-label="bigText">大字 Aa+</button>
  </div>

  <button class="server-toggle hidden" id="serverToggle">&#9656; Server</button>
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
      <div class="emoji"><svg viewBox="0 0 24 24" width="64" height="64" fill="#25D366"><path d="M17 10.5V7a1 1 0 0 0-1-1H4a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-3.5l4 4v-11l-4 4z"/></svg></div>
      <div class="text" data-i18n="waitingOthers">Waiting for others...</div>
      <div class="code" id="waitingRoomCode"></div>
      <div id="qrCanvas" style="background:white; border-radius:12px; padding:10px; margin:12px auto; display:inline-block;"></div>
      <div class="hint" data-i18n="waitingHint">Tap below to invite someone</div>
      <button class="copy-link-btn" id="copyLinkBtn2" data-i18n="shareInvite">Share Invite Link</button>
    </div>

    <div class="video-grid" id="videoGrid" data-count="0"></div>
    <canvas id="frozenCanvas" class="hidden"></canvas>

    <div class="pip" id="pipContainer">
      <video id="pipVideo" autoplay playsinline muted></video>
      <div class="pip-label" data-i18n="you">You</div>
    </div>
  </div>

  <div class="annotation-bar" id="annotationBar">
    <div class="ann-pill">
      <div class="color-dot selected" style="background:#FF3B30" data-color="#FF3B30"></div>
      <div class="color-dot" style="background:#FFCC00" data-color="#FFCC00"></div>
      <div class="color-dot" style="background:#34C759" data-color="#34C759"></div>
      <div class="ann-divider"></div>
      <button class="ann-util" id="undoBtn" disabled aria-label="Undo"><svg viewBox="0 0 24 24"><path d="M12.5 8c-2.65 0-5.05.99-6.9 2.6L2 7v9h9l-3.62-3.62A7.97 7.97 0 0 1 12.5 11c3.52 0 6.52 2.29 7.58 5.47l2.37-.78A10.99 10.99 0 0 0 12.5 8z"/></svg></button>
      <button class="ann-util" id="redoBtn" disabled aria-label="Redo"><svg viewBox="0 0 24 24" style="transform:scaleX(-1)"><path d="M12.5 8c-2.65 0-5.05.99-6.9 2.6L2 7v9h9l-3.62-3.62A7.97 7.97 0 0 1 12.5 11c3.52 0 6.52 2.29 7.58 5.47l2.37-.78A10.99 10.99 0 0 0 12.5 8z"/></svg></button>
      <button class="ann-resume" id="unfreezeBtn"><svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg> <span data-i18n="resume">Resume</span></button>
    </div>
    <div class="ann-secondary">
      <button class="ann-txtbtn" id="saveAnnBtn"><svg viewBox="0 0 24 24"><path d="M17 3H7a2 2 0 0 0-2 2v16l7-3 7 3V5a2 2 0 0 0-2-2zm0 15l-5-2.18L7 18V5h10v13z"/></svg> <span data-i18n="saveImg">Save frame</span></button>
      <span style="flex:1"></span>
      <button class="ann-txtbtn danger" id="clearAnnBtn"><svg viewBox="0 0 24 24"><path d="M6 19a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg> <span data-i18n="clearAll">Clear all</span></button>
    </div>
  </div>

  <div class="call-toolbar" id="callToolbar">
    <div class="toolbar-pill">
      <button class="tool-btn" id="muteBtn" aria-label="Mic">
        <svg viewBox="0 0 24 24"><path d="M12 14a3 3 0 0 0 3-3V5a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.93V21h2v-3.07A7 7 0 0 0 19 11h-2z"/></svg>
      </button>
      <button class="tool-btn" id="flipBtn" aria-label="Flip">
        <svg viewBox="0 0 24 24"><path d="M20 5h-3.17L15 3H9L7.17 5H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2zm-8 13a5.5 5.5 0 0 1 0-11 5.5 5.5 0 0 1 5.21 3.73h-1.71A3.99 3.99 0 0 0 12 8.5a4 4 0 1 0 3.73 5.46h1.71A5.5 5.5 0 0 1 12 18z"/></svg>
      </button>
      <button class="freeze-btn" id="freezeBtn" aria-label="Freeze">
        <svg viewBox="0 0 24 24"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>
        <div class="freeze-coach hidden" id="freezeCoach"><span data-i18n="coachFreeze">Tap to freeze &amp; annotate!</span></div>
      </button>
      <button class="tool-btn" id="shareBtn2" aria-label="Share">
        <svg viewBox="0 0 24 24"><path d="M18 16.08a2.99 2.99 0 0 0-1.98.75L8.91 12.7A3.02 3.02 0 0 0 9 12a3.02 3.02 0 0 0-.09-.7l7.05-4.11A2.99 2.99 0 1 0 15 5a3.02 3.02 0 0 0 .09.7L8.04 9.81A3 3 0 1 0 6 15a2.99 2.99 0 0 0 2.04-.81l7.12 4.15c-.05.21-.08.43-.08.66a2.92 2.92 0 1 0 2.92-2.92z"/></svg>
      </button>
      <button class="end-btn" id="endBtn" aria-label="End">
        <svg viewBox="0 0 24 24"><path d="M12 9c-1.6 0-3.15.25-4.6.72v3.1c0 .39-.23.74-.56.9-.98.49-1.87 1.12-2.66 1.85-.18.18-.43.28-.7.28a.99.99 0 0 1-.71-.3L.29 13.08a.99.99 0 0 1 0-1.42C3.55 8.5 7.56 7 12 7s8.45 1.5 11.71 4.66a.99.99 0 0 1 0 1.42l-2.48 2.48a.99.99 0 0 1-.7.29c-.27 0-.52-.1-.71-.29a11.27 11.27 0 0 0-2.66-1.85c-.33-.16-.56-.5-.56-.9v-3.1C15.15 9.25 13.6 9 12 9z"/></svg>
      </button>
    </div>
  </div>
</div>

<video id="localVideoSrc" class="source-video" autoplay playsinline muted></video>
<div class="toast" id="toast"></div>

<div id="hangupScreen">
  <div class="logo-row" style="margin-bottom:8px;">
    <svg viewBox="0 0 720 220" style="width:220px;height:auto;" xmlns="http://www.w3.org/2000/svg">
      <text x="0" y="170" fill="white" font-family="-apple-system,BlinkMacSystemFont,'SF Pro','Segoe UI',Roboto,sans-serif" font-weight="800" font-size="200" letter-spacing="-6">Clarity</text>
      <path d="M690 18 L548 78 L598 96 L578 130 Z" fill="#25D366"/>
    </svg>
  </div>
  <div class="hangup-tagline" data-i18n="tagline">See Together. Guide Better.</div>
  <div class="hangup-msg" id="hangupMsg" data-i18n="callEnded">Call ended</div>
  <div class="hangup-sub" id="hangupSub" data-i18n="thankYou">Thanks for using Clarity</div>
  <button class="hangup-back-btn" id="hangupBackBtn" data-i18n="backHome">Back to Home</button>
</div>

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
    shareInvite: 'Share Invite Link',
    linkCopied: 'Link Copied!',
    clearAll: 'Clear All',
    saveImg: '💾 Save',
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
    singleCam: 'This device only has one camera.',
    flipFailed: 'Could not flip camera on this device/browser.',
    noVideoFreeze: 'No video to freeze yet',
    frozenShared: 'Frozen — shared board',
    frozenDraw: 'Frozen — draw to annotate',
    screenshotHint: '📸 Tip: Use your phone\'s screenshot to save this frame',
    videoSelected: '✅ Selected — freeze will capture this video',
    localSelected: '✅ Selected your camera — freeze will capture your view',
    videoAuto: 'Auto mode — freeze captures remote video',
    live: 'Live',
    allLeft: 'All peers left',
    networkIssue: 'Network issue. Retrying...',
    newPeer: 'New peer joining...',
    badCode: 'Room code must be 4–8 letters/numbers',
    autoJoinFail: 'Auto-join failed',
    joinHint: 'Got a code from someone? Enter it here',
    coachFreeze: 'Tap to freeze & annotate!',
    callEnded: 'Call ended',
    thankYou: 'Thanks for using Clarity',
    backHome: 'Back to Home',
    undo: 'Undo',
    redo: 'Redo',
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
    shareInvite: '分享邀请链接',
    linkCopied: '链接已复制！',
    clearAll: '清除标注',
    saveImg: '💾 保存',
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
    singleCam: '此设备只有一个摄像头。',
    flipFailed: '此设备/浏览器无法翻转摄像头。',
    noVideoFreeze: '还没有视频可冻结',
    frozenShared: '已冻结 — 共享画板',
    frozenDraw: '已冻结 — 在画面上标注',
    screenshotHint: '📸 提示：可以用手机截图功能保存当前画面',
    videoSelected: '✅ 已选中 — 冻结将捕获此视频',
    localSelected: '✅ 已选中你的摄像头 — 冻结将捕获你的画面',
    videoAuto: '自动模式 — 冻结捕获对方视频',
    live: '通话中',
    allLeft: '所有人已离开',
    networkIssue: '网络问题，重试中...',
    newPeer: '新成员加入中...',
    badCode: '房间号须为4-8位字母或数字',
    autoJoinFail: '自动加入失败',
    joinHint: '收到房间号了？在这里输入',
    coachFreeze: '点这里冻结画面并标注！',
    callEnded: '通话已结束',
    thankYou: '感谢使用 Clarity',
    backHome: '返回首页',
    undo: '撤销',
    redo: '重做',
    peersConnected: (n) => `${n} 人已连接`,
    peerLeft: (n) => `有人离开，剩余 ${n} 人。`,
    waitingPeers: (room) => `房间 ${room} — 等待其他人...`,
  }
};

let currentLang = localStorage.getItem(LANG_KEY) || ((navigator.language || navigator.userLanguage || 'en').startsWith('zh') ? 'zh' : 'en');

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
  // Refresh big text button label (bilingual)
  if (typeof applyBigText === 'function') applyBigText();
  // Toolbar labels
  syncToolbarLabels();
}

function toggleLang() {
  currentLang = currentLang === 'en' ? 'zh' : 'en';
  applyLang();
}

// ═════════════════════════════════
// LARGE TEXT MODE (大字版)
// ═════════════════════════════════
const BIG_TEXT_KEY = 'clarity.bigText';
let isBigText = localStorage.getItem(BIG_TEXT_KEY) === '1';

function applyBigText() {
  document.body.classList.toggle('large-text', isBigText);
  const btn = document.getElementById('bigTextBtn');
  if (btn) {
    btn.classList.toggle('active', isBigText);
    btn.textContent = isBigText
      ? (currentLang === 'zh' ? '标准字体' : 'Aa−')
      : (currentLang === 'zh' ? '大字 Aa+' : 'Aa+');
  }
  localStorage.setItem(BIG_TEXT_KEY, isBigText ? '1' : '0');
}

function toggleBigText() {
  isBigText = !isBigText;
  applyBigText();
}

function syncToolbarLabels() {
  // Mic state
  syncLocalButtonStates();
  // Annotation bar labels
  const clearLabel = clearAnnBtn?.querySelector('[data-i18n="clearAll"]');
  if (clearLabel) clearLabel.textContent = t('clearAll');
  const resumeLabel = unfreezeBtn?.querySelector('[data-i18n="resume"]');
  if (resumeLabel) resumeLabel.textContent = t('resume');
  const saveLabel = document.getElementById('saveAnnBtn')?.querySelector('[data-i18n="saveImg"]');
  if (saveLabel) saveLabel.textContent = t('saveImg');
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
// Zoom & pan state for frozen canvas
let selectedFreezeTarget = null; // null = auto (first remote), 'local' = my camera, or peerId
let isSwapped = false; // true = local video in main grid, remote in PIP
let zoomLevel = 1;
let panX = 0, panY = 0;
let isPinching = false;
let lastPinchDist = 0;
let lastPinchMidX = 0, lastPinchMidY = 0;

// Offscreen board canvas (GPT refinement — no more Image re-encoding)
const boardCanvas = document.createElement('canvas');
const boardCtx = boardCanvas.getContext('2d');

// Undo/redo history for annotations (local only)
let undoStack = []; // array of board canvas data URLs
let redoStack = [];

// Device detection
const isTouchDevice = ('ontouchstart' in window) || (navigator.maxTouchPoints > 0);
const isMobileUA = /iPhone|iPad|iPod|Android/i.test(navigator.userAgent);
const isDesktop = !isMobileUA;
let cameraCount = 0; // detected after media setup
let hasMultipleCameras = false;

async function detectCameras() {
  try {
    const devices = await navigator.mediaDevices.enumerateDevices();
    const videoCams = devices.filter(d => d.kind === 'videoinput');
    cameraCount = videoCams.length;
    hasMultipleCameras = cameraCount > 1;
  } catch(e) {
    cameraCount = 1;
    hasMultipleCameras = false;
  }
  // Hide flip button if only one camera
  if (!hasMultipleCameras && flipBtn) {
    flipBtn.style.display = 'none';
  } else if (flipBtn) {
    flipBtn.style.display = '';
  }
}

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
  // If behind reverse proxy (e.g. /clarity or /zh/clarity), keep the full prefix
  const prefix = location.pathname.replace(/\/+$/, '').match(/^(.*\/clarity)/);
  url.pathname = (prefix && location.origin === url.origin ? prefix[1] : '') + '/ws';
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

function generateQR() {
  const link = getJoinLink();
  if (!link) return;
  const container = document.getElementById('qrCanvas');
  if (!container) return;
  container.innerHTML = '';
  try {
    new QRCode(container, {
      text: link,
      width: 164,
      height: 164,
      colorDark: '#000000',
      colorLight: '#ffffff',
      correctLevel: QRCode.CorrectLevel.M,
    });
  } catch(e) { console.warn('QR generation failed', e); }
}

function showToast(msg, duration = 3000, type = 'success') {
  const t = document.getElementById('toast');
  const colors = { success: '#25D366', warning: '#FF9500', error: '#FF3B30', info: '#60A5FA', tip: '#FFCC00' };
  const icons = {
    success: '<svg viewBox="0 0 24 24"><path d="M9 16.2L4.8 12l-1.4 1.4L9 19 21 7l-1.4-1.4z"/></svg>',
    warning: '<svg viewBox="0 0 24 24"><path d="M1 21h22L12 2 1 21zm12-3h-2v-2h2v2zm0-4h-2v-4h2v4z"/></svg>',
    error: '<svg viewBox="0 0 24 24"><path d="M12 2a10 10 0 1 0 10 10A10 10 0 0 0 12 2zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z"/></svg>',
    info: '<svg viewBox="0 0 24 24"><path d="M12 2a10 10 0 1 0 10 10A10 10 0 0 0 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/></svg>',
    tip: '<svg viewBox="0 0 24 24"><path d="M9 21c0 .55.45 1 1 1h4c.55 0 1-.45 1-1v-1H9v1zm3-19A7 7 0 0 0 8 14.74c0 1.17.5 2.26 1.29 3.03L10 18.5V19h4v-.5l.71-.73A7 7 0 0 0 16 14.74 7 7 0 0 0 12 2z"/></svg>'
  };
  const accent = colors[type] || colors.success;
  const icon = icons[type] || icons.success;
  t.style.setProperty('--toast-accent', accent);
  t.innerHTML = '<div class="toast-icon">' + icon + '</div><span class="toast-msg">' + msg + '</span>';
  t.classList.add('visible');
  clearTimeout(t._tid);
  t._tid = setTimeout(() => t.classList.remove('visible'), duration);
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
  if (n > 1) {
    waitingView.classList.add('hidden');
    setTimeout(showFreezeCoach, 2000);
  }
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
// SVG icon paths for toolbar buttons
const ICONS = {
  micOn: '<svg viewBox="0 0 24 24"><path d="M12 14a3 3 0 0 0 3-3V5a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.93V21h2v-3.07A7 7 0 0 0 19 11h-2z"/></svg>',
  micOff: '<svg viewBox="0 0 24 24"><path d="M19 11h-1.7c0 .74-.16 1.43-.43 2.05l1.23 1.23A6.96 6.96 0 0 0 19 11zm-4.02.17c0-.06.02-.11.02-.17V5a3 3 0 0 0-6 0v.18l5.98 5.99zM4.27 3L3 4.27l6.01 6.01V11a3 3 0 0 0 4.83 2.38l1.46 1.46A4.98 4.98 0 0 1 12 16a5 5 0 0 1-5-5H5a7 7 0 0 0 6 6.93V21h2v-3.07c.88-.11 1.71-.38 2.46-.79l4.07 4.07L20.73 20 4.27 3z"/></svg>',
  micNone: '<svg viewBox="0 0 24 24"><path d="M12 14a3 3 0 0 0 3-3V5a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.93V21h2v-3.07A7 7 0 0 0 19 11h-2z" opacity="0.3"/><line x1="4" y1="4" x2="20" y2="20" stroke="currentColor" stroke-width="2"/></svg>',
  pause: '<svg viewBox="0 0 24 24"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>',
  play: '<svg viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>',
};

function syncLocalButtonStates() {
  const audioTrack = localStream?.getAudioTracks?.()[0] || null;
  const videoTrack = localStream?.getVideoTracks?.()[0] || null;

  isMuted = audioTrack ? !audioTrack.enabled : true;
  isVideoOff = videoTrack ? !videoTrack.enabled : true;

  muteBtn.classList.toggle('active-state', !!audioTrack && isMuted);
  muteBtn.innerHTML = audioTrack ? (isMuted ? ICONS.micOff : ICONS.micOn) : ICONS.micNone;

  const showPip = !!videoTrack && !isFrozen;
  pipContainer.classList.toggle('hidden', !showPip);
}

// ═════════════════════════════════
// VIDEO GRID
// ═════════════════════════════════
function rebuildVideoGrid() {
  const peerIds = Object.keys(peers);
  videoGrid.innerHTML = '';

  if (isSwapped && peerIds.length > 0) {
    // Swapped: show LOCAL in grid, REMOTE in PIP
    videoGrid.setAttribute('data-count', '1');

    const cell = document.createElement('div');
    cell.className = 'video-cell';
    const video = document.createElement('video');
    video.autoplay = true;
    video.playsInline = true;
    video.muted = true; // local is always muted
    video.srcObject = localStream;
    cell.appendChild(video);

    const tag = document.createElement('div');
    tag.className = 'name-tag';
    tag.textContent = t('you');
    cell.appendChild(tag);

    videoGrid.appendChild(cell);
    safePlay(video, true);

    // PIP shows first remote peer
    const firstPeer = peers[peerIds[0]];
    if (firstPeer?.remoteStream) {
      pipVideo.srcObject = firstPeer.remoteStream;
      pipVideo.muted = false;
      safePlay(pipVideo, false);
    }
    const pipLabel = pipContainer.querySelector('.pip-label');
    if (pipLabel) pipLabel.textContent = firstPeer?.label || `${t('peer')} ${peerIds[0].slice(0,4)}`;

  } else {
    // Normal: show REMOTE peers in grid, LOCAL in PIP
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

    // PIP shows local
    if (localStream) {
      pipVideo.srcObject = localStream;
      pipVideo.muted = true;
      safePlay(pipVideo, true);
    }
    const pipLabel = pipContainer.querySelector('.pip-label');
    if (pipLabel) pipLabel.textContent = t('you');
  }

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
  if (!hasMultipleCameras) {
    showToast(t('singleCam'));
    return;
  }
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
  const baseScale = Math.min(frozenCanvas.width / boardCanvas.width, frozenCanvas.height / boardCanvas.height);
  const scale = baseScale * zoomLevel;
  const width = boardCanvas.width * scale;
  const height = boardCanvas.height * scale;
  return {
    x: (frozenCanvas.width - width) / 2 + panX,
    y: (frozenCanvas.height - height) / 2 + panY,
    width, height,
  };
}

function resetZoom() {
  zoomLevel = 1;
  panX = 0;
  panY = 0;
  redrawFrozen();
  updateZoomLabel();
}

function adjustZoom(delta, centerX, centerY) {
  const oldZoom = zoomLevel;
  zoomLevel = Math.min(Math.max(zoomLevel + delta, 1), 6);
  if (zoomLevel === 1) { panX = 0; panY = 0; }
  else if (centerX !== undefined) {
    // Zoom toward the pinch/click center
    const ratio = zoomLevel / oldZoom;
    panX = centerX - ratio * (centerX - panX);
    panY = centerY - ratio * (centerY - panY);
  }
  clampPan();
  redrawFrozen();
  updateZoomLabel();
}

function clampPan() {
  if (zoomLevel <= 1) { panX = 0; panY = 0; return; }
  const r = getBoardRectUnpanned();
  if (!r) return;
  const maxPanX = Math.max(0, (r.width * zoomLevel - frozenCanvas.width) / 2);
  const maxPanY = Math.max(0, (r.height * zoomLevel - frozenCanvas.height) / 2);
  panX = Math.min(maxPanX, Math.max(-maxPanX, panX));
  panY = Math.min(maxPanY, Math.max(-maxPanY, panY));
}

function getBoardRectUnpanned() {
  if (!hasBoard()) return null;
  const baseScale = Math.min(frozenCanvas.width / boardCanvas.width, frozenCanvas.height / boardCanvas.height);
  const w = boardCanvas.width * baseScale;
  const h = boardCanvas.height * baseScale;
  return { x: (frozenCanvas.width - w) / 2, y: (frozenCanvas.height - h) / 2, width: w, height: h };
}

function updateZoomLabel() {
  const el = document.getElementById('zoomLabel');
  if (el) el.textContent = zoomLevel > 1 ? `${zoomLevel.toFixed(1)}x` : '';
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
  // Allow drawing even slightly outside visible board when zoomed
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
  // Determine which video to freeze based on selection
  let sourceVideo = null;
  if (selectedFreezeTarget === 'local') {
    // User selected their own camera
    if (localVideoSrc.readyState >= 2 && localVideoSrc.videoWidth > 0) {
      sourceVideo = localVideoSrc;
    }
  } else if (selectedFreezeTarget && peers[selectedFreezeTarget]) {
    // User selected a specific peer
    const p = peers[selectedFreezeTarget];
    if (p.videoEl?.readyState >= 2 && p.videoEl.videoWidth > 0) {
      sourceVideo = p.videoEl;
    }
  } else {
    // Auto: first remote peer, fallback to local
    const firstPeer = Object.values(peers)[0];
    if (firstPeer?.videoEl && firstPeer.videoEl.readyState >= 2 && firstPeer.videoEl.videoWidth > 0) {
      sourceVideo = firstPeer.videoEl;
    } else if (localVideoSrc.readyState >= 2 && localVideoSrc.videoWidth > 0) {
      sourceVideo = localVideoSrc;
    }
  }
  if (!sourceVideo) { setStatus(t('noVideoFreeze')); return; }

  const url = captureVideoFrame(sourceVideo);
  loadFrozenImage(url, url, { remote: false });
  sendWs({ type: 'freeze_frame', base_data_url: url, current_data_url: url });
}

function enterFreezeMode({ remote = false } = {}) {
  isFrozen = true;
  resetUndoRedo();
  frozenCanvas.classList.remove('hidden');
  resizeCanvas();
  annotationBar.classList.add('active');
  document.getElementById('callToolbar').classList.add('hidden');
  pipContainer.classList.add('hidden');
  freezeBtn.classList.add('frozen');
  freezeBtn.innerHTML = ICONS.play + `<span class="tl">${t('resumeBtn')}</span>`;
  setStatus(remote ? t('frozenShared') : t('frozenDraw'));
  showToast(t('screenshotHint'), 5000);
}

function exitFreezeMode({ notify = true, silent = false } = {}) {
  isFrozen = false;
  isDrawing = false;
  lastX = null;
  lastY = null;
  zoomLevel = 1; panX = 0; panY = 0;
  frozenCanvas.classList.add('hidden');
  annotationBar.classList.remove('active');
  document.getElementById('callToolbar').classList.remove('hidden');
  if (localStream?.getVideoTracks?.().length) {
    pipContainer.classList.remove('hidden');
  }
  freezeBtn.classList.remove('frozen');
  freezeBtn.innerHTML = ICONS.pause + `<span class="tl">${t('freeze')}</span>`;
  if (!silent) {
    const connected = Object.keys(peers).length > 0;
    setStatus(connected ? t('live') : t('waitingPeers', currentRoom || ''), connected);
  }
  if (notify) sendWs({ type: 'resume_live' });
}

function clearAnnotations() {
  if (!frozenBaseDataUrl) return;
  pushUndoState(); // save before clearing
  loadFrozenImage(frozenBaseDataUrl, frozenBaseDataUrl, { remote: false });
  sendWs({ type: 'clear_annotations', base_data_url: frozenBaseDataUrl, current_data_url: frozenBaseDataUrl });
}

function saveAnnotatedImage() {
  if (!hasBoard()) { showToast('Nothing to save'); return; }
  const merged = document.createElement('canvas');
  merged.width = frozenCanvas.width;
  merged.height = frozenCanvas.height;
  const ctx = merged.getContext('2d');
  ctx.drawImage(frozenCanvas, 0, 0);
  ctx.drawImage(boardCanvas, 0, 0, boardCanvas.width, boardCanvas.height, 0, 0, merged.width, merged.height);
  merged.toBlob(function(blob) {
    if (!blob) { showToast('Save failed'); return; }
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'clarity-annotation-' + Date.now() + '.png';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    showToast(currentLang === 'zh' ? '已保存' : 'Saved!');
  }, 'image/png');
}

// ═════════════════════════════════
// UNDO / REDO (unlimited, local only)
// ═════════════════════════════════
function pushUndoState() {
  if (!hasBoard()) return;
  undoStack.push(boardCanvas.toDataURL('image/png'));
  redoStack = []; // new action clears redo history
  syncUndoRedoButtons();
}

function syncUndoRedoButtons() {
  const undoBtn = document.getElementById('undoBtn');
  const redoBtn = document.getElementById('redoBtn');
  if (undoBtn) undoBtn.disabled = undoStack.length === 0;
  if (redoBtn) redoBtn.disabled = redoStack.length === 0;
}

async function undoAnnotation() {
  if (undoStack.length === 0 || !hasBoard()) return;
  // Save current state to redo stack
  redoStack.push(boardCanvas.toDataURL('image/png'));
  // Restore previous state
  const prevState = undoStack.pop();
  try {
    const img = await loadImage(prevState);
    boardCanvas.width = img.width;
    boardCanvas.height = img.height;
    boardCtx.clearRect(0, 0, boardCanvas.width, boardCanvas.height);
    boardCtx.drawImage(img, 0, 0);
    redrawFrozen();
    sendBoardSnapshot();
  } catch(e) { console.warn('Undo failed', e); }
  syncUndoRedoButtons();
}

async function redoAnnotation() {
  if (redoStack.length === 0 || !hasBoard()) return;
  // Save current state to undo stack
  undoStack.push(boardCanvas.toDataURL('image/png'));
  // Restore next state
  const nextState = redoStack.pop();
  try {
    const img = await loadImage(nextState);
    boardCanvas.width = img.width;
    boardCanvas.height = img.height;
    boardCtx.clearRect(0, 0, boardCanvas.width, boardCanvas.height);
    boardCtx.drawImage(img, 0, 0);
    redrawFrozen();
    sendBoardSnapshot();
  } catch(e) { console.warn('Redo failed', e); }
  syncUndoRedoButtons();
}

function resetUndoRedo() {
  undoStack = [];
  redoStack = [];
  syncUndoRedoButtons();
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

// Pointer events — single finger draws, two fingers zoom/pan
const activePointers = new Map(); // pointerId → {x, y}

function handlePointerStart(event) {
  if (!isFrozen || !hasBoard()) return;
  event.preventDefault();
  frozenCanvas.setPointerCapture?.(event.pointerId);
  activePointers.set(event.pointerId, { x: event.clientX, y: event.clientY });

  if (activePointers.size === 2) {
    // Start pinch — cancel any drawing
    isDrawing = false;
    lastX = null; lastY = null;
    isPinching = true;
    const pts = [...activePointers.values()];
    lastPinchDist = Math.hypot(pts[1].x - pts[0].x, pts[1].y - pts[0].y);
    lastPinchMidX = (pts[0].x + pts[1].x) / 2;
    lastPinchMidY = (pts[0].y + pts[1].y) / 2;
    return;
  }

  if (activePointers.size === 1 && !isPinching) {
    // Single finger — start drawing
    const rect = frozenCanvas.getBoundingClientRect();
    const point = canvasToBoard(event.clientX - rect.left, event.clientY - rect.top);
    if (!point) return;
    pushUndoState(); // save state before drawing
    isDrawing = true;
    lastX = point.x;
    lastY = point.y;
  }
}

function handlePointerMove(event) {
  if (!isFrozen || !hasBoard()) return;
  event.preventDefault();
  if (!activePointers.has(event.pointerId)) return;
  activePointers.set(event.pointerId, { x: event.clientX, y: event.clientY });

  if (isPinching && activePointers.size === 2) {
    const pts = [...activePointers.values()];
    const dist = Math.hypot(pts[1].x - pts[0].x, pts[1].y - pts[0].y);
    const midX = (pts[0].x + pts[1].x) / 2;
    const midY = (pts[0].y + pts[1].y) / 2;
    const rect = frozenCanvas.getBoundingClientRect();

    // Zoom
    const pinchDelta = (dist - lastPinchDist) * 0.01;
    if (Math.abs(pinchDelta) > 0.001) {
      adjustZoom(pinchDelta, midX - rect.left - frozenCanvas.width / 2, midY - rect.top - frozenCanvas.height / 2);
    }

    // Pan
    panX += midX - lastPinchMidX;
    panY += midY - lastPinchMidY;
    clampPan();
    redrawFrozen();

    lastPinchDist = dist;
    lastPinchMidX = midX;
    lastPinchMidY = midY;
    return;
  }

  if (isDrawing && activePointers.size === 1) {
    const rect = frozenCanvas.getBoundingClientRect();
    const point = canvasToBoard(event.clientX - rect.left, event.clientY - rect.top);
    if (!point) return;
    drawLineOnBoard(lastX, lastY, point.x, point.y, penColor, penWidth);
    sendWs({ type: 'draw_line', x1: lastX, y1: lastY, x2: point.x, y2: point.y, color: penColor, width: penWidth });
    lastX = point.x;
    lastY = point.y;
  }
}

function handlePointerEnd(event) {
  activePointers.delete(event.pointerId);
  if (activePointers.size < 2) isPinching = false;
  if (activePointers.size === 0) {
    if (isDrawing && hasBoard()) {
      sendBoardSnapshot();
    }
    isDrawing = false;
    lastX = null;
    lastY = null;
  }
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
  selectedFreezeTarget = null;
  isSwapped = false;
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
// COACH MARK
// ═════════════════════════════════
const COACH_KEY = 'clarity.freezeCoachSeen';
function showFreezeCoach() {
  if (localStorage.getItem(COACH_KEY)) return;
  const coach = document.getElementById('freezeCoach');
  if (!coach) return;
  coach.classList.remove('hidden');
  setTimeout(() => { coach.classList.add('hidden'); localStorage.setItem(COACH_KEY, '1'); }, 6000);
}
function dismissFreezeCoach() {
  const coach = document.getElementById('freezeCoach');
  if (coach) coach.classList.add('hidden');
  localStorage.setItem(COACH_KEY, '1');
}

// ═════════════════════════════════
// HANGUP SCREEN
// ═════════════════════════════════
function showHangupScreen() {
  cleanup();
  callScreen.classList.remove('active');
  homeScreen.style.display = 'none';
  const hangup = document.getElementById('hangupScreen');
  hangup.classList.add('active');
  applyLang();
}
function dismissHangup() {
  const hangup = document.getElementById('hangupScreen');
  hangup.classList.remove('active');
  homeScreen.style.display = 'flex';
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
  await detectCameras();

  currentRoom = room;
  roomBadge.textContent = room;
  waitingRoomCode.textContent = room;
  waitingView.classList.remove('hidden');
  generateQR();

  try {
    await connectWs(room);
  } catch (error) {
    console.warn('Join failed', error);
    if (isAutoJoin) {
      setStatus(t('autoJoinFail'), false);
      setTimeout(() => { showHomeScreen(); }, 1500);
    } else {
      showToast(currentLang === 'zh' ? '连接失败，请重试' : 'Connection failed, please try again');
      setTimeout(() => { showHomeScreen(); }, 3000);
    }
  }
}

// ═════════════════════════════════
// EVENT BINDINGS
// ═════════════════════════════════
document.getElementById('bigTextBtn').addEventListener('click', toggleBigText);

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

// Tap PIP to swap main/PIP views (like WeChat)
pipContainer.addEventListener('click', () => {
  if (Object.keys(peers).length === 0) return; // nothing to swap
  isSwapped = !isSwapped;
  selectedFreezeTarget = isSwapped ? 'local' : null;
  rebuildVideoGrid();
});

async function autoShare() {
  const link = getJoinLink();
  if (!link) return;
  try {
    if (navigator.share) {
      await navigator.share({ url: link });
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
  dismissFreezeCoach();
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

endBtn.addEventListener('click', showHangupScreen);
document.getElementById('hangupBackBtn').addEventListener('click', dismissHangup);
clearAnnBtn.addEventListener('click', clearAnnotations);
document.getElementById('saveAnnBtn').addEventListener('click', saveAnnotatedImage);
document.getElementById('undoBtn').addEventListener('click', undoAnnotation);
document.getElementById('redoBtn').addEventListener('click', redoAnnotation);
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
  applyBigText();
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
