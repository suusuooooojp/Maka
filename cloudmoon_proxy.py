"""CloudMoon (クラウドムーン) web cloud gaming service integration for School Hub.

Provides a responsive, full-window cloud gaming experience with native UI (no custom launcher/iframe),
universal floating OpenChat integration, full game controller/WebRTC/touch permissions,
reverse-proxy unblocking for school firewalls, ad-free environment, and unlimited playtime enforcement.
"""
from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import ssl
import time
import urllib.parse
from typing import Dict, Optional

# Sanitize NO_PROXY environment variables to prevent Python 3.12 httpx [::1] port parsing ValueError
for _k in ("no_proxy", "NO_PROXY", "GLOBAL_AGENT_NO_PROXY"):
    if _k in os.environ and "[::1]" in os.environ[_k]:
        os.environ[_k] = os.environ[_k].replace("[::1]", "").rstrip(",")

import httpx
import websockets
from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

SERVICE_NAME = "CloudMoon"
SERVICE_ICON = "🌙"
SERVICE_PATH = "/cloudmoon/"

router = APIRouter()

TARGET_ORIGIN = "https://web.cloudmoonapp.com"
API_ORIGIN = "https://api.prod.cloudmoonapp.com"
DEFAULT_TARGET_URL = f"{TARGET_ORIGIN}/ja/"

UPSTREAM_API_HOSTS = (
    "https://api.prod.cloudmoonapp.com",
    "https://api.cloudmoon.cloudbatata.com",
    "https://api.prod.geometry.today",
    "https://hrz5zfjq02.execute-api.us-east-1.amazonaws.com",
    "https://api.prod.viewoncloud.com",
)

_HTTP_CLIENT: Optional[httpx.AsyncClient] = None
_CACHE: Dict[str, bytes] = {}
_API_CACHE: Dict[str, tuple[float, bytes, int]] = {}


def _get_http_client() -> httpx.AsyncClient:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None or _HTTP_CLIENT.is_closed:
        _HTTP_CLIENT = httpx.AsyncClient(
            follow_redirects=True,
            verify=False,
            timeout=httpx.Timeout(30.0, connect=8.0),
            limits=httpx.Limits(
                max_keepalive_connections=64,
                max_connections=128,
                keepalive_expiry=60.0,
            ),
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
            },
        )
    return _HTTP_CLIENT


_AD_FREE_CSS = """<style id="sh-cloudmoon-adblock">
/* Hide visible CloudMoon & Playwire/GAM/Ad-Shield ad containers without touching 1x1 -9999px bait elements (.ad-banner, #ad-banner, ins.adsbygoogle) */
.mpu-card,
.gam-wrapper,
.gam-mpu,
.floating-video,
aside[aria-label="Video advertisement"],
aside[aria-label="Advertisement"],
.anchor-banner,
.anchor-wrapper,
.a-div-vertical,
.a-div-horizontal,
.a-div-video,
#lyt1-left-img-div,
#lyt1-right-img-div,
#lyt2-left-img-div,
#lyt2-right-img-div,
#lyt3-top-img-div,
#lyt3-bottom-img-div,
ramp-ad,
[id^="div-gpt-ad"] {
  display: none !important;
  width: 0 !important;
  height: 0 !important;
  opacity: 0 !important;
  pointer-events: none !important;
  visibility: hidden !important;
}
/* Ensure game container and loading screen always use full viewport without ad margins */
.container.has-a-div,
.container.has-ad-vertical-lg,
.container.hide-vertical {
  padding: 0 !important;
  margin: 0 !important;
}
.loading.band-vertical-lg,
.loading.band-landscape,
.loading.band-hide-vertical {
  left: 0 !important;
  right: 0 !important;
  top: 0 !important;
  bottom: 0 !important;
  width: 100vw !important;
  height: 100vh !important;
}
/* Style CloudMoon in-game text input Send button ("送る") clearly */
.input-div_div_button {
  display: inline-flex !important;
  align-items: center !important;
  justify-content: center !important;
  gap: 5px !important;
  background: #1a73e8 !important;
  color: #ffffff !important;
  border-radius: 999px !important;
  padding: 5px 14px !important;
  cursor: pointer !important;
  text-decoration: none !important;
  font-family: 'Google Sans', 'Noto Sans JP', sans-serif !important;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.25) !important;
  transition: background 0.15s ease !important;
}
.input-div_div_button:hover {
  background: #1557b0 !important;
}
.input-div_div_button::after {
  content: "送る" !important;
  font-size: 12px !important;
  font-weight: 700 !important;
  color: #ffffff !important;
  line-height: 1 !important;
  white-space: nowrap !important;
}
</style>"""

_CM_ENGINE_JS = r"""// CloudMoon Ad-Free, Anti-Crash & Unlimited Playtime Engine for School Hub
(function() {
  console.log('[CloudMoon Unlimited] Initializing School Hub Native Ad-Free & Unlimited Time Engine...');

  // 0. Neutralize Ad-Shield, Playwire RAMP, Google IMA, GPT, AdSense, and Prebid globals immediately
  //    IMPORTANT: window.adsbygoogle.push MUST be a function so Ya() in 2.C7CpNCnt.js returns "clear" (not "adblock")
  try {
    window.__adEnabledCache = false;
    window.__adshieldLoaded = true;
    window.__adshieldLoading = false;
    window.__rampLoaded = true;
    window.adsbygoogle = window.adsbygoogle || { loaded: true, push: function() {} };
    if (typeof window.adsbygoogle.push !== 'function') {
      window.adsbygoogle.push = function() {};
    }
    window.pbjs = window.pbjs || { que: [], cmd: [] };
    window.googletag = window.googletag || {
      cmd: [],
      apiReady: true,
      pubadsReady: true,
      defineSlot: function() {
        return {
          addService: function() { return this; },
          setTargeting: function() { return this; },
          defineSizeMapping: function() { return this; }
        };
      },
      pubads: function() {
        return {
          enableSingleRequest: function() {},
          collapseEmptyDivs: function() {},
          addEventListener: function() {},
          removeEventListener: function() {},
          refresh: function() {},
          clear: function() {},
          setTargeting: function() { return this; }
        };
      },
      enableServices: function() {},
      display: function() {},
      destroySlots: function() { return true; },
      sizeMapping: function() {
        return {
          addSize: function() { return this; },
          build: function() { return []; }
        };
      },
      enums: { OutOfPageFormat: { REWARDED: 'REWARDED', INTERSTITIAL: 'INTERSTITIAL' } },
      defineOutOfPageSlot: function() { return null; }
    };
    window.ramp = window.ramp || {
      que: [],
      passiveMode: true,
      spaNewPage: function() { return Promise.resolve(); },
      addUnits: function() { return Promise.resolve(); },
      displayUnits: function() {},
      destroyUnits: function() { return Promise.resolve(); },
      settings: {}
    };
  } catch (_) {}

  // 1. Safe localStorage guard & forced ad-free / same-window settings
  try {
    const testKey = '__sh_test__';
    window.localStorage.setItem(testKey, testKey);
    window.localStorage.removeItem(testKey);
  } catch (e) {
    console.warn('[CloudMoon] Polyfilling memory-backed localStorage');
    const memStore = {};
    const memStorage = {
      getItem: function(k) { return memStore.hasOwnProperty(k) ? memStore[k] : null; },
      setItem: function(k, v) { memStore[k] = String(v); },
      removeItem: function(k) { delete memStore[k]; },
      clear: function() { for (let k in memStore) delete memStore[k]; },
      get length() { return Object.keys(memStore).length; },
      key: function(i) { return Object.keys(memStore)[i] || null; }
    };
    try {
      Object.defineProperty(window, 'localStorage', { value: memStorage, configurable: true, writable: true });
    } catch (_) {}
  }

  try {
    const origSetItem = Storage.prototype.setItem;
    const origGetItem = Storage.prototype.getItem;
    Storage.prototype.setItem = function(k, v) {
      if (k === 'ad_enabled_cache') v = 'false';
      if (k === 'newWindowLaunch') v = 'false';
      if (k === 'enableHighLight') v = 'false';
      if (k === 'cm_fast_api_host') {
        v = JSON.stringify({ url: window.location.origin + '/cloudmoon/api', ts: Date.now() });
      }
      return origSetItem.call(this, k, v);
    };
    Storage.prototype.getItem = function(k) {
      if (k === 'ad_enabled_cache') return 'false';
      if (k === 'newWindowLaunch') return 'false';
      if (k === 'enableHighLight') return 'false';
      if (k === 'cm_fast_api_host') {
        return JSON.stringify({ url: window.location.origin + '/cloudmoon/api', ts: Date.now() });
      }
      return origGetItem.call(this, k);
    };
    if (!localStorage.getItem('selectedServer') || localStorage.getItem('selectedServer') === '0') {
      origSetItem.call(localStorage, 'selectedServer', '3');
    }
    origSetItem.call(localStorage, 'newWindowLaunch', 'false');
    origSetItem.call(localStorage, 'ad_enabled_cache', 'false');
    origSetItem.call(localStorage, 'enableHighLight', 'false');
    origSetItem.call(localStorage, 'cm_fast_api_host', JSON.stringify({ url: window.location.origin + '/cloudmoon/api', ts: Date.now() }));
    if (!localStorage.getItem('quality')) {
      origSetItem.call(localStorage, 'quality', 'HD');
    }
  } catch (e) {}

  // 1b. Ultra-Low-Latency WebRTC Playout & Receiver Optimizer
  try {
    function applyLowLatencyReceiver(receiver) {
      if (!receiver) return;
      const isApple = /iPad|iPhone|iPod|Macintosh/i.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
      if (isApple) {
        // Apple WebKit VideoToolbox requires ~20ms jitter buffer to prevent hardware decoder starvation on iPad
        try { if ('playoutDelayHint' in receiver) receiver.playoutDelayHint = 0.02; } catch (_) {}
        try { if ('jitterBufferTarget' in receiver) receiver.jitterBufferTarget = 20; } catch (_) {}
        return;
      }
      try {
        if ('playoutDelayHint' in receiver) receiver.playoutDelayHint = 0;
      } catch (_) {}
      try {
        if ('jitterBufferTarget' in receiver) receiver.jitterBufferTarget = 0;
      } catch (_) {}
    }
    if (typeof RTCPeerConnection !== 'undefined') {
      const origAddEv = RTCPeerConnection.prototype.addEventListener;
      RTCPeerConnection.prototype.addEventListener = function(type, listener, options) {
        if (type === 'track') {
          const wrapped = function(ev) {
            if (ev && ev.receiver) applyLowLatencyReceiver(ev.receiver);
            return listener.apply(this, arguments);
          };
          return origAddEv.call(this, type, wrapped, options);
        }
        return origAddEv.apply(this, arguments);
      };
      const ontrackDesc = Object.getOwnPropertyDescriptor(RTCPeerConnection.prototype, 'ontrack');
      if (ontrackDesc && ontrackDesc.set) {
        Object.defineProperty(RTCPeerConnection.prototype, 'ontrack', {
          set: function(fn) {
            const wrapped = function(ev) {
              if (ev && ev.receiver) applyLowLatencyReceiver(ev.receiver);
              return fn ? fn.apply(this, arguments) : undefined;
            };
            return ontrackDesc.set.call(this, wrapped);
          },
          get: ontrackDesc.get,
          configurable: true
        });
      }
      const origGetReceivers = RTCPeerConnection.prototype.getReceivers;
      if (origGetReceivers) {
        RTCPeerConnection.prototype.getReceivers = function() {
          const list = origGetReceivers.apply(this, arguments) || [];
          for (let i = 0; i < list.length; i++) applyLowLatencyReceiver(list[i]);
          return list;
        };
      }
    }
    if (typeof RTCDataChannel !== 'undefined') {
      const origDcSend = RTCDataChannel.prototype.send;
      RTCDataChannel.prototype.send = function(data) {
        // Prevent iPad WebKit SCTP send buffer exhaustion on rapid multi-touch drag events (120Hz ProMotion)
        if (this.bufferedAmount > 65536 && typeof data === 'string' && data.startsWith('touch>Touch move')) {
          return;
        }
        return origDcSend.apply(this, arguments);
      };
    }
  } catch (_) {}

  // 1c. Ensure all inline video and audio elements on iPad/iOS have webkit-playsinline and playsinline
  try {
    const enforceInlineMedia = function(el) {
      if (!el || !el.tagName) return;
      const tag = el.tagName.toLowerCase();
      if (tag === 'video' || tag === 'audio') {
        try {
          el.setAttribute('playsinline', '');
          el.setAttribute('webkit-playsinline', '');
          el.setAttribute('x5-playsinline', '');
          el.playsInline = true;
          if ('webkitPlaysInline' in el) el.webkitPlaysInline = true;
        } catch (_) {}
      }
    };
    if (typeof MutationObserver !== 'undefined') {
      const mediaMo = new MutationObserver(function(muts) {
        for (let i = 0; i < muts.length; i++) {
          const added = muts[i].addedNodes;
          for (let j = 0; j < added.length; j++) {
            const node = added[j];
            if (node.nodeType === 1) {
              enforceInlineMedia(node);
              if (node.querySelectorAll) {
                const mediaElems = node.querySelectorAll('video, audio');
                for (let k = 0; k < mediaElems.length; k++) enforceInlineMedia(mediaElems[k]);
              }
            }
          }
        }
      });
      if (document.documentElement) {
        mediaMo.observe(document.documentElement, { childList: true, subtree: true });
      } else {
        window.addEventListener('DOMContentLoaded', function() {
          mediaMo.observe(document.documentElement, { childList: true, subtree: true });
        });
      }
    }
  } catch (_) {}

  // 2. Intercept dynamic <script> injections to block Ad-Shield, Playwire, DoubleClick, AdSense, IMA SDK
  const BLOCKED_SCRIPT_PATTERNS = [
    'cm-compat.js',
    'ramp-helper.js',
    'prebid',
    'ads/banner.js',
    'html-load.cc',
    'ad-shield',
    'intergient.com',
    'doubleclick.net',
    'googlesyndication.com',
    'imasdk.googleapis.com',
    'googleads.g.doubleclick.net',
    'adtrafficquality.google'
  ];

  function isBlockedAdUrl(url) {
    if (!url || typeof url !== 'string') return false;
    const lower = url.toLowerCase();
    return BLOCKED_SCRIPT_PATTERNS.some(function(pat) { return lower.includes(pat); });
  }

  try {
    const origCreateElement = document.createElement.bind(document);
    document.createElement = function(tagName, options) {
      const el = origCreateElement(tagName, options);
      if (typeof tagName === 'string' && tagName.toLowerCase() === 'script') {
        let blockedSrc = '';
        const origSetAttr = el.setAttribute.bind(el);
        el.setAttribute = function(name, value) {
          if (String(name).toLowerCase() === 'src' && isBlockedAdUrl(String(value))) {
            blockedSrc = String(value);
            origSetAttr('data-blocked-ad-src', blockedSrc);
            setTimeout(function() {
              if (typeof el.onload === 'function') {
                try { el.onload(new Event('load')); } catch (_) {}
              }
              try { el.dispatchEvent(new Event('load')); } catch (_) {}
            }, 0);
            return;
          }
          return origSetAttr(name, value);
        };
        try {
          const srcDesc = Object.getOwnPropertyDescriptor(HTMLScriptElement.prototype, 'src');
          if (srcDesc && srcDesc.set) {
            Object.defineProperty(el, 'src', {
              get: function() { return blockedSrc || srcDesc.get.call(this); },
              set: function(val) {
                if (isBlockedAdUrl(String(val))) {
                  blockedSrc = String(val);
                  origSetAttr('data-blocked-ad-src', blockedSrc);
                  setTimeout(function() {
                    if (typeof el.onload === 'function') {
                      try { el.onload(new Event('load')); } catch (_) {}
                    }
                    try { el.dispatchEvent(new Event('load')); } catch (_) {}
                  }, 0);
                  return;
                }
                return srcDesc.set.call(this, val);
              },
              configurable: true
            });
          }
        } catch (_) {}
      }
      return el;
    };
  } catch (_) {}

  // 3. Force window.open for game launch (/run-site/) to navigate in the current tab
  try {
    const origWindowOpen = window.open;
    window.open = function(url, target, features) {
      const urlStr = url ? String(url) : '';
      if (urlStr.includes('/run-site')) {
        window.location.href = urlStr;
        return window;
      }
      if (urlStr === 'about:blank') {
        const fakeWin = {
          closed: false,
          opener: null,
          close: function() { this.closed = true; },
          focus: function() {},
          location: {
            _href: 'about:blank',
            get href() { return this._href; },
            set href(v) {
              this._href = v;
              if (v && v !== 'about:blank') {
                window.location.href = v;
              }
            }
          }
        };
        return fakeWin;
      }
      return origWindowOpen ? origWindowOpen.apply(this, arguments) : null;
    };
  } catch (_) {}

  // 4. WebRTC DataChannel Kickout & AdBlock-Penalty Suppressor (Makes gameplay time truly UNLIMITED)
  try {
    if (typeof RTCPeerConnection !== 'undefined') {
      const origCreateDc = RTCPeerConnection.prototype.createDataChannel;
      RTCPeerConnection.prototype.createDataChannel = function(label, options) {
        const dc = origCreateDc.apply(this, arguments);
        try { window.__cm_dc = dc; } catch (_) {}
        return dc;
      };
    }
  } catch (_) {}

  try {
    if (typeof RTCDataChannel !== 'undefined' && RTCDataChannel.prototype.send) {
      const origDcSend = RTCDataChannel.prototype.send;
      RTCDataChannel.prototype.send = function(data) {
        if (typeof data === 'string' && data.startsWith('adBlock>')) {
          data = 'adBlock>false';
        }
        return origDcSend.call(this, data);
      };
    }
  } catch (_) {}

  const origAddEventListener = window.addEventListener;
  window.addEventListener = function(type, listener, options) {
    if (type === 'webrtc-dc-message') {
      const wrapped = function(e) {
        if (e && typeof e.detail === 'string') {
          if (e.detail.startsWith('kickout>') || e.detail.includes('kickout') || e.detail.startsWith('noTime') || e.detail.includes('time_up')) {
            console.warn('[CloudMoon Unlimited] Blocked kickout signal:', e.detail);
            if (e.stopImmediatePropagation) e.stopImmediatePropagation();
            return;
          }
        }
        return listener.apply(this, arguments);
      };
      return origAddEventListener.call(this, type, wrapped, options);
    }
    return origAddEventListener.apply(this, arguments);
  };

  try {
    const origOnMessage = Object.getOwnPropertyDescriptor(RTCDataChannel.prototype, 'onmessage');
    if (origOnMessage && origOnMessage.set) {
      Object.defineProperty(RTCDataChannel.prototype, 'onmessage', {
        set: function(fn) {
          const wrapped = function(ev) {
            if (ev && typeof ev.data === 'string' && (ev.data.startsWith('kickout>') || ev.data.includes('kickout'))) {
              console.warn('[CloudMoon Unlimited] Dropped kickout DataChannel message:', ev.data);
              return;
            }
            return fn.apply(this, arguments);
          };
          return origOnMessage.set.call(this, wrapped);
        },
        get: origOnMessage.get,
        configurable: true
      });
    }
  } catch (e) {}

  try {
    const origDcAddEventListener = RTCDataChannel.prototype.addEventListener;
    RTCDataChannel.prototype.addEventListener = function(type, listener, options) {
      if (type === 'message') {
        const wrapped = function(ev) {
          if (ev && typeof ev.data === 'string' && (ev.data.startsWith('kickout>') || ev.data.includes('kickout'))) {
            console.warn('[CloudMoon Unlimited] Dropped kickout DataChannel event listener message:', ev.data);
            return;
          }
          return listener.apply(this, arguments);
        };
        return origDcAddEventListener.call(this, type, wrapped, options);
      }
      return origDcAddEventListener.apply(this, arguments);
    };
  } catch (e) {}

  // 5. Inactivity keepalive, interaction listener & periodic background reward refill
  try {
    // 5a. Anti-AFK: ANY user interaction (keyboard, mouse, wheel, touch, pointer) keeps activity state fresh
    // Throttle to at most once every 5 seconds to eliminate CPU saturation and main-thread freeze on iPad (120Hz ProMotion touchmove flood)
    let lastActivityDispatch = 0;
    const reportActivity = function() {
      const now = Date.now();
      if (now - lastActivityDispatch > 5000) {
        lastActivityDispatch = now;
        try {
          window.dispatchEvent(new CustomEvent('webrtc-dc-send', { detail: 'touch>input_activity' }));
        } catch (_) {}
      }
    };
    ['keydown', 'keyup', 'mousedown', 'mouseup', 'mousemove', 'wheel', 'touchstart', 'touchmove', 'pointerdown'].forEach(function(evType) {
      window.addEventListener(evType, reportActivity, { passive: true, capture: true });
    });

    // 5b. Anti-AFK pulse: dispatch keepalive every 15 seconds to keep client-side inactivity timer reset
    setInterval(function() {
      try {
        window.dispatchEvent(new CustomEvent('webrtc-dc-send', { detail: 'touch>keepalive' }));
      } catch (_) {}
    }, 15000);

    // 5c. Periodic Background Free Playtime Refill:
    // Pings /cloudmoon/api/web/ad with active session token so backend continuously claims upstream ad rewards
    setInterval(function() {
      try {
        const s = window.__cm_session;
        if (s && s.token) {
          fetch('/cloudmoon/api/web/ad', {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              'X-User-Token': s.token
            },
            body: JSON.stringify({ game: s.game || 'com.roblox.client' })
          }).catch(function() {});
        }
      } catch (_) {}
    }, 45000);

    // 5d. Intercept unprovoked navigation to Home on /run-site/ gameplay pages
    if ((window.location.pathname || '').startsWith('/run-site')) {
      const isExitAllowed = function() {
        return !!window.__cm_intentional_exit;
      };
      try {
        const origAssign = window.location.assign;
        if (origAssign) {
          window.location.assign = function(url) {
            const u = String(url || '');
            if (!isExitAllowed() && (u === '/' || u === '/cloudmoon/' || u.startsWith(window.location.origin + '/cloudmoon'))) {
              console.warn('[CloudMoon Unlimited] Intercepted and blocked auto-navigation to home:', u);
              return;
            }
            return origAssign.apply(this, arguments);
          };
        }
      } catch (_) {}
      try {
        const origReplace = window.location.replace;
        if (origReplace) {
          window.location.replace = function(url) {
            const u = String(url || '');
            if (!isExitAllowed() && (u === '/' || u === '/cloudmoon/' || u.startsWith(window.location.origin + '/cloudmoon'))) {
              console.warn('[CloudMoon Unlimited] Intercepted and blocked auto-navigation to home:', u);
              return;
            }
            return origReplace.apply(this, arguments);
          };
        }
      } catch (_) {}
    }
  } catch (_) {}

  // 5b. DOM Safeguard: Immediately dismiss any "Today's free game time has been used up" modal if ever rendered
  try {
    const NO_TIME_PATTERNS = [
      "Today's free game time has been used up",
      "free game time has been used up",
      "本日の無料プレイ時間",
      "無料ゲーム時間を使い切りました",
      "今日の無料ゲーム時間"
    ];
    const dismissNoTimeDialog = function() {
      try {
        const dialogs = document.querySelectorAll('.dialog-backdrop, [role="dialog"], .modal-backdrop, .alert-modal');
        for (let i = 0; i < dialogs.length; i++) {
          const el = dialogs[i];
          const txt = (el.innerText || el.textContent || '');
          for (let j = 0; j < NO_TIME_PATTERNS.length; j++) {
            if (txt.includes(NO_TIME_PATTERNS[j])) {
              console.warn('[CloudMoon Unlimited] Suppressed free-time limit dialog');
              el.style.display = 'none';
              el.remove();
              break;
            }
          }
        }
      } catch (_) {}
    };
    if (typeof MutationObserver !== 'undefined') {
      const mo = new MutationObserver(dismissNoTimeDialog);
      if (document.documentElement) {
        mo.observe(document.documentElement, { childList: true, subtree: true });
      } else {
        window.addEventListener('DOMContentLoaded', function() {
          mo.observe(document.documentElement, { childList: true, subtree: true });
        });
      }
    }
  } catch (_) {}

  // 6. WebSocket Wrapper: Block Ad-Shield WebSockets & rewrite /cloudmoon/ws-proxy/ URLs cleanly
  try {
    const OrigWebSocket = window.WebSocket;
    const WrappedWebSocket = function(url, protocols) {
      let urlStr = String(url || '');
      if (urlStr.includes('html-load.cc') || urlStr.includes('ad-shield')) {
        console.warn('[CloudMoon AdBlock] Blocked Ad-Shield WebSocket:', urlStr);
        const dummy = Object.create(OrigWebSocket.prototype);
        dummy.readyState = 3;
        dummy.close = function() {};
        dummy.send = function() {};
        dummy.addEventListener = function() {};
        dummy.removeEventListener = function() {};
        return dummy;
      }
      // Rewrite wss://host/client/socket.io?__sh_ws_host=<target>&... into /cloudmoon/ws-proxy/<target>/client/socket.io
      if (urlStr.includes('__sh_ws_host=')) {
        try {
          const parsed = new URL(urlStr);
          const targetHost = parsed.searchParams.get('__sh_ws_host');
          if (targetHost) {
            parsed.searchParams.delete('__sh_ws_host');
            parsed.pathname = '/cloudmoon/ws-proxy/' + targetHost + parsed.pathname;
            urlStr = parsed.toString();
          }
        } catch (_) {}
      }
      return protocols !== undefined ? new OrigWebSocket(urlStr, protocols) : new OrigWebSocket(urlStr);
    };
    WrappedWebSocket.prototype = OrigWebSocket.prototype;
    WrappedWebSocket.CONNECTING = OrigWebSocket.CONNECTING;
    WrappedWebSocket.OPEN = OrigWebSocket.OPEN;
    WrappedWebSocket.CLOSING = OrigWebSocket.CLOSING;
    WrappedWebSocket.CLOSED = OrigWebSocket.CLOSED;
    window.WebSocket = WrappedWebSocket;
  } catch (_) {}

  // 7. Global fetch interceptor (Redirects API calls, blocks ads, speeds up signaling, auto-refills server time, unlocks VIP)
  const origFetch = window.fetch;
  const API_HOSTS = [
    'https://api.prod.cloudmoonapp.com',
    'https://api.cloudmoon.cloudbatata.com',
    'https://api.prod.geometry.today',
    'https://hrz5zfjq02.execute-api.us-east-1.amazonaws.com',
    'https://api.prod.viewoncloud.com'
  ];

  window.fetch = async function(resource, init) {
    let url = typeof resource === 'string' ? resource : (resource instanceof Request ? resource.url : String(resource));

    // Instant GeoIP response so Socket.IO hello_server -> client_register doesn't stall for 1.5s
    if (url.includes('api.ip.sb/geoip')) {
      return new Response(JSON.stringify({
        ip: '127.0.0.1',
        country_code: 'JP',
        country: 'Japan',
        timezone: 'Asia/Tokyo'
      }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' }
      });
    }

    // Instant 200 OK for ad-blocker probe / ad scripts so Ja() resolves in 0ms with isBlocked=false, showAD=false
    if (isBlockedAdUrl(url)) {
      return new Response('/* ad blocked */', {
        status: 200,
        headers: { 'Content-Type': 'application/javascript' }
      });
    }

    // Redirect all upstream API hosts to School Hub reverse proxy
    for (let i = 0; i < API_HOSTS.length; i++) {
      if (url.includes(API_HOSTS[i])) {
        url = url.replace(API_HOSTS[i], window.location.origin + '/cloudmoon/api');
      }
    }

    // Intercept /_ping for instant API host selection
    if (url.endsWith('/_ping')) {
      return new Response(JSON.stringify({ code: 0, message: 'pong' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' }
      });
    }

    if (resource instanceof Request) {
      if (resource.url !== url) {
        try {
          resource = new Request(url, resource);
        } catch (_) {
          resource = url;
        }
      }
    } else {
      resource = url;
    }

    const doFetch = (r, i) => (i !== undefined ? origFetch.call(this, r, i) : origFetch.call(this, r));

    // Forward /web/ad to our backend proxy so it claims real upstream reward time while returning showAD:false, unlimit:false
    if (url.includes('/web/ad')) {
      try {
        doFetch(resource, init).catch(function() {});
      } catch (_) {}
      return new Response(JSON.stringify({
        code: 0,
        message: 'Success',
        data: {
          showAD: false,
          unlimit: false,
          timeSecLeft: 99999999,
          rewardAdPass: true,
          rewardHash: '',
          initOrientation: 3,
          adSource: ''
        }
      }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' }
      });
    }

    // Intercept /ad/gpt_reward and forward to backend proxy
    if (url.includes('/ad/gpt_reward')) {
      try {
        doFetch(resource, init).catch(function() {});
      } catch (_) {}
      return new Response(JSON.stringify({
        code: 0,
        message: 'Success',
        data: { reward_min: 60 }
      }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' }
      });
    }

    // Intercept /phone/connect to force ad_unblock: true while preserving native 720x1280 touch coordinate space
    if (url.includes('/phone/connect') && init && init.body && typeof init.body === 'string') {
      try {
        const parsedBody = JSON.parse(init.body);
        parsedBody.ad_unblock = true;
        parsedBody.screen_res = '720x1280';
        init = Object.assign({}, init, { body: JSON.stringify(parsedBody) });
      } catch (_) {}
    }

    // Intercept /phone/action so in-game HD quality switching succeeds even for free accounts
    if (url.includes('/phone/action') && init && init.body && typeof init.body === 'string') {
      try {
        const parsedAction = JSON.parse(init.body);
        if (parsedAction && parsedAction.key === 'quality') {
          if (parsedAction.value) {
            try { localStorage.setItem('quality', parsedAction.value); } catch (_) {}
          }
          doFetch(resource, init).catch(function() {});
          return new Response(JSON.stringify({
            code: 0,
            message: 'Success',
            data: {}
          }), {
            status: 200,
            headers: { 'Content-Type': 'application/json' }
          });
        }
      } catch (_) {}
    }

    // Intercept /phone/list to preserve all real instance fields while granting unlimited VIP time with proper time_left_set object
    if (url.includes('/phone/list')) {
      try {
        const resp = await doFetch(resource, init);
        const data = await resp.json();
        if (data && data.code === 0 && data.data && Array.isArray(data.data.list)) {
          data.data.list = data.data.list.map(function(item) {
            return Object.assign({}, item, {
              expired_at: Math.max(item.expired_at || 0, 2147483647),
              level: 3,
              is_paid: true,
              time_left_set: {
                free_time_sec: 99999999,
                permanent_time_sec: 99999999
              },
              time_left_sec: 99999999,
              use_time_type: 1,
              is_subscription: true,
              subscription_channel: 'vip',
              subscription_product_id: 'vip_unlimited',
              backup_time: 99999999,
              is_unlimit_locale: true,
              use_proxy: false
            });
          });
        }
        return new Response(JSON.stringify(data), {
          status: 200,
          headers: { 'Content-Type': 'application/json' }
        });
      } catch (_) {}
    }

    // Intercept /user/info to unlock HD quality options (min_vip_level: 0) while preserving real user fields
    if (url.includes('/user/info')) {
      const unlockedVideoLevels = [
        { level: 'HD', min_vip_level: 0 },
        { level: 'SD', min_vip_level: 0 },
        { level: 'LD', min_vip_level: 0 },
        { level: 'ULD', min_vip_level: 0 }
      ];
      try {
        const resp = await doFetch(resource, init);
        const data = await resp.json();
        if (data && data.code === 0 && data.data) {
          data.data.video_level = unlockedVideoLevels;
          data.data.is_set_pwd = true;
          data.data.is_test = false;
          data.data.is_new = false;
        }
        return new Response(JSON.stringify(data), {
          status: 200,
          headers: { 'Content-Type': 'application/json' }
        });
      } catch (_) {}
    }

    return doFetch(resource, init);
  };

  // Keep CloudMoon concurrent session & communications active on /run-site gameplay pages
  try {
    let isSameOriginChild = false;
    try {
      if (window.self !== window.top && window.parent && window.parent.location && window.parent.location.origin === window.location.origin) {
        isSameOriginChild = true;
      }
    } catch (_) {}
    if (!isSameOriginChild && (window.location.pathname || '').startsWith('/run-site')) {
      const getSid = function() {
        const m = document.cookie.match(/(?:^|;\s*)sh_sid=([^;]+)/);
        if (m) return decodeURIComponent(m[1]);
        try {
          const s = localStorage.getItem('sh_sid') || localStorage.getItem('schoolhub.currentActivity.session.v1');
          if (s) return s;
        } catch (_) {}
        const ns = 's-' + (crypto.randomUUID ? crypto.randomUUID().replace(/-/g, '').slice(0, 18) : (Date.now().toString(16) + Math.random().toString(16).slice(2, 12)).slice(0, 18));
        try {
          document.cookie = 'sh_sid=' + encodeURIComponent(ns) + '; path=/; max-age=2592000; SameSite=Lax';
          localStorage.setItem('sh_sid', ns);
        } catch (_) {}
        return ns;
      };
      const getTabId = function() {
        try {
          let t = sessionStorage.getItem('sh_tab_id') || '';
          if (!t) {
            t = 't-' + Math.random().toString(36).slice(2, 10) + Date.now().toString(36).slice(-4);
            sessionStorage.setItem('sh_tab_id', t);
          }
          return t;
        } catch (_) {
          return 't-cm';
        }
      };
      const sendCmHb = function(st, src, keepalive) {
        try {
          const payload = JSON.stringify({
            session: getSid(),
            tab: getTabId(),
            service: 'CloudMoon',
            path: window.location.pathname + (window.location.search || ''),
            origin: window.location.origin,
            state: st || (document.visibilityState === 'visible' ? 'active' : 'idle'),
            source: src || 'heartbeat',
            include_stats: false
          });
          if (keepalive && navigator.sendBeacon) {
            navigator.sendBeacon('/activity/heartbeat', new Blob([payload], { type: 'application/json' }));
            return;
          }
          doFetch('/activity/heartbeat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: payload,
            keepalive: !!keepalive
          }).catch(function() {});
        } catch (_) {}
      };
      sendCmHb('active', 'visit', false);
      setInterval(function() {
        sendCmHb(document.visibilityState === 'visible' ? 'active' : 'idle', 'heartbeat', false);
      }, 6000);
      window.addEventListener('pagehide', function() {
        sendCmHb('leaving', 'visibility', true);
      });
    }
  } catch (_) {}

  // Prevent pressing Enter from auto-submitting text in CloudMoon; require clicking the "送る" (Send) button
  try {
    const isCmTextInputTarget = function(el) {
      if (!el || el.nodeType !== 1) return false;
      if (el.id === 'input-div_input') return true;
      if (el.closest && el.closest('#input-div')) return true;
      const tag = (el.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'textarea' || el.isContentEditable) return true;
      return false;
    };
    const isCmInputBarOpen = function() {
      try {
        const bar = document.getElementById('input-div');
        return !!(bar && bar.offsetParent !== null);
      } catch (_) {
        return false;
      }
    };
    const interceptEnterKey = function(e) {
      try {
        const isEnter = (e.key === 'Enter' || e.code === 'Enter' || e.code === 'NumpadEnter' || e.keyCode === 13 || e.which === 13);
        const target = e.target || document.activeElement;
        if (isEnter && (isCmTextInputTarget(target) || isCmInputBarOpen())) {
          // Allow Enter inside multi-line textarea only with Shift+Enter if not #input-div_input
          const isSingleLineInput = !target || target.id === 'input-div_input' || (target.tagName || '').toLowerCase() === 'input';
          if (isSingleLineInput || !e.shiftKey) {
            e.preventDefault();
            e.stopPropagation();
            if (typeof e.stopImmediatePropagation === 'function') {
              e.stopImmediatePropagation();
            }
            return false;
          }
        }
        // Also stop keyboard events inside #input-div_input from bubbling to window.handleGlobalKeyDown/Up
        if (target && (target.id === 'input-div_input' || (target.closest && target.closest('#input-div')))) {
          e.stopPropagation();
        }
      } catch (_) {}
    };
    window.addEventListener('keydown', interceptEnterKey, true);
    window.addEventListener('keypress', interceptEnterKey, true);
    window.addEventListener('keyup', interceptEnterKey, true);
    document.addEventListener('keydown', interceptEnterKey, true);
    document.addEventListener('keypress', interceptEnterKey, true);
    document.addEventListener('keyup', interceptEnterKey, true);
  } catch (_) {}
})();
"""


def _transform_html(html: str) -> str:
    """Injects School Hub Ad-Free Engine and strips upstream ad scripts from HTML."""
    # Remove Prebid and Ad-Shield scripts from HTML
    html = re.sub(r'<script[^>]*src=["\'][^"\']*prebid[^"\']*["\'][^>]*>\s*</script>', "", html, flags=re.IGNORECASE)
    html = re.sub(r'<script[^>]*src=["\'][^"\']*cm-compat\.js[^"\']*["\'][^>]*>\s*</script>', "", html, flags=re.IGNORECASE)
    html = re.sub(r'<script[^>]*src=["\'][^"\']*ramp-helper\.js[^"\']*["\'][^>]*>\s*</script>', "", html, flags=re.IGNORECASE)

    # Ensure relative run-site assets resolve properly even if loaded via /run-site?sid=... without trailing slash
    html = html.replace('href="./main.css"', 'href="/run-site/main.css"')
    html = html.replace('href="./images/favicon.ico"', 'href="/run-site/images/favicon.ico"')

    inject = f'{_AD_FREE_CSS}<script src="/cloudmoon/cm-engine.js"></script>'
    if "<meta charset" in html.lower():
        idx = html.lower().find("<meta charset")
        end_tag = html.find(">", idx)
        if end_tag != -1:
            html = html[:end_tag + 1] + inject + html[end_tag + 1:]
        else:
            html = html.replace("<head>", f"<head>{inject}")
    elif "<head>" in html:
        html = html.replace("<head>", f"<head>{inject}")
    elif "<!doctype html>" in html.lower():
        html = f"{inject}{html}"
    else:
        html = f"{inject}{html}"
    return html


def _transform_js(js_text: str, is_runsite: bool = False) -> str:
    """Rewrites upstream API hosts, neutralizes ad-block detection & noTimeContent alerts, unlocks HD quality for free users, optimizes WebRTC playout delay, and disables heavy background tasks."""
    for old_host in UPSTREAM_API_HOSTS:
        if f'"{old_host}"' in js_text:
            js_text = js_text.replace(f'"{old_host}"', '(window.location.origin+"/cloudmoon/api")')
        if f"`{old_host}`" in js_text:
            js_text = js_text.replace(f"`{old_host}`", '(window.location.origin+"/cloudmoon/api")')
        if old_host in js_text:
            js_text = js_text.replace(old_host, '(window.location.origin+"/cloudmoon/api")')

    # --- Universal AdBlock Bait, Free-Time Limit & HD Quality Unlock ---
    # 1. Neutralize xe() in DU-wofq6.js so $userADBlock (ae) is ALWAYS false
    js_text = js_text.replace(
        "function xe(){return new Promise",
        "function xe(){return Promise.resolve(!1);return new Promise",
    )
    # 2. Force ad_unblock: true in Pe() (/phone/connect request payload in DU-wofq6.js) while keeping screen_res:"720x1280"
    js_text = js_text.replace("ad_unblock:!a", "ad_unblock:!0")
    # 3. Ensure default $phoneData store (H) in DU-wofq6.js starts with unlimited time_left_sec & isUnlimitLocale: true
    js_text = js_text.replace(
        "level:null,is_paid:!1,time_left_set:null,use_time_type:0,time_left_sec:0,is_subscription:!1,subscription_channel:null,subscription_product_id:null,backup_time:null,locale:null,isUnlimitLocale:!1,useProxy:!1",
        "level:3,is_paid:!0,time_left_set:{free_time_sec:99999999,permanent_time_sec:99999999},use_time_type:1,time_left_sec:99999999,is_subscription:!0,subscription_channel:'vip',subscription_product_id:'vip_unlimited',backup_time:99999999,locale:null,isUnlimitLocale:!0,useProxy:!1",
    )
    # 3b. Ensure default $userData store (q) in DU-wofq6.js always includes HD/SD/LD/ULD with min_vip_level: 0
    js_text = js_text.replace(
        "video_level:[]",
        'video_level:[{level:"HD",min_vip_level:0},{level:"SD",min_vip_level:0},{level:"LD",min_vip_level:0},{level:"ULD",min_vip_level:0}]',
    )
    # 3c. Unlock HD selection in navbar quality dropdown (BCl_jh-1.js) & prevent auto-downgrade from HD to SD
    js_text = js_text.replace("if(s.min_vip_level>v().level)", "if(!1)")
    js_text = js_text.replace('v().level===1&&e(I)==="HD"', "!1")
    js_text = js_text.replace(
        "((pe=e(W))==null?void 0:pe.min_vip_level)&&((ze=e(W))==null?void 0:ze.min_vip_level)>1",
        "!1",
    )
    # 4. Eliminate "Today's free game time has been used up" (alert.noTimeContent) modal checks in CtKM5s2L.js
    js_text = js_text.replace(
        'if((!h().isUnlimitLocale||h().useProxy)&&h().time_left_sec<=0){Ce(f()("alert.notice"),f()("alert.noTimeContent"),f()("alert.noTimeBtn"));return}',
        "if(!1){}",
    )
    js_text = js_text.replace(
        'u.err_code===30002?Ce(f()("alert.notice"),f()("alert.blockFree"),f()("alert.buy")):u.err_code===30003?Ce(f()("alert.notice"),f()("alert.noTimeContent"),f()("alert.noTimeBtn")):',
        "u.err_code===30002?void 0:u.err_code===30003?void 0:",
    )
    # 5. Neutralize Ja() ad-block detector and prevent sending adBlock>true over WebRTC DataChannel in 2.C7CpNCnt.js
    js_text = js_text.replace("async function Ja(){", "async function Ja(){return!1;")
    js_text = js_text.replace("Z?.send(`adBlock>true`)", "void 0")
    js_text = js_text.replace('Z?.send("adBlock>true")', "void 0")
    js_text = js_text.replace("Z?.send(`adBlock>${e}`)", "Z?.send(`adBlock>false`)")
    js_text = js_text.replace("Z?.send(`adBlock>${n(b).isBlocked}`)", "Z?.send(`adBlock>false`)")

    if is_runsite:
        # Ensure run-site image assets always resolve to /run-site/images/ even if path lacks trailing slash
        js_text = js_text.replace('src="./images/logo.png"', 'src="/run-site/images/logo.png"')
        js_text = js_text.replace("src=`/images/logo.png`", "src=`/run-site/images/logo.png`")
        js_text = js_text.replace('src="images/send.png"', 'src="/run-site/images/send.png"')

        # Prevent pressing Enter from auto-sending text in CloudMoon (#input-div_input); only send when clicking the Send ("送る") button
        js_text = js_text.replace(
            'd(`input`,a,function(...e){v()?.apply(this,e)})',
            'd(`input`,a,function(){})',
        )
        js_text = js_text.replace(
            'd("input",a,function(...e){v()?.apply(this,e)})',
            'd("input",a,function(){})',
        )
        js_text = js_text.replace(
            'd(`keydown`,a,e=>{e.key===`Enter`&&v()(),e.key===`Escape`&&y()()})',
            'd(`keydown`,a,e=>{if(e.key===`Enter`){e.preventDefault();e.stopPropagation();return}e.key===`Escape`&&y()()})',
        )
        js_text = js_text.replace(
            'd("keydown",a,e=>{e.key==="Enter"&&v()(),e.key==="Escape"&&y()()})',
            'd("keydown",a,e=>{if(e.key==="Enter"){e.preventDefault();e.stopPropagation();return}e.key==="Escape"&&y()()})',
        )
        js_text = js_text.replace(
            'sendText(){if(this.inputTextShow){if(this.prevInputTextStr!==``&&this.inputTextStr===this.prevInputTextStr){Z.send(br(this.inputEnterType)),this.prevInputTextStr=``,this.inputTextStr=``;return}Z.send(yr(this.inputTextCnt++,this.inputTextStr)),this.prevInputTextStr=this.inputTextStr}}',
            'sendText(){if(this.inputTextShow){let _txt=this.inputTextStr||``;if(!_txt)return;Z.send(yr(this.inputTextCnt++,_txt));let _et=this.inputEnterType;this.prevInputTextStr=``;this.inputTextStr=``;setTimeout(()=>{try{Z.send(br(_et))}catch(_){}},35)}}',
        )
        js_text = js_text.replace(
            'if(this.isTypingMode){let t=mr[e.code];t!==void 0&&(e.preventDefault(),Z.send(Sr(0,t,0,0)));return}',
            'if(this.isTypingMode){if(this.inputTextShow||e.key===`Enter`||e.code===`Enter`||e.code===`NumpadEnter`)return;let t=mr[e.code];t!==void 0&&(e.preventDefault(),Z.send(Sr(0,t,0,0)));return}',
        )
        js_text = js_text.replace(
            'if(this.isTypingMode){let t=mr[e.code];t!==void 0&&(e.preventDefault(),Z.send(Sr(1,t,0,0)));return}',
            'if(this.isTypingMode){if(this.inputTextShow||e.key===`Enter`||e.code===`Enter`||e.code===`NumpadEnter`)return;let t=mr[e.code];t!==void 0&&(e.preventDefault(),Z.send(Sr(1,t,0,0)));return}',
        )

        # Initialize in-game quality state from localStorage ('HD' by default) instead of hardcoded 'SD'
        js_text = js_text.replace(
            "#F=e(`SD`);get quality(){return n(this.#F)}set quality(e){g(this.#F,e,!0)}#I=e(`SD`);",
            "#F=e(localStorage.getItem(`quality`)||`HD`);get quality(){return n(this.#F)}set quality(e){try{localStorage.setItem(`quality`,e)}catch{}g(this.#F,e,!0)}#I=e(localStorage.getItem(`quality`)||`HD`);",
        )
        # Neutralize all server kickout signals in 2.C7CpNCnt.js (time_run_out, connection_too_long, manually_disconnect, etc.)
        js_text = js_text.replace(
            "else if(n===`kickout`){",
            'else if(n===`kickout`){console.warn("[CloudMoon Unlimited] Blocked server kickout:",r);return;',
        )

        # Neutralize client-side 4-minute AFK inactivity dialog & disconnect trigger in 2.C7CpNCnt.js
        js_text = js_text.replace(
            "te=setInterval(()=>{let e=Date.now()-n(k);L(Oe)===`READY`&&e>=24e4",
            "te=setInterval(()=>{let e=Date.now()-n(k);if(!1)L(Oe)===`READY`&&e>=24e4",
        )

        # Track activity timestamp on ANY sent message (keyboard, mouse, touch, keepalive)
        js_text = js_text.replace(
            "typeof t.detail==`string`&&t.detail.includes(`touch`)",
            "typeof t.detail==`string`",
        )

        # Expose active user session for background playtime refill
        js_text = js_text.replace(
            "if(!a)throw Error(Y(`session.invalid`));Ye.set(a)",
            "if(!a)throw Error(Y(`session.invalid`));(window.__cm_session=a,Ye.set(a))",
        )

        # Increase disconnect grace period from 5s to 60s (prevents brief school Wi-Fi jitter from triggering disconnect)
        js_text = js_text.replace("}}},5e3)},x=e=>{cn(`msg`", "}}},6e4)},x=e=>{cn(`msg`")
        js_text = js_text.replace(
            "}}},5e3))},w=()=>{};return{subscribe:t,init:async()=>{i||(",
            "}}},6e4))},w=()=>{};return{subscribe:t,init:async()=>{i||(",
        )

        # Keep sidebar explicit exit to /cloudmoon/ and flag intentional exit
        js_text = js_text.replace(
            "}).catch(e=>{console.error(`Disconnect failed:`,e)}),window.location.href=`/`}",
            "}).catch(e=>{console.error(`Disconnect failed:`,e)}),window.__cm_intentional_exit=!0,window.location.href=`/cloudmoon/`}",
        )

        # Suppress accidental kickout/error redirect navigations so users are NOT booted to home
        js_text = js_text.replace(
            "X.clear(),window.name=``,Z?.cleanup(),window.location.href=`/`",
            'X.clear();console.warn("[CloudMoon] Reconnecting game session...");try{window.location.reload()}catch(_){}',
        )
        js_text = js_text.replace(
            "X.clear(),window.name=``,window.location.href=`/`",
            'X.clear();console.warn("[CloudMoon] Reconnecting game session...");try{window.location.reload()}catch(_){}',
        )

        # Disable background dual-canvas 60fps drawImage + dual 2.5Mbps MediaRecorder highlight recording (eliminates main-thread lag)
        js_text = js_text.replace("function et(){if(!re)return;", "function et(){return;")
        js_text = js_text.replace(
            'localStorage.setItem(`enableHighLight`,`true`),et()',
            'localStorage.setItem(`enableHighLight`,`false`)',
        )
        js_text = js_text.replace(
            'localStorage.setItem("enableHighLight","true"),et()',
            'localStorage.setItem("enableHighLight","false")',
        )

        # Ultra-low-latency WebRTC playout delay (0..25ms with fast recovery),
        # ICE candidate pre-gathering (iceCandidatePoolSize:4), and 1s low-latency ping HUD.
        js_text = js_text.replace(
            "prevDelayMs=15;prevPacketsReceived=0;prevPacketsLost=0;prevRetransmittedPacketsReceived=0;lastAppliedDelayMs=15;goodWindows=0;playoutDelayConfig={minDelayMs:15,maxDelayMs:100,degradationTau:.5,recoveryTau:4.5",
            "prevDelayMs=0;prevPacketsReceived=0;prevPacketsLost=0;prevRetransmittedPacketsReceived=0;lastAppliedDelayMs=0;goodWindows=0;playoutDelayConfig={minDelayMs:0,maxDelayMs:25,degradationTau:1.0,recoveryTau:0.8",
        )
        js_text = js_text.replace(
            "let n=Math.max(15,Math.min(100,e));",
            "let n=Math.max(0,Math.min(25,e));",
        )
        js_text = js_text.replace(
            'let a={iceServers:t,bundlePolicy:"max-bundle",rtcpMuxPolicy:"require",iceTransportPolicy:"all"}',
            'let a={iceServers:t,bundlePolicy:"max-bundle",rtcpMuxPolicy:"require",iceTransportPolicy:"all",iceCandidatePoolSize:4}',
        )
        js_text = js_text.replace(
            'let a={iceServers:t,bundlePolicy:`max-bundle`,rtcpMuxPolicy:`require`,iceTransportPolicy:`all`}',
            'let a={iceServers:t,bundlePolicy:`max-bundle`,rtcpMuxPolicy:`require`,iceTransportPolicy:`all`,iceCandidatePoolSize:4}',
        )
        js_text = js_text.replace(
            "this.statsInterval=setInterval(()=>this.updateStats(),3e3)",
            "this.statsInterval=setInterval(()=>this.updateStats(),1e3)",
        )
        js_text = js_text.replace(
            "et.set(e.currentRoundTripTime*1e3)",
            "et.set(Math.max(5,Math.round((e.currentRoundTripTime||.012)*450)))",
        )

        # Prevent deferred first frame check from disconnecting on iPad screen rotation / tab switching
        js_text = js_text.replace(
            ":(console.warn(`[StreamView] deferred first frame timeout`),de())",
            ":(console.warn(`[StreamView] deferred first frame timeout (suppressed)`))",
        )

        # Eliminate HTMLAudioElement.play() flood on touch in 2.C7CpNCnt.js and drop congested touch move packets
        # Fixes iPad AVFoundation media thread deadlock & WebContent tab crashes on 120Hz ProMotion touchscreens
        js_text = js_text.replace(
            "e.includes(`touch`)&&(this.videoEl?.paused&&this.videoEl.play(),this.audioEl?.paused&&this.audioEl.play());try{this.dataChannel.send(e)}",
            "if(e.includes(`touch`)){let _now=Date.now();if(!this._lastMediaPlayAttempt||_now-this._lastMediaPlayAttempt>3000){this._lastMediaPlayAttempt=_now;try{this.videoEl?.paused&&this.videoEl.play?.().catch(()=>{})}catch(_){}try{this.audioEl?.paused&&this.audioEl.play?.().catch(()=>{})}catch(_){}}}if(this.dataChannel.bufferedAmount>65536&&typeof e===`string`&&e.startsWith(`touch>Touch move`))return;try{this.dataChannel.send(e)}",
        )

        # Neutralize 10-second rotation self-destruct timer on iPad / orientation change
        js_text = js_text.replace(
            "a()!==`READY`&&setTimeout(()=>{if(L(Oe)!==`READY`){",
            "!1&&a()!==`READY`&&setTimeout(()=>{if(L(Oe)!==`READY`){",
        )

        # Neutralize first-frame timeout de() in 2.C7CpNCnt.js so rotation/tab switches do not kill WebRTC session
        js_text = js_text.replace(
            "function de(){B=!1,M(`video_first_frame_timeout`),",
            "function de(){console.warn(`[StreamView] first frame timeout suppressed`);return;",
        )

        # Add webkit-playsinline and x5-playsinline to audio and video elements to keep iPad Safari in inline mode
        js_text = js_text.replace(
            'id="remote-audio" style="display: none" controls="" autoplay="" playsinline=""></audio> <video id="remote-video" autoplay="" playsinline="" controlslist="nodownload"></video>',
            'id="remote-audio" style="display: none" controls="" autoplay="" playsinline="" webkit-playsinline=""></audio> <video id="remote-video" autoplay="" playsinline="" webkit-playsinline="" x5-playsinline="" controlslist="nodownload"></video>',
        )

        # Add automatic School Hub WebSocket proxy fallback candidates when direct coordinator WebSockets are blocked
        old_nn = "function nn(e){return[...new Set([e.coordinatorHost,e.coordinatorTunnel,e.coordinatorCloudfront])].filter(Boolean)}"
        new_nn = (
            "function nn(e){"
            "let raw=[...new Set([e.coordinatorHost,e.coordinatorTunnel,e.coordinatorCloudfront])].filter(Boolean);"
            "let proxied=raw.map(u=>{try{let p=new URL(u);return `${window.location.origin}?__sh_ws_host=${encodeURIComponent(p.host)}`;}catch{return null}}).filter(Boolean);"
            "return[...raw,...proxied]}"
        )
        if old_nn in js_text:
            js_text = js_text.replace(old_nn, new_nn)
    else:
        # Preserve Japanese UI locale when SvelteKit matches /cloudmoon/... as params.lang="cloudmoon"
        js_text = js_text.replace('i.includes(n)||(n="en")', 'i.includes(n)||(n="ja")')
        js_text = js_text.replace(
            'return i.includes(n)?(await a(n),{lang:n,loadFinish:!0,showFaq:!0}):(await a("en"),{lang:"en",loadFinish:!0,showFaq:!0})',
            'return i.includes(n)?(await a(n),{lang:n,loadFinish:!0,showFaq:!0}):(await a("ja"),{lang:"ja",loadFinish:!0,showFaq:!0})',
        )
        js_text = js_text.replace(
            'let r=z.lang||"en";',
            'let r=(z.lang==="cloudmoon"?"ja":z.lang)||"ja";',
        )
        # Disable background 34-request category prefetch flood while browsing/launching games
        js_text = js_text.replace("async function je(){if(!J){", "async function je(){return;if(!J){")

    return js_text


@router.api_route("/cloudmoon", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def cloudmoon_redirect(request: Request):
    q = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(url=f"/cloudmoon/{q}", status_code=307)


@router.api_route("/cloudmoon/cm-engine.js", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def cloudmoon_engine_script(request: Request) -> Response:
    if request.method == "OPTIONS":
        return Response(status_code=204, headers={"Access-Control-Allow-Origin": "*"})
    return Response(
        content=b"" if request.method == "HEAD" else _CM_ENGINE_JS.encode("utf-8"),
        media_type="application/javascript",
        headers={
            "Cache-Control": "no-store",
            "Access-Control-Allow-Origin": "*",
        },
    )


@router.api_route("/cloudmoon/health", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def cloudmoon_health() -> JSONResponse:
    return JSONResponse(
        content={
            "ok": True,
            "service": SERVICE_NAME,
            "path": SERVICE_PATH,
            "target": DEFAULT_TARGET_URL,
            "unlimited": True,
            "ad_free": True,
        }
    )


@router.websocket("/cloudmoon/ws-proxy/{target_host}/{target_path:path}")
async def cloudmoon_ws_proxy(websocket: WebSocket, target_host: str, target_path: str):
    """WebSocket reverse proxy fallback for coordinator signaling servers."""
    await websocket.accept()
    query = websocket.url.query
    clean_path = target_path.lstrip("/")
    upstream_ws_url = f"wss://{target_host}/{clean_path}"
    if query:
        upstream_ws_url = f"{upstream_ws_url}?{query}"

    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    try:
        async with websockets.connect(
            upstream_ws_url,
            ssl=ssl_ctx,
            origin=TARGET_ORIGIN,
            user_agent_header=websocket.headers.get("user-agent") or "Mozilla/5.0",
            open_timeout=8,
            ping_interval=20,
            ping_timeout=20,
            compression=None,
            max_queue=64,
        ) as upstream_ws:
            try:
                import socket as _sock
                tr = getattr(upstream_ws, "transport", None)
                sock = tr.get_extra_info("socket") if tr is not None else None
                if sock is not None:
                    sock.setsockopt(_sock.IPPROTO_TCP, _sock.TCP_NODELAY, 1)
            except Exception:
                pass
            async def client_to_upstream():
                try:
                    while True:
                        msg = await websocket.receive()
                        if msg.get("type") == "websocket.disconnect":
                            break
                        if "text" in msg and msg["text"] is not None:
                            await upstream_ws.send(msg["text"])
                        elif "bytes" in msg and msg["bytes"] is not None:
                            await upstream_ws.send(msg["bytes"])
                except Exception:
                    pass

            async def upstream_to_client():
                try:
                    async for message in upstream_ws:
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(message)
                except Exception:
                    pass

            tasks = [
                asyncio.create_task(client_to_upstream()),
                asyncio.create_task(upstream_to_client()),
            ]
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
    except Exception as exc:
        print(f"[CloudMoon WS Proxy] Error connecting to {upstream_ws_url}: {exc}", flush=True)
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


async def _proxy_html_or_asset(target_url: str, request: Request) -> Response:
    client = _get_http_client()
    raw_ua = request.headers.get("user-agent") or ""
    ua = raw_ua if "Mozilla/5.0" in raw_ua else (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    req_headers = {
        "User-Agent": ua,
        "Accept": request.headers.get("accept") or "*/*",
        "Accept-Language": request.headers.get("accept-language") or "ja,en-US;q=0.9,en;q=0.8",
        "Referer": f"{TARGET_ORIGIN}/",
    }
    try:
        upstream = await client.get(target_url, headers=req_headers)
    except Exception as exc:
        return Response(
            content=f"Proxy error: {exc}".encode("utf-8"),
            status_code=502,
            media_type="text/plain; charset=utf-8",
        )

    ct = upstream.headers.get("content-type") or "application/octet-stream"
    content = upstream.content

    if "text/html" in ct:
        content = _transform_html(upstream.text).encode("utf-8")
    elif "javascript" in ct or target_url.split("?")[0].endswith(".js"):
        content = _transform_js(upstream.text, is_runsite=False).encode("utf-8")

    return Response(
        content=b"" if request.method == "HEAD" else content,
        status_code=upstream.status_code,
        media_type=ct.split(";")[0],
        headers={
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "no-store" if "text/html" in ct else "public, max-age=300",
        },
    )


@router.api_route("/cloudmoon/", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def cloudmoon_index(request: Request) -> Response:
    if request.method == "OPTIONS":
        return Response(status_code=204, headers={"Access-Control-Allow-Origin": "*"})
    q = f"?{request.url.query}" if request.url.query else ""
    target_url = f"{TARGET_ORIGIN}/ja/{q}"
    return await _proxy_html_or_asset(target_url, request)


async def _auto_claim_upstream_reward(
    client: httpx.AsyncClient,
    req_headers: Dict[str, str],
    game_pkg: str = "com.roblox.client",
    rounds: int = 2,
) -> None:
    """Automatically claims server-side free playtime rewards on api.prod.cloudmoonapp.com for the user's token."""
    if not req_headers.get("x-user-token"):
        return
    headers = dict(req_headers)
    headers["Content-Type"] = "application/json"
    for _ in range(max(1, rounds)):
        try:
            ad_resp = await client.post(
                f"{API_ORIGIN}/web/ad",
                headers=headers,
                content=json.dumps({"game": game_pkg or "com.roblox.client"}).encode("utf-8"),
                timeout=5.0,
            )
            if ad_resp.status_code == 200:
                ad_json = ad_resp.json()
                ad_data = ad_json.get("data") if isinstance(ad_json, dict) else None
                if isinstance(ad_data, dict):
                    reward_hash = ad_data.get("rewardHash") or ""
                    ad_source = ad_data.get("adSource") or "admob"
                    if reward_hash:
                        await client.post(
                            f"{API_ORIGIN}/ad/gpt_reward",
                            headers=headers,
                            content=json.dumps({"hash": reward_hash, "source": ad_source}).encode("utf-8"),
                            timeout=5.0,
                        )
                    else:
                        break
        except Exception as exc:
            print(f"[CloudMoon AutoReward] Warning: {exc}", flush=True)
            break


@router.api_route(
    "/cloudmoon/api/{path:path}",
    methods=["GET", "POST", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def cloudmoon_api_proxy(request: Request, path: str) -> Response:
    if request.method == "OPTIONS":
        return Response(
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, HEAD, OPTIONS",
                "Access-Control-Allow-Headers": "*",
            },
        )

    clean_path = path.lstrip("/")

    # 1. Fast local response for /_ping
    if clean_path == "_ping":
        return JSONResponse(
            content={"code": 0, "message": "pong"},
            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"},
        )

    body = await request.body() if request.method == "POST" else None

    client = _get_http_client()
    req_headers = {
        "User-Agent": request.headers.get("user-agent") or "Mozilla/5.0",
        "Accept": request.headers.get("accept") or "application/json",
        "Content-Type": request.headers.get("content-type") or "application/json",
        "Origin": TARGET_ORIGIN,
        "Referer": f"{TARGET_ORIGIN}/",
    }
    if "x-user-token" in request.headers:
        req_headers["x-user-token"] = request.headers["x-user-token"]
    if "x-user-language" in request.headers:
        req_headers["x-user-language"] = request.headers["x-user-language"]
    if "x-user-locale" in request.headers:
        req_headers["x-user-locale"] = request.headers["x-user-locale"]

    # 2. On /web/ad: Claim real upstream free-time reward in background while returning showAD:False, unlimit:False
    #    (unlimit MUST be False so 2.C7CpNCnt.js never starts the 5s adBlock>true DataChannel interval!)
    if clean_path == "web/ad":
        game_pkg = "com.roblox.client"
        if body:
            try:
                parsed = json.loads(body.decode("utf-8", errors="replace"))
                if isinstance(parsed, dict) and parsed.get("game"):
                    game_pkg = str(parsed["game"])
            except Exception:
                pass
        if req_headers.get("x-user-token"):
            asyncio.create_task(_auto_claim_upstream_reward(client, req_headers, game_pkg, rounds=2))
        return JSONResponse(
            content={
                "code": 0,
                "message": "Success",
                "data": {
                    "showAD": False,
                    "unlimit": False,
                    "timeSecLeft": 99999999,
                    "rewardAdPass": True,
                    "rewardHash": "",
                    "initOrientation": 3,
                    "adSource": "",
                },
            },
            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"},
        )

    # 3. On /ad/gpt_reward: Claim upstream reward & return 60 minutes credited
    if clean_path == "ad/gpt_reward":
        if req_headers.get("x-user-token"):
            asyncio.create_task(_auto_claim_upstream_reward(client, req_headers, "com.roblox.client", rounds=2))
        return JSONResponse(
            content={"code": 0, "message": "Success", "data": {"reward_min": 60}},
            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"},
        )

    # 4. Log client connection diagnostics on /report/connect so any WebRTC/Socket issue is visible in logs
    if clean_path == "report/connect":
        if body:
            try:
                print(f"[CloudMoon Connect Report] {body.decode('utf-8', errors='replace')}", flush=True)
            except Exception:
                pass
        return JSONResponse(
            content={"code": 0, "message": "Success", "data": {}},
            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"},
        )

    q = f"?{request.url.query}" if request.url.query else ""
    target_url = f"{API_ORIGIN}/{clean_path}{q}"

    # Short TTL cache for public read-only game catalog endpoints
    is_cacheable_api = request.method == "GET" and (
        clean_path.startswith(("web/MustPlay", "web/page_info", "web/tags", "web/topics", "web/tag_games", "web/topic_games"))
    )
    api_cache_key = f"{clean_path}{q}"
    if is_cacheable_api and api_cache_key in _API_CACHE:
        cached_ts, cached_bytes, cached_status = _API_CACHE[api_cache_key]
        if time.time() - cached_ts < 180.0:
            return Response(
                content=cached_bytes,
                status_code=cached_status,
                media_type="application/json",
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET, POST, HEAD, OPTIONS",
                    "Access-Control-Allow-Headers": "*",
                    "Cache-Control": "public, max-age=120",
                },
            )

    # 5. On /phone/connect: Force ad_unblock=True, screen_res="720x1280" (to match run-site 720x1280 touch coordinate grid), and pre-claim upstream reward time
    connect_game_pkg = "com.roblox.client"
    if clean_path == "phone/connect" and request.method == "POST":
        if body:
            try:
                conn_payload = json.loads(body.decode("utf-8", errors="replace"))
                if isinstance(conn_payload, dict):
                    conn_payload["ad_unblock"] = True
                    conn_payload["screen_res"] = "720x1280"
                    if conn_payload.get("game"):
                        connect_game_pkg = str(conn_payload["game"])
                    body = json.dumps(conn_payload).encode("utf-8")
            except Exception:
                pass
        if req_headers.get("x-user-token"):
            asyncio.create_task(_auto_claim_upstream_reward(client, req_headers, connect_game_pkg, rounds=2))

    try:
        upstream = await client.request(
            method=request.method,
            url=target_url,
            headers=req_headers,
            content=body,
        )
    except Exception as exc:
        return Response(
            content=f'{{"code":-1,"message":"Proxy error: {exc}"}}'.encode("utf-8"),
            status_code=502,
            media_type="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )

    status_code = upstream.status_code
    content = upstream.content

    # If /phone/connect returned an out-of-time error code (30002, 30003, 30004, 10005), claim more reward time and retry once!
    if clean_path == "phone/connect" and status_code == 200:
        try:
            conn_resp = upstream.json()
            if isinstance(conn_resp, dict) and conn_resp.get("code") in (30002, 30003, 30004, 10005):
                print(f"[CloudMoon Connect] Got code {conn_resp.get('code')}, auto-claiming reward & retrying...", flush=True)
                await _auto_claim_upstream_reward(client, req_headers, connect_game_pkg, rounds=3)
                retry_up = await client.request(
                    method=request.method,
                    url=target_url,
                    headers=req_headers,
                    content=body,
                )
                status_code = retry_up.status_code
                content = retry_up.content
        except Exception:
            pass

    if is_cacheable_api and status_code == 200 and len(content) < 2_000_000:
        _API_CACHE[api_cache_key] = (time.time(), content, status_code)

    # Guaranteed Unlimited Playtime on /phone/list with proper time_left_set object structure
    if clean_path == "phone/list":
        try:
            raw_data = upstream.json()
            if raw_data.get("code") == 0 and isinstance(raw_data.get("data"), dict):
                lst = raw_data["data"].get("list")
                if isinstance(lst, list):
                    for itm in lst:
                        itm["expired_at"] = max(itm.get("expired_at") or 0, 2147483647)
                        itm["level"] = 3
                        itm["is_paid"] = True
                        itm["time_left_set"] = {
                            "free_time_sec": 99999999,
                            "permanent_time_sec": 99999999,
                        }
                        itm["time_left_sec"] = 99999999
                        itm["use_time_type"] = 1
                        itm["is_subscription"] = True
                        itm["subscription_channel"] = "vip"
                        itm["subscription_product_id"] = "vip_unlimited"
                        itm["backup_time"] = 99999999
                        itm["is_unlimit_locale"] = True
                        itm["use_proxy"] = False
                content = json.dumps(raw_data).encode("utf-8")
                status_code = 200
        except Exception:
            pass

    # Guaranteed HD quality unlock on /user/info while preserving all upstream fields
    if clean_path == "user/info" and status_code == 200:
        try:
            user_data = upstream.json()
            if user_data.get("code") == 0 and isinstance(user_data.get("data"), dict):
                user_data["data"]["video_level"] = [
                    {"level": "HD", "min_vip_level": 0},
                    {"level": "SD", "min_vip_level": 0},
                    {"level": "LD", "min_vip_level": 0},
                    {"level": "ULD", "min_vip_level": 0},
                ]
                user_data["data"]["is_set_pwd"] = True
                user_data["data"]["is_test"] = False
                user_data["data"]["is_new"] = False
                content = json.dumps(user_data).encode("utf-8")
        except Exception:
            pass

    # Ensure in-game quality switching (/phone/action with key="quality") always succeeds even for free accounts
    if clean_path == "phone/action" and body:
        try:
            req_action = json.loads(body.decode("utf-8", errors="ignore"))
            if isinstance(req_action, dict) and req_action.get("key") == "quality":
                content = json.dumps({"code": 0, "message": "Success", "data": {}}).encode("utf-8")
                status_code = 200
        except Exception:
            pass

    return Response(
        content=b"" if request.method == "HEAD" else content,
        status_code=status_code,
        media_type="application/json" if "json" in upstream.headers.get("content-type", "") else upstream.headers.get("content-type", "application/json"),
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, HEAD, OPTIONS",
            "Access-Control-Allow-Headers": "*",
            "Cache-Control": "no-store",
        },
    )


@router.api_route(
    "/cloudmoon/app/{path:path}",
    methods=["GET", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def cloudmoon_app_proxy(request: Request, path: str) -> Response:
    if request.method == "OPTIONS":
        return Response(status_code=204, headers={"Access-Control-Allow-Origin": "*"})
    clean_path = path.lstrip("/")
    q = f"?{request.url.query}" if request.url.query else ""
    target_url = f"{TARGET_ORIGIN}/{clean_path}{q}"
    return await _proxy_html_or_asset(target_url, request)


@router.api_route(
    "/cloudmoon/{path:path}",
    methods=["GET", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def cloudmoon_subpath_proxy(request: Request, path: str) -> Response:
    if request.method == "OPTIONS":
        return Response(status_code=204, headers={"Access-Control-Allow-Origin": "*"})
    clean_path = path.lstrip("/")
    if clean_path in ("run-site", "run-site/"):
        pkg = request.query_params.get("pkg", "").strip()
        sid = request.query_params.get("sid", "").strip()
        if pkg:
            return RedirectResponse(url=f"/cloudmoon/ja/game/{pkg}", status_code=302)
        if sid:
            q = f"?{request.url.query}" if request.url.query else ""
            return RedirectResponse(url=f"/run-site/{q}", status_code=302)
        return RedirectResponse(url="/cloudmoon/", status_code=302)
    if clean_path in ("", "ja", "ja/"):
        target_url = f"{TARGET_ORIGIN}/ja/"
    else:
        target_url = f"{TARGET_ORIGIN}/{clean_path}"
    q = f"?{request.url.query}" if request.url.query else ""
    return await _proxy_html_or_asset(f"{target_url}{q}", request)


@router.api_route(
    "/game/{path:path}",
    methods=["GET", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def cloudmoon_game_proxy(request: Request, path: str) -> Response:
    if request.method == "OPTIONS":
        return Response(status_code=204, headers={"Access-Control-Allow-Origin": "*"})
    clean_path = path.lstrip("/")
    q = f"?{request.url.query}" if request.url.query else ""
    target_url = f"{TARGET_ORIGIN}/game/{clean_path}{q}"
    return await _proxy_html_or_asset(target_url, request)


@router.api_route(
    "/list/{path:path}",
    methods=["GET", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def cloudmoon_list_proxy(request: Request, path: str) -> Response:
    if request.method == "OPTIONS":
        return Response(status_code=204, headers={"Access-Control-Allow-Origin": "*"})
    clean_path = path.lstrip("/")
    q = f"?{request.url.query}" if request.url.query else ""
    target_url = f"{TARGET_ORIGIN}/list/{clean_path}{q}"
    return await _proxy_html_or_asset(target_url, request)


@router.api_route(
    "/h5/{path:path}",
    methods=["GET", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def cloudmoon_h5_proxy(request: Request, path: str) -> Response:
    if request.method == "OPTIONS":
        return Response(status_code=204, headers={"Access-Control-Allow-Origin": "*"})
    clean_path = path.lstrip("/")
    q = f"?{request.url.query}" if request.url.query else ""
    target_url = f"{TARGET_ORIGIN}/h5/{clean_path}{q}"
    return await _proxy_html_or_asset(target_url, request)


@router.api_route(
    "/data/{path:path}",
    methods=["GET", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def cloudmoon_data_proxy(request: Request, path: str) -> Response:
    if request.method == "OPTIONS":
        return Response(status_code=204, headers={"Access-Control-Allow-Origin": "*"})
    clean_path = path.lstrip("/")
    if clean_path.startswith("games/cloudmoon/"):
        clean_path = "games/ja/" + clean_path[len("games/cloudmoon/"):]
    elif clean_path.startswith("recommend/cloudmoon/"):
        clean_path = "recommend/ja/" + clean_path[len("recommend/cloudmoon/"):]
    q = f"?{request.url.query}" if request.url.query else ""
    target_url = f"{TARGET_ORIGIN}/data/{clean_path}{q}"
    return await _proxy_html_or_asset(target_url, request)


@router.api_route(
    "/main.css",
    methods=["GET", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def cloudmoon_runsite_main_css(request: Request) -> Response:
    return await cloudmoon_runsite_proxy(request, "main.css")


@router.api_route(
    "/run-site",
    methods=["GET", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def cloudmoon_runsite_no_slash(request: Request) -> Response:
    q = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(url=f"/run-site/{q}", status_code=307)


@router.api_route(
    "/run-site/",
    methods=["GET", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def cloudmoon_runsite_root(request: Request) -> Response:
    return await cloudmoon_runsite_proxy(request, "index.html")


@router.api_route(
    "/run-site/{path:path}",
    methods=["GET", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def cloudmoon_runsite_proxy(request: Request, path: str) -> Response:
    if request.method == "OPTIONS":
        return Response(status_code=204, headers={"Access-Control-Allow-Origin": "*"})
    clean_path = path.lstrip("/") or "index.html"

    # Neutralize ad / Prebid / anti-adblock scripts under /run-site/
    if (
        clean_path.startswith("ads/")
        or "prebid" in clean_path.lower()
        or "ramp-helper" in clean_path.lower()
        or "cm-compat" in clean_path.lower()
    ):
        return Response(
            content=b"/* ad blocked */",
            status_code=200,
            media_type="application/javascript",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "public, max-age=86400",
            },
        )

    is_immutable = clean_path.startswith("_app/immutable/")
    cache_key = f"run-site/{clean_path}"
    if request.method == "GET" and is_immutable and cache_key in _CACHE:
        cached_content = _CACHE[cache_key]
        ct = mimetypes.guess_type(clean_path)[0] or "application/javascript"
        return Response(
            content=cached_content,
            media_type=ct,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "public, max-age=300",
            },
        )

    q = f"?{request.url.query}" if request.url.query else ""
    target_url = f"{TARGET_ORIGIN}/run-site/{clean_path}{q}"

    client = _get_http_client()
    raw_ua = request.headers.get("user-agent") or ""
    ua = raw_ua if "Mozilla/5.0" in raw_ua else (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    req_headers = {
        "User-Agent": ua,
        "Accept": request.headers.get("accept") or "*/*",
        "Referer": f"{TARGET_ORIGIN}/run-site/",
    }
    try:
        upstream = await client.get(target_url, headers=req_headers)
    except Exception as exc:
        return Response(
            content=f"Proxy error: {exc}".encode("utf-8"),
            status_code=502,
            media_type="text/plain; charset=utf-8",
        )

    ct = upstream.headers.get("content-type") or "application/octet-stream"
    content = upstream.content

    if "text/html" in ct or clean_path.endswith(".html"):
        content = _transform_html(upstream.text).encode("utf-8")
    elif "javascript" in ct or clean_path.endswith(".js"):
        content = _transform_js(upstream.text, is_runsite=True).encode("utf-8")

    if is_immutable and upstream.status_code == 200 and len(content) < 5_000_000:
        _CACHE[cache_key] = content

    return Response(
        content=b"" if request.method == "HEAD" else content,
        status_code=upstream.status_code,
        media_type=ct.split(";")[0],
        headers={
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": (
                "public, max-age=300"
                if is_immutable
                else ("no-store" if ("text/html" in ct or clean_path.endswith(".js")) else "public, max-age=86400")
            ),
        },
    )


@router.api_route(
    "/images/{path:path}",
    methods=["GET", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def cloudmoon_images_proxy(request: Request, path: str) -> Response:
    if request.method == "OPTIONS":
        return Response(status_code=204, headers={"Access-Control-Allow-Origin": "*"})
    clean_path = path.lstrip("/")
    q = f"?{request.url.query}" if request.url.query else ""
    target_url = f"{TARGET_ORIGIN}/images/{clean_path}{q}"
    resp = await _proxy_html_or_asset(target_url, request)
    if resp.status_code == 404:
        return await _proxy_html_or_asset(f"{TARGET_ORIGIN}/run-site/images/{clean_path}{q}", request)
    return resp


@router.api_route(
    "/favicon/{path:path}",
    methods=["GET", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def cloudmoon_favicon_proxy(request: Request, path: str) -> Response:
    if request.method == "OPTIONS":
        return Response(status_code=204, headers={"Access-Control-Allow-Origin": "*"})
    clean_path = path.lstrip("/")
    q = f"?{request.url.query}" if request.url.query else ""
    target_url = f"{TARGET_ORIGIN}/favicon/{clean_path}{q}"
    return await _proxy_html_or_asset(target_url, request)


@router.api_route(
    "/favicon.ico",
    methods=["GET", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def cloudmoon_favicon_ico(request: Request) -> Response:
    if request.method == "OPTIONS":
        return Response(status_code=204, headers={"Access-Control-Allow-Origin": "*"})
    ico_path = os.path.join(os.path.dirname(__file__), "favicon.ico")
    if os.path.exists(ico_path):
        with open(ico_path, "rb") as f:
            return Response(content=f.read(), status_code=200, media_type="image/x-icon", headers={"Cache-Control": "public, max-age=86400", "Access-Control-Allow-Origin": "*"})
    target_url = f"{TARGET_ORIGIN}/favicon.ico"
    return await _proxy_html_or_asset(target_url, request)


@router.api_route(
    "/js/{path:path}",
    methods=["GET", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def cloudmoon_js_proxy(request: Request, path: str) -> Response:
    if request.method == "OPTIONS":
        return Response(status_code=204, headers={"Access-Control-Allow-Origin": "*"})
    clean_path = path.lstrip("/")
    # Block Ad-Shield anti-adblock (cm-compat.js), ramp-helper.js, and all.js
    if clean_path in ("all.js", "cm-compat.js", "ramp-helper.js") or "ad" in clean_path.lower():
        return Response(
            content=b"/* ad-shield blocked */",
            status_code=200,
            media_type="application/javascript",
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "public, max-age=86400",
            },
        )
    q = f"?{request.url.query}" if request.url.query else ""
    target_url = f"{TARGET_ORIGIN}/js/{clean_path}{q}"
    return await _proxy_html_or_asset(target_url, request)


@router.api_route(
    "/_app/{path:path}",
    methods=["GET", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def cloudmoon_assets_proxy(request: Request, path: str) -> Response:
    if request.method == "OPTIONS":
        return Response(status_code=204, headers={"Access-Control-Allow-Origin": "*"})

    cache_key = f"_app/{path}"
    if request.method == "GET" and cache_key in _CACHE:
        content = _CACHE[cache_key]
        ct = mimetypes.guess_type(path)[0] or "application/javascript"
        return Response(
            content=content,
            media_type=ct,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "public, max-age=300",
            },
        )

    clean_path = path.lstrip("/")
    q = f"?{request.url.query}" if request.url.query else ""
    target_url = f"{TARGET_ORIGIN}/_app/{clean_path}{q}"

    client = _get_http_client()
    try:
        upstream = await client.get(target_url)
    except Exception as exc:
        return Response(
            content=f"Asset error: {exc}".encode("utf-8"),
            status_code=502,
            media_type="text/plain; charset=utf-8",
        )

    content = upstream.content
    ct = upstream.headers.get("content-type") or mimetypes.guess_type(path)[0] or "application/octet-stream"

    # Rewrite upstream API URLs in JS chunks
    if "javascript" in ct or path.endswith(".js"):
        content = _transform_js(upstream.text, is_runsite=False).encode("utf-8")

    if upstream.status_code == 200 and len(content) < 5_000_000:
        _CACHE[cache_key] = content

    return Response(
        content=b"" if request.method == "HEAD" else content,
        status_code=upstream.status_code,
        media_type=ct.split(";")[0],
        headers={
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "public, max-age=300",
        },
    )


@router.api_route(
    "/cloudmoon/proxy/{url_path:path}",
    methods=["GET", "POST", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def cloudmoon_proxy_relay(request: Request, url_path: str) -> Response:
    if request.method == "OPTIONS":
        return Response(status_code=204, headers={"Access-Control-Allow-Origin": "*"})
    target = urllib.parse.unquote(url_path)
    if not target.startswith(("http://", "https://")):
        target = f"{TARGET_ORIGIN}/{target.lstrip('/')}"
    if request.url.query:
        sep = "&" if "?" in target else "?"
        target = f"{target}{sep}{request.url.query}"
    return await _proxy_html_or_asset(target, request)
