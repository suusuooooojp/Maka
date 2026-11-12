"""Official YouTube launcher for School Hub using the existing Scramjet 2.x runtime.

This file is intentionally standalone and is meant to be placed beside the
existing ``app.py``.  It exposes ``router`` plus the three metadata constants
used by School Hub's proxy auto-loader.

The parent project already provides these shared endpoints/assets:
  /scramjet/{asset_name}
  /scramjet/{transport}/{asset_path}
  /youtube/sw.js
  /wisp-youtube/
  /youtube/google-login

Stability goals:
* exact Scramjet 2.x package generation used by the project;
* high-speed HTTP/2 + Chrome-131 TLS transport relay (/youtube/runtime/http-relay)
  so YouTube pages & search load in ~1.5s without triggering Google bot 302s or
  WebKit module-import aborts during Service Worker lifecycle events;
* WebKit/iPadOS Service Worker POST body ArrayBuffer serialization in
  controller.sw.js so Google Login (accounts.google.com) and YouTube Innertube
  POST endpoints (/youtubei/v1/next, /browse, /get_watch, /player, /search)
  never drop request bodies on iOS/iPadOS Safari & Chrome;
* native YouTube #movie_player & #shorts-player playback by patching
  playabilityStatus/streamingData in ytInitialPlayerResponse & Innertube
  playerResponse when datacenter IPs hit unauthenticated LOGIN_REQUIRED,
  backed by an RFC-compliant byte-range stream proxy (/youtube/runtime/stream/{id})
  with exact Content-Range totals for AppleCoreMedia/WebKit;
* automatic Home feed population with native ytd-rich-item-renderer video cards
  when unauthenticated YouTube returns an empty feedNudgeRenderer;
* race-free Wisp v1/v2 stream establishment (buffers initial TLS ClientHello
  0x02 data frames while upstream TCP connection is still opening);
* adaptive WebKit/Safari ReadableStream transfer patch for controller.api.js;
* realistic browser User-Agent preservation on Google Accounts so BotGuard
  telemetry matches HTTP headers, while using desktop Chrome UA on YouTube;
* no breaking mutation of the host application's other routes.
"""
from __future__ import annotations

import asyncio
import base64
import gzip
import ipaddress
import json
import os
import re
import socket
import struct
import time
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse, urlunparse

for _np_var in ("NO_PROXY", "no_proxy", "GLOBAL_AGENT_NO_PROXY"):
    _np_val = os.environ.get(_np_var, "")
    if _np_val:
        os.environ[_np_var] = ",".join(
            p.strip() for p in _np_val.split(",") if p.strip() and not ("::" in p or "[" in p or "]" in p)
        )

import httpx
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute

try:
    from curl_cffi.const import CurlOpt
    from curl_cffi.requests import AsyncSession as CurlAsyncSession
except Exception:  # pragma: no cover
    CurlOpt = None
    CurlAsyncSession = None


def _scrub_curl_cookies(curl, *, fresh: bool = False) -> None:
    if curl is None or CurlOpt is None:
        return
    try:
        curl.setopt(CurlOpt.COOKIELIST, "ALL")
        curl.setopt(CurlOpt.COOKIELIST, "SESS")
        tcp_keepalive = getattr(CurlOpt, "TCP_KEEPALIVE", None)
        if tcp_keepalive is not None:
            curl.setopt(tcp_keepalive, 1)
        fresh_connect = getattr(CurlOpt, "FRESH_CONNECT", None)
        if fresh_connect is not None:
            curl.setopt(fresh_connect, 1 if fresh else 0)
        if not (os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or os.getenv("https_proxy") or os.getenv("http_proxy")):
            ipresolve = getattr(CurlOpt, "IPRESOLVE", None)
            if ipresolve is not None:
                curl.setopt(ipresolve, 1)  # CURL_IPRESOLVE_V4
        connect_timeout_ms = getattr(CurlOpt, "CONNECTTIMEOUT_MS", None)
        if connect_timeout_ms is not None:
            curl.setopt(connect_timeout_ms, 12000)
        happy_eyeballs_ms = getattr(CurlOpt, "HAPPY_EYEBALLS_TIMEOUT_MS", None)
        if happy_eyeballs_ms is not None:
            curl.setopt(happy_eyeballs_ms, 300)
    except Exception:
        pass


if CurlAsyncSession is not None:

    class StatelessCurlSession(CurlAsyncSession):
        """HTTP/2 + Chrome-TLS session with zero C-level or Python-level cookie storage.

        Every request is 100% isolated to the headers explicitly passed by that
        request; Set-Cookie headers from upstream are returned to the caller's
        browser (Scramjet IndexedDB cookie jar) and immediately purged from
        libcurl's internal cookie engine so no session or login token can ever
        leak across users.
        """

        _force_fresh: bool = False

        async def pop_curl(self):
            curl = await super().pop_curl()
            _scrub_curl_cookies(curl, fresh=getattr(self, "_force_fresh", False))
            self._force_fresh = False
            return curl

        def push_curl(self, curl) -> None:
            _scrub_curl_cookies(curl, fresh=False)
            super().push_curl(curl)

        async def request(self, method: str, url: str, *args, **kwargs):
            kwargs["discard_cookies"] = True
            try:
                self._cookies.clear()
            except Exception:
                pass
            try:
                try:
                    return await super().request(method, url, *args, **kwargs)
                except Exception:
                    self._force_fresh = True
                    return await super().request(method, url, *args, **kwargs)
            finally:
                try:
                    self._cookies.clear()
                except Exception:
                    pass

else:
    StatelessCurlSession = None

SERVICE_NAME = "YouTube"
SERVICE_ICON = "▶"
SERVICE_PATH = "/youtube/"
PROXY_MODULE = "youtube_scramjet_proxy.py"

SCRAMJET_VERSION = "2.0.67-alpha.2"
CONTROLLER_VERSION = "0.0.14"
UTILS_VERSION = "0.0.3"
LIBCURL_VERSION = "2.0.5"
EPOXY_VERSION = "3.0.1"
RUNTIME_REV = "20261112-yt-v46"

SCRAMJET_WISP_URL = os.getenv("SCRAMJET_WISP_URL", "").strip()

YOUTUBE_HOME = "https://www.youtube.com/"
GOOGLE_LOGIN = (
    "https://accounts.google.com/ServiceLogin"
    "?service=youtube&hl=ja"
    "&continue=https%3A%2F%2Fwww.youtube.com%2F"
)

YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "studio.youtube.com",
    "upload.youtube.com",
    "youtu.be",
}
YOUTUBE_IMAGE_SUFFIXES = (
    "ytimg.com",
    "i.ytimg.com",
    "s.ytimg.com",
    "img.youtube.com",
    "ggpht.com",
    "yt3.ggpht.com",
    "yt4.ggpht.com",
    "googleusercontent.com",
    "yt3.googleusercontent.com",
    "yt4.googleusercontent.com",
    "lh3.googleusercontent.com",
    "ytimg.l.google.com",
    "fonts.gstatic.com",
    "www.gstatic.com",
    "fonts.googleapis.com",
    "usercontent.goog",
    "playables.usercontent.goog",
)
AUTH_HOSTS = {
    "accounts.google.com",
    "accounts.google.co.jp",
    "accounts.youtube.com",
    "myaccount.google.com",
    "gds.google.com",
    "ogs.google.com",
    "apis.google.com",
    "ssl.gstatic.com",
    "www.gstatic.com",
    "play.google.com",
    "oauth2.googleapis.com",
    "youtube.googleapis.com",
}
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

router = APIRouter()


# ---------------------------------------------------------------------------
# WebKit / iPadOS compatibility patches for controller.api.js, controller.sw.js,
# and controller.inject.js
# ---------------------------------------------------------------------------
def _patch_controller_api(source: bytes) -> bytes:
    text = source.decode("utf-8")
    if "__schoolhub_safari_buffered_body" in text:
        return source

    old_idb = (
        'function g(){return p||(p=new Promise((e,t)=>{let r=indexedDB.open("__scramjet_controller",1);'
        "r.onupgradeneeded=()=>{let e=r.result;e.objectStoreNames.contains(h)||e.createObjectStore(h)},"
        'r.onsuccess=()=>e(r.result),r.onerror=()=>t(r.error??Error("Failed to open cookie database"))}))}'
        'async function w(){try{let e=(await g()).transaction(h,"readonly"),t=e.objectStore(h),r=await y(t.get(f));'
        'return await m(e),u(r)}catch(e){return console.error("Failed to read persisted controller cookies:",e),null}}'
        'async function k(e,t){try{let r=(await g()).transaction(h,"readwrite"),o=r.objectStore(h),'
        "s=u(await y(o.get(f))),i=Math.max(Date.now(),t+1,(s?.updatedAt??0)+1);"
        'return o.put({updatedAt:i,cookies:e},f),await m(r),i}catch(e){return console.error("Failed to persist controller cookies:",e),t}}'
    )
    new_idb = (
        'try{indexedDB.deleteDatabase("__scramjet_controller")}catch(_){}'
        'const _shVaultKey="__sh_cookie_vault_yt";'
        "function g(){return Promise.resolve(null)}"
        "function _readVaultRaw(){let a=null,b=null;try{let s=localStorage.getItem(_shVaultKey);if(s)a=u(JSON.parse(s))}catch(_){}try{let s=sessionStorage.getItem(_shVaultKey);if(s)b=u(JSON.parse(s))}catch(_){}let res=null;if(a&&b){try{let ca=JSON.parse(a.cookies||'{}'),cb=JSON.parse(b.cookies||'{}');res=a.updatedAt>=b.updatedAt?a:b}catch(_){res=a.updatedAt>=b.updatedAt?a:b}}else{res=a||b||null}return res}"
        "async function w(){return _readVaultRaw()}"
        "async function k(e,t){try{let s=_readVaultRaw(),i=Math.max(Date.now(),t+1,(s?.updatedAt??0)+1),merged=e;try{let prev=JSON.parse(s?.cookies||'{}'),curr=JSON.parse(e||'{}');if(Object.keys(curr).length===0&&Object.keys(prev).length>0&&!globalThis.__shExplicitClear){merged=s.cookies}}catch(_){}let payload=JSON.stringify({updatedAt:i,cookies:merged});try{localStorage.setItem(_shVaultKey,payload)}catch(_){}try{sessionStorage.setItem(_shVaultKey,payload)}catch(_){}return i}catch(e){return t}}"
    )
    text = text.replace(old_idb, new_idb, 1)
    text = text.replace(
        'new BroadcastChannel("__scramjet_controller_channel")',
        "({postMessage(){},addEventListener(){},close(){}})",
        1,
    )
    text = text.replace(
        "let t=e.data.$controller$setCookie;t.options?.clear&&this.cookieJar.clear(),this.applyCookieSyncEntries(t.cookies),",
        'let t=e.data.$controller$setCookie;if(t.cid&&t.cid!==this.id){"string"==typeof t.id&&(navigator.serviceWorker.controller||this.serviceWorkerController)?.postMessage({$sw$setCookieDone:{id:t.id}});return}t.options?.clear&&this.cookieJar.clear(),this.applyCookieSyncEntries(t.cookies),this.persistCookies().catch(()=>{}),',
        1,
    )
    text = text.replace(
        "async sendSetCookie(t,r){await e.persistCookies(),await e.propagateCookieSync(t.map(({url:e,cookie:t})=>({url:e.href,cookie:t})),r)}",
        "async sendSetCookie(t,r){let n=t.map(({url:e,cookie:t})=>({url:e.href,cookie:t}));e.applyCookieSyncEntries(n),await e.persistCookies(),await e.propagateCookieSync(n,r)}",
        1,
    )

    # Never ignore $controller$swrevive when a new or woken Service Worker
    # claims the page, and always post $controller$init to the currently active
    # navigator.serviceWorker.controller.
    text = text.replace("guardServiceWorkerRevive=!0;", "guardServiceWorkerRevive=!1;")
    text = text.replace(
        "setTimeout(()=>{this.guardServiceWorkerRevive=!1},5e3)",
        "this.guardServiceWorkerRevive=!1",
    )
    text = text.replace(
        "this.serviceWorkerController.postMessage",
        "(navigator.serviceWorker.controller||this.serviceWorkerController).postMessage",
    )
    text = text.replace(
        "sendSetCookie:async({cookies:e,options:t})=>{await this.loadSavedCookies(!0),",
        "sendSetCookie:async({cookies:e,options:t})=>{",
        1,
    )
    text = text.replace(
        "await this.loadSavedCookies(!0);",
        "await Promise.race([this.loadSavedCookies(!0),new Promise(r=>setTimeout(r,1200))]).catch(()=>{});",
    )

    # Fix initRemoteTransport so HEAD/204/304 null bodies and WebKit ReadableStream
    # bodies never throw TypeError/DataCloneError during MessagePort transfer.
    old_remote = (
        "let s=await this.transport.request(new URL(e),t,r,o,void 0);return[s,[s.body]]"
    )
    new_remote = (
        "let s=await this.transport.request(new URL(e),t,r,o,void 0);"
        "if(s.body instanceof ReadableStream){s.body=await new Response(s.body).arrayBuffer()}"
        "return[s,s.body instanceof ArrayBuffer?[s.body]:[]]"
    )
    text = text.replace(old_remote, new_remote)

    transfer_re = re.compile(
        r"(?P<var>[A-Za-z_$][\w$]*)\.body\s*instanceof\s*ReadableStream\s*"
        r"\|\|\s*(?P=var)\.body\s*instanceof\s*ArrayBuffer\s*"
        r"\?\s*\[\s*(?P=var)\.body\s*\]\s*:\s*\[\s*\]"
    )
    m = transfer_re.search(text)
    if not m:
        return text.encode("utf-8")
    var_name = m.group("var")
    returns = list(re.finditer(r"return\s*\[", text[: m.start()]))
    if not returns:
        return text.encode("utf-8")
    return_pos = returns[-1].start()
    body_match = re.search(
        rf"body\s*:\s*{re.escape(var_name)}\.body", text[return_pos : m.start()]
    )
    if not body_match:
        return text.encode("utf-8")
    body_start = return_pos + body_match.start()
    body_end = return_pos + body_match.end()
    body_name = "__schoolhub_safari_buffered_body"
    # Always convert ReadableStream to ArrayBuffer before cross-process
    # Window <-> ServiceWorker MessagePort transfer.
    inject = (
        f"const {body_name} = {var_name}.body instanceof ReadableStream ? "
        f"await new Response({var_name}.body).arrayBuffer() : {var_name}.body;\n"
    )
    s2 = text[:return_pos] + inject + text[return_pos:]
    shift = len(inject)
    s2 = s2[: body_start + shift] + "body: " + body_name + s2[body_end + shift :]
    tm = re.compile(
        rf"{re.escape(var_name)}\.body\s*instanceof\s*ReadableStream\s*"
        rf"\|\|\s*{re.escape(var_name)}\.body\s*instanceof\s*ArrayBuffer\s*"
        rf"\?\s*\[\s*{re.escape(var_name)}\.body\s*\]\s*:\s*\[\s*\]"
    ).search(s2, body_start + shift)
    if not tm:
        return text.encode("utf-8")
    replacement = f"{body_name} instanceof ArrayBuffer ? [{body_name}] : []"
    s2 = s2[: tm.start()] + replacement + s2[tm.end() :]
    return s2.encode("utf-8")


def _patch_controller_sw(source: bytes) -> bytes:
    text = source.decode("utf-8")
    if "_shBodyVal" in text:
        return source

    text = text.replace(
        "a.postMessage({$controller$setCookie:{cookies:e,options:r,id:o}})",
        "a.postMessage({$controller$setCookie:{cookies:e,options:r,id:o,cid:this.id,pfx:this.prefix}})",
        1,
    )

    old_init_rt = (
        'if(e.data.$sw$initRemoteTransport&&"object"==typeof e.data.$sw$initRemoteTransport)'
        '{let{port:t,prefix:r}=e.data.$sw$initRemoteTransport,'
        'o=i.find(e=>new URL(r).pathname.startsWith(e.prefix));'
        'if(!o)return void console.error("No relevant controller found for transport init");'
        'o.rpc.call("initRemoteTransport",t,[t])}'
    )
    new_init_rt = (
        'if(e.data.$sw$initRemoteTransport&&"object"==typeof e.data.$sw$initRemoteTransport)'
        '{let{port:t,prefix:r}=e.data.$sw$initRemoteTransport;'
        '_waitCtrl(new URL(r).pathname).then(o=>{'
        'if(!o)return void console.error("No relevant controller found for transport init");'
        'o.rpc.call("initRemoteTransport",t,[t])});return}'
    )
    text = text.replace(old_init_rt, new_init_rt, 1)

    old_route = (
        "let i=[];"
        "function n(e){let t=new URL(e.request.url);return void 0!==i.find(e=>t.pathname.startsWith(e.prefix))}"
        "async function a(e){try{let t=new URL(e.request.url),r=i.find(e=>t.pathname.startsWith(e.prefix)),"
    )
    new_route = (
        "let i=[];"
        "async function _waitCtrl(p){"
        "let c=i.find(e=>p.startsWith(e.prefix));if(c)return c;"
        "try{for(let e of await self.clients.matchAll())e.postMessage({$controller$swrevive:{}})}catch(_){}"
        "for(let k=0;k<80;k++){await new Promise(r=>setTimeout(r,50));"
        "c=i.find(e=>p.startsWith(e.prefix));if(c)return c;"
        "if(k===20||k===40){try{for(let e of await self.clients.matchAll())e.postMessage({$controller$swrevive:{}})}catch(_){}}}"
        "return i[0]}"
        "function n(e){let t=new URL(e.request.url);return t.pathname.includes('/~/sj/')||void 0!==i.find(e=>t.pathname.startsWith(e.prefix))}"
        "async function a(e){try{let t=new URL(e.request.url),r=await _waitCtrl(t.pathname);if(!r)throw Error('Controller unavailable');"
        "let "
    )
    text = text.replace(old_route, new_route, 1)

    old_call = "s=[...e.request.headers],n=await r.rpc.call("
    new_call = (
        "s=[...e.request.headers],"
        '_shBody=(e.request.method!=="GET"&&e.request.method!=="HEAD")'
        "?await e.request.arrayBuffer().catch(()=>null):null,"
        "_shBodyVal=(_shBody&&_shBody.byteLength>0)?_shBody:null,"
        "n=await r.rpc.call("
    )
    if old_call in text:
        text = text.replace(old_call, new_call, 1)
        text = text.replace("body:e.request.body,", "body:_shBodyVal,", 1)
        text = text.replace(
            "e.request.body instanceof ReadableStream||e.request.body instanceof ArrayBuffer?[e.request.body]:void 0",
            "_shBodyVal instanceof ArrayBuffer?[_shBodyVal]:void 0",
            1,
        )

    old_cookie_wait = (
        'r=setTimeout(()=>{if(!n){let r=s.filter(e=>void 0!==t[e]);'
        'console.error(`timed out waiting for set cookie response (deadlock?): cookies=${e.length} clients=${o.length} pending=${r.length}/${s.length} clientUrls=${o.map(e=>e.url).join(",")}`)}i()},1e3)'
    )
    text = text.replace(old_cookie_wait, "r=setTimeout(()=>{i()},50)", 1)

    old_timer = (
        'setTimeout(async()=>{for(let e of(console.log("service worker activated, notifying clients to revive"),'
        "await clients.matchAll()))e.postMessage({$controller$swrevive:{}})},100)"
    )
    new_timer = (
        "setTimeout(async()=>{if(i.length===0){for(let e of await clients.matchAll())"
        "e.postMessage({$controller$swrevive:{}})}},100)"
    )
    text = text.replace(old_timer, new_timer, 1)
    return text.encode("utf-8")


def _patch_controller_inject(source: bytes) -> bytes:
    """Patch controller.inject.js so:
    1) `navigator.serviceWorker?.controller` uses optional chaining (doesn't throw
       inside Web Workers where `navigator.serviceWorker` is undefined).
    2) Same-origin iframes share `window.top.__schoolhub_controller.cookieJar` and
       connect `initRemoteTransport` directly to
       `window.top.__schoolhub_controller.methods.initRemoteTransport(o.port2)`
       without a double Window->SW->Window MessagePort transfer, falling back to
       the active `navigator.serviceWorker?.controller`.
    3) Request bodies (`ReadableStream` / `Blob` / `URLSearchParams`) are
       serialized before `MessagePort.postMessage` so WebKit never throws
       `DataCloneError`.
    """
    text = source.decode("utf-8")
    if "__schoolhub_controller" in text:
        return source
    text = text.replace(
        "let c=navigator.serviceWorker.controller;",
        "let c=navigator.serviceWorker?.controller;",
        1,
    )
    text = text.replace(
        "if(i.pX in globalThis)return void globalThis[i.pX].syncDocumentInit({initHeaders:e.initHeaders,history:e.history,cookies:e.cookies});",
        "if(i.pX in globalThis)return void globalThis[i.pX].syncDocumentInit({initHeaders:e.initHeaders,history:e.history,cookies:(()=>{try{const _c=window.top?.__schoolhub_controller||window.opener?.top?.__schoolhub_controller||window.opener?.__schoolhub_controller;return _c?.cookieJar?void 0:e.cookies}catch(_){return e.cookies}})()});",
        1,
    )
    text = text.replace(
        "this.cookieJar=new i.cP,this.cookieJar.load(this.init.cookies),",
        "this.cookieJar=(()=>{try{const _c=window.top?.__schoolhub_controller||window.opener?.top?.__schoolhub_controller||window.opener?.__schoolhub_controller;return _c?.cookieJar}catch(_){return null}})()||(new i.cP),(()=>{try{const _c=window.top?.__schoolhub_controller||window.opener?.top?.__schoolhub_controller||window.opener?.__schoolhub_controller;if(this.cookieJar!==_c?.cookieJar)this.cookieJar.load(this.init.cookies)}catch(_){this.cookieJar.load(this.init.cookies)}})(),",
        1,
    )
    text = text.replace(
        "let t=e.data.$controller$setCookie;if(t.options?.clear&&this.cookieJar.clear(),",
        'let t=e.data.$controller$setCookie;if(t.pfx&&!this.init.prefix?.pathname?.startsWith(t.pfx)){if("string"==typeof t.id){let e=navigator.serviceWorker?.controller??c;e?.postMessage({$sw$setCookieDone:{id:t.id}})}return}if(t.options?.clear&&this.cookieJar.clear(),',
        1,
    )
    text = text.replace(
        "sendSetCookie:async(e,t)=>{await this.transport.sendSetCookie(e,t)}",
        "sendSetCookie:async(e,t)=>{try{const _c=window.top?.__schoolhub_controller||window.opener?.top?.__schoolhub_controller||window.opener?.__schoolhub_controller;await _c?.persistCookies?.()}catch(_){}await this.transport.sendSetCookie(e,t)}",
        1,
    )
    old_req = (
        'async request(e,t,o,r,i){return await this.rpc.call("request",{remote:e.href,method:t,body:o,headers:r})}'
    )
    new_req = (
        "async request(e,t,o,r,i){"
        "if(o instanceof ReadableStream||o instanceof Blob){o=await new Response(o).arrayBuffer()}"
        'else if("undefined"!=typeof URLSearchParams&&o instanceof URLSearchParams){o=o.toString()}'
        'return await this.rpc.call("request",{remote:e.href,method:t,body:o,headers:r})}'
    )
    text = text.replace(old_req, new_req, 1)
    old_post = (
        "c?.postMessage({$sw$initRemoteTransport:{port:o.port2,prefix:this.init.prefix.href}},[o.port2])"
    )
    new_post = (
        "(()=>{try{const _c='undefined'!=typeof window&&(window.top?.__schoolhub_controller||window.opener?.top?.__schoolhub_controller||window.opener?.__schoolhub_controller);if(_c?.methods?.initRemoteTransport)"
        "{_c.methods.initRemoteTransport(o.port2);return}}catch(_){}"
        "(navigator.serviceWorker?.controller||c)?.postMessage({$sw$initRemoteTransport:{port:o.port2,prefix:this.init.prefix.href}},[o.port2])})()"
    )
    text = text.replace(old_post, new_post, 1)
    inject_preamble = (
        'try{if(typeof navigator!=="undefined"){'
        'const _np=typeof Navigator!=="undefined"&&Navigator.prototype?Navigator.prototype:Object.getPrototypeOf(navigator);'
        'if(_np){try{Object.defineProperty(_np,"onLine",{get:()=>true,configurable:true});}catch(_){}}'
        'try{Object.defineProperty(navigator,"onLine",{get:()=>true,configurable:true});}catch(_){}'
        'try{const _cn={downlink:10,effectiveType:"4g",rtt:50,saveData:false,addEventListener:()=>{},removeEventListener:()=>{},dispatchEvent:()=>true};if(_np){Object.defineProperty(_np,"connection",{get:()=>_cn,configurable:true});}Object.defineProperty(navigator,"connection",{get:()=>_cn,configurable:true});}catch(_){}'
        '}}catch(_){}\n'
        'try{if(typeof window!=="undefined"){'
        'if(window.addEventListener){const _oa=window.addEventListener;window.addEventListener=function(y,f,p){if(y==="offline")return;_oa.call(this,y,f,p);};}'
        'try{Object.defineProperty(window,"onoffline",{get:()=>null,set:()=>{},configurable:true});}catch(_){}'
        'try{Object.defineProperty(window,"ononline",{get:()=>null,set:()=>{},configurable:true});}catch(_){}'
        'try{const _op=window.open;window.open=function(u,...a){if(u&&(String(u).includes("accounts.google.")||String(u).includes("ServiceLogin"))){window.location.href=u;return window;}return _op?_op.call(this,u,...a):null;};}catch(_){}'
        '}}catch(_){}\n'
    )
    if not text.startswith("try{if(typeof navigator"):
        text = inject_preamble + text
    return text.encode("utf-8")


def _patch_scramjet_js(source: bytes) -> bytes:
    text = source.decode("utf-8")
    text = text.replace("(?i:url)", "(?:[uU][rR][lL])")
    old_code = "let r=e.box.histories.get(t.this),s=(0,n.Qf)(t.args[2]);"
    new_code = "let r=e.box.histories.get(t.this),s=null!=t.args[2]?(0,n.Qf)(t.args[2]):null;"
    if old_code in text:
        text = text.replace(old_code, new_code, 1)

    old_storage_mod = (
        'function n(e,t){let r={get(t,r){switch(r){'
        'case"getItem":return r=>t.getItem(e.url.host+"@"+r);'
        'case"setItem":return(r,i)=>t.setItem(e.url.host+"@"+r,i);'
        'case"removeItem":return r=>t.removeItem(e.url.host+"@"+r);'
        'case"clear":return()=>{for(let r in(0,i.BR)(t))r.startsWith(e.url.host)&&t.removeItem(r)};'
        'case"key":return r=>{let n=(0,i.BR)(t).filter(t=>t.startsWith(e.url.host));return t.getItem(n[r])};'
        'case"length":return(0,i.BR)(t).filter(t=>t.startsWith(e.url.host)).length;'
        'default:if(r in Object.prototype||"symbol"==typeof r)return(0,i.rF)(t,r);return t.getItem(e.url.host+"@"+r)}},'
        'set:(t,r,i)=>(t.setItem(e.url.host+"@"+r,i),!0),'
        'has:(t,r)=>null!==t.getItem(e.url.host+"@"+r),'
        'ownKeys:t=>(0,i.lK)(t).filter(t=>"string"==typeof t&&t.startsWith(e.url.host)).map(t=>"string"==typeof t?t.substring(e.url.host.length+1):t),'
        'getOwnPropertyDescriptor(t,r){if(null!==t.getItem(e.url.host+"@"+r))return{value:t.getItem(e.url.host+"@"+r),enumerable:!0,configurable:!0,writable:!0}},'
        'defineProperty:(t,r,i)=>(t.setItem(e.url.host+"@"+r,i.value),!0)},'
        "n=new Proxy(t.localStorage,r),s=new Proxy(t.sessionStorage,r);"
        "delete t.localStorage,delete t.sessionStorage,t.localStorage=n,t.sessionStorage=s}"
    )
    new_storage_mod = (
        'function n(e,t){const _pfx=()=>"yt:"+e.url.host+"@";let r={get(t,r){switch(r){'
        'case"getItem":return r=>t.getItem(_pfx()+r);'
        'case"setItem":return(r,i)=>t.setItem(_pfx()+r,i);'
        'case"removeItem":return r=>t.removeItem(_pfx()+r);'
        'case"clear":return()=>{for(let r in(0,i.BR)(t))r.startsWith(_pfx())&&t.removeItem(r)};'
        'case"key":return r=>{let n=(0,i.BR)(t).filter(t=>t.startsWith(_pfx()));return n[r]?n[r].substring(_pfx().length):null};'
        'case"length":return(0,i.BR)(t).filter(t=>t.startsWith(_pfx())).length;'
        'default:if(r in Object.prototype||"symbol"==typeof r)return(0,i.rF)(t,r);return t.getItem(_pfx()+r)}},'
        'set:(t,r,i)=>(t.setItem(_pfx()+r,i),!0),'
        'has:(t,r)=>null!==t.getItem(_pfx()+r),'
        'ownKeys:t=>(0,i.lK)(t).filter(t=>"string"==typeof t&&t.startsWith(_pfx())).map(t=>"string"==typeof t?t.substring(_pfx().length):t),'
        'getOwnPropertyDescriptor(t,r){if(null!==t.getItem(_pfx()+r))return{value:t.getItem(_pfx()+r),enumerable:!0,configurable:!0,writable:!0}},'
        'defineProperty:(t,r,i)=>(t.setItem(_pfx()+r,i.value),!0),'
        'deleteProperty:(t,r)=>(t.removeItem(_pfx()+r),!0)},'
        "n=new Proxy(t.localStorage,r),s=new Proxy(t.sessionStorage,r);"
        "delete t.localStorage,delete t.sessionStorage,"
        'Object.defineProperty(t,"localStorage",{get:()=>n,set:()=>{},configurable:!1,enumerable:!0}),'
        'Object.defineProperty(t,"sessionStorage",{get:()=>s,set:()=>{},configurable:!1,enumerable:!0})}'
    )
    if old_storage_mod in text:
        text = text.replace(old_storage_mod, new_storage_mod, 1)

    old_parent_top = (
        '(0,o.pS)(t.Object.prototype,e.config.globals.wrappropertybase+"parent",'
        '{get:function(){return e.wrapfn(this.parent,!1)},set(e){this.top=e},configurable:!1,enumerable:!1})'
    )
    old_parent_top_exact = (
        '(0,o.pS)(t.Object.prototype,e.config.globals.wrappropertybase+"parent",'
        '{get:function(){return e.wrapfn(this.parent,!1)},set(e){this.parent=e},configurable:!1,enumerable:!1}),'
        '(0,o.pS)(t.Object.prototype,e.config.globals.wrappropertybase+"top",'
        '{get:function(){return e.wrapfn(this.top,!1)},set(e){this.top=e},configurable:!1,enumerable:!1})'
    )
    new_parent_top_exact = (
        '(()=>{try{'
        'e.Proxy(["Object.getOwnPropertyNames","Reflect.ownKeys"],{apply(t){let r=t.call();return t.return(Array.isArray(r)?r.filter(k=>"string"!=typeof k||(!k.startsWith("$scram")&&!k.startsWith("__sh_"))):r)}});'
        'const _np=t.Navigator&&t.Navigator.prototype;if(_np&&t.navigator){'
        'try{if(t.navigator.webdriver){const _wd=Object.getOwnPropertyDescriptor(_np,"webdriver");if(_wd&&_wd.get){const _wg=new Proxy(_wd.get,{apply:()=>!1});e.box.unproxy.set(_wg,_wd.get);Object.defineProperty(_np,"webdriver",{..._wd,get:_wg})}}}catch(_){}'
        'const _u=String(t.navigator.userAgent||"");if(_u.includes("HeadlessChrome/")){'
        'const _c=_u.replace(/HeadlessChrome\\//g,"Chrome/"),_av=_c.replace(/^Mozilla\\//,"");'
        'const _ud=Object.getOwnPropertyDescriptor(_np,"userAgent");if(_ud&&_ud.get){const _ug=new Proxy(_ud.get,{apply:()=>_c});e.box.unproxy.set(_ug,_ud.get);Object.defineProperty(_np,"userAgent",{..._ud,get:_ug})}'
        'const _ad=Object.getOwnPropertyDescriptor(_np,"appVersion");if(_ad&&_ad.get){const _ag=new Proxy(_ad.get,{apply:()=>_av});e.box.unproxy.set(_ag,_ad.get);Object.defineProperty(_np,"appVersion",{..._ad,get:_ag})}'
        '}'
        'try{const _od=Object.getOwnPropertyDescriptor(_np,"onLine");const _og=new Proxy(_od&&_od.get?_od.get:()=>!0,{apply:()=>!0});e.box.unproxy.set(_og,_od?.get||(()=>!0));Object.defineProperty(_np,"onLine",{get:_og,enumerable:!0,configurable:!0});}catch(_){}'
        'try{Object.defineProperty(t.navigator,"onLine",{get:()=>!0,enumerable:!0,configurable:!0});}catch(_){}'
        'try{const _cn={downlink:10,effectiveType:"4g",rtt:50,saveData:!1,addEventListener:()=>{},removeEventListener:()=>{},dispatchEvent:()=>!0};Object.defineProperty(_np,"connection",{get:()=>_cn,enumerable:!0,configurable:!0});Object.defineProperty(t.navigator,"connection",{get:()=>_cn,enumerable:!0,configurable:!0});}catch(_){}'
        'try{if(t.addEventListener){const _oa=t.addEventListener;t.addEventListener=function(y,f,p){if(y==="offline")return;_oa.call(this,y,f,p);};}}catch(_){}'
        'try{Object.defineProperty(t,"onoffline",{get:()=>null,set:()=>{},configurable:!0});}catch(_){}'
        'try{Object.defineProperty(t,"ononline",{get:()=>null,set:()=>{},configurable:!0});}catch(_){}'
        '}}catch(_){}})(),'
        '(0,o.pS)(t.Object.prototype,e.config.globals.wrappropertybase+"parent",'
        '{get:function(){return this===t||this===t.globalThis||this&&this.window===this?e.wrapfn(this.parent,!1):this.parent},set(e){this.parent=e},configurable:!1,enumerable:!1}),'
        '(0,o.pS)(t.Object.prototype,e.config.globals.wrappropertybase+"top",'
        '{get:function(){return this===t||this===t.globalThis||this&&this.window===this?e.wrapfn(this.top,!1):this.top},set(e){this.top=e},configurable:!1,enumerable:!1})'
    )
    if old_parent_top_exact in text:
        text = text.replace(old_parent_top_exact, new_parent_top_exact, 1)

    old_wrappm = (
        "(0,s.pS)(t,e.config.globals.wrappostmessagefn,"
        '{value:function(e){return e&&"function"==typeof e.postMessage?{postMessage:e.postMessage.bind(e)}:e},'
        "configurable:!1,writable:!1,enumerable:!1})"
    )
    new_wrappm = (
        "(0,s.pS)(t,e.config.globals.wrappostmessagefn,"
        '{value:function(r){return r&&"function"==typeof r.postMessage?{postMessage:function(...a){const u=this&&o.POLLUTANT in this?this[o.POLLUTANT]:t;try{(0,s.pS)(r,o.POLLUTANT,{value:u,writable:!0,configurable:!0,enumerable:!1})}catch(_){}try{return r.postMessage(...a)}finally{try{delete r[o.POLLUTANT]}catch(_){}}}}:r},'
        "configurable:!1,writable:!1,enumerable:!1})"
    )
    if old_wrappm in text:
        text = text.replace(old_wrappm, new_wrappm, 1)

    old_smap_push = (
        '!function(e,t,r){let i=Uint8Array.from(t),n=new DataView(i.buffer),s=new TextDecoder("utf-8"),o=[],a=n.getUint32(0,!0),A=4;'
        'for(let e=0;e<a;e++){let e=n.getUint32(A,!0);A+=4;let t=n.getUint32(A,!0);A+=4;let r=n.getUint8(A);'
        'if(A+=1,0==r)o.push({type:r,start:e,size:t});else if(1==r){let a=e+t,l=n.getUint32(A,!0);A+=4;let c=s.decode(i.subarray(A,A+l));o.push({type:r,start:e,end:a,str:c}),A+=l}}'
        'e.box.sourcemaps[r]=o}(e,t,r)'
    )
    new_smap_push = (
        '(()=>{const o=Uint8Array.from(t);'
        'try{(top.__sh_smaps||(top.__sh_smaps={}))[r]=o}catch(_){}'
        '(globalThis.__sh_smaps||(globalThis.__sh_smaps={}))[r]=e.box.sourcemaps[r]=o})()'
    )
    if old_smap_push in text:
        text = text.replace(old_smap_push, new_smap_push, 1)

    old_ftostr_tag = 'if(-1===r)throw n.error("unreachable",e,t,r),new i.$D("unreachable");let o=e.substring(t+2,r).split(" ");if(3!==o.length||"scramtag"!==o[0]||!(0,i.Aw)(+o[1]))throw n.error("invalid tag",e,t,r,o),new i.$D("invalid tag");'
    new_ftostr_tag = 'if(-1===r)return null;let o=e.substring(t+2,r).split(" ");if(3!==o.length||"scramtag"!==o[0]||!(0,i.Aw)(+o[1]))return null;'
    if old_ftostr_tag in text:
        text = text.replace(old_ftostr_tag, new_ftostr_tag, 1)

    old_ftostr = 'u=e.box.sourcemaps[a];if(!u)return n.warn("failed to get rewrites for tag",a),t.return(r);'
    new_ftostr = (
        'u=e.box.sourcemaps[a]||globalThis.__sh_smaps?.[a]||(()=>{try{return top.__sh_smaps?.[a]}catch(_){return null}})();'
        'if(!u)return t.return(r.replace(/\\/\\*scramtag [^*]*\\*\\//g,""));'
        'if(u instanceof Uint8Array||ArrayBuffer.isView(u)){'
        'let _i=new Uint8Array(u.buffer,u.byteOffset,u.byteLength),_n=new DataView(_i.buffer,_i.byteOffset,_i.byteLength),_s=new TextDecoder("utf-8"),_o=[],_a=_n.getUint32(0,!0),_A=4;'
        'for(let _e=0;_e<_a;_e++){let _st=_n.getUint32(_A,!0);_A+=4;let _sz=_n.getUint32(_A,!0);_A+=4;let _tp=_n.getUint8(_A);_A+=1;'
        'if(0==_tp)_o.push({type:_tp,start:_st,size:_sz});else if(1==_tp){let _ed=_st+_sz,_ln=_n.getUint32(_A,!0);_A+=4;let _tx=_s.decode(_i.subarray(_A,_A+_ln));_o.push({type:_tp,start:_st,end:_ed,str:_tx}),_A+=_ln}}'
        'u=e.box.sourcemaps[a]=(globalThis.__sh_smaps||(globalThis.__sh_smaps={}))[a]=_o;try{(top.__sh_smaps||(top.__sh_smaps={}))[a]=_o}catch(_){}}'
    )
    if old_ftostr in text:
        text = text.replace(old_ftostr, new_ftostr, 1)

    old_ftostr_end = 'f+=r.slice(m),f=f.replace(`${s}${l} ${a}*/`,""),t.return(f)'
    new_ftostr_end = 'f+=r.slice(m),f=f.replace(`${s}${l} ${a}*/`,"").replace(/\\/\\*scramtag [^*]*\\*\\//g,""),t.return(f)'
    if old_ftostr_end in text:
        text = text.replace(old_ftostr_end, new_ftostr_end, 1)

    old_msg_null = 'origin(){return i.iswindow?"object"==typeof this.data&&"$scramjet$origin"in this.data?this.data.$scramjet$origin:e.url.origin:""},data(){return"object"==typeof this.data&&"$scramjet$data"in this.data?this.data.$scramjet$data:this.data}'
    new_msg_null = 'origin(){return i.iswindow?"object"==typeof this.data&&null!==this.data&&"$scramjet$origin"in this.data?this.data.$scramjet$origin:e.url.origin:""},data(){return"object"==typeof this.data&&null!==this.data&&"$scramjet$data"in this.data?this.data.$scramjet$data:this.data}'
    if old_msg_null in text:
        text = text.replace(old_msg_null, new_msg_null, 1)

    old_evt_listener = (
        'e.Proxy("EventTarget.prototype.addEventListener",{apply(t){if("function"!=typeof t.args[1])return;let r=t.args[1],i=a(r);t.args[1]=i;let n=e.eventcallbacks.get(t.this);(n||=[]).push({event:t.args[0],originalCallback:r,proxiedCallback:i}),e.eventcallbacks.set(t.this,n)}}),'
        'e.Proxy("EventTarget.prototype.removeEventListener",{apply(t){if("function"!=typeof t.args[1])return;let r=e.eventcallbacks.get(t.this);if(!r)return;let i=r.findIndex(e=>e.event===t.args[0]&&e.originalCallback===t.args[1]);if(-1===i)return;let n=r.splice(i,1);e.eventcallbacks.set(t.this,r),t.args[1]=n[0].proxiedCallback}})'
    )
    new_evt_listener = (
        'e.Proxy("EventTarget.prototype.addEventListener",{apply(t){let r=t.args[1];if("function"!=typeof r&&!("object"==typeof r&&null!==r&&"function"==typeof r.handleEvent))return;let i="function"==typeof r?a(r):a(function(...o){return r.handleEvent.apply(r,o)});t.args[1]=i;let n=e.eventcallbacks.get(t.this);(n||=[]).push({event:t.args[0],originalCallback:r,proxiedCallback:i}),e.eventcallbacks.set(t.this,n)}}),'
        'e.Proxy("EventTarget.prototype.removeEventListener",{apply(t){let o=t.args[1];if("function"!=typeof o&&!("object"==typeof o&&null!==o&&"function"==typeof o.handleEvent))return;let r=e.eventcallbacks.get(t.this);if(!r)return;let i=r.findIndex(e=>e.event===t.args[0]&&e.originalCallback===o);if(-1===i)return;let n=r.splice(i,1);e.eventcallbacks.set(t.this,r),t.args[1]=n[0].proxiedCallback}})'
    )
    if old_evt_listener in text:
        text = text.replace(old_evt_listener, new_evt_listener, 1)

    old_win_pm = (
        'i.iswindow&&e.Proxy("window.postMessage",{apply(e){let{constructor:{constructor:t}}="object"==typeof e.args[0]&&null!==e.args[0]?e.args[0]:"object"==typeof e.args[2]&&null!==e.args[2]?e.args[2]:e.this&&o.POLLUTANT in e.this&&"object"==typeof e.this[o.POLLUTANT]&&null!==e.this[o.POLLUTANT]?e.this[o.POLLUTANT]:{},r=t("return globalThis")()[n.p],i=t("...args","this(...args)"),s="about:srcdoc"===r.url.href||"about:blank"===r.url.href;e.args[0]={$scramjet$messagetype:"window",$scramjet$origin:s?r.global.parent[n.p].url.origin:r.url.origin,$scramjet$data:e.args[0]},"string"==typeof e.args[1]&&(e.args[1]="*"),"object"==typeof e.args[1]&&(e.args[1].targetOrigin="*"),e.return(i.call(e.fn,...e.args))}})'
    )
    new_win_pm = (
        'i.iswindow&&e.Proxy("window.postMessage",{apply(c){let r=null;try{let{constructor:{constructor:f}}="object"==typeof c.args[0]&&null!==c.args[0]?c.args[0]:"object"==typeof c.args[2]&&null!==c.args[2]?c.args[2]:c.this&&o.POLLUTANT in c.this&&"object"==typeof c.this[o.POLLUTANT]&&null!==c.this[o.POLLUTANT]?c.this[o.POLLUTANT]:{};r=f("return globalThis")()?.[n.p]}catch(_){}r||(r=c.this?.[n.p]||t?.[n.p]||e);let u=r?.url?.href||"",s="about:srcdoc"===u||"about:blank"===u,g=(s?r?.global?.parent?.[n.p]?.url?.origin:r?.url?.origin)||e?.url?.origin||"";c.args[0]={$scramjet$messagetype:"window",$scramjet$origin:g,$scramjet$data:c.args[0]},"string"==typeof c.args[1]&&(c.args[1]="*"),"object"==typeof c.args[1]&&null!==c.args[1]&&(c.args[1].targetOrigin="*"),c.return(c.fn.apply(c.this||t,c.args))}})'
    )
    if old_win_pm in text:
        text = text.replace(old_win_pm, new_win_pm, 1)

    old_smap = "let e=globalThis[r.config.globals.pushsourcemapfn];"
    new_smap = "let e=(n?.base?.defaultView||globalThis)[r.config.globals.pushsourcemapfn]||globalThis[r.config.globals.pushsourcemapfn];"
    if old_smap in text:
        text = text.replace(old_smap, new_smap, 1)

    old_stack = 'let r=(t,r)=>{let n=t.stack;for(let t=0;t<r.length;t++){let s=r[t].getFileName();try{if(e.config.maskedfiles.some(e=>s.endsWith(e))){let e=n.split("\\n"),t=e.find(e=>e.includes(s));e.splice(t,1),n=e.join("\\n");continue}}catch{}try{n=n.replaceAll(s,(0,i.v2)(s,e.context))}catch{}}return n};e.Trap("Error.prepareStackTrace",{get:e=>r,set(e){}})'
    new_stack = 'let _inSt=!1,_usrSt=null,r=(t,a)=>{if(_inSt)return String(t);_inSt=!0;try{if("function"==typeof _usrSt)return _usrSt(t,a);let n=[String(t)];for(let o=0;o<a.length;o++){let c=a[o],s=String(c),f="";try{f=c.getFileName()||""}catch{}try{const _fc=f.split("?")[0];if(f&&(_fc.includes("/youtube/runtime/")||_fc.endsWith("scramjet.js")||_fc.endsWith("scramjet.wasm.js")||_fc.endsWith("controller.inject.js")||e.config.maskedfiles.some(m=>_fc.endsWith(m))))continue}catch{}try{s=s.replace(/https?:\\/\\/[^\\s)]+\\/youtube\\/~\\/sj\\/(?:[^/]+\\/){0,2}(https?(?:%3A|:)[^\\s):]+)/gi,(_,u)=>{try{return decodeURIComponent(u).split("?")[0]}catch{return _}})}catch{}n.push("    at "+s)}return n.join("\\n")}finally{_inSt=!1}};try{Error.prepareStackTrace=r}catch{}e.Trap("Error.prepareStackTrace",{get:()=>_usrSt||r,set(v){_usrSt="function"==typeof v?v:null;try{Error.prepareStackTrace=r}catch{}}})'
    if old_stack in text:
        text = text.replace(old_stack, new_stack, 1)

    old_unrew = 'if(!r.href.startsWith(t.prefix.href))return o.error("unrewriteurl: unexpected url",e),e;'
    new_unrew = (
        'if(!r.href.startsWith(t.prefix.href)){'
        'try{const _sm=r.pathname.match(/^\\/youtube\\/runtime\\/stream\\/([A-Za-z0-9_-]{11})$/);if(_sm)return "https://www.youtube.com/yts/stream/"+_sm[1]+".mp4"+r.search;}catch(_){}'
        'try{const _k=r.searchParams?.get("k");if(_k){let _b=_k.replace(/-/g,"+").replace(/_/g,"/");while(_b.length%4)_b+="=";return decodeURIComponent(atob(_b).split("").reverse().join(""))}}catch(_){}'
        'return r.searchParams?.get("url")||e;'
        '}'
    )
    if old_unrew in text:
        text = text.replace(old_unrew, new_unrew, 1)
    old_srcset = 'function x(e,t,r){return e.split(/ .*,/).map'
    new_srcset = 'function x(e,t,r){return e.split(",").map'
    if old_srcset in text:
        text = text.replace(old_srcset, new_srcset, 1)
    old_srcset_prop = 'credentialless:[t.HTMLIFrameElement],src:[t.HTMLImageElement,'
    new_srcset_prop = 'credentialless:[t.HTMLIFrameElement],srcset:[t.HTMLImageElement,t.HTMLSourceElement],src:[t.HTMLImageElement,'
    if old_srcset_prop in text:
        text = text.replace(old_srcset_prop, new_srcset_prop, 1)
    old_blob = 'if(e.startsWith("blob:"))return t.prefix.href+e;'
    new_blob = 'if(e.startsWith("blob:"))return l(e,t,r);'
    if old_blob in text:
        text = text.replace(old_blob, new_blob, 1)
    text = text.replace(
        'e.Trap("Node.prototype.baseURI",',
        'e.Trap(["HTMLImageElement.prototype.currentSrc","HTMLMediaElement.prototype.currentSrc"],{get:t=>(0,l.v2)(t.get(),e.context)}),e.Trap("Node.prototype.baseURI",',
        1,
    )
    old_rew = 'let l=t.interface.codecEncode(A.hash.slice(1));A.hash="";'
    new_rew = (
        'try{'
        'if(A.pathname==="/youtube/runtime/image-relay"&&(A.searchParams.has("k")||A.searchParams.has("url")))return A.href;'
        'if(A.pathname==="/youtube/runtime/gv-stream"&&(A.searchParams.has("k")||A.searchParams.has("url")))return A.href;'
        'if(A.pathname.startsWith("/youtube/runtime/stream/"))return A.href;'
        'const _sm=A.pathname.match(/^\\/yts\\/stream\\/([A-Za-z0-9_-]{11})\\.mp4$/);'
        'if(_sm)return t.prefix.origin+"/youtube/runtime/stream/"+_sm[1]+A.search;'
        'if((A.hostname.endsWith(".ytimg.com")||A.hostname==="ytimg.com"||A.hostname==="img.youtube.com"||A.hostname.endsWith(".ggpht.com")||A.hostname==="ggpht.com"||A.hostname.endsWith(".googleusercontent.com"))&&!/\\.(js|mjs|cjs|json|css|html|wasm)(\\?|$)/i.test(A.pathname)&&!o?.isModule&&o?.destination!=="script"&&o?.destination!=="worker"&&o?.destination!=="sharedworker"){'
        'return t.prefix.origin+"/youtube/runtime/image-relay?k="+btoa(encodeURIComponent(A.href).split("").reverse().join("")).replace(/\\+/g,"-").replace(/\\//g,"_").replace(/=+$/,"");'
        '}'
        '}catch(_){}'
        'let l=t.interface.codecEncode(A.hash.slice(1));A.hash="";'
    )
    if old_rew in text:
        text = text.replace(old_rew, new_rew, 1)
    old_fn_ctor = 'e.RawProxy(i.prototype,"constructor",r)'
    new_fn_ctor = 'Object.defineProperty(i.prototype,"constructor",{value:t.Function,writable:!0,enumerable:!1,configurable:!0})'
    if old_fn_ctor in text:
        text = text.replace(old_fn_ctor, new_fn_ctor, 1)
    old_nonce_rule = '{fn:()=>null,nonce:"*",csp:["iframe"],credentialless:["iframe"]}'
    new_nonce_rule = '{fn:()=>null,csp:["iframe"],credentialless:["iframe"]}'
    if old_nonce_rule in text:
        text = text.replace(old_nonce_rule, new_nonce_rule, 1)
    old_attr_iter = '"getNamedItemNS"===n?(e,t)=>r[`${e}:${t}`]:'
    new_attr_iter = '"getNamedItemNS"===n?(e,t)=>r[`${e}:${t}`]:"item"===n?e=>r[e]:n===Symbol.iterator?function*(){const _ks=(0,i.BR)(r);for(let _k=0;_k<_ks.length;_k++)yield t[_ks[_k]]}:'
    if old_attr_iter in text:
        text = text.replace(old_attr_iter, new_attr_iter, 1)
    old_perf = 'e.Trap("PerformanceEntry.prototype.name",{get(t){let r=(0,i.Qf)(t.get());return r&&r.startsWith(e.context.prefix.href)?e.unrewriteUrl(r):r}}),e.Proxy(["Performance.prototype.getEntries","Performance.prototype.getEntriesByType","Performance.prototype.getEntriesByName","PerformanceObserverEntryList.prototype.getEntries","PerformanceObserverEntryList.prototype.getEntriesByType","PerformanceObserverEntryList.prototype.getEntriesByName"],{apply(t){let r=t.call();return t.return(r.filter(t=>{for(let r of e.config.maskedfiles)if((0,i.Qf)(e.descriptors.get("PerformanceEntry.prototype.name",t)).endsWith(r))return!1;return!0}))}})'
    new_perf = 'e.Trap("PerformanceEntry.prototype.name",{get(t){let r=(0,i.Qf)(t.get());return r&&(r.startsWith(e.context.prefix.href)||r.includes("/youtube/runtime/image-relay")||r.includes("/youtube/runtime/gv-stream")||r.includes("/youtube/runtime/stream/"))?e.unrewriteUrl(r):r}}),e.Proxy(["Performance.prototype.getEntries","Performance.prototype.getEntriesByType","Performance.prototype.getEntriesByName","PerformanceObserverEntryList.prototype.getEntries","PerformanceObserverEntryList.prototype.getEntriesByType","PerformanceObserverEntryList.prototype.getEntriesByName"],{apply(t){let r=t.call();return t.return(r.filter(t=>{const _nm=(0,i.Qf)(e.descriptors.get("PerformanceEntry.prototype.name",t))||"",_nc=_nm.split("?")[0];if(_nc.includes("/youtube/runtime/scramjet")||_nc.includes("/youtube/runtime/controller")||_nc.endsWith("scramjet.wasm.js"))return!1;for(let r of e.config.maskedfiles)if(_nc.endsWith(r))return!1;return!0}))}})'
    if old_perf in text:
        text = text.replace(old_perf, new_perf, 1)
    old_wasm_init = "(0,n.QR)({module:new WebAssembly.Module(i)});"
    new_wasm_init = "0===c.length&&(0,n.QR)({module:new WebAssembly.Module(i)});"
    if old_wasm_init in text:
        text = text.replace(old_wasm_init, new_wasm_init, 1)
    preamble = (
        'try{if(typeof window!=="undefined"){'
        'try{delete window.IdentityCredential;delete Window.prototype.IdentityCredential;}catch(_){}'
        'try{delete window.FederatedCredential;delete Window.prototype.FederatedCredential;}catch(_){}'
        'try{if(window.addEventListener){const _origAdd=window.addEventListener;window.addEventListener=function(type,fn,opt){if(type==="offline")return;_origAdd.call(this,type,fn,opt);};}}catch(_){}'
        'try{Object.defineProperty(window,"onoffline",{get:()=>null,set:()=>{},configurable:true});}catch(_){}'
        '}'
        'if(typeof navigator!=="undefined"){'
        'const _np=typeof Navigator!=="undefined"&&Navigator.prototype?Navigator.prototype:Object.getPrototypeOf(navigator);'
        'if(navigator.webdriver&&_np){Object.defineProperty(_np,"webdriver",{get:()=>false,enumerable:true,configurable:true});}'
        'if(String(navigator.userAgent||"").includes("HeadlessChrome/")&&_np){'
        'const _ua=navigator.userAgent.replace(/HeadlessChrome\\//g,"Chrome/");'
        'Object.defineProperty(_np,"userAgent",{get:()=>_ua,enumerable:true,configurable:true});'
        'Object.defineProperty(_np,"appVersion",{get:()=>_ua.replace(/^Mozilla\\//,""),enumerable:true,configurable:true});'
        '}'
        'try{if(_np){Object.defineProperty(_np,"onLine",{get:()=>true,enumerable:true,configurable:true});}}catch(_){}'
        'try{if(!navigator.onLine){Object.defineProperty(navigator,"onLine",{get:()=>true,enumerable:true,configurable:true});}}catch(_){}'
        'try{const _conn={downlink:10,effectiveType:"4g",rtt:50,saveData:false,addEventListener:()=>{},removeEventListener:()=>{},dispatchEvent:()=>true};if(_np){Object.defineProperty(_np,"connection",{get:()=>_conn,enumerable:true,configurable:true});}}catch(_){}'
        '}'
        'if(typeof document!=="undefined"&&typeof location!=="undefined"&&location.pathname.includes("/youtube/~/sj/")&&location.pathname.includes("youtube.com")&&!String(document.cookie||"").includes("PREF=")){'
        'document.cookie="PREF=f6=400&hl=ja&gl=JP; path=/; max-age=31536000";'
        '}}catch(_){}\n'
    )
    if not text.startswith("try{if(typeof window") and not text.startswith("try{if(typeof navigator"):
        text = preamble + text
    return text.encode("utf-8")


_PATCHED_RUNTIME_CACHE: dict[str, bytes] = {}
_PATCHED_RUNTIME_GZIP_CACHE: dict[str, bytes] = {}


async def _ensure_patched_runtime(asset_name: str, patch_fn) -> bytes | Response:
    cached = _PATCHED_RUNTIME_CACHE.get(asset_name)
    if cached is not None:
        return cached
    import app as host_app

    resp = await host_app.scramjet_asset(asset_name)
    if getattr(resp, "status_code", 500) != 200 or not getattr(resp, "body", b""):
        return resp
    patched = patch_fn(bytes(resp.body))
    _PATCHED_RUNTIME_CACHE[asset_name] = patched
    _PATCHED_RUNTIME_GZIP_CACHE[asset_name] = gzip.compress(patched, compresslevel=5)
    return patched


async def _serve_patched_runtime(request: Request, asset_name: str, patch_fn) -> Response:
    res = await _ensure_patched_runtime(asset_name, patch_fn)
    if isinstance(res, Response):
        return res
    headers = {"Cache-Control": "public, max-age=31536000, immutable", "Vary": "Accept-Encoding"}
    ae = (request.headers.get("accept-encoding") or "").lower()
    if "gzip" in ae:
        gz = _PATCHED_RUNTIME_GZIP_CACHE.get(asset_name)
        if gz is None:
            gz = gzip.compress(res, compresslevel=5)
            _PATCHED_RUNTIME_GZIP_CACHE[asset_name] = gz
        headers["Content-Encoding"] = "gzip"
        return Response(gz, media_type="application/javascript", headers=headers)
    return Response(res, media_type="application/javascript", headers=headers)


@router.get("/youtube/runtime/scramjet.js", include_in_schema=False)
async def youtube_scramjet_js(request: Request):
    return await _serve_patched_runtime(request, "scramjet.js", _patch_scramjet_js)


@router.get("/youtube/runtime/controller.api.js", include_in_schema=False)
async def youtube_controller_api(request: Request):
    return await _serve_patched_runtime(request, "controller.api.js", _patch_controller_api)


@router.get("/youtube/runtime/controller.sw.js", include_in_schema=False)
async def youtube_controller_sw(request: Request):
    return await _serve_patched_runtime(request, "controller.sw.js", _patch_controller_sw)


@router.get("/youtube/runtime/controller.inject.js", include_in_schema=False)
async def youtube_controller_inject(request: Request):
    return await _serve_patched_runtime(request, "controller.inject.js", _patch_controller_inject)


async def _youtube_sw_override(request: Request) -> Response:
    code = f"""
importScripts('/youtube/runtime/controller.sw.js?v={RUNTIME_REV}');
const YT_MEDIA_CACHE = 'sh-yt-media-{RUNTIME_REV}';
const YT_REWRITTEN_CACHE = 'sh-yt-rewritten-{RUNTIME_REV}';
const YT_IMAGE_SUFFIXES = [
  'i.ytimg.com','s.ytimg.com','img.youtube.com','ytimg.com',
  'yt3.ggpht.com','yt4.ggpht.com','ggpht.com',
  'yt3.googleusercontent.com','yt4.googleusercontent.com','lh3.googleusercontent.com','googleusercontent.com',
  'ytimg.l.google.com','fonts.gstatic.com','www.gstatic.com','fonts.googleapis.com',
  'usercontent.goog','playables.usercontent.goog'
];
function encodeMediaKey(urlStr) {{
  try {{
    const rev = encodeURIComponent(String(urlStr)).split('').reverse().join('');
    return btoa(rev).replace(/\\+/g, '-').replace(/\\//g, '_').replace(/=+$/, '');
  }} catch {{
    return '';
  }}
}}
function decodeMediaKey(k) {{
  try {{
    let b = String(k || '').replace(/-/g, '+').replace(/_/g, '/');
    while (b.length % 4) b += '=';
    return decodeURIComponent(atob(b).split('').reverse().join(''));
  }} catch {{
    return '';
  }}
}}
function isAllowedImageHost(host) {{
  const h = String(host || '').toLowerCase().replace(/\\.$/, '');
  return YT_IMAGE_SUFFIXES.some(x => h === x || h.endsWith('.' + x));
}}
function isAllowedImageTarget(target) {{
  if (!target || !isAllowedImageHost(target.hostname)) return false;
  const h = String(target.hostname || '').toLowerCase().replace(/\\.$/, '');
  const p = String(target.pathname || '');
  if (/\\.(js|mjs|cjs|json|css|html|wasm)(\\?|$)/i.test(p)) return false;
  if (h.endsWith('gstatic.com') || h.endsWith('googleapis.com')) {{
    return /\\.(png|jpe?g|gif|webp|svg|ico|avif|bmp|woff2?|ttf|otf)(\\?|$)/i.test(p);
  }}
  return true;
}}
function decodeRepeated(value) {{
  let out = String(value || '');
  for (let i = 0; i < 5; i++) {{
    try {{ const next = decodeURIComponent(out); if (next === out) break; out = next; }} catch {{ break; }}
  }}
  return out;
}}
function extractVirtualStreamId(url) {{
  try {{
    const u = new URL(url);
    const decoded = decodeRepeated(u.pathname);
    const m = decoded.match(/\\/yts\\/stream\\/([A-Za-z0-9_-]{{11}})\\.mp4/);
    if (m) return m[1];
  }} catch {{}}
  return null;
}}
function stripScramjetParams(target) {{
  try {{
    target.pathname = target.pathname.replace(/\\?\\$[^/]*$/, '');
    for (const k of [...target.searchParams.keys()]) {{
      if (k.startsWith('$')) target.searchParams.delete(k);
    }}
  }} catch {{}}
  return target;
}}
function extractAnyVirtualUrl(url) {{
  try {{
    const u = new URL(url);
    if (u.pathname === '/youtube/runtime/image-relay') {{
      const kv = u.searchParams.get('k');
      const uv = kv ? decodeMediaKey(kv) : u.searchParams.get('url');
      if (uv) return stripScramjetParams(new URL(uv.replace(/([?&])\\$[^&#]*(?:&|$)/g, '$1').replace(/[?&]$/, '')));
    }}
    const marker = '/youtube/~/sj/';
    const i = u.pathname.indexOf(marker);
    if (i < 0) return null;
    const rawAfter = u.pathname.slice(i + marker.length);
    const directMatch = rawAfter.match(/(?:^|\\/)(https?(?:%3A|:)(?:%2F|\\/){{2}}.*)$/i);
    if (directMatch && directMatch[1]) {{
      try {{
        const decoded = decodeRepeated(directMatch[1]).replace(/([?&])\\$[^&#]*(?:&|$)/g, '$1').replace(/[?&]$/, '');
        const target = new URL(decoded);
        if (u.search) {{
          const extra = new URLSearchParams(u.search);
          for (const [k, v] of extra.entries()) {{
            if (!k.startsWith('$') && !target.searchParams.has(k)) target.searchParams.set(k, v);
          }}
        }}
        stripScramjetParams(target);
        target.hash = '';
        return target;
      }} catch {{}}
    }}
    const tail = decodeRepeated(rawAfter);
    const bits = tail.split('/');
    for (let n = Math.min(3, bits.length); n >= 0; n--) {{
      const candidate = decodeRepeated(bits.slice(n).join('/')).replace(/([?&])\\$[^&#]*(?:&|$)/g, '$1').replace(/[?&]$/, '');
      try {{
        const target = stripScramjetParams(new URL(candidate));
        target.hash = '';
        return target;
      }} catch {{}}
    }}
  }} catch {{}}
  return null;
}}
function extractVirtualTarget(url) {{
  const target = extractAnyVirtualUrl(url);
  if (target && isAllowedImageTarget(target)) return target;
  return null;
}}
function isImmutableYtStaticBundle(target) {{
  if (!target) return false;
  const h = String(target.hostname || '').toLowerCase().replace(/\\.$/, '');
  const p = target.pathname || '';
  if ((h === 'www.youtube.com' || h === 'm.youtube.com' || h === 'music.youtube.com') && (p.startsWith('/s/') || p.includes('/_/js/'))) return true;
  if (h.endsWith('.ytimg.com') && (p.startsWith('/yts/') || /\\.(js|mjs|css|woff2?|svg)(\\?|$)/i.test(p))) return true;
  if ((h === 'fonts.gstatic.com' || h === 'www.gstatic.com' || h === 'fonts.googleapis.com') && (/\\.(woff2?|ttf|svg|png|css|js)(\\?|$)/i.test(p) || p.startsWith('/s/i/') || p.startsWith('/css') || p.startsWith('/icon'))) return true;
  if (/\\.(js|mjs|css)(\\?|$)/i.test(p) && (h.endsWith('.youtube.com') || h.endsWith('.ytimg.com') || h.endsWith('.google.com') || h.endsWith('.gstatic.com'))) return true;
  return false;
}}
function checkShortCircuit(target, reqOrigin) {{
  if (!target) return null;
  const h = String(target.hostname || '').toLowerCase().replace(/\\.$/, '');
  const p = target.pathname || '';
  const corsHeaders = {{
    'Access-Control-Allow-Origin': reqOrigin || '*',
    'Access-Control-Allow-Credentials': 'true',
  }};
  if (h === 'fonts.googleapis.com' && (p === '/css' || p === '/css2')) {{
    return new Response('/* sh-fast-fonts */', {{
      status: 200,
      headers: {{'Content-Type': 'text/css; charset=utf-8', ...corsHeaders, 'Cache-Control': 'public, max-age=86400'}},
    }});
  }}
  if (h.endsWith('.googlevideo.com') || h.endsWith('.c.youtube.com')) {{
    const exp = target.searchParams.get('expire') || '';
    if (p.includes(' ') || exp === '9421968081' || exp === '1685394838') {{
      return new Response(null, {{status: 204, headers: corsHeaders}});
    }}
  }}
  if (
    (p.includes('generate_204') && h !== 'accounts.google.com' && h !== 'accounts.youtube.com') || p.includes('error_204') || p.includes('csi_204') ||
    p.startsWith('/pagead/') || p.startsWith('/api/stats/') || p.startsWith('/ptracking') ||
    h.endsWith('doubleclick.net') || h.endsWith('googlesyndication.com') ||
    h.endsWith('googleadservices.com') || h.includes('adservice.google') ||
    h.endsWith('google-analytics.com') || ((h === 'play.google.com' || h.endsWith('.play.google.com')) && p.includes('log'))
  ) {{
    return new Response(null, {{status: 204, headers: corsHeaders}});
  }}
  if (
    p.startsWith('/youtubei/v1/log_event') ||
    p.startsWith('/youtubei/v1/ads/') ||
    p.startsWith('/youtubei/v1/player/ad_break') ||
    p.includes('/get_midroll_info')
  ) {{
    return new Response('{{}}', {{
      status: 200,
      headers: {{'Content-Type': 'application/json', ...corsHeaders}},
    }});
  }}
  return null;
}}
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', event => {{
  try {{
    const reqUrlObj = new URL(event.request.url);
    if (reqUrlObj.origin === self.location.origin && (reqUrlObj.pathname.startsWith('/youtube/runtime/stream/') || reqUrlObj.pathname.startsWith('/youtube/runtime/gv-stream'))) {{
      return;
    }}
    const sjIdx = reqUrlObj.pathname.indexOf('/youtube/~/sj/');
    if (sjIdx >= 0) {{
      const afterSj = decodeRepeated(reqUrlObj.pathname.slice(sjIdx + 14));
      const blobM = afterSj.match(/(?:^|\\/)(blob:https?:\\/\\/[^?#]+)/i);
      if (blobM && blobM[1]) {{
        event.respondWith(fetch(blobM[1]).catch(() => new Response(null, {{status: 204}})));
        return;
      }}
    }}
    const anyTarget = extractAnyVirtualUrl(event.request.url);
    if (anyTarget) {{
      const reqOrigin = event.request.headers.get('Origin') || '';
      const sc = checkShortCircuit(anyTarget, reqOrigin);
      if (sc) {{
        event.respondWith(sc);
        return;
      }}
      const anyHost = String(anyTarget.hostname || '').toLowerCase().replace(/\\.$/, '');
      if (anyHost.endsWith('.googlevideo.com') || anyHost.endsWith('.c.youtube.com')) {{
        const gvRelay = new URL('/youtube/runtime/gv-stream', self.location.origin);
        gvRelay.searchParams.set('k', encodeMediaKey(anyTarget.href));
        const m = event.request.method;
        const fwdHeaders = {{}};
        const rng = event.request.headers.get('range');
        if (rng) fwdHeaders['Range'] = rng;
        const acc = event.request.headers.get('accept');
        if (acc) fwdHeaders['Accept'] = acc;
        const ct = event.request.headers.get('content-type');
        if (ct) fwdHeaders['Content-Type'] = ct;
        event.respondWith((async () => {{
          const bodyBuf = (m !== 'GET' && m !== 'HEAD' && m !== 'OPTIONS')
            ? await event.request.arrayBuffer().catch(() => null)
            : null;
          return fetch(gvRelay.href, {{
            method: m,
            headers: fwdHeaders,
            body: (bodyBuf && bodyBuf.byteLength > 0) ? bodyBuf : undefined,
            credentials: 'omit',
          }});
        }})());
        return;
      }}
    }}
    if (event.request.method === 'GET' || event.request.method === 'HEAD') {{
      const asSynthetic = (r, m) => new Response((m === 'HEAD' || [101, 204, 205, 304].includes(r.status)) ? null : r.body, {{
        status: r.status,
        statusText: r.statusText,
        headers: r.headers,
      }});
      const streamVid = extractVirtualStreamId(event.request.url);
      if (streamVid) {{
        const streamUrl = new URL('/youtube/runtime/stream/' + streamVid, self.location.origin);
        try {{
          const reqU = new URL(decodeRepeated(event.request.url));
          const rParam = reqU.searchParams.get('range');
          if (rParam) streamUrl.searchParams.set('range', rParam);
        }} catch {{}}
        const fwdHeaders = {{}};
        const rng = event.request.headers.get('range');
        if (rng) fwdHeaders['Range'] = rng;
        const acc = event.request.headers.get('accept');
        if (acc) fwdHeaders['Accept'] = acc;
        const m = event.request.method;
        event.respondWith(
          fetch(streamUrl.href, {{
            method: m,
            headers: fwdHeaders,
            credentials: 'omit',
          }})
        );
        return;
      }}
      if (anyTarget && isAllowedImageTarget(anyTarget) && event.request.destination !== 'script' && event.request.destination !== 'worker' && event.request.destination !== 'sharedworker' && event.request.destination !== 'style' && !event.request.url.includes('%24module=module') && !event.request.url.includes('$module=module')) {{
        const relay = new URL('/youtube/runtime/image-relay', self.location.origin);
        relay.searchParams.set('k', encodeMediaKey(anyTarget.href));
        const method = event.request.method;
        const acc = event.request.headers.get('accept');
        event.respondWith((async () => {{
          if (method === 'GET' && typeof caches !== 'undefined') {{
            try {{
              const cache = await caches.open(YT_MEDIA_CACHE);
              const hit = await cache.match(relay.href);
              if (hit) return asSynthetic(hit, method);
              const resp = await fetch(relay.href, {{
                method: 'GET',
                headers: acc ? {{'Accept': acc}} : {{}},
                credentials: 'omit',
              }});
              if (resp && resp.status === 200) {{
                try {{ await cache.put(relay.href, resp.clone()); }} catch {{}}
              }}
              return asSynthetic(resp, method);
            }} catch {{}}
          }}
          const resp = await fetch(relay.href, {{
            method,
            headers: acc ? {{'Accept': acc}} : {{}},
            credentials: 'omit',
          }});
          return asSynthetic(resp, method);
        }})());
        return;
      }}
    }}
    if (typeof $scramjetController !== 'undefined' && $scramjetController.shouldRoute(event)) {{
      if (event.request.method === 'GET' && anyTarget && isImmutableYtStaticBundle(anyTarget) && typeof caches !== 'undefined') {{
        const cacheKey = event.request.url;
        event.respondWith((async () => {{
          try {{
            const cache = await caches.open(YT_REWRITTEN_CACHE);
            const hit = await cache.match(cacheKey);
            if (hit) return hit;
            const resp = await $scramjetController.route(event);
            if (resp && resp.status === 200) {{
              try {{ await cache.put(cacheKey, resp.clone()); }} catch {{}}
            }}
            return resp;
          }} catch {{
            return $scramjetController.route(event);
          }}
        }})());
        return;
      }}
      event.respondWith($scramjetController.route(event));
      return;
    }}
  }} catch (error) {{
    console.error('[SchoolHub isolated SW:youtube]', error);
    event.respondWith(new Response('Internal Service Worker Error: ' + (error?.message || error), {{status: 500}}));
  }}
}});
self.addEventListener('message', event => {{
  if (event.data === 'keepalive') return;
  if (event.data === 'claim') {{
    try {{ event.waitUntil(self.clients.claim()); }} catch {{}}
    return;
  }}
}});
"""
    return Response(
        code,
        media_type="application/javascript",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Service-Worker-Allowed": "/youtube/",
        },
    )


# ---------------------------------------------------------------------------
# Fast HTTP/2 + Chrome-TLS Relay for Scramjet Transport & Stream Resolver
# ---------------------------------------------------------------------------
_CURL_SESSION: StatelessCurlSession | None = None
_CURL_SESSION_LOCK = asyncio.Lock()
_MEDIA_HTTP_CLIENT: httpx.AsyncClient | None = None
_MEDIA_HTTP_CLIENT_LOCK = asyncio.Lock()
_IMAGE_CACHE: dict[str, tuple[float, int, str, bytes, str]] = {}
_IMAGE_CACHE_TTL = 86400.0
_IMAGE_CACHE_MAX_ITEMS = 2048
_IMAGE_CACHE_MAX_BYTES = 1024 * 1024
_STATIC_ASSET_CACHE: dict[str, tuple[float, int, str, list[list[str]], bytes]] = {}
_STATIC_ASSET_TTL = 86400.0
_STATIC_ASSET_MAX_ITEMS = 2048
_STATIC_ASSET_MAX_BYTES = 8 * 1024 * 1024

_RELAY_STRIP_REQ_HEADERS = {
    "host",
    "connection",
    "keep-alive",
    "proxy-connection",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "accept-encoding",
}

_RELAY_STRIP_RESP_HEADERS = {
    "content-encoding",
    "transfer-encoding",
    "content-length",
    "connection",
    "keep-alive",
    "alt-svc",
    "strict-transport-security",
    "content-security-policy",
    "content-security-policy-report-only",
    "x-frame-options",
    "cross-origin-opener-policy",
    "cross-origin-embedder-policy",
    "cross-origin-resource-policy",
}

_DESKTOP_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


async def _get_curl_session():
    global _CURL_SESSION
    if StatelessCurlSession is None:
        return None
    if _CURL_SESSION is not None:
        return _CURL_SESSION
    async with _CURL_SESSION_LOCK:
        if _CURL_SESSION is None:
            _CURL_SESSION = StatelessCurlSession(
                impersonate="chrome131",
                default_headers=False,
                allow_redirects=False,
                timeout=25,
                max_clients=64,
            )
    return _CURL_SESSION


async def _get_media_http_client() -> httpx.AsyncClient:
    """Shared cookieless persistent HTTP client for thumbnails, icons, and video streams."""
    global _MEDIA_HTTP_CLIENT
    if _MEDIA_HTTP_CLIENT is not None and not _MEDIA_HTTP_CLIENT.is_closed:
        return _MEDIA_HTTP_CLIENT
    async with _MEDIA_HTTP_CLIENT_LOCK:
        if _MEDIA_HTTP_CLIENT is None or _MEDIA_HTTP_CLIENT.is_closed:
            limits = httpx.Limits(max_connections=300, max_keepalive_connections=150, keepalive_expiry=60.0)
            timeout = httpx.Timeout(connect=5.0, read=45.0, write=10.0, pool=10.0)
            if not (os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or os.getenv("https_proxy") or os.getenv("http_proxy")):
                transport = httpx.AsyncHTTPTransport(
                    retries=2,
                    limits=limits,
                )
                _MEDIA_HTTP_CLIENT = httpx.AsyncClient(
                    transport=transport,
                    follow_redirects=True,
                    timeout=timeout,
                )
            else:
                _MEDIA_HTTP_CLIENT = httpx.AsyncClient(
                    follow_redirects=True,
                    limits=limits,
                    timeout=timeout,
                )
    return _MEDIA_HTTP_CLIENT


def _strip_dollar_query(raw_url: str) -> str:
    if not raw_url:
        return raw_url
    cleaned = re.sub(r"([?&])(?:%24|\$)[^&#]*(?:&|$)", r"\1", raw_url)
    cleaned = re.sub(r"[?&]+(?=#|$)", "", cleaned)
    if "?" not in cleaned:
        return cleaned
    try:
        from urllib.parse import unquote
        p = urlparse(cleaned)
        if not p.query:
            return urlunparse((p.scheme, p.netloc, p.path, p.params, "", ""))
        clean_parts = [
            part for part in p.query.split("&")
            if part and not unquote(part.split("=", 1)[0]).startswith("$")
        ]
        return urlunparse((p.scheme, p.netloc, p.path, p.params, "&".join(clean_parts), ""))
    except Exception:
        return cleaned


def _encode_media_key(url: str) -> str:
    rev = quote(url, safe="-_.!~*'()")[::-1].encode("ascii")
    return base64.urlsafe_b64encode(rev).rstrip(b"=").decode("ascii")


def _decode_media_key(k: str) -> str:
    if not k:
        return ""
    try:
        s = k.strip()
        pad = (-len(s)) % 4
        if pad:
            s += "=" * pad
        raw = base64.urlsafe_b64decode(s.encode("ascii")).decode("ascii")
        return unquote(raw[::-1])
    except Exception:
        return ""


async def _serve_youtube_image_url(raw_url: str, method: str, accept_header: str = "") -> Response:
    if not raw_url:
        return Response(b"missing url", status_code=400)
    try:
        for _ in range(3):
            p_check = urlparse(raw_url)
            if p_check.path == "/youtube/runtime/image-relay":
                qs = parse_qs(p_check.query)
                k_nested = (qs.get("k") or [""])[0]
                nested = _decode_media_key(k_nested) if k_nested else (qs.get("url") or [""])[0]
                if nested:
                    raw_url = nested
                    continue
            break
        raw_url = _strip_dollar_query(raw_url)
        parsed = urlparse(raw_url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if (
            parsed.scheme not in ("http", "https")
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 80, 443)
            or not any(
                host == s or host.endswith("." + s) for s in YOUTUBE_IMAGE_SUFFIXES
            )
        ):
            return Response(b"disallowed image host", status_code=403)
    except Exception:
        return Response(b"bad image url", status_code=400)

    now = time.time()
    cached = _IMAGE_CACHE.get(raw_url)
    if cached and (now - cached[0]) < _IMAGE_CACHE_TTL:
        _, c_status, c_media_type, c_body, c_cc = cached
        body_out = b"" if method == "HEAD" else c_body
        return Response(
            body_out,
            status_code=c_status,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cross-Origin-Resource-Policy": "cross-origin",
                "Cache-Control": c_cc,
                "Content-Length": str(len(body_out)),
            },
            media_type=c_media_type,
        )

    req_headers = {
        "User-Agent": _DESKTOP_CHROME_UA,
        "Accept": accept_header
        or "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Referer": "https://www.youtube.com/",
    }
    try:
        client = await _get_media_http_client()
        resp = await client.request(method, raw_url, headers=req_headers)
        media_type = resp.headers.get("content-type") or "image/jpeg"
        body = b"" if method == "HEAD" else bytes(resp.content or b"")
        cc = resp.headers.get("cache-control") or "public, max-age=86400"
        if (
            method == "GET"
            and resp.status_code == 200
            and 0 < len(body) <= _IMAGE_CACHE_MAX_BYTES
        ):
            if len(_IMAGE_CACHE) >= _IMAGE_CACHE_MAX_ITEMS:
                oldest_k = next(iter(_IMAGE_CACHE))
                _IMAGE_CACHE.pop(oldest_k, None)
            _IMAGE_CACHE[raw_url] = (now, resp.status_code, media_type, body, cc)
        out_headers = {
            "Access-Control-Allow-Origin": "*",
            "Cross-Origin-Resource-Policy": "cross-origin",
            "Cache-Control": cc,
            "Content-Length": str(len(body)),
        }
        return Response(
            body,
            status_code=resp.status_code,
            headers=out_headers,
            media_type=media_type,
        )
    except Exception as exc:
        return Response(
            f"image error: {exc}".encode("utf-8", "ignore"), status_code=502
        )


@router.api_route(
    "/youtube/runtime/image-relay",
    methods=["GET", "HEAD"],
    include_in_schema=False,
)
async def youtube_runtime_image_relay(request: Request):
    k_val = (request.query_params.get("k") or "").strip()
    raw_url = _decode_media_key(k_val) if k_val else (request.query_params.get("url") or "").strip()
    return await _serve_youtube_image_url(
        raw_url, request.method, request.headers.get("accept") or ""
    )


_PUBLIC_HOST_CACHE: dict[tuple[str, int], float] = {}


async def _ensure_public_host(hostname: str, port: int) -> None:
    host = (hostname or "").strip().lower().rstrip(".")
    if (
        not host
        or host in ("localhost", "localhost.localdomain")
        or host.endswith((".local", ".internal", ".localhost"))
    ):
        raise PermissionError(f"blocked host {host}")
    try:
        ip_lit = ipaddress.ip_address(host)
        if (
            ip_lit.is_private
            or ip_lit.is_loopback
            or ip_lit.is_link_local
            or ip_lit.is_reserved
            or ip_lit.is_multicast
        ):
            raise PermissionError(f"non-public literal address {host}")
        return
    except ValueError:
        pass
    if "." not in host:
        raise PermissionError(f"blocked single-label host {host}")

    key = (host, port)
    now = time.time()
    cached = _PUBLIC_HOST_CACHE.get(key)
    if cached and (now - cached) < 300.0:
        return
    _trusted_suffixes = (
        "youtube.com",
        "youtube-nocookie.com",
        "youtu.be",
        "googlevideo.com",
        "ytimg.com",
        "ggpht.com",
        "google.com",
        "gstatic.com",
        "googleapis.com",
        "googleusercontent.com",
        "doubleclick.net",
        "googlesyndication.com",
        "googleadservices.com",
        "gvt1.com",
        "gvt2.com",
        "1e100.net",
        "usercontent.goog",
        "playables.usercontent.goog",
        "goog",
    )
    if any(host == sfx or host.endswith("." + sfx) for sfx in _trusted_suffixes):
        _PUBLIC_HOST_CACHE[key] = now
        return
    if not (os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or os.getenv("https_proxy") or os.getenv("http_proxy")):
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.run_in_executor(
                None, lambda: socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
            )
        except Exception:
            infos = await loop.run_in_executor(
                None, lambda: socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
            )
        for _, _, _, _, sockaddr in infos:
            ip_text = sockaddr[0]
            try:
                ip = ipaddress.ip_address(ip_text)
            except ValueError:
                continue
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_multicast
            ):
                raise PermissionError(f"non-public address {ip_text}")
    _PUBLIC_HOST_CACHE[key] = now


def _is_cacheable_static_asset(method: str, url: str) -> bool:
    if method != "GET":
        return False
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        path = parsed.path or ""
        if host in ("www.youtube.com", "m.youtube.com", "music.youtube.com") and (
            path.startswith("/s/") or "/_/js/" in path
        ):
            return True
        if host.endswith(".ytimg.com") and (
            path.startswith("/yts/") or path.endswith((".js", ".css", ".woff2", ".woff", ".ttf", ".svg", ".png", ".jpg", ".webp"))
        ):
            return True
        if host in ("fonts.gstatic.com", "www.gstatic.com", "fonts.googleapis.com") and (
            path.endswith((".woff2", ".woff", ".ttf", ".svg", ".png", ".css", ".js"))
            or path.startswith(("/s/i/", "/css", "/icon"))
        ):
            return True
        if path.endswith((".js", ".css", ".woff2", ".woff", ".ttf")) and (
            host.endswith(".youtube.com") or host.endswith(".ytimg.com") or host.endswith(".gstatic.com")
        ):
            return True
    except Exception:
        pass
    return False


def _pack_relay_response(
    status: int,
    status_text: str,
    headers_list: list[list[str]],
    body: bytes,
    accept_encoding: str = "",
) -> Response:
    meta_bytes = json.dumps(
        {
            "status": status,
            "statusText": status_text,
            "headers": headers_list,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    payload = struct.pack(">I", len(meta_bytes)) + meta_bytes + body
    headers = {
        "Cache-Control": "private, no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
        "Vary": "Accept-Encoding",
    }
    if "gzip" in (accept_encoding or "").lower() and len(payload) >= 1024:
        try:
            gz_payload = gzip.compress(payload, compresslevel=4)
            if len(gz_payload) < len(payload):
                headers["Content-Encoding"] = "gzip"
                return Response(
                    gz_payload,
                    media_type="application/octet-stream",
                    headers=headers,
                )
        except Exception:
            pass
    return Response(
        payload,
        media_type="application/octet-stream",
        headers=headers,
    )


# ---------------------------------------------------------------------------
# Direct Video Stream Resolver & Proxy (/youtube/runtime/stream/{video_id})
# ---------------------------------------------------------------------------
# Maps video_id -> (timestamp, resolved_url, total_bytes, duration_sec)
_STREAM_META_CACHE: dict[str, tuple[float, str, int, float]] = {}
_STREAM_META_TTL = 1800.0

_COMPANION_STREAM_ENDPOINTS = (
    "https://invid-api.poketube.fun/companion/latest_version?id={vid}&itag=18&local=true",
)


async def _resolve_android_vr_stream_info(video_id: str) -> tuple[str, int, float]:
    session = await _get_curl_session()
    payload = {
        "videoId": video_id,
        "contentCheckOk": True,
        "racyCheckOk": True,
        "context": {
            "client": {
                "clientName": "ANDROID_VR",
                "clientVersion": "1.60.19",
                "deviceMake": "Oculus",
                "deviceModel": "Quest 3",
                "osName": "Android",
                "osVersion": "12L",
                "androidSdkVersion": 32,
                "hl": "ja",
                "gl": "JP",
            }
        },
    }
    headers = {
        "Content-Type": "application/json",
        "User-Agent": (
            "com.google.android.apps.youtube.vr.oculus/1.60.19 "
            "(Linux; U; Android 12L; eureka-user Build/SQ3A.220605.009.A1) gzip"
        ),
        "X-YouTube-Client-Name": "28",
        "X-YouTube-Client-Version": "1.60.19",
    }
    try:
        if session is not None:
            r = await session.post(
                "https://www.youtube.com/youtubei/v1/player?prettyPrint=false",
                headers=headers,
                json=payload,
                timeout=6,
            )
            data = r.json()
        else:
            client = await _get_media_http_client()
            r = await client.post(
                "https://www.youtube.com/youtubei/v1/player?prettyPrint=false",
                headers=headers,
                json=payload,
                timeout=6,
            )
            data = r.json()
        sd = data.get("streamingData") or {}
        vd = data.get("videoDetails") or {}
        for fmt in sd.get("formats") or []:
            u = fmt.get("url")
            if u and "mp4" in (fmt.get("mimeType") or ""):
                clen = int(fmt.get("contentLength") or 0)
                dur_ms = int(fmt.get("approxDurationMs") or 0)
                dur_s = (dur_ms / 1000.0) if dur_ms > 0 else float(vd.get("lengthSeconds") or 0)
                return u, clen, dur_s
    except Exception:
        pass
    return "", 0, 0.0


def _parse_mp4_mvhd_duration(data: bytes) -> float:
    """Extract exact video duration in seconds from the MP4 mvhd atom in the initial header chunk."""
    if not data:
        return 0.0
    idx = data.find(b"mvhd")
    if idx < 0 or idx + 24 > len(data):
        return 0.0
    try:
        version = data[idx + 4]
        if version == 0:
            timescale, duration = struct.unpack(">II", data[idx + 16 : idx + 24])
        elif version == 1 and idx + 36 <= len(data):
            timescale, duration = struct.unpack(">IQ", data[idx + 24 : idx + 36])
        else:
            return 0.0
        if timescale > 0 and duration > 0:
            dur = duration / float(timescale)
            if 0.5 <= dur <= 86400.0:
                return round(dur, 3)
    except Exception:
        pass
    return 0.0


def _parse_mp4_total_len(data: bytes) -> int:
    """Extract exact total MP4 file byte length from top-level ftyp + moov + mdat box headers."""
    pos = 0
    n = len(data)
    try:
        while pos + 8 <= n:
            box_size = struct.unpack(">I", data[pos : pos + 4])[0]
            box_type = data[pos + 4 : pos + 8]
            header_len = 8
            if box_size == 1:
                if pos + 16 > n:
                    break
                box_size = struct.unpack(">Q", data[pos + 8 : pos + 16])[0]
                header_len = 16
            elif box_size == 0:
                break
            if box_size < header_len:
                break
            box_end = pos + box_size
            if box_type == b"mdat":
                return box_end
            pos = box_end
    except Exception:
        pass
    return 0


def _calc_mp4_prewarm_target(data: bytes, total_len: int) -> int:
    """Inspect initial MP4 atoms (ftyp, moov, etc.) to determine how many bytes cover the full moov atom + initial mdat."""
    default_target = 512 * 1024
    max_target = 1024 * 1024
    pos = 0
    n = len(data)
    try:
        while pos + 8 <= n:
            box_size = struct.unpack(">I", data[pos : pos + 4])[0]
            box_type = data[pos + 4 : pos + 8]
            header_len = 8
            if box_size == 1:
                if pos + 16 > n:
                    break
                box_size = struct.unpack(">Q", data[pos + 8 : pos + 16])[0]
                header_len = 16
            elif box_size == 0:
                break
            if box_size < header_len:
                break
            box_end = pos + box_size
            if box_type == b"moov":
                target = box_end + 256 * 1024
                if total_len > 0:
                    target = min(target, total_len)
                return max(default_target, min(target, max_target))
            if box_type == b"mdat":
                break
            pos = box_end
    except Exception:
        pass
    if total_len > 0:
        return min(default_target, total_len)
    return default_target


class _StreamHeadPrewarm:
    __slots__ = (
        "vid",
        "final_url",
        "created_at",
        "total_len",
        "dur_s",
        "media_type",
        "buf",
        "target_bytes",
        "headers_ready",
        "cond",
        "ok",
        "done",
        "task",
    )

    def __init__(self, vid: str, final_url: str):
        self.vid = vid
        self.final_url = final_url
        self.created_at = time.time()
        self.total_len = 0
        self.dur_s = 0.0
        self.media_type = "video/mp4"
        self.buf = bytearray()
        self.target_bytes = 512 * 1024
        self.headers_ready = asyncio.Event()
        self.cond = asyncio.Condition()
        self.ok = False
        self.done = False
        self.task: asyncio.Task | None = None


_STREAM_HEAD_CACHE: dict[str, _StreamHeadPrewarm] = {}
_PREWARM_REQUESTED_VIDS: set[str] = set()


async def _run_stream_head_prewarm(entry: _StreamHeadPrewarm):
    vid = entry.vid
    init_url = entry.final_url
    try:
        client = await _get_media_http_client()
        for attempt in range(3):
            if attempt == 0:
                req_url = entry.final_url or init_url
            elif attempt == 1:
                u_vr, c_vr, d_vr = await _resolve_android_vr_stream_info(vid)
                if not u_vr:
                    u_vr, c_vr, d_vr = await _resolve_piped_stream_info(vid)
                req_url = u_vr or init_url
                if c_vr > 0 and entry.total_len <= 0:
                    entry.total_len = c_vr
                if d_vr > 0 and entry.dur_s <= 0:
                    entry.dur_s = d_vr
            else:
                req_url = init_url

            ua_hdr = (
                "com.google.android.apps.youtube.vr.oculus/1.60.19 (Linux; U; Android 12L; eureka-user Build/SQ3A.220605.009.A1) gzip"
                if "c=ANDROID_VR" in req_url
                else _DESKTOP_CHROME_UA
            )
            try:
                # Always send a bounded Range (bytes=0-524287) so invid-api.poketube.fun
                # completes cleanly without aborting an open-ended full-file download.
                req_end = entry.target_bytes - 1
                async with client.stream(
                    "GET",
                    req_url,
                    headers={
                        "User-Agent": ua_hdr,
                        "Accept": "*/*",
                        "Accept-Encoding": "identity",
                        "Referer": "https://www.youtube.com/",
                        "Range": f"bytes=0-{req_end}",
                    },
                    timeout=10.0,
                ) as resp:
                    ctype = (resp.headers.get("content-type") or "").lower()
                    if resp.status_code not in (200, 206) or (
                        "video" not in ctype and "octet-stream" not in ctype
                    ):
                        continue

                    entry.final_url = str(resp.url)
                    entry.media_type = resp.headers.get("content-type") or "video/mp4"
                    cr = resp.headers.get("content-range") or ""
                    clen = 0
                    if "/" in cr:
                        tail = cr.rsplit("/", 1)[-1].strip()
                        if tail.isdigit():
                            clen = int(tail)
                    if not clen and not resp.headers.get("content-encoding") and resp.status_code == 200:
                        cl_hdr = resp.headers.get("content-length") or ""
                        if cl_hdr.isdigit():
                            clen = int(cl_hdr)
                    qs = parse_qs(urlparse(entry.final_url).query)
                    if not clen and qs.get("clen"):
                        try:
                            clen = int(qs["clen"][0])
                        except Exception:
                            pass
                    dur_s = entry.dur_s
                    if dur_s <= 0:
                        try:
                            dur_s = float((qs.get("dur") or ["0"])[0])
                        except Exception:
                            pass

                    if clen > 0:
                        entry.total_len = clen
                    if dur_s > 0:
                        entry.dur_s = dur_s
                    entry.ok = True
                    if entry.total_len > 0:
                        cached = _STREAM_META_CACHE.get(vid)
                        _STREAM_META_CACHE[vid] = (
                            time.time(),
                            entry.final_url,
                            entry.total_len,
                            entry.dur_s if entry.dur_s > 0 else (cached[3] if (cached and len(cached) > 3) else 0.0),
                        )
                        entry.headers_ready.set()

                    mvhd_checked = False
                    async for chunk in resp.aiter_bytes():
                        if not chunk:
                            continue
                        entry.buf.extend(chunk)
                        if entry.total_len <= 0 and len(entry.buf) >= 32:
                            parsed_total = _parse_mp4_total_len(bytes(entry.buf))
                            if parsed_total > 0:
                                entry.total_len = parsed_total
                                cached = _STREAM_META_CACHE.get(vid)
                                _STREAM_META_CACHE[vid] = (
                                    time.time(),
                                    entry.final_url,
                                    entry.total_len,
                                    entry.dur_s if entry.dur_s > 0 else (cached[3] if (cached and len(cached) > 3) else 0.0),
                                )
                        if not mvhd_checked and len(entry.buf) >= 512:
                            mvhd_checked = True
                            if entry.dur_s <= 0:
                                parsed_d = _parse_mp4_mvhd_duration(bytes(entry.buf[:32768]))
                                if parsed_d > 0:
                                    entry.dur_s = parsed_d
                                    cached = _STREAM_META_CACHE.get(vid)
                                    if cached:
                                        _STREAM_META_CACHE[vid] = (cached[0], cached[1], cached[2], parsed_d)
                        if not entry.headers_ready.is_set() and entry.total_len > 0 and len(entry.buf) >= 1024:
                            entry.headers_ready.set()
                        async with entry.cond:
                            entry.cond.notify_all()
                        if len(entry.buf) >= entry.target_bytes:
                            break
                    if len(entry.buf) > 0:
                        if entry.total_len <= 0:
                            parsed_total = _parse_mp4_total_len(bytes(entry.buf))
                            if parsed_total > 0:
                                entry.total_len = parsed_total
                        if entry.total_len > 0:
                            cached = _STREAM_META_CACHE.get(vid)
                            _STREAM_META_CACHE[vid] = (
                                time.time(),
                                entry.final_url,
                                entry.total_len,
                                entry.dur_s if entry.dur_s > 0 else (cached[3] if (cached and len(cached) > 3) else 0.0),
                            )
                        entry.headers_ready.set()
                        break
            except Exception:
                if len(entry.buf) > 0:
                    break
                continue
    except Exception:
        pass
    finally:
        entry.done = True
        entry.headers_ready.set()
        try:
            async with entry.cond:
                entry.cond.notify_all()
        except Exception:
            pass


def _ensure_stream_head_prewarm(vid: str, final_url: str) -> _StreamHeadPrewarm | None:
    if not vid or not final_url:
        return None
    existing = _STREAM_HEAD_CACHE.get(vid)
    if existing and (time.time() - existing.created_at) < 300.0 and (not existing.done or existing.ok):
        return existing
    while len(_STREAM_HEAD_CACHE) >= 16:
        oldest_k = next(iter(_STREAM_HEAD_CACHE))
        _STREAM_HEAD_CACHE.pop(oldest_k, None)
    entry = _StreamHeadPrewarm(vid, final_url)
    _STREAM_HEAD_CACHE[vid] = entry
    entry.task = asyncio.create_task(_run_stream_head_prewarm(entry))
    return entry


async def _probe_single_companion(client, url: str) -> tuple[str, int, float]:
    # Fast path: resolve the 302 redirect from invid-api.poketube.fun without opening & aborting
    # a full multi-hundred-MB Range stream before the player or prewarmer connects.
    try:
        resp_hop = await client.get(
            url,
            headers={
                "User-Agent": _DESKTOP_CHROME_UA,
                "Accept": "*/*",
                "Accept-Encoding": "identity",
                "Referer": "https://www.youtube.com/",
            },
            follow_redirects=False,
            timeout=4.5,
        )
        if resp_hop.status_code in (301, 302, 303, 307, 308):
            loc = resp_hop.headers.get("location") or ""
            if loc and ("videoplayback" in loc or "googlevideo" in loc):
                final_url = urljoin(str(resp_hop.url), loc)
                qs = parse_qs(urlparse(final_url).query)
                clen = 0
                if qs.get("clen"):
                    try:
                        clen = int(qs["clen"][0])
                    except Exception:
                        pass
                dur_s = 0.0
                try:
                    dur_s = float((qs.get("dur") or ["0"])[0])
                except Exception:
                    pass
                return final_url, clen, dur_s
    except Exception:
        pass

    try:
        async with client.stream(
            "GET",
            url,
            headers={
                "User-Agent": _DESKTOP_CHROME_UA,
                "Accept": "*/*",
                "Accept-Encoding": "identity",
                "Referer": "https://www.youtube.com/",
                "Range": "bytes=0-",
            },
            timeout=5.5,
        ) as resp:
            ctype = (resp.headers.get("content-type") or "").lower()
            if resp.status_code not in (200, 206) or (
                "video" not in ctype and "octet-stream" not in ctype
            ):
                return "", 0, 0.0
            final_url = str(resp.url)
            clen = 0
            cr = resp.headers.get("content-range") or ""
            if "/" in cr:
                tail = cr.rsplit("/", 1)[-1].strip()
                if tail.isdigit():
                    clen = int(tail)
            if not clen and not resp.headers.get("content-encoding") and resp.status_code == 200:
                cl_hdr = resp.headers.get("content-length") or ""
                if cl_hdr.isdigit():
                    clen = int(cl_hdr)
            qs = parse_qs(urlparse(final_url).query)
            if not clen and qs.get("clen"):
                try:
                    clen = int(qs["clen"][0])
                except Exception:
                    pass
            dur_s = 0.0
            try:
                dur_s = float((qs.get("dur") or ["0"])[0])
            except Exception:
                pass
            if dur_s <= 0:
                try:
                    buf = bytearray()
                    async for chunk in resp.aiter_bytes(4096):
                        buf.extend(chunk)
                        if len(buf) >= 4096 or b"mvhd" in buf:
                            if len(buf) >= 512:
                                break
                    dur_s = _parse_mp4_mvhd_duration(bytes(buf))
                except Exception:
                    pass
            if final_url:
                return final_url, clen, dur_s
    except Exception:
        pass
    return "", 0, 0.0


async def _probe_companion_stream_info(video_id: str) -> tuple[str, int, float]:
    ep_url = _COMPANION_STREAM_ENDPOINTS[0].format(vid=video_id)
    entry = _ensure_stream_head_prewarm(video_id, ep_url)
    if entry is not None:
        try:
            await asyncio.wait_for(entry.headers_ready.wait(), timeout=7.5)
            if entry.ok and entry.final_url:
                return entry.final_url, entry.total_len, entry.dur_s
        except Exception:
            pass
    return "", 0, 0.0


async def _resolve_wakame_stream_info(video_id: str) -> tuple[str, int, float]:
    try:
        import wakame_stream
        data = await wakame_stream.STREAM_MANAGER.resolve(video_id)
        u = ""
        for item in data.get("progressiveStreams") or []:
            if item.get("url"):
                u = item["url"]
                break
        if not u:
            u = data.get("streamUrl") or ""
        if u:
            return u, 0, 0.0
    except Exception:
        pass
    return "", 0, 0.0


async def _resolve_piped_stream_info(video_id: str) -> tuple[str, int, float]:
    piped_endpoints = (
        "https://api.piped.private.coffee/streams/{vid}",
        "https://pipedapi.ducks.party/streams/{vid}",
    )
    try:
        client = await _get_media_http_client()
        for ep in piped_endpoints:
            try:
                url = ep.format(vid=video_id)
                resp = await client.get(url, timeout=3.5)
                if resp.status_code == 200:
                    data = resp.json()
                    dur_s = float(data.get("duration") or 0.0)
                    streams = data.get("videoStreams") or []
                    for s in streams:
                        if s.get("format") == "MPEG_4" and not s.get("videoOnly") and s.get("url"):
                            u = s["url"]
                            clen = max(0, int(s.get("contentLength") or 0))
                            return u, clen, dur_s
                    for s in streams:
                        if not s.get("videoOnly") and s.get("url"):
                            u = s["url"]
                            clen = max(0, int(s.get("contentLength") or 0))
                            return u, clen, dur_s
            except Exception:
                continue
    except Exception:
        pass
    return "", 0, 0.0


_INFLIGHT_STREAM_RESOLVES: dict[str, asyncio.Future] = {}


async def _refresh_total_length(vid: str, final_url: str):
    try:
        client = await _get_media_http_client()
        async with client.stream(
            "GET",
            final_url,
            headers={
                "User-Agent": _DESKTOP_CHROME_UA,
                "Accept": "*/*",
                "Accept-Encoding": "identity",
                "Referer": "https://www.youtube.com/",
                "Range": "bytes=0-",
            },
            timeout=5.5,
        ) as resp:
            if resp.status_code in (200, 206):
                clen = 0
                cr = resp.headers.get("content-range") or ""
                if "/" in cr:
                    tail = cr.rsplit("/", 1)[-1].strip()
                    if tail.isdigit():
                        clen = int(tail)
                if not clen and not resp.headers.get("content-encoding") and resp.status_code == 200:
                    cl_hdr = resp.headers.get("content-length") or ""
                    if cl_hdr.isdigit():
                        clen = int(cl_hdr)
                dur_s = 0.0
                try:
                    buf = bytearray()
                    async for chunk in resp.aiter_bytes(4096):
                        buf.extend(chunk)
                        if len(buf) >= 4096:
                            break
                    dur_s = _parse_mp4_mvhd_duration(bytes(buf))
                except Exception:
                    pass
                if clen > 0 or dur_s > 0:
                    cached = _STREAM_META_CACHE.get(vid)
                    if cached:
                        _STREAM_META_CACHE[vid] = (
                            cached[0],
                            cached[1],
                            clen if clen > 0 else cached[2],
                            dur_s if dur_s > 0 else cached[3],
                        )
                    else:
                        _STREAM_META_CACHE[vid] = (time.time(), final_url, clen, dur_s)
    except Exception:
        pass


async def _resolve_stream_info_impl(video_id: str) -> tuple[str, int, float]:
    # Prioritize companion itag=18 muxed MP4 stream first so _STREAM_HEAD_CACHE and _STREAM_META_CACHE stay 100% consistent
    comp_task = asyncio.create_task(_probe_companion_stream_info(video_id))
    url, clen, dur_s = "", 0, 0.0

    try:
        u, c, d = await asyncio.wait_for(comp_task, timeout=6.5)
        if u:
            url, clen, dur_s = u, max(0, c), d
    except Exception:
        pass

    if not url:
        piped_task = asyncio.create_task(_resolve_piped_stream_info(video_id))
        vr_task = asyncio.create_task(_resolve_android_vr_stream_info(video_id))
        tasks = {piped_task, vr_task}
        try:
            for fut in asyncio.as_completed(tasks, timeout=4.5):
                try:
                    u, c, d = await fut
                    if u:
                        url, clen, dur_s = u, max(0, c), d
                        break
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()

    if not url:
        try:
            url, clen, dur_s = await asyncio.wait_for(_resolve_wakame_stream_info(video_id), timeout=3.5)
        except Exception:
            pass

    if url:
        head_e = _STREAM_HEAD_CACHE.get(video_id)
        if clen <= 0 and head_e and head_e.total_len > 0:
            clen = head_e.total_len
        if dur_s <= 0 and head_e and head_e.dur_s > 0:
            dur_s = head_e.dur_s
        existing_c = _STREAM_META_CACHE.get(video_id)
        if clen <= 0 and existing_c and existing_c[2] > 0:
            clen = existing_c[2]
        if dur_s <= 0 and existing_c and len(existing_c) > 3 and existing_c[3] > 0:
            dur_s = existing_c[3]
        _STREAM_META_CACHE[video_id] = (time.time(), url, clen, dur_s)
        if video_id in _PREWARM_REQUESTED_VIDS:
            _PREWARM_REQUESTED_VIDS.discard(video_id)
            _ensure_stream_head_prewarm(video_id, url)
    return url, clen, dur_s


async def _resolve_stream_info(video_id: str, force_refresh: bool = False) -> tuple[str, int, float]:
    if not force_refresh:
        cached = _STREAM_META_CACHE.get(video_id)
        if cached and (time.time() - cached[0]) < _STREAM_META_TTL:
            clen_c = cached[2]
            if clen_c <= 0:
                he = _STREAM_HEAD_CACHE.get(video_id)
                if he and he.total_len > 0:
                    clen_c = he.total_len
                    _STREAM_META_CACHE[video_id] = (cached[0], cached[1], clen_c, cached[3])
            return cached[1], clen_c, cached[3]
        if video_id in _INFLIGHT_STREAM_RESOLVES:
            try:
                return await asyncio.shield(_INFLIGHT_STREAM_RESOLVES[video_id])
            except Exception:
                pass

    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    _INFLIGHT_STREAM_RESOLVES[video_id] = fut
    try:
        res = await _resolve_stream_info_impl(video_id)
        if not fut.done():
            fut.set_result(res)
        return res
    except Exception:
        if not fut.done():
            fut.set_result(("", 0, 0.0))
        return "", 0, 0.0
    finally:
        _INFLIGHT_STREAM_RESOLVES.pop(video_id, None)


@router.api_route(
    "/youtube/runtime/stream/{video_id}",
    methods=["GET", "HEAD"],
    include_in_schema=False,
)
async def youtube_runtime_stream(video_id: str, request: Request):
    vid = (video_id or "").strip()
    if not VIDEO_ID_RE.match(vid):
        return Response(b"invalid video id", status_code=400)

    fast_primary = f"https://invid-api.poketube.fun/companion/latest_version?id={vid}&itag=18&local=true"
    if request.query_params.get("prewarm") == "1":
        _PREWARM_REQUESTED_VIDS.add(vid)
        cached_m = _STREAM_META_CACHE.get(vid)
        _ensure_stream_head_prewarm(vid, cached_m[1] if (cached_m and cached_m[1]) else fast_primary)
        if not cached_m:
            asyncio.create_task(_resolve_stream_info(vid))
        return Response(b"", status_code=204, headers={"Access-Control-Allow-Origin": "*"})

    client_range = request.headers.get("range")
    q_range = request.query_params.get("range")
    if not client_range and q_range:
        client_range = f"bytes={q_range}" if not q_range.startswith("bytes=") else q_range

    req_start_b = 0
    req_end_b: int | None = None
    if client_range:
        m_cr_in = re.match(r"bytes=(\d+)-(\d*)", client_range)
        if m_cr_in:
            req_start_b = int(m_cr_in.group(1))
            if m_cr_in.group(2):
                req_end_b = int(m_cr_in.group(2))

    client = await _get_media_http_client()

    # Always ensure head prewarm is running or completed so total_len and initial moov/mdat are available
    head_entry = _STREAM_HEAD_CACHE.get(vid)
    if head_entry is None:
        cached_m = _STREAM_META_CACHE.get(vid)
        head_entry = _ensure_stream_head_prewarm(
            vid, cached_m[1] if (cached_m and cached_m[1]) else fast_primary
        )
    if head_entry and (time.time() - head_entry.created_at) < 300.0:
        if not head_entry.headers_ready.is_set():
            try:
                await asyncio.wait_for(head_entry.headers_ready.wait(), timeout=8.0)
            except Exception:
                pass
        if head_entry.ok and (head_entry.total_len > 0 or len(head_entry.buf) > 0):
            if head_entry.total_len <= 0 and len(head_entry.buf) > 0:
                p_len = _parse_mp4_total_len(bytes(head_entry.buf))
                if p_len > 0:
                    head_entry.total_len = p_len
                    cached_m = _STREAM_META_CACHE.get(vid)
                    if cached_m:
                        _STREAM_META_CACHE[vid] = (cached_m[0], cached_m[1], p_len, cached_m[3])
            total_b = head_entry.total_len if head_entry.total_len > 0 else (len(head_entry.buf) + 1048576)
            total_str = str(head_entry.total_len) if head_entry.total_len > 0 else "*"
            out_headers = {
                "Accept-Ranges": "bytes",
                "Cache-Control": "public, max-age=3600, immutable",
                "Access-Control-Allow-Origin": "*",
            }
            if request.method == "HEAD":
                eff_end = req_end_b if (req_end_b is not None and req_end_b < total_b) else (total_b - 1)
                eff_len = max(0, eff_end - req_start_b + 1)
                if client_range:
                    out_headers["Content-Range"] = f"bytes {req_start_b}-{eff_end}/{total_str}"
                out_headers["Content-Length"] = str(eff_len)
                return Response(
                    b"",
                    status_code=206 if client_range else 200,
                    headers=out_headers,
                    media_type=head_entry.media_type,
                )

            if req_start_b < len(head_entry.buf) or (not head_entry.done and req_start_b < head_entry.target_bytes):
                # Cap each HTTP 206 response to the prewarmed buffer target (512KB-1MB) so Google Frontend
                # flushes the complete 206 Partial Content response immediately without buffering full videos.
                cap_end = min(total_b - 1, max(len(head_entry.buf), head_entry.target_bytes) - 1)
                if req_end_b is not None:
                    want_end = min(req_end_b, cap_end)
                else:
                    want_end = cap_end

                deadline = time.monotonic() + 8.0
                while len(head_entry.buf) <= want_end and not head_entry.done:
                    rem = deadline - time.monotonic()
                    if rem <= 0:
                        break
                    try:
                        async with head_entry.cond:
                            if len(head_entry.buf) <= want_end and not head_entry.done:
                                await asyncio.wait_for(head_entry.cond.wait(), timeout=min(0.4, rem))
                    except Exception:
                        pass

                avail = len(head_entry.buf)
                if avail > req_start_b:
                    if head_entry.total_len <= 0:
                        p_len = _parse_mp4_total_len(bytes(head_entry.buf))
                        if p_len > 0:
                            head_entry.total_len = p_len
                            total_str = str(p_len)
                            cached_m = _STREAM_META_CACHE.get(vid)
                            if cached_m:
                                _STREAM_META_CACHE[vid] = (cached_m[0], cached_m[1], p_len, cached_m[3])
                    slice_end = min(avail, want_end + 1)
                    body_bytes = bytes(head_entry.buf[req_start_b:slice_end])
                    actual_end = req_start_b + len(body_bytes) - 1
                    out_headers["Content-Range"] = f"bytes {req_start_b}-{actual_end}/{total_str}"
                    out_headers["Content-Length"] = str(len(body_bytes))
                    return Response(
                        body_bytes,
                        status_code=206,
                        headers=out_headers,
                        media_type=head_entry.media_type,
                    )

    max_chunk = 1048576  # 1 MB bounded chunk per HTTP 206 response
    for attempt in range(2):
        cached = _STREAM_META_CACHE.get(vid)
        resolved_url = cached[1] if (cached and (time.time() - cached[0]) < _STREAM_META_TTL) else ""
        known_total = 0
        if head_entry and head_entry.total_len > 0:
            known_total = head_entry.total_len
        elif cached and cached[2] > 0:
            known_total = cached[2]

        candidates: list[str] = []
        if resolved_url:
            candidates.append(resolved_url)
        if head_entry and head_entry.final_url and head_entry.final_url not in candidates:
            candidates.append(head_entry.final_url)
        if fast_primary not in candidates:
            candidates.append(fast_primary)

        if attempt == 1:
            _STREAM_META_CACHE.pop(vid, None)
            u_vr, c_vr, d_vr = await _resolve_android_vr_stream_info(vid)
            if not u_vr:
                u_vr, c_vr, d_vr = await _resolve_piped_stream_info(vid)
            if u_vr and u_vr not in candidates:
                candidates.insert(0, u_vr)
            if c_vr > 0 and known_total <= 0:
                known_total = c_vr

        if known_total > 0 and req_start_b >= known_total:
            return Response(
                b"",
                status_code=416,
                headers={
                    "Content-Range": f"bytes */{known_total}",
                    "Access-Control-Allow-Origin": "*",
                },
            )

        for target_url in candidates:
            try:
                ua_hdr = (
                    "com.google.android.apps.youtube.vr.oculus/1.60.19 (Linux; U; Android 12L; eureka-user Build/SQ3A.220605.009.A1) gzip"
                    if "c=ANDROID_VR" in target_url
                    else _DESKTOP_CHROME_UA
                )
                if req_end_b is not None:
                    eff_req_end = min(req_end_b, req_start_b + max_chunk - 1)
                else:
                    eff_req_end = req_start_b + max_chunk - 1
                if known_total > 0:
                    eff_req_end = min(eff_req_end, known_total - 1)

                req_headers = {
                    "User-Agent": ua_hdr,
                    "Accept": "*/*",
                    "Accept-Encoding": "identity",
                    "Referer": "https://www.youtube.com/",
                    "Range": f"bytes={req_start_b}-{eff_req_end}",
                }

                req = client.build_request(request.method, target_url, headers=req_headers, timeout=12.0)
                resp = await client.send(req, stream=True)
                if resp.status_code == 416 or (resp.status_code == 400 and req_start_b > 0):
                    await resp.aclose()
                    eof_total = known_total if known_total > 0 else req_start_b
                    return Response(
                        b"",
                        status_code=416,
                        headers={
                            "Content-Range": f"bytes */{eof_total}",
                            "Access-Control-Allow-Origin": "*",
                        },
                    )
                ctype = (resp.headers.get("content-type") or "").lower()
                if resp.status_code not in (200, 206) or (
                    "video" not in ctype and "octet-stream" not in ctype
                ):
                    if target_url == resolved_url and resp.status_code in (403, 410):
                        _STREAM_META_CACHE.pop(vid, None)
                    await resp.aclose()
                    continue

                final_url = str(resp.url)
                cr_val = resp.headers.get("content-range") or ""
                m_cr = re.match(r"bytes\s+(\d+)-(\d+)/(.*)", cr_val)
                if m_cr:
                    tail_total = m_cr.group(3).strip()
                    if tail_total.isdigit() and int(tail_total) > 0:
                        known_total = int(tail_total)

                if known_total <= 0:
                    try:
                        qs_f = parse_qs(urlparse(final_url).query)
                        if qs_f.get("clen") and str(qs_f["clen"][0]).isdigit():
                            known_total = int(qs_f["clen"][0])
                    except Exception:
                        pass

                cached_dur = cached[3] if (cached and len(cached) > 3) else 0.0
                if cached_dur <= 0:
                    try:
                        qs = parse_qs(urlparse(final_url).query)
                        cached_dur = float((qs.get("dur") or ["0"])[0])
                    except Exception:
                        pass

                if final_url:
                    _STREAM_META_CACHE[vid] = (time.time(), final_url, known_total, cached_dur)

                media_type = resp.headers.get("content-type") or "video/mp4"
                out_headers = {
                    "Accept-Ranges": "bytes",
                    "Cache-Control": "public, max-age=3600, immutable",
                    "Access-Control-Allow-Origin": "*",
                }
                for h_name in ("last-modified", "etag"):
                    val = resp.headers.get(h_name)
                    if val:
                        out_headers[h_name.title()] = val

                if request.method == "HEAD":
                    await resp.aclose()
                    eff_total = str(known_total) if known_total > 0 else "*"
                    eff_len = max(0, eff_req_end - req_start_b + 1)
                    out_headers["Content-Range"] = f"bytes {req_start_b}-{eff_req_end}/{eff_total}"
                    out_headers["Content-Length"] = str(eff_len)
                    return Response(
                        b"", status_code=206, headers=out_headers, media_type=media_type
                    )

                want_len = max(1, eff_req_end - req_start_b + 1)
                buf = bytearray()
                try:
                    async for chunk in resp.aiter_bytes():
                        if chunk:
                            buf.extend(chunk)
                            if len(buf) >= want_len:
                                break
                except Exception:
                    pass
                finally:
                    await resp.aclose()

                if not buf:
                    continue

                body_bytes = bytes(buf[:want_len])
                actual_end = req_start_b + len(body_bytes) - 1
                if known_total <= 0 and len(body_bytes) < want_len:
                    known_total = actual_end + 1
                    if head_entry:
                        head_entry.total_len = known_total
                    if final_url:
                        _STREAM_META_CACHE[vid] = (time.time(), final_url, known_total, cached_dur)
                eff_total = str(known_total) if known_total > 0 else "*"
                out_headers["Content-Range"] = f"bytes {req_start_b}-{actual_end}/{eff_total}"
                out_headers["Content-Length"] = str(len(body_bytes))
                return Response(
                    body_bytes,
                    status_code=206,
                    headers=out_headers,
                    media_type=media_type,
                )
            except Exception:
                continue

    print(f"[YOUTUBE] stream failed video_id={vid}", flush=True)
    return Response(b"stream unavailable", status_code=502)


def _decode_repeated_str(val: str) -> str:
    out = val or ""
    for _ in range(5):
        from urllib.parse import unquote

        nxt = unquote(out)
        if nxt == out:
            break
        out = nxt
    return out


def _extract_sj_virtual_url(raw_rest: str, query_str: str) -> str:
    from urllib.parse import unquote
    import base64
    decoded_rest = _decode_repeated_str(raw_rest)
    m = re.search(r"(https?://.*)$", decoded_rest, re.I)
    if not m:
        for part in raw_rest.split("/"):
            if not part:
                continue
            try:
                padded = part + "=" * ((4 - len(part) % 4) % 4)
                dec = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", "ignore")
                if dec.startswith("http://") or dec.startswith("https://"):
                    m_b64 = re.search(r"(https?://.*)$", dec, re.I)
                    if m_b64:
                        decoded_rest = m_b64.group(1)
                        m = m_b64
                        break
            except Exception:
                pass
    if not m:
        return ""
    base = m.group(1)
    if query_str:
        qs_parts = [
            p
            for p in query_str.split("&")
            if p and not unquote(p.split("=", 1)[0]).startswith("$")
        ]
        if qs_parts:
            sep = "&" if "?" in base else "?"
            base = base + sep + "&".join(qs_parts)
    return _strip_dollar_query(base)


_WASM_JS_CACHE: bytes = b""


async def _serve_virtual_wasm_js() -> Response:
    global _WASM_JS_CACHE
    if not _WASM_JS_CACHE:
        try:
            import base64
            import app as host_app
            resp = await host_app.scramjet_asset("scramjet.wasm")
            raw_wasm = bytes(getattr(resp, "body", b"") or b"")
            if raw_wasm:
                b64 = base64.b64encode(raw_wasm).decode("ascii")
                _WASM_JS_CACHE = f"self.WASM = '{b64}';".encode("ascii")
        except Exception:
            pass
    return Response(
        _WASM_JS_CACHE or b"",
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.api_route(
    "/youtube/~/sj/{rest:path}",
    methods=["GET", "HEAD", "POST", "OPTIONS"],
    include_in_schema=False,
)
async def youtube_sj_direct_fallback(rest: str, request: Request):
    """Server-side fallback when iOS/iPadOS WebKit AVFoundation or early resource
    fetches bypass the Service Worker and request /youtube/~/sj/... directly."""
    if request.method == "OPTIONS":
        req_orig = request.headers.get("origin") or "*"
        return Response(
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": req_orig,
                "Access-Control-Allow-Credentials": "true",
                "Access-Control-Allow-Methods": "GET, HEAD, POST, OPTIONS",
                "Access-Control-Allow-Headers": "*",
            },
        )
    if rest.endswith("scramjet.wasm.js"):
        return await _serve_virtual_wasm_js()
    decoded_rest = _decode_repeated_str(rest)
    m_data = re.search(r"(data:([A-Za-z0-9.+/-]+);base64,([A-Za-z0-9+/=]+))", decoded_rest)
    if m_data:
        try:
            import base64
            raw_bytes = base64.b64decode(m_data.group(3))
            return Response(
                b"" if request.method == "HEAD" else raw_bytes,
                media_type=m_data.group(2),
                headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=86400"},
            )
        except Exception:
            pass
    virtual_url = _extract_sj_virtual_url(rest, request.url.query)
    if not virtual_url:
        return Response(b"invalid virtual url", status_code=404)
    m_stream = re.search(r"/yts/stream/([A-Za-z0-9_-]{11})\.mp4", virtual_url)
    if m_stream:
        return await youtube_runtime_stream(m_stream.group(1), request)
    try:
        parsed = urlparse(virtual_url)
        if parsed.path == "/youtube/runtime/image-relay":
            qs = parse_qs(parsed.query)
            k_nested = (qs.get("k") or [""])[0]
            nested = _decode_media_key(k_nested) if k_nested else (qs.get("url") or [""])[0]
            if nested:
                return await _serve_youtube_image_url(
                    nested, request.method, request.headers.get("accept") or ""
                )
        host = (parsed.hostname or "").lower().rstrip(".")
        path_str = parsed.path or ""
        q_str = parsed.query or ""
        if path_str in ("/generate_204", "/csi_204", "/error_204") and not (host in AUTH_HOSTS or host.endswith(".google.com")):
            return Response(status_code=204, headers={"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Credentials": "true"})
        if host.endswith((".googlevideo.com", ".c.youtube.com")):
            if " " in path_str or "expire=9421968081" in q_str or "expire=1685394838" in q_str or path_str in ("/generate_204", "/gen_204"):
                return Response(status_code=204, headers={"Access-Control-Allow-Origin": "*"})
            return await youtube_runtime_gv_stream(request)
        if any(host == s or host.endswith("." + s) for s in YOUTUBE_IMAGE_SUFFIXES):
            path_l = (parsed.path or "").lower()
            if host.endswith(("gstatic.com", "googleapis.com")):
                if path_l.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".avif", ".bmp", ".woff2", ".woff", ".ttf", ".otf")):
                    return await _serve_youtube_image_url(
                        virtual_url, request.method, request.headers.get("accept") or ""
                    )
            elif not path_l.endswith((".js", ".mjs", ".cjs", ".json", ".css", ".html", ".wasm")):
                return await _serve_youtube_image_url(
                    virtual_url, request.method, request.headers.get("accept") or ""
                )
    except Exception:
        pass

    req_body = await request.body()
    raw_headers = list(request.headers.items())
    status, status_text, out_headers, resp_body = await _execute_relay_core(
        virtual_url, request.method, raw_headers, req_body
    )
    resp = Response(
        content=b"" if request.method == "HEAD" else resp_body,
        status_code=status,
    )
    raw_out = []
    for k, v in out_headers:
        kl = str(k).lower()
        if kl in ("transfer-encoding", "content-length", "connection", "access-control-allow-origin", "access-control-allow-credentials"):
            continue
        raw_out.append((kl.encode("latin-1", "replace"), str(v).encode("latin-1", "replace")))
    req_orig = request.headers.get("origin") or "*"
    raw_out.append((b"access-control-allow-origin", req_orig.encode("latin-1")))
    raw_out.append((b"access-control-allow-credentials", b"true"))
    raw_out.append((b"access-control-expose-headers", b"*"))
    resp.raw_headers = raw_out
    return resp


# ---------------------------------------------------------------------------
# Native YouTube Player & Home Feed Enhancers for youtube_http_relay
# ---------------------------------------------------------------------------
_ANON_VISITOR_COOKIE_NAMES = frozenset({
    "__Secure-BUCKET",
    "__Secure-YNID",
    "GPS",
    "YSC",
    "__Secure-ROLLOUT_TOKEN",
    "VISITOR_INFO1_LIVE",
    "VISITOR_PRIVACY_METADATA",
})
_AUTH_COOKIE_MARKERS = (
    "SID=",
    "HSID=",
    "SSID=",
    "APISID=",
    "SAPISID=",
    "__Secure-1PSID=",
    "__Secure-3PSID=",
    "__Secure-1PAPISID=",
    "__Secure-3PAPISID=",
    "LOGIN_INFO=",
)
_ANON_VISITOR_CACHE: tuple[float, dict[str, str]] = (0.0, {})
_ANON_VISITOR_TTL = 900.0
_ANON_VISITOR_LOCK = asyncio.Lock()

_HOME_RICH_ITEMS_CACHE: tuple[float, list[dict]] = (0.0, [])
_HOME_RICH_ITEMS_TTL = 600.0
_WATCH_NEXT_CACHE: dict[str, tuple[float, dict]] = {}
_WATCH_NEXT_TTL = 600.0


def _extract_anon_cookies_from_headers(raw_multi: list[tuple[str, str]]) -> dict[str, str]:
    found: dict[str, str] = {}
    for k, v in raw_multi:
        if k.lower() == "set-cookie":
            first = v.split(";", 1)[0].strip()
            if "=" in first:
                ck_name, ck_val = first.split("=", 1)
                ck_name = ck_name.strip()
                if ck_name in _ANON_VISITOR_COOKIE_NAMES and ck_val:
                    found[ck_name] = ck_val.strip()
    return found


async def _get_anon_visitor_cookies(force_refresh: bool = False) -> dict[str, str]:
    """Fetch strictly non-authenticated anonymous visitor bootstrap cookies from
    GET https://www.youtube.com/ so direct unauthenticated requests to /watch or
    /results from Render's IP are not redirected to google.com/sorry/index."""
    global _ANON_VISITOR_CACHE
    now = time.time()
    if (
        not force_refresh
        and _ANON_VISITOR_CACHE[1]
        and (now - _ANON_VISITOR_CACHE[0]) < _ANON_VISITOR_TTL
    ):
        return _ANON_VISITOR_CACHE[1]

    async with _ANON_VISITOR_LOCK:
        now = time.time()
        if (
            not force_refresh
            and _ANON_VISITOR_CACHE[1]
            and (now - _ANON_VISITOR_CACHE[0]) < _ANON_VISITOR_TTL
        ):
            return _ANON_VISITOR_CACHE[1]
        session = await _get_curl_session()
        headers = {
            "User-Agent": _DESKTOP_CHROME_UA,
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        }
        try:
            if session is not None:
                r = await session.get(
                    "https://www.youtube.com/?hl=ja&gl=JP",
                    headers=headers,
                    allow_redirects=False,
                    timeout=8,
                )
                raw_multi = r.headers.multi_items()
            else:
                client = await _get_media_http_client()
                r = await client.get(
                    "https://www.youtube.com/?hl=ja&gl=JP",
                    headers=headers,
                    follow_redirects=False,
                )
                raw_multi = list(r.headers.multi_items())
            found = _extract_anon_cookies_from_headers(raw_multi)
            if found:
                merged = dict(_ANON_VISITOR_CACHE[1])
                merged.update(found)
                _ANON_VISITOR_CACHE = (now, merged)
                return merged
        except Exception:
            pass
        return _ANON_VISITOR_CACHE[1]


async def _get_anon_visitor_cookie_header(force_refresh: bool = False) -> str:
    cks = await _get_anon_visitor_cookies(force_refresh=force_refresh)
    if not cks:
        return ""
    return "; ".join(f"{k}={v}" for k, v in cks.items())


def _decode_js_hex_string(raw_s: str) -> str:
    def _repl(m):
        esc = m.group(0)
        if esc.startswith("\\x") and len(esc) == 4:
            return chr(int(esc[2:], 16))
        if esc.startswith("\\u") and len(esc) == 6:
            return chr(int(esc[2:], 16))
        if esc == "\\\\":
            return "\\"
        if esc == '\\"':
            return '"'
        if esc == "\\'":
            return "'"
        if esc == "\\n":
            return "\n"
        if esc == "\\r":
            return "\r"
        if esc == "\\t":
            return "\t"
        if esc == "\\/":
            return "/"
        return esc

    return re.sub(r"\\(?:x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|.)", _repl, raw_s)


def _extract_js_var_json(html_text: str, marker: str) -> dict | None:
    idx = html_text.find(marker)
    if idx < 0:
        return None
    start = idx + len(marker)
    while start < len(html_text) and html_text[start] in " \t\r\n":
        start += 1
    if start < len(html_text) and html_text[start] == "'":
        m_str = re.match(r"'((?:\\.|[^'\\])*)'", html_text[start:])
        if m_str:
            try:
                decoded = _decode_js_hex_string(m_str.group(1))
                obj = json.loads(decoded)
                return obj if isinstance(obj, dict) else None
            except Exception:
                return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(html_text, start)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _replace_js_var_json(html_text: str, marker: str, mutator) -> tuple[str, bool]:
    idx = html_text.find(marker)
    if idx < 0:
        return html_text, False
    start = idx + len(marker)
    while start < len(html_text) and html_text[start] in " \t\r\n":
        start += 1
    if start < len(html_text) and html_text[start] == "'":
        m_str = re.match(r"'((?:\\.|[^'\\])*)'", html_text[start:])
        if m_str:
            try:
                decoded = _decode_js_hex_string(m_str.group(1))
                obj = json.loads(decoded)
                if not isinstance(obj, dict) or not mutator(obj):
                    return html_text, False
                new_json = json.dumps(obj, ensure_ascii=False)
                end_quote = start + m_str.end()
                var_name = marker.replace("var ", "").replace("=", "").strip()
                tail_Updated = html_text[end_quote:].replace(
                    f"JSON.parse({var_name})", var_name, 1
                )
                return html_text[:start] + new_json + tail_Updated, True
            except Exception:
                return html_text, False
    try:
        obj, end = json.JSONDecoder().raw_decode(html_text, start)
        if not isinstance(obj, dict):
            return html_text, False
        if not mutator(obj):
            return html_text, False
        new_json = json.dumps(obj, ensure_ascii=False)
        return html_text[:start] + new_json + html_text[end:], True
    except Exception:
        return html_text, False


async def _refresh_home_rich_items() -> list[dict]:
    global _HOME_RICH_ITEMS_CACHE
    now = time.time()
    session = await _get_curl_session()
    anon_cookie = await _get_anon_visitor_cookie_header()

    # 1. Primary path: POST /youtubei/v1/search (never blocked by /sorry/ on Render datacenter IPs)
    api_url = "https://www.youtube.com/youtubei/v1/search?prettyPrint=false"
    api_headers = {
        "User-Agent": _DESKTOP_CHROME_UA,
        "Content-Type": "application/json",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        "Origin": "https://www.youtube.com",
        "Referer": "https://www.youtube.com/",
    }
    if anon_cookie:
        api_headers["Cookie"] = anon_cookie
    api_body = json.dumps({
        "query": "人気 急上昇 音楽 アニメ 公式",
        "context": {
            "client": {
                "clientName": "WEB",
                "clientVersion": "2.20250219.01.00",
                "hl": "ja",
                "gl": "JP",
            }
        },
    }).encode("utf-8")


    try:
        if session is not None:
            r = await session.post(api_url, headers=api_headers, data=api_body, timeout=8)
            raw_text = r.text
        else:
            client = await _get_media_http_client()
            r = await client.post(api_url, headers=api_headers, content=api_body)
            raw_text = r.text
        if r.status_code == 200 and raw_text:
            data = json.loads(raw_text)
            sections = (
                data.get("contents", {})
                .get("twoColumnSearchResultsRenderer", {})
                .get("primaryContents", {})
                .get("sectionListRenderer", {})
                .get("contents", [])
            )
            rich_items: list[dict] = []
            for sec in sections:
                for item in sec.get("itemSectionRenderer", {}).get("contents", []):
                    vr = item.get("videoRenderer")
                    if vr and vr.get("videoId"):
                        rich_items.append({"richItemRenderer": {"content": {"videoRenderer": vr}}})
            if rich_items:
                _HOME_RICH_ITEMS_CACHE = (now, rich_items)
                return rich_items
    except Exception:
        pass

    # 2. Fallback path: GET /results?search_query=...
    search_url = (
        "https://www.youtube.com/results?search_query="
        + quote("人気 急上昇 音楽 アニメ 公式", safe="")
    )
    headers = {
        "User-Agent": _DESKTOP_CHROME_UA,
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    }
    if anon_cookie:
        headers["Cookie"] = anon_cookie
    try:
        if session is not None:
            r = await session.get(search_url, headers=headers, timeout=8)
            if r.status_code in (302, 303):
                anon_cookie = await _get_anon_visitor_cookie_header(force_refresh=True)
                if anon_cookie:
                    headers["Cookie"] = anon_cookie
                    r = await session.get(search_url, headers=headers, timeout=8)
            html = r.text
        else:
            client = await _get_media_http_client()
            r = await client.get(search_url, headers=headers)
            html = r.text
        data = _extract_js_var_json(html, "var ytInitialData = ")
        if data:
            sections = (
                data.get("contents", {})
                .get("twoColumnSearchResultsRenderer", {})
                .get("primaryContents", {})
                .get("sectionListRenderer", {})
                .get("contents", [])
            )
            rich_items = []
            for sec in sections:
                for item in sec.get("itemSectionRenderer", {}).get("contents", []):
                    vr = item.get("videoRenderer")
                    if vr and vr.get("videoId"):
                        rich_items.append({"richItemRenderer": {"content": {"videoRenderer": vr}}})
            if rich_items:
                _HOME_RICH_ITEMS_CACHE = (now, rich_items)
                return rich_items
    except Exception:
        pass
    return _HOME_RICH_ITEMS_CACHE[1]


async def _get_home_rich_items() -> list[dict]:
    global _HOME_RICH_ITEMS_CACHE
    now = time.time()
    if _HOME_RICH_ITEMS_CACHE[1]:
        if (now - _HOME_RICH_ITEMS_CACHE[0]) >= _HOME_RICH_ITEMS_TTL:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_refresh_home_rich_items())
            except Exception:
                pass
        return _HOME_RICH_ITEMS_CACHE[1]
    return await _refresh_home_rich_items()


def _video_renderer_to_compact(vr: dict) -> dict | None:
    if not isinstance(vr, dict):
        return None
    vid = vr.get("videoId")
    if not vid or not isinstance(vid, str):
        return None
    title_obj = vr.get("title") or {}
    runs = title_obj.get("runs") or []
    simple_title = title_obj.get("simpleText") or (
        runs[0].get("text") if runs and isinstance(runs[0], dict) else "YouTube Video"
    )
    compact: dict = {
        "videoId": vid,
        "thumbnail": vr.get("thumbnail") or {
            "thumbnails": [
                {"url": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg", "width": 336, "height": 188}
            ]
        },
        "title": {"simpleText": simple_title},
        "longBylineText": vr.get("longBylineText") or vr.get("ownerText") or vr.get("shortBylineText") or {"runs": [{"text": "YouTube"}]},
        "shortBylineText": vr.get("shortBylineText") or vr.get("ownerText") or vr.get("longBylineText") or {"runs": [{"text": "YouTube"}]},
        "viewCountText": vr.get("viewCountText") or vr.get("shortViewCountText") or {"simpleText": ""},
        "shortViewCountText": vr.get("shortViewCountText") or vr.get("viewCountText") or {"simpleText": ""},
        "navigationEndpoint": vr.get("navigationEndpoint") or {
            "commandMetadata": {
                "webCommandMetadata": {
                    "url": f"/watch?v={vid}",
                    "webPageType": "WEB_PAGE_TYPE_WATCH",
                    "rootVe": 3832,
                }
            },
            "watchEndpoint": {"videoId": vid},
        },
    }
    if "publishedTimeText" in vr:
        compact["publishedTimeText"] = vr["publishedTimeText"]
    if "lengthText" in vr:
        compact["lengthText"] = vr["lengthText"]
    if "thumbnailOverlays" in vr:
        compact["thumbnailOverlays"] = vr["thumbnailOverlays"]
    if "channelThumbnailSupportedRenderers" in vr:
        compact["channelThumbnail"] = (
            vr["channelThumbnailSupportedRenderers"]
            .get("channelThumbnailWithLinkRenderer", {})
            .get("thumbnail", {})
        )
    if "ownerBadges" in vr:
        compact["ownerBadges"] = vr["ownerBadges"]
    if "menu" in vr:
        compact["menu"] = vr["menu"]
    return {"compactVideoRenderer": compact}


def _extract_secondary_results_list(wn_obj: dict) -> list:
    if not isinstance(wn_obj, dict):
        return []
    sec = (
        wn_obj.get("contents", {})
        .get("twoColumnWatchNextResults", {})
        .get("secondaryResults", {})
        .get("secondaryResults", {})
        .get("results", [])
    )
    return sec if isinstance(sec, list) else []


def _has_related_video_items(wn_obj: dict) -> bool:
    results = _extract_secondary_results_list(wn_obj)
    for item in results:
        if not isinstance(item, dict):
            continue
        if any(
            k in item
            for k in (
                "lockupViewModel",
                "compactVideoRenderer",
                "compactRadioRenderer",
                "compactPlaylistRenderer",
            )
        ):
            return True
        isr = item.get("itemSectionRenderer")
        if isinstance(isr, dict):
            for sub in isr.get("contents") or []:
                if isinstance(sub, dict) and any(
                    k in sub
                    for k in (
                        "lockupViewModel",
                        "compactVideoRenderer",
                        "compactRadioRenderer",
                        "compactPlaylistRenderer",
                    )
                ):
                    return True
    return False


async def _ensure_watch_next_secondary_results(wn_obj: dict, current_vid: str = "") -> bool:
    """Ensure twoColumnWatchNextResults.secondaryResults.secondaryResults.results
    is populated with related video cards and formatted so both direct /youtubei/v1/next
    and /youtubei/v1/get_watch render sidebar recommendations reliably."""
    if not isinstance(wn_obj, dict):
        return False
    changed = False
    contents = wn_obj.setdefault("contents", {})
    tc = contents.setdefault("twoColumnWatchNextResults", {})

    # Fix topbar login URL if it came from /youtubei/v1/next so sign-in returns to /
    try:
        buttons = (
            wn_obj.get("topbar", {})
            .get("desktopTopbarRenderer", {})
            .get("topbarButtons", [])
        )
        for btn in buttons:
            if not isinstance(btn, dict):
                continue
            br = btn.get("buttonRenderer")
            if isinstance(br, dict):
                wcm = (
                    br.get("navigationEndpoint", {})
                    .get("commandMetadata", {})
                    .get("webCommandMetadata", {})
                )
                u = wcm.get("url") or ""
                if "%2Fyoutubei%2Fv1%2Fnext" in u or "%252Fyoutubei%252Fv1%252Fnext" in u:
                    wcm["url"] = GOOGLE_LOGIN
                    changed = True
    except Exception:
        pass

    if not _has_related_video_items(wn_obj):
        try:
            home_items = await asyncio.wait_for(_get_home_rich_items(), timeout=4.0)
        except Exception:
            home_items = []
        fallback_results: list[dict] = []
        for hi in home_items:
            vr = (
                hi.get("richItemRenderer", {})
                .get("content", {})
                .get("videoRenderer")
                if isinstance(hi, dict)
                else None
            )
            if isinstance(vr, dict) and vr.get("videoId") != current_vid:
                cv = _video_renderer_to_compact(vr)
                if cv:
                    fallback_results.append(cv)
        if fallback_results:
            tc["secondaryResults"] = {
                "secondaryResults": {
                    "results": fallback_results,
                    "targetId": "watch-next-feed",
                }
            }
            changed = True
    return changed


async def _fetch_full_watch_next(video_id: str) -> dict:
    """Fetch complete watchNextResponse (ytInitialData) for a video.
    Uses POST /youtubei/v1/next?prettyPrint=false first because on Render datacenter IPs,
    GET /watch?v={video_id} is redirected to google.com/sorry/index, whereas
    POST /youtubei/v1/next returns 200 OK with full videoPrimaryInfoRenderer,
    videoSecondaryInfoRenderer, and 20+ related videos in secondaryResults.
    """
    now = time.time()
    cached = _WATCH_NEXT_CACHE.get(video_id)
    if cached and (now - cached[0]) < _WATCH_NEXT_TTL and _has_related_video_items(cached[1]):
        return cached[1]

    session = await _get_curl_session()
    anon_cookie = await _get_anon_visitor_cookie_header()

    # 1. Primary path: POST /youtubei/v1/next?prettyPrint=false
    next_url = "https://www.youtube.com/youtubei/v1/next?prettyPrint=false"
    next_headers = {
        "User-Agent": _DESKTOP_CHROME_UA,
        "Content-Type": "application/json",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        "Origin": "https://www.youtube.com",
        "Referer": f"https://www.youtube.com/watch?v={video_id}",
    }
    if anon_cookie:
        next_headers["Cookie"] = anon_cookie
    next_body = json.dumps({
        "videoId": video_id,
        "context": {
            "client": {
                "clientName": "WEB",
                "clientVersion": "2.20250219.01.00",
                "hl": "ja",
                "gl": "JP",
            }
        },
    }).encode("utf-8")
    try:
        if session is not None:
            r = await session.post(next_url, headers=next_headers, data=next_body, timeout=8)
            raw_text = r.text
        else:
            client = await _get_media_http_client()
            r = await client.post(next_url, headers=next_headers, content=next_body)
            raw_text = r.text
        if r.status_code == 200 and raw_text:
            data = json.loads(raw_text)
            if isinstance(data, dict) and "contents" in data:
                await _ensure_watch_next_secondary_results(data, video_id)
                if len(_WATCH_NEXT_CACHE) >= 256:
                    _WATCH_NEXT_CACHE.pop(next(iter(_WATCH_NEXT_CACHE)), None)
                _WATCH_NEXT_CACHE[video_id] = (now, data)
                return data
    except Exception:
        pass

    # 2. Fallback path: GET /watch?v={video_id}
    watch_url = f"https://www.youtube.com/watch?v={video_id}&hl=ja&gl=JP"
    headers = {
        "User-Agent": _DESKTOP_CHROME_UA,
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    }
    if anon_cookie:
        headers["Cookie"] = anon_cookie
    try:
        if session is not None:
            r = await session.get(watch_url, headers=headers, timeout=8)
            if r.status_code in (302, 303):
                anon_cookie = await _get_anon_visitor_cookie_header(force_refresh=True)
                if anon_cookie:
                    headers["Cookie"] = anon_cookie
                    r = await session.get(watch_url, headers=headers, timeout=8)
            html = r.text
        else:
            client = await _get_media_http_client()
            r = await client.get(watch_url, headers=headers)
            html = r.text
        data = _extract_js_var_json(html, "var ytInitialData = ")
        if isinstance(data, dict) and "contents" in data:
            await _ensure_watch_next_secondary_results(data, video_id)
            if len(_WATCH_NEXT_CACHE) >= 256:
                _WATCH_NEXT_CACHE.pop(next(iter(_WATCH_NEXT_CACHE)), None)
            _WATCH_NEXT_CACHE[video_id] = (now, data)
            return data
    except Exception:
        pass
    return {}


def _parse_json_req_body(req_body: bytes) -> dict:
    if not req_body:
        return {}
    raw = req_body
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
    try:
        obj = json.loads(raw.decode("utf-8", "ignore"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _extract_request_video_id(parsed_url, req_body: bytes) -> str:
    path = parsed_url.path or ""
    if path == "/watch":
        qs = parse_qs(parsed_url.query)
        v = (qs.get("v") or [""])[0]
        if VIDEO_ID_RE.match(v):
            return v
    elif path in ("", "/") and "sh_watch=" in (parsed_url.query or ""):
        qs = parse_qs(parsed_url.query)
        v = (qs.get("sh_watch") or [""])[0]
        if VIDEO_ID_RE.match(v):
            return v
    m_shorts = re.match(r"^/shorts/([A-Za-z0-9_-]{11})", path)
    if m_shorts:
        return m_shorts.group(1)
    if path.startswith("/youtubei/v1/"):
        body_obj = _parse_json_req_body(req_body)
        vid = body_obj.get("videoId") or (body_obj.get("playerRequest") or {}).get("videoId") or ""
        if isinstance(vid, str) and VIDEO_ID_RE.match(vid):
            return vid
    return ""


def _extract_details_from_watch_next(
    wn_obj: dict, fallback_vid: str = ""
) -> tuple[str, str, str, str]:
    vid = fallback_vid
    title = "YouTube Video"
    author = "YouTube"
    channel_id = "UCBR8-60-B28hp2BmDPdntcQ"
    if not isinstance(wn_obj, dict):
        return vid, title, author, channel_id
    try:
        cve = wn_obj.get("currentVideoEndpoint", {}).get("watchEndpoint", {})
        if cve.get("videoId") and VIDEO_ID_RE.match(str(cve["videoId"])):
            vid = str(cve["videoId"])
        if not vid:
            rwe = wn_obj.get("replacementEndpoint", {}).get("reelWatchEndpoint", {})
            if rwe.get("videoId") and VIDEO_ID_RE.match(str(rwe["videoId"])):
                vid = str(rwe["videoId"])
        contents = (
            wn_obj.get("contents", {})
            .get("twoColumnWatchNextResults", {})
            .get("results", {})
            .get("results", {})
            .get("contents", [])
        )
        for item in contents:
            if not isinstance(item, dict):
                continue
            vpi = item.get("videoPrimaryInfoRenderer")
            if isinstance(vpi, dict):
                runs = vpi.get("title", {}).get("runs", [])
                if runs and isinstance(runs[0], dict) and runs[0].get("text"):
                    title = str(runs[0]["text"])
            vsi = item.get("videoSecondaryInfoRenderer")
            if isinstance(vsi, dict):
                owner = vsi.get("owner", {}).get("videoOwnerRenderer", {})
                oruns = owner.get("title", {}).get("runs", [])
                if oruns and isinstance(oruns[0], dict) and oruns[0].get("text"):
                    author = str(oruns[0]["text"])
                bid = (
                    owner.get("navigationEndpoint", {})
                    .get("browseEndpoint", {})
                    .get("browseId")
                )
                if bid:
                    channel_id = str(bid)
        # Check Shorts overlay header if title is still default
        if title == "YouTube Video":
            label = (
                wn_obj.get("overlay", {})
                .get("reelPlayerOverlayRenderer", {})
                .get("reelPlayerHeaderSupportedRenderers", {})
                .get("reelPlayerHeaderRenderer", {})
                .get("accessibility", {})
                .get("accessibilityData", {})
                .get("label")
            )
            if label:
                title = str(label)
    except Exception:
        pass
    return vid, title, author, channel_id


def _build_playable_response_patch(
    vid: str,
    title: str = "YouTube Video",
    author: str = "YouTube",
    channel_id: str = "UCBR8-60-B28hp2BmDPdntcQ",
    clen: int = 0,
    dur_s: float = 0.0,
) -> dict:
    if dur_s <= 0:
        dur_s = 240.0
    if clen <= 0:
        cached = _STREAM_META_CACHE.get(vid)
        if cached and cached[2] > 0:
            clen = cached[2]
    stream_url = f"https://www.youtube.com/yts/stream/{vid}.mp4"
    fmt = {
        "itag": 18,
        "url": stream_url,
        "mimeType": 'video/mp4; codecs="avc1.42001E, mp4a.40.2"',
        "bitrate": 500000,
        "width": 640,
        "height": 360,
        "lastModified": "1700000000000000",
        "quality": "medium",
        "fps": 30,
        "qualityLabel": "360p",
        "projectionType": "RECTANGULAR",
        "averageBitrate": 500000,
        "audioQuality": "AUDIO_QUALITY_LOW",
        "approxDurationMs": str(int(dur_s * 1000)),
        "audioSampleRate": "44100",
        "audioChannels": 2,
    }
    if clen > 0:
        fmt["contentLength"] = str(clen)

    return {
        "playabilityStatus": {"status": "OK", "playableInEmbed": True},
        "streamingData": {
            "expiresInSeconds": "21540",
            "formats": [fmt],
            "adaptiveFormats": [],
        },
        "videoDetails": {
            "videoId": vid,
            "title": title,
            "lengthSeconds": str(max(1, int(round(dur_s)))),
            "channelId": channel_id,
            "isOwnerViewing": False,
            "shortDescription": "",
            "isCrawlable": True,
            "thumbnail": {
                "thumbnails": [
                    {
                        "url": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                        "width": 480,
                        "height": 360,
                    }
                ]
            },
            "allowRatings": True,
            "viewCount": "1000",
            "author": author,
            "isPrivate": False,
            "isUnpluggedCorpus": False,
            "isLiveContent": False,
        },
    }


def _strip_feed_ads(data: any) -> None:
    """Recursively removes adSlotRenderer, promotedSparkles, and other ad cards from feeds."""
    ad_keys = {
        "adSlotRenderer",
        "inFeedAdLayoutRenderer",
        "promotedSparklesWebRenderer",
        "promotedSparklesTextSearchRenderer",
        "promotedVideoRenderer",
        "statementBannerRenderer",
        "brandVideoSingletonRenderer",
        "bannerPromoRenderer",
    }
    if isinstance(data, dict):
        keys_to_delete = [k for k in data if k in ad_keys]
        for k in keys_to_delete:
            del data[k]
        for v in data.values():
            _strip_feed_ads(v)
    elif isinstance(data, list):
        filtered = []
        for item in data:
            if isinstance(item, dict):
                if any(ak in item for ak in ad_keys):
                    continue
                content = item.get("richItemRenderer", {}).get("content") or item.get("content")
                if isinstance(content, dict) and any(ak in content for ak in ad_keys):
                    continue
                _strip_feed_ads(item)
            filtered.append(item)
        data[:] = filtered


_ANDROID_VR_PLAYER_CACHE: dict[str, tuple[float, dict]] = {}
_ANDROID_VR_UA = (
    "com.google.android.apps.youtube.vr.oculus/1.60.19 "
    "(Linux; U; Android 12L; eureka-user Build/SQ3A.220605.009.A1) gzip"
)


def _has_playable_streaming_data(sd: any) -> bool:
    if not isinstance(sd, dict):
        return False
    if sd.get("hlsManifestUrl") or sd.get("dashManifestUrl"):
        return True
    for key in ("formats", "adaptiveFormats"):
        items = sd.get(key)
        if isinstance(items, list):
            for f in items:
                if isinstance(f, dict) and (f.get("url") or f.get("signatureCipher") or f.get("cipher")):
                    return True
    return False


async def _fetch_android_vr_player(vid: str) -> dict | None:
    if not vid or not VIDEO_ID_RE.match(vid):
        return None
    now = time.time()
    cached = _ANDROID_VR_PLAYER_CACHE.get(vid)
    if cached and (now - cached[0]) < 1200.0:
        return cached[1] or None
    payload = json.dumps(
        {
            "videoId": vid,
            "context": {
                "client": {
                    "clientName": "ANDROID_VR",
                    "clientVersion": "1.60.19",
                    "deviceMake": "Oculus",
                    "deviceModel": "Quest 3",
                    "osName": "Android",
                    "osVersion": "12L",
                    "androidSdkVersion": 32,
                    "hl": "ja",
                    "gl": "JP",
                }
            },
            "contentCheckOk": True,
            "racyCheckOk": True,
        }
    ).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": _ANDROID_VR_UA,
        "X-YouTube-Client-Name": "28",
        "Origin": "https://www.youtube.com",
    }
    try:
        session = await _get_curl_session()
        if session is not None:
            r = await session.post(
                "https://www.youtube.com/youtubei/v1/player?prettyPrint=false",
                headers=headers,
                data=payload,
                timeout=3.5,
            )
            raw = r.text
            sc = r.status_code
        else:
            client = await _get_media_http_client()
            r = await client.post(
                "https://www.youtube.com/youtubei/v1/player?prettyPrint=false",
                headers=headers,
                content=payload,
                timeout=3.5,
            )
            raw = r.text
            sc = r.status_code
        if sc == 200 and raw:
            d = json.loads(raw)
            st = (d.get("playabilityStatus") or {}).get("status")
            sd = d.get("streamingData") or {}
            direct_ok = bool(sd.get("hlsManifestUrl")) or any(
                isinstance(f, dict) and f.get("url")
                for f in ((sd.get("formats") or []) + (sd.get("adaptiveFormats") or []))
            )
            if st == "OK" and direct_ok:
                out = {
                    "playabilityStatus": {"status": "OK", "playableInEmbed": True},
                    "streamingData": sd,
                }
                if isinstance(d.get("videoDetails"), dict):
                    out["videoDetails"] = d["videoDetails"]
                if isinstance(d.get("captions"), dict):
                    out["captions"] = d["captions"]
                if isinstance(d.get("storyboards"), dict):
                    out["storyboards"] = d["storyboards"]
                if len(_ANDROID_VR_PLAYER_CACHE) >= 256:
                    _ANDROID_VR_PLAYER_CACHE.pop(next(iter(_ANDROID_VR_PLAYER_CACHE)), None)
                _ANDROID_VR_PLAYER_CACHE[vid] = (now, out)
                return out
            _ANDROID_VR_PLAYER_CACHE[vid] = (now, {})
    except Exception:
        pass
    return None


def _apply_playable_patch(
    pr_dict: dict, patch_data: dict, *, has_user_auth_cookie: bool = False
) -> bool:
    if not isinstance(pr_dict, dict):
        return False
    removed_ad = False
    for ad_key in (
        "adPlacements",
        "adSlots",
        "playerAds",
        "adBreakHeartbeatParams",
        "adSlotsSupportedRenderers",
        "invideoAds",
        "adSignalsInfo",
    ):
        if ad_key in pr_dict:
            pr_dict.pop(ad_key, None)
            removed_ad = True
    ps = pr_dict.get("playabilityStatus") or {}
    status = ps.get("status")
    if status == "OK" and _has_playable_streaming_data(pr_dict.get("streamingData")):
        return removed_ad
    if has_user_auth_cookie and status not in ("LOGIN_REQUIRED", None):
        reason_str = str(ps.get("reason") or "").lower()
        if "bot" not in reason_str:
            return removed_ad
    for err_key in ("frameworkUpdates", "auxiliaryUi", "errorScreen", "messages"):
        pr_dict.pop(err_key, None)
    pr_dict.update(patch_data)
    return True


async def _build_best_fallback_patch(
    vid: str,
    title: str,
    author: str,
    cid: str,
    stream_task: asyncio.Task | None,
    req_vid: str,
    *,
    is_shorts: bool = False,
    fallback_dur: float = 0.0,
) -> dict:
    vr_patch = await _fetch_android_vr_player(vid)
    if vr_patch:
        return vr_patch
    clen, dur_s = await _ensure_resolved_stream_meta(
        vid, stream_task, req_vid, is_shorts=is_shorts, fallback_dur=fallback_dur
    )
    return _build_playable_response_patch(vid, title, author, cid, clen, dur_s)


async def _ensure_resolved_stream_meta(
    vid: str,
    stream_task: asyncio.Task | None,
    req_vid: str,
    is_shorts: bool = False,
    fallback_dur: float = 0.0,
) -> tuple[int, float]:
    dur_s = fallback_dur if fallback_dur > 0 else (60.0 if is_shorts else 240.0)
    _PREWARM_REQUESTED_VIDS.add(vid)
    _ensure_stream_head_prewarm(
        vid,
        f"https://invid-api.poketube.fun/companion/latest_version?id={vid}&itag=18&local=true",
    )
    cached_meta = _STREAM_META_CACHE.get(vid)
    if not cached_meta or not cached_meta[1]:
        t_task = (
            stream_task
            if (stream_task is not None and vid == req_vid)
            else asyncio.create_task(_resolve_stream_info(vid))
        )
        wait_timeout = 0.15
        try:
            await asyncio.wait_for(asyncio.shield(t_task), timeout=wait_timeout)
        except Exception:
            pass
        cached_meta = _STREAM_META_CACHE.get(vid)
    if cached_meta and cached_meta[1]:
        _PREWARM_REQUESTED_VIDS.discard(vid)
        _ensure_stream_head_prewarm(vid, cached_meta[1])
    clen = cached_meta[2] if cached_meta else 0
    if cached_meta and len(cached_meta) > 3 and cached_meta[3] > 0:
        dur_s = cached_meta[3]
    return clen, dur_s


def _patch_reel_sequence_obj(seq_obj: dict, *, has_user_auth_cookie: bool = False) -> bool:
    if not isinstance(seq_obj, dict):
        return False
    changed = False
    _strip_feed_ads(seq_obj)
    if has_user_auth_cookie:
        return True
    rep_vid = (
        seq_obj.get("replacementEndpoint", {})
        .get("reelWatchEndpoint", {})
        .get("videoId")
        or ""
    )
    if rep_vid and VIDEO_ID_RE.match(str(rep_vid)):
        rep_vid = str(rep_vid)
        if rep_vid not in _STREAM_META_CACHE:
            asyncio.create_task(_resolve_stream_info(rep_vid))
    for idx, entry in enumerate(seq_obj.get("entries") or []):
        if not isinstance(entry, dict):
            continue
        cmd = entry.get("command") or {}
        rwe = cmd.get("reelWatchEndpoint") or entry.get("reelWatchEndpoint") or {}
        if not isinstance(rwe, dict):
            continue
        rvid = rwe.get("videoId") or ""
        if rvid and VIDEO_ID_RE.match(str(rvid)):
            rvid = str(rvid)
            if idx < 2 and rvid not in _STREAM_META_CACHE:
                asyncio.create_task(_resolve_stream_info(rvid))
            c_meta = _STREAM_META_CACHE.get(rvid)
            c_len = c_meta[2] if c_meta else 0
            d_s = c_meta[3] if (c_meta and len(c_meta) > 3 and c_meta[3] > 0) else 60.0
            upd = rwe.get("unserializedPrefetchData")
            if isinstance(upd, dict):
                r_title = "YouTube Short"
                riwr = upd.get("reelItemWatchResponse")
                if isinstance(riwr, dict):
                    _, r_title, r_auth, r_cid = _extract_details_from_watch_next(riwr, rvid)
                else:
                    r_auth, r_cid = "YouTube", "UCBR8-60-B28hp2BmDPdntcQ"
                p_patch = _build_playable_response_patch(rvid, r_title, r_auth, r_cid, c_len, d_s)
                if isinstance(upd.get("playerResponse"), dict):
                    if _apply_playable_patch(upd["playerResponse"], p_patch):
                        changed = True
                if isinstance(riwr, dict) and isinstance(riwr.get("playerResponse"), dict):
                    if _apply_playable_patch(riwr["playerResponse"], p_patch):
                        changed = True
            elif idx == 0:
                p_patch = _build_playable_response_patch(
                    rvid, "YouTube Short", "YouTube", "UCBR8-60-B28hp2BmDPdntcQ", c_len, d_s
                )
                rwe["unserializedPrefetchData"] = {"playerResponse": p_patch}
                changed = True
    return changed


async def _maybe_patch_youtube_payload(
    parsed_url,
    req_vid: str,
    headers_list: list[list[str]],
    resp_body: bytes,
    stream_task: asyncio.Task | None,
    home_task: asyncio.Task | None,
    next_task: asyncio.Task | None = None,
    *,
    has_user_auth_cookie: bool = False,
) -> bytes:
    ctype = ""
    for k, v in headers_list:
        if k.lower() == "content-type":
            ctype = v.lower()
            break
    if "html" not in ctype and "json" not in ctype:
        return resp_body

    try:
        text = resp_body.decode("utf-8")
    except Exception:
        return resp_body

    modified = False

    # 1. Populate empty Home feed nudge ONLY when unauthenticated
    is_sh_watch = bool(parsed_url and "sh_watch=" in (parsed_url.query or ""))
    if not has_user_auth_cookie and '"feedNudgeRenderer"' in text and not is_sh_watch:
        rich_items: list[dict] = []
        if home_task is not None:
            try:
                rich_items = await asyncio.wait_for(home_task, timeout=4.0)
            except Exception:
                pass
        if not rich_items:
            try:
                rich_items = await asyncio.wait_for(_get_home_rich_items(), timeout=4.0)
            except Exception:
                pass
        if rich_items:
            if "var ytInitialData = " in text:
                def _mutate_home(d: dict) -> bool:
                    changed = False
                    tabs = (
                        d.get("contents", {})
                        .get("twoColumnBrowseResultsRenderer", {})
                        .get("tabs", [])
                    )
                    for t in tabs:
                        rg = (
                            t.get("tabRenderer", {})
                            .get("content", {})
                            .get("richGridRenderer")
                        )
                        if isinstance(rg, dict):
                            rg["contents"] = rich_items
                            changed = True
                    return changed

                text, did_home = _replace_js_var_json(text, "var ytInitialData = ", _mutate_home)
                if did_home:
                    modified = True
            elif text.startswith("{"):
                try:
                    d = json.loads(text)
                    tabs = (
                        d.get("contents", {})
                        .get("twoColumnBrowseResultsRenderer", {})
                        .get("tabs", [])
                    )
                    for t in tabs:
                        rg = (
                            t.get("tabRenderer", {})
                            .get("content", {})
                            .get("richGridRenderer")
                        )
                        if isinstance(rg, dict):
                            rg["contents"] = rich_items
                            modified = True
                    if modified:
                        text = json.dumps(d, ensure_ascii=False)
                except Exception:
                    pass

    # 2. Patch ytInitialPlayerResponse & ytInitialData in HTML (GET /watch, GET /shorts, or channel trailer)
    if "var ytInitialPlayerResponse = " in text:
        vid = req_vid
        if not vid:
            m_vid = re.search(r'"videoId":\s*"([A-Za-z0-9_-]{11})"', text)
            vid = m_vid.group(1) if m_vid else ""
        title, author, cid = "YouTube Video", "YouTube", "UCBR8-60-B28hp2BmDPdntcQ"
        parsed_id_data = _extract_js_var_json(text, "var ytInitialData = ")
        path_str = (parsed_url.path or "") if parsed_url else ""
        is_shorts = bool(path_str == "/shorts" or path_str.startswith("/shorts/"))
        if parsed_id_data:
            try:
                _strip_feed_ads(parsed_id_data)
                if not has_user_auth_cookie and not is_shorts and not _has_related_video_items(parsed_id_data) and vid:
                    full_wn = (
                        await asyncio.wait_for(next_task, timeout=2.0)
                        if next_task is not None
                        else await asyncio.wait_for(_fetch_full_watch_next(vid), timeout=2.0)
                    )
                    if full_wn:
                        _strip_feed_ads(full_wn)
                        parsed_id_data = full_wn
                        text, did_id = _replace_js_var_json(
                            text, "var ytInitialData = ", lambda orig: (orig.clear(), orig.update(full_wn), True)[2]
                        )
                        if did_id:
                            modified = True
                    else:
                        if await _ensure_watch_next_secondary_results(parsed_id_data, vid):
                            text, did_id = _replace_js_var_json(
                                text, "var ytInitialData = ", lambda orig: (orig.clear(), orig.update(parsed_id_data), True)[2]
                            )
                            if did_id:
                                modified = True
                vid, title, author, cid = _extract_details_from_watch_next(
                    parsed_id_data, vid
                )
                if not has_user_auth_cookie and vid and isinstance(parsed_id_data, dict) and "contents" in parsed_id_data:
                    if len(_WATCH_NEXT_CACHE) >= 256:
                        _WATCH_NEXT_CACHE.pop(next(iter(_WATCH_NEXT_CACHE)), None)
                    _WATCH_NEXT_CACHE[vid] = (time.time(), parsed_id_data)
            except Exception:
                pass

        if vid:
            initial_pr = _extract_js_var_json(text, "var ytInitialPlayerResponse = ")
            pr_already_ok = (
                isinstance(initial_pr, dict)
                and (initial_pr.get("playabilityStatus") or {}).get("status") == "OK"
                and _has_playable_streaming_data(initial_pr.get("streamingData"))
            )
            if pr_already_ok:
                text, did_pr = _replace_js_var_json(
                    text,
                    "var ytInitialPlayerResponse = ",
                    lambda orig: _apply_playable_patch(orig, {}, has_user_auth_cookie=has_user_auth_cookie),
                )
                if did_pr:
                    modified = True
            else:
                vd_len = 0.0
                m_len = re.search(r'"lengthSeconds"\s*:\s*"(\d+)"', text)
                if m_len:
                    try:
                        vd_len = float(m_len.group(1))
                    except Exception:
                        pass
                if vd_len <= 0:
                    m_approx = re.search(r'"approxDurationMs"\s*:\s*"(\d+)"', text)
                    if m_approx:
                        try:
                            vd_len = float(m_approx.group(1)) / 1000.0
                        except Exception:
                            pass
                patch_data = await _build_best_fallback_patch(
                    vid, title, author, cid, stream_task, req_vid, is_shorts=is_shorts, fallback_dur=vd_len
                )
                text, did_pr = _replace_js_var_json(
                    text,
                    "var ytInitialPlayerResponse = ",
                    lambda orig: _apply_playable_patch(orig, patch_data, has_user_auth_cookie=has_user_auth_cookie),
                )
                if did_pr:
                    modified = True

                if not has_user_auth_cookie and is_shorts and isinstance(parsed_id_data, dict) and isinstance(parsed_id_data.get("replacementEndpoint"), dict):
                    rwe_rep = parsed_id_data["replacementEndpoint"].get("reelWatchEndpoint")
                    if isinstance(rwe_rep, dict) and rwe_rep.get("videoId") == vid:
                        rwe_rep["unserializedPrefetchData"] = {"playerResponse": patch_data}
                        text, did_rep = _replace_js_var_json(
                            text,
                            "var ytInitialData = ",
                            lambda orig: (orig.clear(), orig.update(parsed_id_data), True)[2],
                        )
                        if did_rep:
                            modified = True

        if "var ytInitialReelWatchSequenceResponse = " in text:
            def _mutate_reel_seq(seq_d: dict) -> bool:
                did_s = _patch_reel_sequence_obj(seq_d, has_user_auth_cookie=has_user_auth_cookie)
                if not has_user_auth_cookie and not seq_d.get("entries") and vid and is_shorts and isinstance(parsed_id_data, dict):
                    rep_ep = parsed_id_data.get("replacementEndpoint")
                    if isinstance(rep_ep, dict):
                        seq_d["entries"] = [{"command": rep_ep}]
                        _patch_reel_sequence_obj(seq_d, has_user_auth_cookie=has_user_auth_cookie)
                        did_s = True
                return did_s

            text, did_reel_seq = _replace_js_var_json(
                text,
                "var ytInitialReelWatchSequenceResponse = ",
                _mutate_reel_seq,
            )
            if did_reel_seq:
                modified = True

    # 3. Patch playerResponse & watchNextResponse in JSON array (/youtubei/v1/get_watch)
    elif text.startswith("[") and '"playerResponse"' in text:
        try:
            arr = json.loads(text)
            _strip_feed_ads(arr)
            wn = {}
            vd_len = 0.0
            any_needs_fallback = False
            for item in arr:
                if isinstance(item, dict):
                    if isinstance(item.get("watchNextResponse"), dict):
                        wn = item["watchNextResponse"]
                    pr_item = item.get("playerResponse")
                    if isinstance(pr_item, dict):
                        if vd_len <= 0:
                            try:
                                vd_len = float((pr_item.get("videoDetails") or {}).get("lengthSeconds") or 0.0)
                            except Exception:
                                pass
                        st_item = (pr_item.get("playabilityStatus") or {}).get("status")
                        if st_item != "OK" or not _has_playable_streaming_data(pr_item.get("streamingData")):
                            any_needs_fallback = True
            vid, title, author, cid = _extract_details_from_watch_next(wn, req_vid)
            if not vid:
                m_vid = re.search(r'"videoId":\s*"([A-Za-z0-9_-]{11})"', text)
                vid = m_vid.group(1) if m_vid else ""
            if vid:
                is_shorts = bool(parsed_url and "/shorts" in (parsed_url.path or ""))
                if not has_user_auth_cookie and not is_shorts and not _has_related_video_items(wn):
                    full_wn: dict = {}
                    if next_task is not None:
                        try:
                            full_wn = await asyncio.wait_for(next_task, timeout=2.5)
                        except Exception:
                            pass
                    else:
                        try:
                            full_wn = await asyncio.wait_for(
                                _fetch_full_watch_next(vid), timeout=2.5
                            )
                        except Exception:
                            pass
                    if full_wn:
                        _strip_feed_ads(full_wn)
                        vid, title, author, cid = _extract_details_from_watch_next(
                            full_wn, vid
                        )
                        for item in arr:
                            if isinstance(item, dict) and isinstance(
                                item.get("watchNextResponse"), dict
                            ):
                                item["watchNextResponse"] = full_wn
                                modified = True
                    else:
                        for item in arr:
                            if isinstance(item, dict) and isinstance(
                                item.get("watchNextResponse"), dict
                            ):
                                if await _ensure_watch_next_secondary_results(item["watchNextResponse"], vid):
                                    modified = True

                patch_data = (
                    await _build_best_fallback_patch(
                        vid, title, author, cid, stream_task, req_vid, is_shorts=is_shorts, fallback_dur=vd_len
                    )
                    if any_needs_fallback
                    else {}
                )
                for item in arr:
                    if isinstance(item, dict) and isinstance(item.get("playerResponse"), dict):
                        if _apply_playable_patch(
                            item["playerResponse"], patch_data, has_user_auth_cookie=has_user_auth_cookie
                        ):
                            modified = True
                if modified:
                    text = json.dumps(arr, ensure_ascii=False)
        except Exception:
            pass

    # 4. Patch watchNextResponse in direct /youtubei/v1/next JSON object
    elif text.startswith("{") and '"twoColumnWatchNextResults"' in text and '"playabilityStatus"' not in text:
        try:
            obj = json.loads(text)
            _strip_feed_ads(obj)
            if not has_user_auth_cookie:
                vid = req_vid
                if not vid:
                    vid, _, _, _ = _extract_details_from_watch_next(obj, "")
                is_shorts = bool(parsed_url and "/shorts" in (parsed_url.path or ""))
                if not is_shorts and not _has_related_video_items(obj) and vid:
                    full_wn = (
                        await asyncio.wait_for(next_task, timeout=2.5)
                        if next_task is not None
                        else await asyncio.wait_for(_fetch_full_watch_next(vid), timeout=2.5)
                    )
                    if full_wn and _has_related_video_items(full_wn):
                        _strip_feed_ads(full_wn)
                        obj = full_wn
                        modified = True
                    elif await _ensure_watch_next_secondary_results(obj, vid):
                        modified = True
                else:
                    if await _ensure_watch_next_secondary_results(obj, vid):
                        modified = True
            if modified:
                text = json.dumps(obj, ensure_ascii=False)
        except Exception:
            pass

    # 5. Patch playerResponse in JSON object (/youtubei/v1/player, /youtubei/v1/reel/reel_item_watch, /youtubei/v1/reel/reel_watch_sequence, /shorts?pbj=1)
    elif text.startswith("{") and '"playabilityStatus"' in text:
        try:
            obj = json.loads(text)
            _strip_feed_ads(obj)
            if isinstance(obj.get("entries"), list):
                if _patch_reel_sequence_obj(obj, has_user_auth_cookie=has_user_auth_cookie):
                    modified = True
            if isinstance(obj.get("response"), dict):
                if _patch_reel_sequence_obj(obj["response"], has_user_auth_cookie=has_user_auth_cookie):
                    modified = True
            if "playerResponse" in obj and isinstance(obj["playerResponse"], dict):
                pr = obj["playerResponse"]
                wn_src = obj.get("response") if isinstance(obj.get("response"), dict) else obj
                vid, title, author, cid = _extract_details_from_watch_next(
                    wn_src, req_vid
                )
                if not vid:
                    m_vid = re.search(r'"videoId":\s*"([A-Za-z0-9_-]{11})"', text)
                    vid = m_vid.group(1) if m_vid else ""
                if vid:
                    pr_ok = (
                        (pr.get("playabilityStatus") or {}).get("status") == "OK"
                        and _has_playable_streaming_data(pr.get("streamingData"))
                    )
                    if pr_ok:
                        if _apply_playable_patch(pr, {}, has_user_auth_cookie=has_user_auth_cookie):
                            modified = True
                    else:
                        is_shorts = bool(
                            (
                                parsed_url
                                and (
                                    "/shorts" in (parsed_url.path or "")
                                    or "/reel/" in (parsed_url.path or "")
                                )
                            )
                            or obj.get("page") == "shorts"
                        )
                        vd_len = 0.0
                        try:
                            vd_len = float((obj.get("videoDetails") or pr.get("videoDetails") or {}).get("lengthSeconds") or 0.0)
                        except Exception:
                            pass
                        p_patch = await _build_best_fallback_patch(
                            vid, title, author, cid, stream_task, req_vid, is_shorts=is_shorts, fallback_dur=vd_len
                        )
                        if _apply_playable_patch(pr, p_patch, has_user_auth_cookie=has_user_auth_cookie):
                            modified = True
                        if not has_user_auth_cookie and isinstance(wn_src, dict) and isinstance(wn_src.get("replacementEndpoint"), dict):
                            rwe_rep = wn_src["replacementEndpoint"].get("reelWatchEndpoint")
                            if isinstance(rwe_rep, dict) and rwe_rep.get("videoId") == vid:
                                rwe_rep["unserializedPrefetchData"] = {"playerResponse": p_patch}
                                modified = True
            elif isinstance(obj.get("playabilityStatus"), dict):
                vid = (obj.get("videoDetails") or {}).get("videoId") or req_vid
                if not vid:
                    m_vid = re.search(r'"videoId":\s*"([A-Za-z0-9_-]{11})"', text)
                    vid = m_vid.group(1) if m_vid else ""
                if vid:
                    pr_ok = (
                        (obj.get("playabilityStatus") or {}).get("status") == "OK"
                        and _has_playable_streaming_data(obj.get("streamingData"))
                    )
                    if pr_ok:
                        if _apply_playable_patch(obj, {}, has_user_auth_cookie=has_user_auth_cookie):
                            modified = True
                    else:
                        is_shorts = bool(
                            parsed_url
                            and (
                                "/shorts" in (parsed_url.path or "")
                                or "/reel/" in (parsed_url.path or "")
                            )
                        )
                        vd_len = 0.0
                        try:
                            vd_len = float((obj.get("videoDetails") or {}).get("lengthSeconds") or 0.0)
                        except Exception:
                            pass
                        p_patch = await _build_best_fallback_patch(
                            vid, "YouTube Video", "YouTube", "UCBR8-60-B28hp2BmDPdntcQ",
                            stream_task, req_vid, is_shorts=is_shorts, fallback_dur=vd_len
                        )
                        if _apply_playable_patch(obj, p_patch, has_user_auth_cookie=has_user_auth_cookie):
                            modified = True
            if modified:
                text = json.dumps(obj, ensure_ascii=False)
        except Exception:
            pass

    # 6. Patch & pre-resolve upcoming Shorts in reel_watch_sequence & reel_item_watch
    elif text.startswith("{") and ('"reelWatchEndpoint"' in text or '"reelPlayerOverlayRenderer"' in text):
        try:
            obj = json.loads(text)
            if _patch_reel_sequence_obj(obj, has_user_auth_cookie=has_user_auth_cookie):
                text = json.dumps(obj, ensure_ascii=False)
                modified = True
        except Exception:
            pass

    # 7. Strip ads in browse/search feeds
    elif text.startswith("{") and any(k in text for k in ('"twoColumnBrowseResultsRenderer"', '"itemSectionRenderer"', '"richGridRenderer"')):
        try:
            obj = json.loads(text)
            _strip_feed_ads(obj)
            text = json.dumps(obj, ensure_ascii=False)
            modified = True
        except Exception:
            pass

    return text.encode("utf-8") if modified else resp_body


@router.api_route(
    "/youtube/runtime/gv-stream",
    methods=["GET", "POST", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def youtube_runtime_gv_stream(request: Request):
    cors_hdrs = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, HEAD, OPTIONS",
        "Access-Control-Allow-Headers": "*",
        "Access-Control-Expose-Headers": "*",
        "Cross-Origin-Resource-Policy": "cross-origin",
    }
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=cors_hdrs)
    k_val = (request.query_params.get("k") or "").strip()
    raw_url = _decode_media_key(k_val) if k_val else (request.query_params.get("url") or "").strip()
    raw_url = _strip_dollar_query(raw_url)
    try:
        parsed = urlparse(raw_url)
    except Exception:
        return Response(b"bad url", status_code=400, headers=cors_hdrs)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host.endswith((".googlevideo.com", ".c.youtube.com")):
        return Response(b"forbidden host", status_code=403, headers=cors_hdrs)
    q_str = parsed.query or ""
    if (
        (parsed.path or "") in ("/generate_204", "/gen_204")
        or " " in (parsed.path or "")
        or "expire=9421968081" in q_str
        or "expire=1685394838" in q_str
    ):
        return Response(status_code=204, headers=cors_hdrs)
    try:
        await _ensure_public_host(host, 443)
    except Exception:
        return Response(b"blocked host", status_code=403, headers=cors_hdrs)

    fwd = {
        "User-Agent": _DESKTOP_CHROME_UA,
        "Origin": "https://www.youtube.com",
        "Referer": "https://www.youtube.com/",
        "Accept": request.headers.get("accept") or "*/*",
    }
    for hk in ("range", "content-type", "x-client-data"):
        hv = request.headers.get(hk)
        if hv:
            fwd[hk.title()] = hv

    post_body = await request.body() if request.method not in ("GET", "HEAD", "OPTIONS") else None
    client = await _get_media_http_client()
    try:
        req_out = client.build_request(
            request.method,
            raw_url,
            headers=fwd,
            content=post_body if post_body else None,
        )
        upstream = await client.send(req_out, stream=True, follow_redirects=True)
    except Exception:
        return Response(b"upstream media error", status_code=502, headers=cors_hdrs)

    out_hdrs = dict(cors_hdrs)
    for hk in (
        "content-type",
        "content-length",
        "content-range",
        "accept-ranges",
        "cache-control",
        "x-head-time-millis",
        "x-head-seqnum",
        "x-walltime-ms",
        "x-sequence-num",
    ):
        hv = upstream.headers.get(hk)
        if hv:
            out_hdrs[hk] = hv
    if request.method == "HEAD" or upstream.status_code in (204, 304):
        await upstream.aclose()
        return Response(status_code=upstream.status_code, headers=out_hdrs)

    async def _iter_gv():
        try:
            async for chunk in upstream.aiter_bytes(65536):
                yield chunk
        except Exception:
            pass
        finally:
            await upstream.aclose()

    return StreamingResponse(_iter_gv(), status_code=upstream.status_code, headers=out_hdrs)


async def _execute_relay_core(
    url: str,
    method: str,
    raw_headers: list | dict,
    req_body: bytes,
) -> tuple[int, str, list[list[str]], bytes]:
    try:
        parsed = urlparse(url)
    except Exception:
        return 400, "Bad Request", [["content-type", "text/plain"]], b"bad url"

    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return 400, "Bad Request", [["content-type", "text/plain"]], b"unsupported scheme"

    url = _strip_dollar_query(url)
    parsed = urlparse(url)
    host = parsed.hostname.lower().rstrip(".")
    if host == "fonts.googleapis.com" and (parsed.path or "") in ("/css", "/css2"):
        return (
            200,
            "OK",
            [
                ["content-type", "text/css; charset=utf-8"],
                ["access-control-allow-origin", "*"],
                ["cache-control", "public, max-age=86400"],
            ],
            b"/* sh-fast-fonts */",
        )
    if host.endswith((".googlevideo.com", ".c.youtube.com")):
        q_str = parsed.query or ""
        if " " in (parsed.path or "") or "expire=9421968081" in q_str or "expire=1685394838" in q_str:
            return (
                204, "No Content", [["access-control-allow-origin", "*"]], b""
            )
    path_str = parsed.path or ""
    if path_str in ("/generate_204", "/csi_204", "/error_204") and not (host in AUTH_HOSTS or host.endswith(".google.com")):
        return (
            204,
            "No Content",
            [
                ["content-type", "text/plain"],
                ["access-control-allow-origin", "*"],
                ["access-control-allow-credentials", "true"],
            ],
            b"",
        )
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port not in (80, 443):
        return 403, "Forbidden", [["content-type", "text/plain"]], b"unsupported port"

    try:
        await _ensure_public_host(host, port)
    except Exception:
        return 403, "Forbidden", [["content-type", "text/plain"]], b"blocked host"

    if host in YOUTUBE_HOSTS and not req_body and "pbj=1" in (parsed.query or "") and (parsed.path or "").rstrip("/") == "/shorts":
        method = "GET"

    cacheable = _is_cacheable_static_asset(method, url)
    if cacheable:
        cached = _STATIC_ASSET_CACHE.get(url)
        if cached and (time.time() - cached[0]) < _STATIC_ASSET_TTL:
            _, c_status, c_text, c_headers, c_body = cached
            return c_status, c_text, c_headers, c_body

    req_headers: list[tuple[str, str]] = []
    seen_ua = False
    seen_al = False
    if isinstance(raw_headers, list):
        items = raw_headers
    elif isinstance(raw_headers, dict):
        items = list(raw_headers.items())
    else:
        items = []

    _STRIP_CH_HEADERS = {
        "sec-ch-ua",
        "sec-ch-ua-mobile",
        "sec-ch-ua-platform",
        "sec-ch-ua-platform-version",
        "sec-ch-ua-full-version",
        "sec-ch-ua-full-version-list",
        "sec-ch-ua-arch",
        "sec-ch-ua-bitness",
        "sec-ch-ua-model",
        "sec-ch-ua-wow64",
        "sec-ch-ua-form-factors",
    }

    is_yt_host = host in YOUTUBE_HOSTS
    is_google_auth_req = (
        not is_yt_host
        and not host.endswith((".ytimg.com", ".googlevideo.com", ".ggpht.com", ".googleusercontent.com"))
        and (
            host in AUTH_HOSTS
            or host.endswith(".google.com")
            or host.endswith(".gstatic.com")
        )
    )

    client_ua = ""
    client_ch_ua = ""
    client_ch_mobile = ""
    client_ch_platform = ""
    for item in items:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            ik = str(item[0]).strip().lower()
            iv = str(item[1]).strip()
            if ik == "user-agent":
                client_ua = iv.replace("HeadlessChrome/", "Chrome/")
            elif ik == "sec-ch-ua":
                client_ch_ua = iv
            elif ik == "sec-ch-ua-mobile":
                client_ch_mobile = iv
            elif ik == "sec-ch-ua-platform":
                client_ch_platform = iv

    is_safari_ua = (
        bool(client_ua)
        and "Safari/" in client_ua
        and "Chrome/" not in client_ua
        and "Chromium/" not in client_ua
        and "CriOS/" not in client_ua
        and "Edg/" not in client_ua
    )

    effective_ua = (
        client_ua
        if (is_google_auth_req and len(client_ua) >= 20)
        else _DESKTOP_CHROME_UA
    )

    for item in items:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        k = str(item[0]).strip()
        v = str(item[1]).strip()
        kl = k.lower()
        if not k or kl in _RELAY_STRIP_REQ_HEADERS or kl in _STRIP_CH_HEADERS or kl.startswith(":"):
            continue
        if kl == "content-encoding":
            if method in ("GET", "HEAD") or not req_body or (v.lower() == "gzip" and req_body[:2] != b"\x1f\x8b"):
                continue
        if kl == "content-type" and method in ("GET", "HEAD") and not req_body:
            continue
        if kl == "user-agent":
            seen_ua = True
            v = effective_ua
        elif kl == "accept-language":
            seen_al = True
        req_headers.append((k, v))

    if not seen_ua:
        req_headers.append(("User-Agent", effective_ua))
    if not seen_al:
        req_headers.append(("Accept-Language", "ja,en-US;q=0.9,en;q=0.8"))
    if not (is_google_auth_req and is_safari_ua):
        if is_google_auth_req:
            m_ver = re.search(r"(?:Chrome|Chromium)/(\d+)", effective_ua)
            c_ver = m_ver.group(1) if m_ver else "131"
            ch_ua = client_ch_ua or f'"Google Chrome";v="{c_ver}", "Chromium";v="{c_ver}", "Not_A Brand";v="24"'
            ch_mob = client_ch_mobile or ("?1" if ("Mobile" in effective_ua and "iPad" not in effective_ua) else "?0")
            if client_ch_platform:
                ch_plat = client_ch_platform
            elif "Android" in effective_ua:
                ch_plat = '"Android"'
            elif "CrOS" in effective_ua:
                ch_plat = '"Chrome OS"'
            elif "Macintosh" in effective_ua or "Mac OS X" in effective_ua:
                ch_plat = '"macOS"'
            elif "Linux" in effective_ua:
                ch_plat = '"Linux"'
            else:
                ch_plat = '"Windows"'
            req_headers.extend([
                ("sec-ch-ua", ch_ua),
                ("sec-ch-ua-mobile", ch_mob),
                ("sec-ch-ua-platform", ch_plat),
            ])
        else:
            req_headers.extend([
                ("sec-ch-ua", '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"'),
                ("sec-ch-ua-mobile", "?0"),
                ("sec-ch-ua-platform", '"Windows"'),
            ])

    header_dict: dict[str, str] = {}
    cookie_parts: list[str] = []
    for k, v in req_headers:
        if k.lower() == "cookie":
            cookie_parts.append(v)
        else:
            header_dict[k] = v
    if cookie_parts and not cacheable:
        header_dict["Cookie"] = "; ".join(cookie_parts)

    # Relay real googlevideo.com / .c.youtube.com MSE, SABR, and HLS requests
    # (dummy bandwidth probes were already filtered above by expire=9421968081 / 1685394838)
    if host.endswith((".googlevideo.com", ".c.youtube.com")):
        if method == "OPTIONS":
            return (
                204,
                "No Content",
                [
                    ["access-control-allow-origin", "https://www.youtube.com"],
                    ["access-control-allow-credentials", "true"],
                    ["access-control-allow-methods", "GET, HEAD, POST, OPTIONS"],
                    ["access-control-allow-headers", "*"],
                    ["access-control-expose-headers", "*"],
                ],
                b"",
            )
        gv_headers: dict[str, str] = {
            "User-Agent": _DESKTOP_CHROME_UA,
            "Origin": "https://www.youtube.com",
            "Referer": "https://www.youtube.com/",
            "Accept": header_dict.get("Accept") or header_dict.get("accept") or "*/*",
        }
        for hk, hv in header_dict.items():
            if hk.lower() in ("range", "content-type", "x-client-data"):
                gv_headers[hk] = hv
        try:
            session = await _get_curl_session()
            cur_gv_url = url
            status = 502
            status_text = "Bad Gateway"
            resp_body = b""
            raw_multi = []
            for _hop in range(5):
                if session is not None:
                    up = await session.request(
                        method=method,
                        url=cur_gv_url,
                        headers=gv_headers,
                        data=req_body if req_body else None,
                        allow_redirects=False,
                        timeout=25,
                    )
                    status = int(up.status_code)
                    status_text = str(getattr(up, "reason", "") or "OK")
                    resp_body = bytes(up.content or b"")
                    raw_multi = list(up.headers.multi_items())
                else:
                    client = await _get_media_http_client()
                    up = await client.request(
                        method=method,
                        url=cur_gv_url,
                        headers=gv_headers,
                        content=req_body if req_body else None,
                        follow_redirects=False,
                    )
                    status = int(up.status_code)
                    status_text = str(up.reason_phrase or "OK")
                    resp_body = bytes(up.content or b"")
                    raw_multi = list(up.headers.multi_items())
                if status in (301, 302, 303, 307, 308):
                    loc = ""
                    for rk, rv in raw_multi:
                        if rk.lower() == "location":
                            loc = rv.strip()
                            break
                    if loc:
                        next_u = urljoin(cur_gv_url, loc)
                        np = urlparse(next_u)
                        nh = (np.hostname or "").lower().rstrip(".")
                        if np.scheme == "https" and nh.endswith((".googlevideo.com", ".c.youtube.com")):
                            await _ensure_public_host(nh, 443)
                            cur_gv_url = next_u
                            continue
                break
            gv_out: list[list[str]] = [
                ["access-control-allow-origin", "https://www.youtube.com"],
                ["access-control-allow-credentials", "true"],
                ["access-control-expose-headers", "*"],
                ["cross-origin-resource-policy", "cross-origin"],
            ]
            for rk, rv in raw_multi:
                rkl = rk.lower()
                if rkl in _RELAY_STRIP_RESP_HEADERS or rkl.startswith(":") or rkl.startswith("access-control-"):
                    continue
                gv_out.append([rk, rv])
            return status, status_text, gv_out, resp_body
        except Exception as exc:
            return (
                502,
                "Bad Gateway",
                [["content-type", "text/plain; charset=utf-8"], ["access-control-allow-origin", "*"]],
                f"googlevideo relay error: {exc}".encode("utf-8", "ignore"),
            )

    if host == "youtube.com" and method == "GET":
        return (
            301,
            "Moved Permanently",
            [["location", urlunparse(("https", "www.youtube.com", parsed.path or "/", "", parsed.query, ""))]],
            b"",
        )

    # Handle virtual /yts/stream/{vid}.mp4 if fetched via Scramjet fetch/XHR
    if is_yt_host and method in ("GET", "HEAD"):
        m_vstream = re.match(r"^/yts/stream/([A-Za-z0-9_-]{11})\.mp4$", parsed.path or "")
        if m_vstream:
            vid_s = m_vstream.group(1)
            return (
                302,
                "Found",
                [
                    ["location", f"/youtube/runtime/stream/{vid_s}"],
                    ["access-control-allow-origin", "*"],
                ],
                b"",
            )

    # Ensure unauthenticated YouTube requests have anonymous visitor bootstrap cookies
    # so direct navigations to /watch or /results from Render's IP never hit 302 /sorry/index.
    existing_cookie_str = header_dict.get("Cookie") or ""
    has_user_auth_cookie = any(m in existing_cookie_str for m in _AUTH_COOKIE_MARKERS)
    has_sensitive_auth = (
        has_user_auth_cookie
        or host in AUTH_HOSTS
        or host.endswith(".google.com")
        or any(k.lower() in ("authorization", "x-goog-authuser") for k in header_dict)
    )
    is_root_nav = (parsed.path or "") in ("", "/") and not (parsed.query or "")
    if (
        is_yt_host
        and not cacheable
        and not is_root_nav
        and not has_user_auth_cookie
        and "__Secure-YNID=" not in existing_cookie_str
    ):
        anon_hdr = await _get_anon_visitor_cookie_header()
        if anon_hdr:
            existing_cookie_str = (
                f"{anon_hdr}; {existing_cookie_str}" if existing_cookie_str else anon_hdr
            )
            header_dict["Cookie"] = existing_cookie_str
    injected_pref = False
    if is_yt_host and not cacheable and not has_user_auth_cookie and "PREF=" not in existing_cookie_str:
        pref_c = "PREF=f6=400&hl=ja&gl=JP"
        existing_cookie_str = f"{existing_cookie_str}; {pref_c}" if existing_cookie_str else pref_c
        header_dict["Cookie"] = existing_cookie_str
        injected_pref = True

    if is_yt_host and "themeRefresh=1" in (parsed.query or ""):
        clean_q = "&".join(
            p for p in (parsed.query or "").split("&") if p and not p.startswith("themeRefresh=")
        )
        parsed = parsed._replace(query=clean_q)
        url = parsed.geturl()

    # Start stream resolution, moov head pre-warming, watch_next fetch, or home feed fetch in parallel when unauthenticated
    req_vid = _extract_request_video_id(parsed, req_body) if is_yt_host else ""
    if is_yt_host and method == "GET" and parsed.path in ("", "/") and "sh_watch=" in (parsed.query or "") and req_vid:
        url = f"https://www.youtube.com/watch?v={req_vid}"
        parsed = urlparse(url)
    if req_vid and not has_user_auth_cookie:
        _PREWARM_REQUESTED_VIDS.add(req_vid)
        _ensure_stream_head_prewarm(
            req_vid,
            f"https://invid-api.poketube.fun/companion/latest_version?id={req_vid}&itag=18&local=true",
        )

    # Sanitize Origin & Referer without clobbering legitimate virtual origins (critical for SAPISIDHASH)
    orig_ref = ""
    orig_origin = ""
    has_origin = False
    for hk in list(header_dict.keys()):
        hkl = hk.lower()
        if hkl == "referer":
            orig_ref = header_dict.pop(hk)
        elif hkl == "origin":
            has_origin = True
            orig_origin = header_dict.pop(hk)

    def _is_leaked_proxy_url(u_val: str) -> bool:
        if not u_val:
            return True
        ul = u_val.lower()
        return (
            "/~/sj/" in ul
            or ".onrender.com" in ul
            or ".ai.studio" in ul
            or "localhost" in ul
            or "127.0.0.1" in ul
        )

    default_yt_origin = (
        f"https://{host}"
        if host in ("studio.youtube.com", "music.youtube.com", "accounts.youtube.com")
        else "https://www.youtube.com"
    )

    if is_yt_host or host.endswith(".youtube.com") or host == "youtube.com":
        if has_origin or method == "POST":
            header_dict["Origin"] = orig_origin if not _is_leaked_proxy_url(orig_origin) else default_yt_origin
        if _is_leaked_proxy_url(orig_ref):
            if req_vid and host == "www.youtube.com":
                header_dict["Referer"] = f"https://www.youtube.com/watch?v={req_vid}"
            elif "/playables" in (parsed.path or ""):
                header_dict["Referer"] = "https://www.youtube.com/playables"
            else:
                header_dict["Referer"] = f"{default_yt_origin}/"
        else:
            header_dict["Referer"] = orig_ref
    elif host in AUTH_HOSTS or host.endswith(".google.com") or host == "google.com":
        if has_origin or method == "POST":
            header_dict["Origin"] = orig_origin if not _is_leaked_proxy_url(orig_origin) else f"https://{host}"
        if _is_leaked_proxy_url(orig_ref):
            header_dict["Referer"] = f"https://{host}/"
        else:
            header_dict["Referer"] = orig_ref
    elif host.endswith((".usercontent.goog", ".playables.usercontent.goog")):
        if has_origin or method == "POST":
            header_dict["Origin"] = orig_origin if not _is_leaked_proxy_url(orig_origin) else "https://www.youtube.com"
        header_dict["Referer"] = orig_ref if not _is_leaked_proxy_url(orig_ref) else "https://www.youtube.com/"
    else:
        if not _is_leaked_proxy_url(orig_ref):
            header_dict["Referer"] = orig_ref
        if has_origin:
            header_dict["Origin"] = orig_origin if not _is_leaked_proxy_url(orig_origin) else f"https://{host}"

    stream_task = (
        asyncio.create_task(_resolve_stream_info(req_vid))
        if (req_vid and not has_user_auth_cookie)
        else None
    )
    is_get_watch = (
        is_yt_host
        and not has_user_auth_cookie
        and method == "POST"
        and (
            parsed.path.startswith("/youtubei/v1/get_watch")
            or parsed.path.startswith("/youtubei/v1/next")
        )
        and bool(req_vid)
    )
    next_task = (
        asyncio.create_task(_fetch_full_watch_next(req_vid)) if is_get_watch else None
    )
    is_home_req = (
        is_yt_host
        and not has_user_auth_cookie
        and "sh_watch=" not in (parsed.query or "")
        and (
            (method == "GET" and parsed.path in ("", "/"))
            or (method == "POST" and parsed.path.startswith("/youtubei/v1/browse"))
        )
    )
    home_task = asyncio.create_task(_get_home_rich_items()) if is_home_req else None

    ephemeral_session: StatelessCurlSession | None = None
    try:
        if StatelessCurlSession is not None:
            if is_google_auth_req and is_safari_ua:
                tls_imp = "safari18_0_ios" if ("iPad" in client_ua or "iPhone" in client_ua or "Mobile" in client_ua) else "safari18_0"
                ephemeral_session = StatelessCurlSession(
                    impersonate=tls_imp,
                    allow_redirects=False,
                    timeout=25,
                    max_clients=4,
                )
                session = ephemeral_session
            else:
                session = await _get_curl_session()
        else:
            session = None

        used_curl = False
        if session is not None:
            try:
                upstream = await session.request(
                    method=method,
                    url=url,
                    headers=header_dict,
                    data=req_body if req_body else None,
                    allow_redirects=False,
                    timeout=22,
                )
                status = int(upstream.status_code)
                status_text = str(getattr(upstream, "reason", "") or "OK")
                resp_body = bytes(upstream.content or b"")
                raw_multi = upstream.headers.multi_items()
                if (
                    is_yt_host
                    and not has_user_auth_cookie
                    and status in (302, 303)
                    and any(
                        k.lower() == "location" and "/sorry/" in v
                        for k, v in raw_multi
                    )
                ):
                    anon_hdr = await _get_anon_visitor_cookie_header(force_refresh=True)
                    if anon_hdr:
                        header_dict["Cookie"] = f"{anon_hdr}; PREF=f6=400&hl=ja&gl=JP"
                        upstream = await session.request(
                            method=method,
                            url=url,
                            headers=header_dict,
                            data=req_body if req_body else None,
                            allow_redirects=False,
                            timeout=22,
                        )
                        status = int(upstream.status_code)
                        status_text = str(getattr(upstream, "reason", "") or "OK")
                        resp_body = bytes(upstream.content or b"")
                        raw_multi = upstream.headers.multi_items()
                used_curl = True
            except Exception:
                used_curl = False

        if not used_curl:
            client = await _get_media_http_client()
            upstream = await client.request(
                method=method,
                url=url,
                headers=header_dict,
                content=req_body if req_body else None,
                follow_redirects=False,
            )
            status = int(upstream.status_code)
            status_text = str(upstream.reason_phrase or "OK")
            resp_body = bytes(upstream.content or b"")
            raw_multi = list(upstream.headers.multi_items())
            if (
                is_yt_host
                and not has_user_auth_cookie
                and status in (302, 303)
                and any(
                    k.lower() == "location" and "/sorry/" in v
                    for k, v in raw_multi
                )
            ):
                anon_hdr = await _get_anon_visitor_cookie_header(force_refresh=True)
                if anon_hdr:
                    header_dict["Cookie"] = f"{anon_hdr}; PREF=f6=400&hl=ja&gl=JP"
                    upstream = await client.request(
                        method=method,
                        url=url,
                        headers=header_dict,
                        content=req_body if req_body else None,
                        follow_redirects=False,
                    )
                    status = int(upstream.status_code)
                    status_text = str(upstream.reason_phrase or "OK")
                    resp_body = bytes(upstream.content or b"")
                    raw_multi = list(upstream.headers.multi_items())
    except Exception as exc:
        for t in (stream_task, home_task, next_task):
            if t and not t.done():
                t.cancel()
        err_msg = f"upstream error: {exc}".encode("utf-8", "ignore")
        return (
            502,
            "Bad Gateway",
            [["content-type", "text/plain; charset=utf-8"]],
            err_msg,
        )
    finally:
        if ephemeral_session is not None:
            try:
                await ephemeral_session.close()
            except Exception:
                pass

    if is_yt_host and not has_user_auth_cookie and status == 200:
        found_anon = _extract_anon_cookies_from_headers(raw_multi)
        if found_anon:
            global _ANON_VISITOR_CACHE
            merged_anon = dict(_ANON_VISITOR_CACHE[1])
            merged_anon.update(found_anon)
            _ANON_VISITOR_CACHE = (time.time(), merged_anon)

    if (
        is_yt_host
        and method == "GET"
        and req_vid
        and status in (302, 303)
        and any(k.lower() == "location" and "/sorry/" in v for k, v in raw_multi)
    ):
        _PREWARM_REQUESTED_VIDS.add(req_vid)
        for t in (home_task, next_task):
            if t and not t.done():
                t.cancel()
        return (
            302,
            "Found",
            [
                ["location", f"https://www.youtube.com/?sh_watch={req_vid}"],
                ["cache-control", "no-store"],
            ],
            b"",
        )

    is_logout_req = (
        (is_yt_host and (parsed.path or "").lower().startswith("/logout"))
        or (host in AUTH_HOSTS and "/logout" in (parsed.path or "").lower())
    )
    out_headers: list[list[str]] = []
    if is_logout_req:
        out_headers.append(["x-sh-clear-auth", "1"])
    has_set_cookie = False
    for k, v in raw_multi:
        kl = k.lower()
        if kl in _RELAY_STRIP_RESP_HEADERS or kl.startswith(":"):
            continue
        if kl == "set-cookie":
            has_set_cookie = True
            v_norm = re.sub(r";\s*partitioned\b", "", v, flags=re.IGNORECASE)
            v_norm = re.sub(r";\s*priority=[^;]*", "", v_norm, flags=re.IGNORECASE)
            if not v_norm.lstrip().startswith("__Host-"):
                v_norm = re.sub(r";\s*domain=google\.com\b", "; Domain=.google.com", v_norm, flags=re.IGNORECASE)
                v_norm = re.sub(r";\s*domain=youtube\.com\b", "; Domain=.youtube.com", v_norm, flags=re.IGNORECASE)
            v = v_norm
        out_headers.append([k, v])
    if injected_pref and not cacheable and not is_logout_req:
        out_headers.append(
            [
                "set-cookie",
                "PREF=f6=400&hl=ja&gl=JP; Path=/; Domain=.youtube.com; Max-Age=31536000; SameSite=Lax",
            ]
        )

    if is_yt_host and status == 200 and resp_body:
        resp_body = await _maybe_patch_youtube_payload(
            parsed,
            req_vid,
            out_headers,
            resp_body,
            stream_task,
            home_task,
            next_task,
            has_user_auth_cookie=has_user_auth_cookie,
        )
    else:
        for t in (stream_task, home_task, next_task):
            if t and not t.done():
                t.cancel()

    if (
        cacheable
        and not has_set_cookie
        and status == 200
        and 0 < len(resp_body) <= _STATIC_ASSET_MAX_BYTES
    ):
        if len(_STATIC_ASSET_CACHE) >= _STATIC_ASSET_MAX_ITEMS:
            oldest_key = next(iter(_STATIC_ASSET_CACHE))
            _STATIC_ASSET_CACHE.pop(oldest_key, None)
        _STATIC_ASSET_CACHE[url] = (
            time.time(),
            status,
            status_text,
            out_headers,
            resp_body,
        )

    return status, status_text, out_headers, resp_body


@router.post("/youtube/runtime/http-relay", include_in_schema=False)
async def youtube_http_relay(request: Request):
    raw = await request.body()
    if len(raw) < 4:
        return Response(b"invalid frame", status_code=400)
    meta_len = struct.unpack(">I", raw[:4])[0]
    if len(raw) < 4 + meta_len:
        return Response(b"truncated frame", status_code=400)
    try:
        meta = json.loads(raw[4 : 4 + meta_len].decode("utf-8"))
    except Exception:
        return Response(b"bad json", status_code=400)

    req_body = raw[4 + meta_len :]
    url = str(meta.get("url") or "").strip()
    method = str(meta.get("method") or "GET").upper().strip()
    raw_headers = meta.get("headers") or []

    status, status_text, out_headers, resp_body = await _execute_relay_core(
        url, method, raw_headers, req_body
    )
    accept_enc = request.headers.get("accept-encoding", "")
    return _pack_relay_response(status, status_text, out_headers, resp_body, accept_encoding=accept_enc)


# ---------------------------------------------------------------------------
# Race-free Wisp endpoint (fixes dropped TLS ClientHello 0x02 packets)
# ---------------------------------------------------------------------------
async def _youtube_wisp_endpoint(websocket: WebSocket):
    import app as host_app

    requested = list(websocket.scope.get("subprotocols", []) or [])
    v2 = bool(requested)
    try:
        await websocket.accept(subprotocol=requested[0] if requested else None)
    except Exception:
        return

    streams: dict[int, asyncio.StreamWriter] = {}
    tasks: dict[int, asyncio.Task] = {}
    pending_queues: dict[int, list[bytes]] = {}
    extensions: set[int] = set()
    alive = True

    async def send_packet(data: bytes) -> bool:
        nonlocal alive
        if not alive:
            return False
        try:
            await websocket.send_bytes(data)
            return True
        except Exception:
            alive = False
            return False

    async def close_stream(stream_id: int, reason: int = 2, notify: bool = True):
        pending_queues.pop(stream_id, None)
        writer = streams.pop(stream_id, None)
        task = tasks.pop(stream_id, None)
        if task and task is not asyncio.current_task():
            task.cancel()
        if writer:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        if notify:
            await send_packet(host_app.wisp_close(stream_id, reason))

    async def pump(stream_id: int, reader: asyncio.StreamReader):
        try:
            while alive:
                data = await asyncio.wait_for(
                    reader.read(64 * 1024), host_app.WISP_IDLE_TIMEOUT
                )
                if not data:
                    await send_packet(host_app.wisp_close(stream_id, 2))
                    break
                if not await send_packet(host_app.wisp_packet(0x02, stream_id, data)):
                    break
        except Exception:
            await send_packet(host_app.wisp_close(stream_id, 0x03))
        finally:
            pending_queues.pop(stream_id, None)
            writer = streams.pop(stream_id, None)
            tasks.pop(stream_id, None)
            if writer:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

    try:
        if v2:
            await send_packet(host_app.wisp_info())
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    return
                raw = message.get("bytes") or b""
                if len(raw) < 5:
                    continue
                kind = raw[0]
                stream_id = struct.unpack_from("<I", raw, 1)[0]
                payload = raw[5:]
                if stream_id != 0:
                    await send_packet(host_app.wisp_close(stream_id, 0x41))
                    continue
                if kind == 0x05:
                    if len(payload) < 2 or payload[0] != 2:
                        await send_packet(host_app.wisp_close(0, 0x04))
                        return
                    extensions = host_app.wisp_extensions(payload)
                    break
                if kind == 0x03:
                    v2 = False
                    break
                if kind == 0x04:
                    return
                await send_packet(host_app.wisp_close(0, 0x04))
                return

        await send_packet(host_app.wisp_continue(0, host_app.WISP_BUFFER))
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            raw = message.get("bytes")
            if not raw or len(raw) < 5:
                continue
            kind = raw[0]
            stream_id = struct.unpack_from("<I", raw, 1)[0]
            payload = raw[5:]
            if kind == 0x01:
                if (
                    stream_id == 0
                    or stream_id in streams
                    or stream_id in pending_queues
                    or len(streams) + len(pending_queues) >= host_app.WISP_MAX_STREAMS
                    or len(payload) < 3
                ):
                    await send_packet(host_app.wisp_close(stream_id, 0x41))
                    continue
                stream_type = payload[0]
                port = struct.unpack_from("<H", payload, 1)[0]
                try:
                    host = (
                        payload[3:]
                        .decode("utf-8", "strict")
                        .strip()
                        .rstrip(".")
                        .lower()
                    )
                except UnicodeDecodeError:
                    await send_packet(host_app.wisp_close(stream_id, 0x41))
                    continue
                if (
                    stream_type != 1
                    or not host
                    or len(host) > 253
                    or not (1 <= port <= 65535)
                    or any(c.isspace() for c in host)
                ):
                    await send_packet(host_app.wisp_close(stream_id, 0x41))
                    continue

                pending_queues[stream_id] = []

                async def open_one(sid=stream_id, hostname=host, dst_port=port):
                    try:
                        reader, writer = await host_app.public_tcp(hostname, dst_port)
                        buf = pending_queues.pop(sid, None)
                        if buf is None:
                            writer.close()
                            try:
                                await writer.wait_closed()
                            except Exception:
                                pass
                            return
                        streams[sid] = writer
                        if v2 and 0x05 in extensions:
                            await send_packet(
                                host_app.wisp_continue(sid, host_app.WISP_BUFFER)
                            )
                        if buf:
                            for chunk in buf:
                                writer.write(chunk)
                            await writer.drain()
                            await send_packet(
                                host_app.wisp_continue(sid, host_app.WISP_REPLENISH)
                            )
                        tasks[sid] = asyncio.create_task(pump(sid, reader))
                    except Exception:
                        pending_queues.pop(sid, None)
                        await send_packet(host_app.wisp_close(sid, 0x03))

                tasks[stream_id] = asyncio.create_task(open_one())
            elif kind == 0x02:
                writer = streams.get(stream_id)
                if writer is not None:
                    try:
                        if payload:
                            writer.write(payload)
                            await writer.drain()
                            await send_packet(
                                host_app.wisp_continue(
                                    stream_id, host_app.WISP_REPLENISH
                                )
                            )
                    except Exception:
                        await close_stream(stream_id, 0x03)
                elif stream_id in pending_queues:
                    if payload:
                        pending_queues[stream_id].append(payload)
            elif kind == 0x04:
                await close_stream(stream_id, 0x02)
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        pending_queues.clear()
        for task in list(tasks.values()):
            if task is not asyncio.current_task():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks.values(), return_exceptions=True)
        for writer in list(streams.values()):
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        try:
            await websocket.close()
        except Exception:
            pass


@router.websocket("/youtube/wisp/")
async def youtube_wisp_route(websocket: WebSocket):
    await _youtube_wisp_endpoint(websocket)


async def _youtube_google_login_override(request: Request):
    target = request.query_params.get("url", GOOGLE_LOGIN)
    try:
        parsed = urlparse(target)
        host = (parsed.hostname or "").lower().rstrip(".")
        if host not in {"accounts.google.com", "google.com", "www.google.com"} and not host.endswith(".google.com"):
            return Response("invalid google auth target", status_code=400)
        clean = urlunparse((
            parsed.scheme or "https",
            parsed.netloc,
            parsed.path or "/",
            "",
            parsed.query,
            "",
        ))
        return RedirectResponse(
            "/youtube/?q=" + quote(clean, safe=""),
            status_code=302,
            headers={"Cache-Control": "no-store"},
        )
    except Exception:
        return Response("invalid google auth target", status_code=400)


async def _youtube_204_probe(request: Request) -> Response:
    origin = request.headers.get("origin") or "*"
    headers = {
        "access-control-allow-origin": origin,
        "access-control-allow-credentials": "true",
        "access-control-allow-methods": "GET, HEAD, POST, OPTIONS",
        "access-control-allow-headers": "*",
        "cache-control": "no-cache, no-store, must-revalidate",
        "content-length": "0",
    }
    return Response(b"", status_code=204, headers=headers)


@router.api_route("/generate_204", methods=["GET", "HEAD", "POST", "OPTIONS"], include_in_schema=False)
@router.api_route("/gen_204", methods=["GET", "HEAD", "POST", "OPTIONS"], include_in_schema=False)
@router.api_route("/csi_204", methods=["GET", "HEAD", "POST", "OPTIONS"], include_in_schema=False)
@router.api_route("/error_204", methods=["GET", "HEAD", "POST", "OPTIONS"], include_in_schema=False)
async def route_youtube_204_probe(request: Request):
    return await _youtube_204_probe(request)


def _install_wisp_fix() -> None:
    """Install the race-free Wisp handler, SW override, and google-login redirect fix in the host app."""
    try:
        import app as host_app

        host_app.wisp_media_endpoint = _youtube_wisp_endpoint
        if not any(
            isinstance(r, WebSocketRoute)
            and r.path == "/wisp-youtube/"
            and r.endpoint is _youtube_wisp_endpoint
            for r in host_app.app.router.routes
        ):
            host_app.app.router.routes.insert(
                0, WebSocketRoute("/wisp-youtube/", _youtube_wisp_endpoint)
            )
        if not any(
            isinstance(r, Route)
            and r.path == "/youtube/sw.js"
            and r.endpoint is _youtube_sw_override
            for r in host_app.app.router.routes
        ):
            host_app.app.router.routes.insert(
                0, Route("/youtube/sw.js", _youtube_sw_override, methods=["GET", "HEAD"])
            )
        if not any(
            isinstance(r, Route)
            and r.path == "/youtube/google-login"
            and r.endpoint is _youtube_google_login_override
            for r in host_app.app.router.routes
        ):
            host_app.app.router.routes.insert(
                0, Route("/youtube/google-login", _youtube_google_login_override, methods=["GET"])
            )
        for p204 in ("/generate_204", "/gen_204", "/csi_204", "/error_204"):
            if not any(
                isinstance(r, Route) and r.path == p204
                for r in host_app.app.router.routes
            ):
                host_app.app.router.routes.insert(
                    0, Route(p204, _youtube_204_probe, methods=["GET", "HEAD", "POST", "OPTIONS"])
                )
    except Exception as exc:
        print(f"[YOUTUBE] Wisp route install warning: {exc}", flush=True)


_install_wisp_fix()


def _hostname(value: str) -> str:
    try:
        return (urlparse(value).hostname or "").lower().rstrip(".")
    except Exception:
        return ""


def _is_official_youtube_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except Exception:
        return False
    return (
        parsed.scheme == "https"
        and parsed.port in (None, 443)
        and not parsed.username
        and not parsed.password
        and _hostname(value) in YOUTUBE_HOSTS
    )


def _is_google_auth_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except Exception:
        return False
    h = _hostname(value)
    return (
        parsed.scheme == "https"
        and parsed.port in (None, 443)
        and not parsed.username
        and not parsed.password
        and (h in AUTH_HOSTS or h.endswith(".google.com") or h.endswith(".google.co.jp"))
    )


def _canonical_official_url(value: str) -> str:
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    port = parsed.port
    netloc = host
    if port is not None:
        netloc += f":{port}"
    return urlunparse((
        "https",
        netloc,
        parsed.path or "/",
        parsed.params,
        parsed.query,
        parsed.fragment,
    ))


def _target_from_input(raw: str) -> str:
    """Turn launcher input into one official destination, otherwise a search URL."""
    value = (raw or "").strip()
    if len(value) > 2048:
        value = value[:2048]
    if not value:
        return YOUTUBE_HOME

    candidate = value
    lower = candidate.lower()
    if lower.startswith((
        "http://youtube.com/",
        "http://www.youtube.com/",
        "http://m.youtube.com/",
        "http://music.youtube.com/",
        "http://youtu.be/",
        "http://accounts.google.com/",
    )):
        candidate = "https://" + candidate[7:]
    if lower.startswith((
        "youtube.com/",
        "www.youtube.com/",
        "m.youtube.com/",
        "music.youtube.com/",
        "youtu.be/",
    )):
        candidate = "https://" + candidate

    if lower in ("playables", "game", "games", "ゲーム", "プレイアブル") or lower.startswith(("playables", "ゲーム")):
        return "https://www.youtube.com/playables?hl=ja"

    if _is_official_youtube_url(candidate) or _is_google_auth_url(candidate):
        return _canonical_official_url(candidate)

    return "https://www.youtube.com/results?search_query=" + quote(value, safe="")


def _js_literal(value: str) -> str:
    """Safe JSON literal for an inline script; prevent </script> termination."""
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _shell_html(initial_target: str) -> str:
    home_js = _js_literal(YOUTUBE_HOME)
    login_js = _js_literal(GOOGLE_LOGIN)
    initial_js = _js_literal(initial_target)
    wisp_override_js = _js_literal(SCRAMJET_WISP_URL)
    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="theme-color" content="#0f0f0f">
<title>Google</title>
<link rel="icon" type="image/x-icon" href="/favicon.ico">
<link rel="preload" href="/youtube/runtime/scramjet.js?v={RUNTIME_REV}" as="script">
<link rel="preload" href="/youtube/runtime/controller.api.js?v={RUNTIME_REV}" as="script">
<link rel="preload" href="/youtube/runtime/controller.inject.js?v={RUNTIME_REV}" as="script">
<link rel="preload" href="/scramjet/scramjet-utils.js?v={UTILS_VERSION}" as="script">
<link rel="preload" href="/scramjet/scramjet.wasm" as="fetch" crossorigin>
<script>
if ("serviceWorker" in navigator) {{
  window.__shSwRegP = navigator.serviceWorker.register("/youtube/sw.js?v={RUNTIME_REV}", {{
    scope: "/youtube/",
    updateViaCache: "none",
    type: "classic",
  }}).catch(() => null);
}}
</script>
<style>
*{{box-sizing:border-box;touch-action:manipulation}}
html,body{{margin:0;width:100%;height:100%;overflow:hidden;background:#0f0f0f;color:#f1f1f1;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;touch-action:manipulation}}
.stage{{position:fixed;inset:0;width:100%;height:100%;background:#0f0f0f;touch-action:manipulation}}
#frame{{position:fixed;inset:0;display:block;width:100%;height:100%;border:0;background:#0f0f0f;touch-action:manipulation}}
.cover{{position:fixed;inset:0;z-index:5;display:grid;place-items:center;padding:32px;background:#0f0f0f;color:#aaa;font-size:14px;font-weight:500;text-align:center}}
.cover.hidden{{display:none}}
</style>
</head>
<body>
<main class="stage">
  <div class="cover" id="cover">読み込み中…</div>
  <iframe id="frame" title="YouTube"
    allow="autoplay; clipboard-read; clipboard-write; encrypted-media; fullscreen; picture-in-picture; gamepad; microphone; camera; display-capture; web-share; accelerometer; gyroscope"
    referrerpolicy="strict-origin-when-cross-origin"></iframe>
</main>

<script src="/youtube/runtime/scramjet.js?v={RUNTIME_REV}"></script>
<script src="/youtube/runtime/controller.api.js?v={RUNTIME_REV}"></script>
<script src="/scramjet/scramjet-utils.js?v={UTILS_VERSION}"></script>
<script>
(() => {{
  "use strict";

  const HOME = {home_js};
  const LOGIN = {login_js};
  const INITIAL = {initial_js};
  const WISP_OVERRIDE = {wisp_override_js};
  const SW = "/youtube/sw.js?v={RUNTIME_REV}";
  const SW_SCOPE = "/youtube/";
  const PREFIX = "/youtube/~/sj/";
  const WISP_PATH = "/wisp-youtube/";
  const RELAY_ENDPOINT = "/youtube/runtime/http-relay";
  const IMAGE_RELAY_ENDPOINT = "/youtube/runtime/image-relay";
  const GV_STREAM_ENDPOINT = "/youtube/runtime/gv-stream";
  const STATIC_CACHE_NAME = "sh-yt-static-{RUNTIME_REV}";
  const MEDIA_CACHE_NAME = "sh-yt-media-{RUNTIME_REV}";
  const YOUTUBE_HOSTS = new Set(["youtube.com","www.youtube.com","m.youtube.com","music.youtube.com","studio.youtube.com","upload.youtube.com","youtu.be"]);
  const AUTH_HOSTS = new Set(["accounts.google.com","accounts.google.co.jp","accounts.youtube.com","myaccount.google.com","gds.google.com","ogs.google.com","apis.google.com","ssl.gstatic.com","www.gstatic.com","play.google.com","oauth2.googleapis.com","youtube.googleapis.com"]);
  const YT_IMAGE_SUFFIXES = [
    "i.ytimg.com","s.ytimg.com","img.youtube.com","ytimg.com",
    "yt3.ggpht.com","yt4.ggpht.com","ggpht.com",
    "yt3.googleusercontent.com","yt4.googleusercontent.com","lh3.googleusercontent.com","googleusercontent.com",
    "ytimg.l.google.com","fonts.gstatic.com","www.gstatic.com","fonts.googleapis.com",
    "usercontent.goog","playables.usercontent.goog"
  ];
  const SESSION_KEY = "schoolhub.youtube.target.v9";
  const SW_GUARD = "schoolhub.youtube.sw-reload.v9";
  const MAX_RECOVERY = 3;
  const CONTROL_WAIT = 12000;
  const HEALTH_DELAY = 22000;
  const DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";

  let controller = null;
  let frame = null;
  let transport = null;
  let transportName = "";
  let registration = null;
  let recovery = 0;
  let healthTimer = 0;
  let recoveryTimer = 0;
  let keepaliveTimer = 0;
  let pollTimer = 0;
  let ready = false;
  let navigationToken = 0;
  let lastUrl = INITIAL;
  let lastTitle = "";
  let autoStartedVideoId = "";
  let lastShWatchTriggered = "";
  let lastShWatchTime = 0;
  let reqSeq = 0;
  const startedAt = performance.now();

  const $ = (id) => document.getElementById(id);
  const cover = $("cover");
  const status = null;
  const help = null;
  const input = null;
  const frameEl = $("frame");

  function textOf(value) {{
    if (typeof value === "string") return value;
    try {{ return JSON.stringify(value); }} catch (_) {{ return String(value); }}
  }}

  function log(...args) {{
    console.debug.apply(console, ["[SchoolHub YouTube]", ...args]);
  }}

  function fail(msg, error) {{
    const detail = error && (error.stack || error.message) ? (error.stack || error.message) : textOf(error || "");
    console.error("[SchoolHub YouTube] FATAL:", msg, detail);
  }}

  async function withTimeout(promise, ms, label) {{
    let timer = 0;
    try {{
      return await Promise.race([
        Promise.resolve(promise),
        new Promise((_, reject) => {{
          timer = setTimeout(() => reject(new Error(label + " timed out after " + ms + "ms")), ms);
        }}),
      ]);
    }} finally {{
      clearTimeout(timer);
    }}
  }}

  function message(text) {{
    log("STATUS:", String(text));
  }}

  function notify(text) {{
    log("NOTICE:", String(text));
  }}

  function wispUrl() {{
    if (WISP_OVERRIDE) {{
      try {{
        const u = new URL(WISP_OVERRIDE, location.href);
        if (u.protocol === "ws:" || u.protocol === "wss:") return u.href;
      }} catch (error) {{
        console.warn("[SchoolHub YouTube] invalid SCRAMJET_WISP_URL override", error);
      }}
    }}
    return (location.protocol === "https:" ? "wss://" : "ws://") + location.host + WISP_PATH;
  }}

  function syncOuterHistoryUrl(targetUrl) {{
    try {{
      const cleaned = cleanTargetUrl(targetUrl);
      if (!cleaned) return;
      const u = new URL(cleaned);
      const h = u.hostname.toLowerCase().replace(/\\.$/, "");
      if (h !== "www.youtube.com" && h !== "youtube.com" && h !== "m.youtube.com") return;
      if (u.searchParams.has("sh_watch")) return;
      const subPath = u.pathname === "/" ? "/youtube/" : ("/youtube" + u.pathname);
      if (
        subPath.startsWith("/youtube/runtime/") ||
        subPath.startsWith("/youtube/~/sj/") ||
        subPath === "/youtube/sw.js" ||
        subPath === "/youtube/health" ||
        subPath.startsWith("/youtube/wisp")
      ) {{
        return;
      }}
      const nextOuter = subPath + (u.search || "") + (u.hash || "");
      const curOuter = location.pathname + location.search + location.hash;
      if (nextOuter !== curOuter) {{
        history.replaceState({{ shYtTarget: u.href }}, "", nextOuter);
      }}
    }} catch (_) {{}}
  }}

  function remember(url) {{
    if (!url) return;
    const s = cleanTargetUrl(String(url)) || String(url);
    lastUrl = s;
    if (isGoogleAuthTarget(s)) return;
    try {{ sessionStorage.setItem(SESSION_KEY, s); }} catch (_) {{}}
    try {{ localStorage.setItem(SESSION_KEY, s); }} catch (_) {{}}
    syncOuterHistoryUrl(s);
  }}

  function restore() {{
    try {{
      const params = new URLSearchParams(location.search);
      if (location.pathname !== "/youtube/" || location.search.length > 1 || params.get("q") || params.get("v") || params.get("url") || params.get("search_query")) {{
        return INITIAL;
      }}
      const saved = sessionStorage.getItem(SESSION_KEY) || localStorage.getItem(SESSION_KEY);
      if (saved && !isGoogleAuthTarget(saved)) return saved;
      return INITIAL;
    }} catch (_) {{
      return INITIAL;
    }}
  }}

  const LOG_ENDPOINT = "/_diag/youtube";
  let logSequence = 0;
  const clientLog = (level, event, details = {{}}) => {{
    const payload = {{
      source: "youtube-scramjet",
      level: String(level || "info"),
      event: String(event || "event"),
      seq: ++logSequence,
      ts: new Date().toISOString(),
      transport: transportName || null,
      details,
    }};
    try {{
      const fn = level === "error" ? console.error : level === "warn" ? console.warn : console.debug;
      fn.call(console, "[SchoolHub YouTube]", event, details);
    }} catch (_) {{}}
    try {{
      fetch(LOG_ENDPOINT, {{
        method: "POST",
        credentials: "omit",
        cache: "no-store",
        keepalive: true,
        headers: {{ "content-type": "application/json" }},
        body: JSON.stringify(payload),
      }}).catch(() => {{}});
    }} catch (_) {{}}
  }};

  function safeError(error) {{
    if (!error) return {{}};
    return {{
      name: String(error.name || "Error"),
      message: String(error.message || error),
      stack: String(error.stack || "").slice(0, 12000),
    }};
  }}

  function safeSourceUrl(value) {{
    try {{
      const u = new URL(String(value), location.href);
      return u.origin + u.pathname;
    }} catch (_) {{
      return "";
    }}
  }}

  window.addEventListener("error", (event) => {{
    clientLog("error", "window-error", {{
      message: String(event.message || ""),
      source: safeSourceUrl(event.filename || ""),
      line: Number(event.lineno || 0),
      column: Number(event.colno || 0),
      error: safeError(event.error),
    }});
  }});

  window.addEventListener("unhandledrejection", (event) => {{
    clientLog("error", "unhandled-rejection", {{ reason: safeError(event.reason) }});
  }});

  window.addEventListener("securitypolicyviolation", (event) => {{
    clientLog("error", "csp-violation", {{
      effectiveDirective: String(event.effectiveDirective || ""),
      blockedURI: safeSourceUrl(event.blockedURI || ""),
      violatedDirective: String(event.violatedDirective || ""),
      sourceFile: safeSourceUrl(event.sourceFile || ""),
      line: Number(event.lineNumber || 0),
      column: Number(event.columnNumber || 0),
      disposition: String(event.disposition || ""),
    }});
  }});

  function normalize(raw) {{
    let value = String(raw || "").trim();
    if (value.length > 2048) value = value.slice(0, 2048);
    if (!value) return HOME;

    const lower = value.toLowerCase();
    if (lower === "playables" || lower === "game" || lower === "games" || lower === "ゲーム" || lower === "プレイアブル" || lower.startsWith("playables")) {{
      return "https://www.youtube.com/playables?hl=ja";
    }}

    let candidate = value;
    const bareOfficial = /^(?:youtube\\.com|www\\.youtube\\.com|m\\.youtube\\.com|music\\.youtube\\.com|studio\\.youtube\\.com|youtu\\.be)\\//i;
    if (bareOfficial.test(candidate)) candidate = "https://" + candidate;

    try {{
      const u = new URL(candidate);
      const host = (u.hostname || "").toLowerCase().replace(/\\.$/, "");
      const allowed = YOUTUBE_HOSTS.has(host) || AUTH_HOSTS.has(host) || host.endsWith(".google.com") || host.endsWith(".google.co.jp");
      if (allowed && (u.protocol === "http:" || u.protocol === "https:")) {{
        if (u.username || u.password || (u.port && u.port !== "443")) throw new Error("unsafe URL authority");
        u.protocol = "https:";
        if (host === "m.youtube.com") u.hostname = "www.youtube.com";
        return u.href;
      }}
    }} catch (_) {{}}

    return "https://www.youtube.com/results?search_query=" + encodeURIComponent(value);
  }}

  function cleanTargetUrl(rawUrl) {{
    if (!rawUrl || typeof rawUrl !== "string") return "";
    try {{
      const stripped = rawUrl.replace(/([?&])(?:%24|\\$)[^&#]*(?:&|$)/gi, "$1").replace(/[?&]+(?=#|$)/g, "");
      const u = new URL(stripped);
      if (u.protocol !== "http:" && u.protocol !== "https:") return "";
      u.pathname = u.pathname.replace(/\\?\\$[^/]*$/, "");
      for (const k of [...u.searchParams.keys()]) {{
        if (k.startsWith("$")) u.searchParams.delete(k);
      }}
      return u.href;
    }} catch (_) {{
      return "";
    }}
  }}

  function extractVideoId(urlStr) {{
    if (!urlStr) return "";
    try {{
      const u = new URL(urlStr);
      const host = u.hostname.toLowerCase();
      if (host === "youtu.be") {{
        const id = u.pathname.replace(/^\\/+/, "").split("/")[0];
        return /^[A-Za-z0-9_-]{{11}}$/.test(id) ? id : "";
      }}
      if (YOUTUBE_HOSTS.has(host)) {{
        if (u.pathname === "/watch") {{
          const v = u.searchParams.get("v") || "";
          return /^[A-Za-z0-9_-]{{11}}$/.test(v) ? v : "";
        }}
        const shortsMatch = u.pathname.match(/^\\/shorts\\/([A-Za-z0-9_-]{{11}})/);
        if (shortsMatch) return shortsMatch[1];
      }}
    }} catch (_) {{}}
    return "";
  }}

  function setAddress() {{}}

  function observeUrl(url) {{
    const cleaned = cleanTargetUrl(url);
    if (!cleaned) return;
    if (cleaned !== lastUrl) {{
      lastUrl = cleaned;
      remember(cleaned);
      ensureWatchPlayer(cleaned);
    }}
  }}

  function isGoogleAuthTarget(urlStr) {{
    if (!urlStr) return false;
    try {{
      const h = new URL(urlStr).hostname.toLowerCase().replace(/\\.$/, "");
      if (YOUTUBE_HOSTS.has(h) || h.endsWith(".ytimg.com") || h.endsWith(".googlevideo.com") || h.endsWith(".ggpht.com")) {{
        return false;
      }}
      return h === "accounts.google.com" || h === "accounts.youtube.com" || h.endsWith(".google.com") || h.endsWith(".gstatic.com");
    }} catch (_) {{
      return false;
    }}
  }}

  function isAllowedImageHost(host) {{
    const h = String(host || "").toLowerCase().replace(/\\.$/, "");
    return YT_IMAGE_SUFFIXES.some(x => h === x || h.endsWith("." + x));
  }}

  function isAllowedImageTarget(u) {{
    if (!u || !isAllowedImageHost(u.hostname)) return false;
    const h = String(u.hostname || "").toLowerCase().replace(/\\.$/, "");
    const p = String(u.pathname || "");
    if (/\\.(js|mjs|cjs|json|css|html|wasm)(\\?|$)/i.test(p)) return false;
    if (h.endsWith("gstatic.com") || h.endsWith("googleapis.com")) {{
      return /\\.(png|jpe?g|gif|webp|svg|ico|avif|bmp|woff2?|ttf|otf)(\\?|$)/i.test(p);
    }}
    return true;
  }}

  function isClientCacheableAsset(method, urlStr) {{
    if (method !== "GET") return false;
    try {{
      const u = new URL(urlStr);
      const h = u.hostname.toLowerCase().replace(/\\.$/, "");
      const p = u.pathname || "";
      if ((h === "www.youtube.com" || h === "m.youtube.com" || h === "music.youtube.com") && (p.startsWith("/s/") || p.includes("/_/js/"))) return true;
      if (h.endsWith(".ytimg.com") && (p.startsWith("/yts/") || /\\.(js|css|woff2|woff|ttf|svg|png|jpg|webp)$/i.test(p))) return true;
      if ((h === "fonts.gstatic.com" || h === "www.gstatic.com" || h === "fonts.googleapis.com") && (p.startsWith("/s/i/") || p.startsWith("/css") || p.startsWith("/icon") || /\\.(woff2|woff|ttf|svg|png|css|js)$/i.test(p))) return true;
      if (/\\.(js|mjs|css|woff2?)(\\?|$)/i.test(p) && (h.endsWith(".youtube.com") || h.endsWith(".ytimg.com") || h.endsWith(".gstatic.com"))) return true;
    }} catch (_) {{}}
    return false;
  }}

  function checkTransportShortCircuit(urlStr) {{
    try {{
      const u = new URL(urlStr);
      const h = u.hostname.toLowerCase().replace(/\\.$/, "");
      const p = u.pathname || "";
      const corsOrigin = (typeof location !== "undefined" && location.origin) ? location.origin : "*";
      const corsHeaders = [
        ["access-control-allow-origin", corsOrigin],
        ["access-control-allow-credentials", "true"],
      ];
      if (h === "fonts.googleapis.com" && (p === "/css" || p === "/css2")) {{
        const b = new TextEncoder().encode("/* sh-fast-fonts */").buffer;
        return {{
          body: b,
          headers: [["content-type", "text/css; charset=utf-8"], ...corsHeaders, ["cache-control", "public, max-age=86400"]],
          status: 200,
          statusText: "OK",
        }};
      }}
      if (h.endsWith(".googlevideo.com") || h.endsWith(".c.youtube.com")) {{
        const exp = u.searchParams.get("expire") || "";
        if (p.includes(" ") || exp === "9421968081" || exp === "1685394838") {{
          return {{ body: new ArrayBuffer(0), headers: [...corsHeaders], status: 204, statusText: "No Content" }};
        }}
      }}
      if (
        (p.includes("generate_204") && !isGoogleAuthTarget(urlStr)) || p.includes("error_204") || p.includes("csi_204") ||
        p.startsWith("/pagead/") || p.startsWith("/api/stats/") || p.startsWith("/ptracking") ||
        p.startsWith("/s/search/audio/") ||
        h.endsWith("doubleclick.net") || h.endsWith("googlesyndication.com") ||
        h.endsWith("googleadservices.com") || h.includes("adservice.google")
      ) {{
        return {{ body: new ArrayBuffer(0), headers: [...corsHeaders], status: 204, statusText: "No Content" }};
      }}
      if (
        p.startsWith("/youtubei/v1/log_event") ||
        p.startsWith("/youtubei/v1/ads/") ||
        p.startsWith("/youtubei/v1/player/ad_break") ||
        p.includes("/get_midroll_info")
      ) {{
        const b = new TextEncoder().encode("{{}}").buffer;
        return {{
          body: b,
          headers: [["content-type", "application/json"], ...corsHeaders],
          status: 200,
          statusText: "OK",
        }};
      }}
    }} catch (_) {{}}
    return null;
  }}

  function normalizeHeadersList(headers, targetUrl = "") {{
    const rawBrowserUa = String(navigator.userAgent || "").replace(/HeadlessChrome\\//g, "Chrome/");
    const isAuthTarget = isGoogleAuthTarget(targetUrl);
    const effectiveUa = (isAuthTarget && rawBrowserUa.length >= 20)
      ? rawBrowserUa
      : DEFAULT_UA;
    const list = Array.isArray(headers)
      ? headers.map(([k, v]) => [String(k), String(v)])
      : (headers && typeof headers === "object" ? Object.entries(headers).map(([k, v]) => [String(k), String(v)]) : []);
    const has = (name) => list.some(([k]) => k.toLowerCase() === name.toLowerCase());
    if (!has("user-agent")) {{
      list.push(["User-Agent", effectiveUa]);
    }} else {{
      for (let i = 0; i < list.length; i++) {{
        if (list[i][0].toLowerCase() === "user-agent") {{
          list[i][1] = effectiveUa;
        }}
      }}
    }}
    if (isAuthTarget && navigator.userAgentData && Array.isArray(navigator.userAgentData.brands)) {{
      try {{
        const chUa = navigator.userAgentData.brands
          .map((b) => `"${{String(b.brand || "").replace(/"/g, '\\"')}}";v="${{String(b.version || "")}}"`)
          .join(", ");
        if (chUa && !has("sec-ch-ua")) list.push(["sec-ch-ua", chUa]);
        if (!has("sec-ch-ua-mobile")) list.push(["sec-ch-ua-mobile", navigator.userAgentData.mobile ? "?1" : "?0"]);
        if (navigator.userAgentData.platform && !has("sec-ch-ua-platform")) {{
          list.push(["sec-ch-ua-platform", `"${{String(navigator.userAgentData.platform)}}"`]);
        }}
      }} catch (_) {{}}
    }}
    if (!has("accept-language")) {{
      list.push(["Accept-Language", "ja,en-US;q=0.9,en;q=0.8"]);
    }}
    return list;
  }}

  async function serializeRequestBody(body) {{
    if (!body) return new Uint8Array(0);
    if (body instanceof Uint8Array) return body;
    if (body instanceof ArrayBuffer) return new Uint8Array(body);
    if (ArrayBuffer.isView(body)) return new Uint8Array(body.buffer, body.byteOffset, body.byteLength);
    if (typeof body === "string") return new TextEncoder().encode(body);
    if (body instanceof Blob) return new Uint8Array(await body.arrayBuffer());
    if (body instanceof ReadableStream) return new Uint8Array(await new Response(body).arrayBuffer());
    return new TextEncoder().encode(String(body));
  }}

  function decorateTransportClient(client) {{
    if (!client || typeof client.request !== "function") return client;
    const origRequest = client.request.bind(client);
    client.request = async (remote, method, body, headers, signal) => {{
      const urlStr = remote instanceof URL ? remote.href : String(remote || "");
      const list = normalizeHeadersList(headers, urlStr);
      return await origRequest(remote, method, body, list, signal);
    }};
    return client;
  }}

  async function loadTransportCtor(kind) {{
    const path = kind === "libcurl"
      ? "/scramjet/libcurl/index.mjs?v=" + '2.0.5'
      : "/scramjet/epoxy/index.mjs?v=" + '3.0.1';
    const mod = await import(path);
    const Ctor = mod.default ||
      mod.LibcurlClient || mod.EpoxyClient ||
      mod.LibcurlTransport?.LibcurlClient ||
      mod.EpoxyTransport?.EpoxyClient;
    if (typeof Ctor !== "function") {{
      throw new Error(kind + " transport constructor not exported by " + path);
    }}
    return Ctor;
  }}

  class SchoolHubHybridTransport {{
    constructor() {{
      this.wispClient = null;
      this.wispPromise = null;
    }}
    async init() {{
      return;
    }}
    async ensureWispClient() {{
      if (this.wispClient) return this.wispClient;
      if (this.wispPromise) return await this.wispPromise;
      this.wispPromise = (async () => {{
        for (const kind of ["libcurl", "epoxy"]) {{
          try {{
            const Ctor = await loadTransportCtor(kind);
            const c = decorateTransportClient(new Ctor({{ wisp: wispUrl() }}));
            if (typeof c.init === "function") {{
              await withTimeout(Promise.resolve().then(() => c.init()), 9000, kind + " init");
            }}
            this.wispClient = c;
            return c;
          }} catch (e) {{
            console.warn("[SchoolHub YouTube] fallback wisp transport init failed", kind, e);
          }}
        }}
        throw new Error("All Wisp fallback transports failed");
      }})();
      try {{
        return await this.wispPromise;
      }} finally {{
        this.wispPromise = null;
      }}
    }}
    async request(remote, method, body, headers, signal) {{
      const urlStr = remote instanceof URL ? remote.href : String(remote || "");
      const m = String(method || "GET").toUpperCase();
      const sc = checkTransportShortCircuit(urlStr);
      if (sc) return sc;

      // Fast path for googlevideo.com / .c.youtube.com MSE & media segments
      try {{
        const gvU = new URL(urlStr);
        const gvH = gvU.hostname.toLowerCase().replace(/\\.$/, "");
        if (gvH.endsWith(".googlevideo.com") || gvH.endsWith(".c.youtube.com")) {{
          const gvRelayUrl = GV_STREAM_ENDPOINT + "?k=" + encodeMediaKey(gvU.href);
          const gvHeaders = {{}};
          if (Array.isArray(headers)) {{
            for (const [hk, hv] of headers) {{
              const kl = String(hk || "").toLowerCase();
              if (kl === "range" || kl === "accept" || kl === "content-type" || kl === "x-client-data") {{
                gvHeaders[hk] = String(hv);
              }}
            }}
          }} else if (headers && typeof headers === "object") {{
            for (const [hk, hv] of Object.entries(headers)) {{
              const kl = String(hk || "").toLowerCase();
              if (kl === "range" || kl === "accept" || kl === "content-type" || kl === "x-client-data") {{
                gvHeaders[hk] = String(hv);
              }}
            }}
          }}
          const gvBody = (m !== "GET" && m !== "HEAD" && m !== "OPTIONS")
            ? await serializeRequestBody(body)
            : null;
          const r = await fetch(gvRelayUrl, {{
            method: m,
            headers: gvHeaders,
            body: (gvBody && gvBody.byteLength > 0) ? gvBody : undefined,
            credentials: "omit",
            signal,
          }});
          return {{
            body: (m === "HEAD" || r.status === 204 || r.status === 304) ? new ArrayBuffer(0) : await r.arrayBuffer(),
            headers: [...r.headers.entries()],
            status: r.status,
            statusText: r.statusText || "OK",
          }};
        }}
      }} catch (_) {{}}

      // Fast path for GET/HEAD to image/icon/font hosts (ytimg, ggpht, gstatic)
      if (m === "GET" || m === "HEAD") {{
        try {{
          let u = new URL(urlStr);
          if (u.pathname === "/youtube/runtime/image-relay") {{
            const kv = u.searchParams.get("k");
            const uv = kv ? decodeMediaKey(kv) : u.searchParams.get("url");
            if (uv) u = new URL(uv);
          }}
          if (isAllowedImageTarget(u)) {{
            const relayUrl = IMAGE_RELAY_ENDPOINT + "?k=" + encodeMediaKey(u.href);
            if (m === "GET" && typeof caches !== "undefined") {{
              try {{
                const cache = await caches.open(MEDIA_CACHE_NAME);
                const hit = await cache.match(relayUrl);
                if (hit) {{
                  return {{
                    body: await hit.arrayBuffer(),
                    headers: [...hit.headers.entries()],
                    status: hit.status,
                    statusText: hit.statusText || "OK",
                  }};
                }}
                const r = await fetch(relayUrl, {{ method: "GET", credentials: "omit", signal }});
                if (r.status === 200) {{
                  try {{ await cache.put(relayUrl, r.clone()); }} catch (_) {{}}
                }}
                return {{
                  body: await r.arrayBuffer(),
                  headers: [...r.headers.entries()],
                  status: r.status,
                  statusText: r.statusText || "OK",
                }};
              }} catch (_) {{}}
            }}
            const r = await fetch(relayUrl, {{ method: m, credentials: "omit", signal }});
            return {{
              body: m === "HEAD" ? new ArrayBuffer(0) : await r.arrayBuffer(),
              headers: [...r.headers.entries()],
              status: r.status,
              statusText: r.statusText || "OK",
            }};
          }}
        }} catch (_) {{}}
      }}

      const list = normalizeHeadersList(headers, urlStr);
      const canClientCache = isClientCacheableAsset(m, urlStr);
      if (canClientCache && typeof caches !== "undefined") {{
        try {{
          const cache = await caches.open(STATIC_CACHE_NAME);
          const hit = await cache.match(urlStr);
          if (hit) {{
            return {{
              body: await hit.arrayBuffer(),
              headers: [...hit.headers.entries()],
              status: hit.status || 200,
              statusText: "OK",
            }};
          }}
        }} catch (_) {{}}
      }}

      const n = ++reqSeq;
      try {{
        const bodyBytes = await serializeRequestBody(body);
        const metaBytes = new TextEncoder().encode(JSON.stringify({{
          url: urlStr,
          method: m,
          headers: list,
        }}));
        const frameBuf = new Uint8Array(4 + metaBytes.byteLength + bodyBytes.byteLength);
        new DataView(frameBuf.buffer).setUint32(0, metaBytes.byteLength, false);
        frameBuf.set(metaBytes, 4);
        frameBuf.set(bodyBytes, 4 + metaBytes.byteLength);

        const resp = await fetch(RELAY_ENDPOINT, {{
          method: "POST",
          credentials: "omit",
          cache: "no-store",
          headers: {{ "content-type": "application/octet-stream" }},
          body: frameBuf,
          signal,
        }});
        if (!resp.ok) throw new Error("HTTP relay status " + resp.status);
        const rawBuf = await resp.arrayBuffer();
        if (rawBuf.byteLength < 4) throw new Error("HTTP relay short buffer");
        const metaLen = new DataView(rawBuf).getUint32(0, false);
        if (rawBuf.byteLength < 4 + metaLen) throw new Error("HTTP relay truncated meta");
        const metaJson = new TextDecoder().decode(new Uint8Array(rawBuf, 4, metaLen));
        const meta = JSON.parse(metaJson);
        const payloadBytes = new Uint8Array(rawBuf, 4 + metaLen);
        const respBody = (m === "HEAD" || meta.status === 204 || meta.status === 304)
          ? new ArrayBuffer(0)
          : payloadBytes.buffer.slice(payloadBytes.byteOffset, payloadBytes.byteOffset + payloadBytes.byteLength);
        const respHeaders = Array.isArray(meta.headers) ? meta.headers : [];
        if (controller) {{
          try {{
            let clearAuth = false;
            const setCookieEntries = [];
            for (const [k, v] of respHeaders) {{
              const kl = String(k || "").toLowerCase();
              if (kl === "x-sh-clear-auth" && v === "1") {{
                clearAuth = true;
              }} else if (kl === "set-cookie" && v) {{
                let scStr = String(v);
                scStr = scStr.replace(/;\\s*partitioned\\b/gi, "");
                scStr = scStr.replace(/;\\s*priority=[^;]*/gi, "");
                if (!scStr.trim().startsWith("__Host-")) {{
                  scStr = scStr.replace(/;\\s*domain=google\\.com\\b/gi, "; Domain=.google.com");
                  scStr = scStr.replace(/;\\s*domain=youtube\\.com\\b/gi, "; Domain=.youtube.com");
                }}
                setCookieEntries.push({{ url: urlStr, cookie: scStr }});
              }}
            }}
            if (clearAuth || setCookieEntries.length > 0) {{
              if (clearAuth) globalThis.__shExplicitClear = true;
              if (setCookieEntries.length > 0) {{
                controller.applyCookieSyncEntries?.(setCookieEntries);
              }}
              Promise.resolve(controller.persistCookies?.()).catch(() => {{}}).finally(() => {{
                if (clearAuth) globalThis.__shExplicitClear = false;
              }});
              if (setCookieEntries.length > 0) {{
                Promise.resolve(controller.propagateCookieSync?.(setCookieEntries, {{}})).catch(() => {{}});
              }}
            }}
          }} catch (_) {{}}
        }}
        const statusNum = Number(meta.status || 200);
        if (n <= 12) {{
          log("TRANSPORT HTTP OK:", "#" + n, statusNum, String(meta.statusText || "OK"), m, safeSourceUrl(urlStr), "via=http-relay");
        }}
        if (canClientCache && statusNum === 200 && respBody.byteLength > 0 && typeof caches !== "undefined") {{
          try {{
            const hObj = new Headers();
            for (const [k, v] of respHeaders) {{
              if (k && k.toLowerCase() !== "set-cookie") {{
                try {{ hObj.append(k, v); }} catch (_) {{}}
              }}
            }}
            const cache = await caches.open(STATIC_CACHE_NAME);
            await cache.put(urlStr, new Response(respBody.slice(0), {{ status: 200, headers: hObj }}));
          }} catch (_) {{}}
        }}
        return {{
          body: respBody,
          headers: respHeaders,
          status: statusNum,
          statusText: String(meta.statusText || "OK"),
        }};
      }} catch (relayError) {{
        console.warn("[SchoolHub YouTube] relay fallback to wisp for", urlStr, relayError);
        const wisp = await this.ensureWispClient();
        return await wisp.request(remote, method, body, list, signal);
      }}
    }}
    connect(url, protocols, requestHeaders, onopen, onmessage, onclose, onerror) {{
      this.ensureWispClient()
        .then((wisp) => wisp.connect(url, protocols, requestHeaders, onopen, onmessage, onclose, onerror))
        .catch((err) => {{
          try {{ onerror?.(err); }} catch (_) {{}}
          try {{ onclose?.(1006, "Wisp connect failed"); }} catch (_) {{}}
        }});
    }}
    destroy() {{
      try {{ this.wispClient?.destroy?.(); }} catch (_) {{}}
    }}
    close() {{
      try {{ this.wispClient?.close?.(); }} catch (_) {{}}
    }}
  }}

  async function getTransport(kind) {{
    if (kind === "hybrid") {{
      const hybrid = new SchoolHubHybridTransport();
      await hybrid.init();
      return hybrid;
    }}
    const Ctor = await loadTransportCtor(kind);
    const client = decorateTransportClient(new Ctor({{ wisp: wispUrl() }}));
    try {{
      if (typeof client.init === "function") {{
        await withTimeout(
          Promise.resolve().then(() => client.init()),
          9000,
          kind + " transport init"
        );
      }}
      return client;
    }} catch (error) {{
      try {{ client.destroy?.(); }} catch (_) {{}}
      try {{ client.close?.(); }} catch (_) {{}}
      throw error;
    }}
  }}

  function buildPlugins() {{
    const U = globalThis.ScramjetUtils || globalThis.$scramjetUtils || {{}};
    const result = [];
    if (typeof U.HttpCachePlugin === "function") {{
      try {{ result.push(new U.HttpCachePlugin()); }} catch (e) {{ console.debug("HttpCachePlugin", e); }}
    }}
    if (typeof U.UrlWatcherPlugin === "function") {{
      try {{
        result.push(new U.UrlWatcherPlugin((url) => observeUrl(String(url))));
      }} catch (e) {{ console.debug("UrlWatcherPlugin", e); }}
    }}
    if (typeof U.CatchEscapedLinksPlugin === "function") {{
      try {{
        result.push(new U.CatchEscapedLinksPlugin((escapedUrl) => {{
          try {{
            const u = escapedUrl instanceof URL ? escapedUrl.href : String(escapedUrl || "");
            if (u && (u.startsWith("http://") || u.startsWith("https://"))) {{
              return new URL(PREFIX + encodeURIComponent(u), location.origin);
            }}
          }} catch (_) {{}}
          return new URL(location.href);
        }}));
      }} catch (e) {{ console.debug("CatchEscapedLinksPlugin", e); }}
    }}
    return result;
  }}

  function readFrameTarget() {{
    try {{
      let href = "";
      try {{ href = frameEl.contentWindow?.location?.href || ""; }} catch (_) {{}}
      if (!href || href === "about:blank") {{
        try {{ href = frameEl.contentDocument?.location?.href || ""; }} catch (_) {{}}
      }}
      if (!href || href === "about:blank") return "";

      if (href.startsWith("http://") || href.startsWith("https://")) {{
        try {{
          const u = new URL(href);
          if (YOUTUBE_HOSTS.has(u.hostname.toLowerCase())) {{
            const cleaned = cleanTargetUrl(href);
            if (cleaned) return cleaned;
          }}
        }} catch (_) {{}}
      }}

      const fPrefix = frame?.prefix || "/youtube/~/sj/";
      const idx = href.indexOf(fPrefix);
      if (idx >= 0) {{
        let rawTail = href.slice(idx + fPrefix.length);
        for (let k = 0; k < 3; k++) {{
          try {{
            const n = decodeURIComponent(rawTail);
            if (n === rawTail) break;
            rawTail = n;
          }} catch (_) {{ break; }}
        }}
        const cleaned = cleanTargetUrl(rawTail);
        if (cleaned) return cleaned;
      }}
      const m = href.match(/\\/youtube\\/~\\/sj\\/(?:[^/]+\\/[^/]+\\/)?(https?(?:%3A|:).*)$/i);
      if (m && m[1]) {{
        let rawPart = m[1];
        for (let k = 0; k < 3; k++) {{
          try {{
            const n = decodeURIComponent(rawPart);
            if (n === rawPart) break;
            rawPart = n;
          }} catch (_) {{ break; }}
        }}
        const cleaned = cleanTargetUrl(rawPart);
        if (cleaned) return cleaned;
      }}
      return "";
    }} catch (error) {{
      console.debug("[SchoolHub YouTube] frame URL unavailable", error);
      return "";
    }}
  }}

  function encodeMediaKey(urlStr) {{
    try {{
      const rev = encodeURIComponent(String(urlStr)).split("").reverse().join("");
      return btoa(rev).replace(/\\+/g, "-").replace(/\\//g, "_").replace(/=+$/, "");
    }} catch (_) {{
      return "";
    }}
  }}
  function decodeMediaKey(k) {{
    try {{
      let b = String(k || "").replace(/-/g, "+").replace(/_/g, "/");
      while (b.length % 4) b += "=";
      return decodeURIComponent(atob(b).split("").reverse().join(""));
    }} catch (_) {{
      return "";
    }}
  }}
  function extractVirtualMediaFromAttr(rawVal) {{
    if (!rawVal || typeof rawVal !== "string") return null;
    try {{
      const u = new URL(rawVal, location.origin);
      if (u.pathname === "/youtube/runtime/image-relay") {{
        if (u.searchParams.has("k")) return null;
        if (u.searchParams.has("url")) {{
          const t = new URL(u.searchParams.get("url"));
          if (isAllowedImageTarget(t)) return t;
        }}
        return null;
      }}
      const marker = "/youtube/~/sj/";
      const idx = u.pathname.indexOf(marker);
      let candidate = "";
      if (idx >= 0) {{
        const rawAfter = u.pathname.slice(idx + marker.length);
        const m = rawAfter.match(/(?:^|\\/)(https?(?:%3A|:)(?:%2F|\\/){{2}}.*)$/i);
        if (m && m[1]) {{
          let out = m[1];
          for (let k = 0; k < 4; k++) {{
            try {{ const n = decodeURIComponent(out); if (n === out) break; out = n; }} catch (_) {{ break; }}
          }}
          candidate = out;
        }}
      }} else if (u.origin !== location.origin && (u.protocol === "https:" || u.protocol === "http:")) {{
        candidate = u.href;
      }}
      if (!candidate) return null;
      const target = new URL(candidate);
      for (const k of [...target.searchParams.keys()]) {{
        if (k.startsWith("$")) target.searchParams.delete(k);
      }}
      if (isAllowedImageTarget(target)) return target;
    }} catch (_) {{}}
    return null;
  }}

  function ensureWatchPlayer(targetUrl="") {{
    try {{
      const doc = frameEl.contentDocument;
      if (!doc || !doc.body) return;
      const existingOverlay = doc.getElementById("schoolhub-yt-player-host");
      if (existingOverlay) existingOverlay.remove();

      if (doc.head && !doc.getElementById("schoolhub-yt-watch-style")) {{
        const st = doc.createElement("style");
        st.id = "schoolhub-yt-watch-style";
        st.textContent = `
          ytd-watch-flexy #secondary,
          ytd-watch-flexy #secondary-inner,
          ytd-watch-flexy #related,
          ytd-watch-flexy ytd-watch-next-secondary-results-renderer {{
            display: block !important;
            visibility: visible !important;
            opacity: 1 !important;
          }}
          ytd-watch-flexy #player-container-outer,
          ytd-watch-flexy #player-container-inner,
          ytd-watch-flexy #player-container,
          ytd-watch-flexy ytd-player,
          ytd-watch-flexy ytd-player #container,
          #movie_player,
          #shorts-player,
          .html5-video-player,
          .html5-video-container {{
            display: block !important;
            visibility: visible !important;
            opacity: 1 !important;
            width: 100% !important;
            height: 100% !important;
          }}
          ytd-watch-flexy #player-container-inner,
          ytd-watch-flexy #player-container {{
            aspect-ratio: 16 / 9 !important;
            min-height: 280px !important;
          }}
          .html5-video-container {{
            position: relative !important;
            overflow: hidden !important;
            min-height: 240px !important;
          }}
          video.html5-main-video {{
            display: block !important;
            visibility: visible !important;
            opacity: 1 !important;
            width: 100% !important;
            height: 100% !important;
            left: 0px !important;
            top: 0px !important;
            object-fit: contain !important;
            z-index: 1 !important;
            background: transparent !important;
          }}
          #cinematics,
          #player-cinematics,
          ytd-player #cinematics,
          #movie_player.playing-mode .ytp-cued-thumbnail-overlay,
          #movie_player.playing-mode #error-screen {{
            display: none !important;
            pointer-events: none !important;
          }}
          @media (min-width: 900px) {{
            ytd-watch-flexy:not([theater]):not([fullscreen]) #columns {{
              display: flex !important;
              flex-direction: row !important;
              align-items: flex-start !important;
            }}
            ytd-watch-flexy:not([theater]):not([fullscreen]) #primary {{
              flex: 1 1 auto !important;
              min-width: 0 !important;
            }}
            ytd-watch-flexy:not([theater]):not([fullscreen]) #secondary {{
              width: 380px !important;
              min-width: 320px !important;
              max-width: 402px !important;
              flex: 0 0 380px !important;
              margin-left: 16px !important;
            }}
          }}
          yt-lockup-view-model img,
          ytd-compact-video-renderer img,
          yt-thumbnail-view-model img {{
            visibility: visible !important;
            opacity: 1 !important;
          }}
          ytd-promoted-sparkles-web-renderer,
          ytd-promoted-video-renderer,
          ytd-display-ad-renderer,
          ytd-statement-banner-renderer,
          ytd-in-feed-ad-layout-renderer,
          ytd-banner-promo-renderer,
          .ytd-ad-slot-renderer,
          #masthead-ad,
          #player-ads,
          .video-ads,
          .ytp-ad-module,
          .ytp-ad-overlay-container,
          ytd-ad-slot-renderer,
          yt-mealbar-promo-renderer,
          ytd-action-companion-ad-renderer,
          ytd-ad-hover-text-button-renderer,
          ytd-shorts player-interstitial-renderer {{
            display: none !important;
          }}
        `;
        doc.head.appendChild(st);
      }}

      const rawSetAttr = window.Element.prototype.setAttribute;
      const rawGetAttr = window.Element.prototype.getAttribute;
      const rawRemoveAttr = window.Element.prototype.removeAttribute;

      if (doc.documentElement && !doc.documentElement.dataset.shPrewarmBound) {{
        doc.documentElement.dataset.shPrewarmBound = "1";
        const onLoginClick = (ev) => {{
          try {{
            if (ev.defaultPrevented || ev.button !== 0 || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
            const t = ev.target;
            if (!t || typeof t.closest !== "function") return;
            const loginLink = t.closest(
              "a[href*='ServiceLogin'], a[scramjet-attr-href*='ServiceLogin'], " +
              "a[href*='accounts.google.'], a[scramjet-attr-href*='accounts.google.'], " +
              "a[href*='/signin'], a[scramjet-attr-href*='/signin'], " +
              "ytd-button-renderer a[href*='ServiceLogin'], yt-button-shape a[href*='ServiceLogin']"
            );
            if (!loginLink) return;
            const rawH = rawGetAttr.call(loginLink, "scramjet-attr-href") || rawGetAttr.call(loginLink, "href") || loginLink.href || "";
            if (rawH) {{
              ev.preventDefault();
              ev.stopPropagation();
              ev.stopImmediatePropagation();
              let targetAuth = rawH;
              if (targetAuth.startsWith("/")) {{
                targetAuth = "https://www.youtube.com" + targetAuth;
              }}
              navigate(targetAuth, "login-click");
            }}
          }} catch (_) {{}}
        }};
        doc.addEventListener("click", onLoginClick, {{ passive: false, capture: true }});

        const onWatchClick = (ev) => {{
          try {{
            if (ev.defaultPrevented || ev.button !== 0 || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
            const t = ev.target;
            if (!t || typeof t.closest !== "function") return;
            const a = t.closest("a[href*='watch?v='], a[href*='watch%3Fv%3D'], a[scramjet-attr-href*='watch?v='], a[href*='/shorts/'], a[scramjet-attr-href*='/shorts/']");
            if (!a) return;
            const h = rawGetAttr.call(a, "scramjet-attr-href") || rawGetAttr.call(a, "href") || a.href || "";
            const mWatch = h.match(/(?:[?&]|%3F|%26)v(?:=|%3D)([A-Za-z0-9_-]{{11}})/);
            const mShorts = !mWatch ? h.match(/\\/shorts\\/([A-Za-z0-9_-]{{11}})/) : null;
            if (!mWatch && !mShorts) return;
            const vid = (mWatch || mShorts)[1];
            const targetWatchUrl = mShorts
              ? ("https://www.youtube.com/shorts/" + vid)
              : ("https://www.youtube.com/watch?v=" + vid);
            lastShWatchTriggered = vid;
            lastShWatchTime = Date.now();
            setTimeout(() => {{
              try {{ observeUrl(readFrameTarget() || targetWatchUrl); }} catch (_) {{}}
            }}, 400);
          }} catch (_) {{}}
        }};
        doc.addEventListener("click", onWatchClick, {{ passive: true, capture: true }});
      }}

      // Populate missing sidebar related video thumbnails (yt-lockup-view-model / ytd-compact-video-renderer)
      const relatedCards = doc.querySelectorAll("#secondary yt-lockup-view-model, #secondary ytd-compact-video-renderer, #related yt-lockup-view-model, #related ytd-compact-video-renderer");
      for (let i = 0; i < relatedCards.length; i++) {{
        const card = relatedCards[i];
        const linkEl = card.querySelector("a[href*='watch?v='], a[href*='watch%3Fv%3D']");
        if (!linkEl) continue;
        const hrefAttr = rawGetAttr.call(linkEl, "href") || "";
        const mVid = hrefAttr.match(/(?:[?&]|%3F|%26)v(?:=|%3D)([A-Za-z0-9_-]{{11}})/);
        if (!mVid) continue;
        const vid = mVid[1];
        const thumbHost = card.querySelector("yt-thumbnail-view-model, ytd-thumbnail, yt-image");
        if (!thumbHost) continue;
        let img = thumbHost.querySelector("img");
        if (!img) {{
          img = doc.createElement("img");
          img.className = "ytCoreImageHost ytCoreImageFillParentHeight ytCoreImageFillParentWidth ytCoreImageContentModeScaleAspectFill ytCoreImageLoaded";
          img.style.cssText = "width:100%;height:100%;object-fit:cover;border-radius:8px;display:block;";
          thumbHost.appendChild(img);
        }}
        const curSrc = rawGetAttr.call(img, "src") || "";
        if (!curSrc || (!curSrc.includes("/youtube/runtime/image-relay?k=") && !curSrc.startsWith("data:"))) {{
          const fallbackThumb = "https://i.ytimg.com/vi/" + vid + "/hqdefault.jpg";
          rawSetAttr.call(img, "src", location.origin + "/youtube/runtime/image-relay?k=" + encodeMediaKey(fallbackThumb));
          img.classList.add("ytCoreImageLoaded");
        }}
      }}

      const allImgs = doc.querySelectorAll("img");
      for (let i = 0; i < allImgs.length; i++) {{
        const img = allImgs[i];
        const actualSrc = rawGetAttr.call(img, "src") || "";
        const origSrc = rawGetAttr.call(img, "scramjet-attr-src") || "";
        if (actualSrc.includes("/youtube/~/sj/") && actualSrc.includes("https%3A%2F%2Fwww.youtube.com%2F?")) {{
          rawRemoveAttr.call(img, "src");
          continue;
        }}
        if ((actualSrc || origSrc) && !actualSrc.includes("/youtube/runtime/image-relay?k=")) {{
          const target = extractVirtualMediaFromAttr(actualSrc) || extractVirtualMediaFromAttr(origSrc);
          if (target) {{
            const directUrl = location.origin + "/youtube/runtime/image-relay?k=" + encodeMediaKey(target.href);
            rawSetAttr.call(img, "src", directUrl);
            if (rawGetAttr.call(img, "srcset")) rawRemoveAttr.call(img, "srcset");
            if (rawGetAttr.call(img, "scramjet-attr-srcset")) rawRemoveAttr.call(img, "scramjet-attr-srcset");
          }}
        }}
        if (img.complete && img.naturalWidth > 0) {{
          if (img.classList.contains("ytCoreImageHost") && !img.classList.contains("ytCoreImageLoaded")) {{
            img.classList.add("ytCoreImageLoaded");
          }}
          if (img.style.visibility === "hidden") img.style.visibility = "visible";
          if (img.style.opacity === "0") img.style.opacity = "1";
        }}
      }}

      const currentTarget = targetUrl || readFrameTarget() || lastUrl || "";
      let shWatchParam = "";
      try {{
        const uCur = new URL(currentTarget);
        const shWatch = uCur.searchParams.get("sh_watch") || "";
        if (/^[A-Za-z0-9_-]{{11}}$/.test(shWatch)) {{
          shWatchParam = shWatch;
          const flexy = doc.querySelector("ytd-watch-flexy");
          const flexyVid = flexy ? (rawGetAttr.call(flexy, "video-id") || "") : "";
          const nowSh = Date.now();
          if (flexyVid !== shWatch && (lastShWatchTriggered !== shWatch || (nowSh - lastShWatchTime) > 2500)) {{
            lastShWatchTriggered = shWatch;
            lastShWatchTime = nowSh;
            navigate("https://www.youtube.com/watch?v=" + shWatch, "sh-watch");
            return;
          }}
        }}
      }} catch (_) {{}}

      let videoId = extractVideoId(currentTarget);
      if (!videoId) {{
        const flexy = doc.querySelector("ytd-watch-flexy");
        const fv = flexy ? (rawGetAttr.call(flexy, "video-id") || "") : "";
        if (/^[A-Za-z0-9_-]{{11}}$/.test(fv)) videoId = fv;
        else if (shWatchParam && doc.querySelector("ytd-watch-flexy, #movie_player video.html5-main-video")) videoId = shWatchParam;
      }}
      const isShorts = /\\/shorts(\\/|$|\\?)/.test(currentTarget);

      // If on a standard watch page and #movie_player is trapped inside a hidden #player.skeleton while ytd-watch-flexy is visible, adopt #movie_player into ytd-watch-flexy's active player container
      if (!isShorts && videoId) {{
        try {{
          const watchFlexy = doc.querySelector("ytd-watch-flexy");
          const moviePlayer = doc.getElementById("movie_player");
          if (watchFlexy && moviePlayer) {{
            const flexyPlayerHost = watchFlexy.querySelector("#ytd-player #container, #player-container-inner #player-container, #player-container");
            if (flexyPlayerHost && !flexyPlayerHost.contains(moviePlayer)) {{
              const mpRect = moviePlayer.getBoundingClientRect();
              const inSkeleton = Boolean(moviePlayer.closest("#player.skeleton, .skeleton"));
              if (inSkeleton || mpRect.width < 50 || mpRect.height < 50) {{
                flexyPlayerHost.appendChild(moviePlayer);
                try {{ frameEl.contentWindow?.dispatchEvent(new Event("resize")); }} catch (_) {{}}
              }}
            }}
          }}
        }} catch (_) {{}}
      }}

      const mainVid = isShorts
        ? (doc.querySelector("#shorts-player video.html5-main-video, ytd-shorts video.html5-main-video") || doc.querySelector("video.html5-main-video"))
        : (doc.querySelector("#movie_player video.html5-main-video, ytd-watch-flexy video.html5-main-video") || doc.querySelector("video.html5-main-video"));

      if (isShorts && !videoId && mainVid) {{
        const vSrc = rawGetAttr.call(mainVid, "src") || mainVid.src || "";
        const mStream = vSrc.match(/\\/yts\\/stream\\/([A-Za-z0-9_-]{{11}})\\.mp4/);
        if (mStream) videoId = mStream[1];
      }}

      if (!videoId && !isShorts) {{
        autoStartedVideoId = "";
        // When browsing Home, Search results, or Channel page:
        // Close miniplayer and pause any background playback so sound doesn't persist
        try {{
          const miniPlayer = doc.querySelector("ytd-miniplayer");
          if (miniPlayer) {{
            const closeBtn = miniPlayer.querySelector("#close-button, button[aria-label*='Close'], button[aria-label*='閉じる']");
            if (closeBtn) closeBtn.click();
            else {{
              const mpV = miniPlayer.querySelector("video");
              if (mpV && !mpV.paused) mpV.pause();
            }}
          }}
        }} catch (_) {{}}
        return;
      }}

      // In Shorts, ensure standard watch / miniplayer video is paused so audio never overlaps
      if (isShorts) {{
        try {{
          const watchVideos = doc.querySelectorAll("#movie_player video, ytd-miniplayer video");
          for (let i = 0; i < watchVideos.length; i++) {{
            if (watchVideos[i] !== mainVid && !watchVideos[i].paused) {{
              watchVideos[i].pause();
            }}
          }}
        }} catch (_) {{}}
      }} else {{
        // When on standard watch page, ensure background shorts or miniplayer are not playing
        try {{
          const miniPlayer = doc.querySelector("ytd-miniplayer");
          if (miniPlayer) {{
            const mpV = miniPlayer.querySelector("video");
            if (mpV && !mpV.paused) mpV.pause();
          }}
          const shortsVideos = doc.querySelectorAll("ytd-shorts video, #shorts-player video");
          for (let i = 0; i < shortsVideos.length; i++) {{
            if (shortsVideos[i] !== mainVid && !shortsVideos[i].paused) shortsVideos[i].pause();
          }}
        }} catch (_) {{}}
      }}

      // Safe ad skip: click skip / close buttons if any ad overlay appears (never jump mainVid.currentTime to duration)
      try {{
        const skipBtn = doc.querySelector(".ytp-ad-skip-button, .ytp-ad-skip-button-modern, .ytp-skip-ad-button");
        if (skipBtn) skipBtn.click();
        const adCloseBtn = doc.querySelector(".ytp-ad-overlay-close-button, .ytp-ad-skip-button-slot button");
        if (adCloseBtn) adCloseBtn.click();
      }} catch (_) {{}}

      // Clear stuck spinner when playback is active, ensure video element & container have non-zero visible geometry, and recover from media errors
      const autoKey = videoId || (isShorts ? "shorts" : "");
      if (mainVid && autoKey) {{
        if (mainVid.preload !== "auto") {{
          try {{ mainVid.preload = "auto"; }} catch (_) {{}}
        }}
        // Guarantee non-zero visible video box so video is never black while audio plays
        try {{
          const vRect = mainVid.getBoundingClientRect();
          if (vRect.width < 50 || vRect.height < 50 || mainVid.style.display === "none" || mainVid.style.visibility === "hidden" || mainVid.style.opacity === "0") {{
            mainVid.style.cssText = "display:block !important;visibility:visible !important;opacity:1 !important;width:100% !important;height:100% !important;left:0px !important;top:0px !important;object-fit:contain !important;";
            if (mainVid.parentElement && mainVid.parentElement.classList.contains("html5-video-container")) {{
              mainVid.parentElement.style.cssText = "display:block !important;visibility:visible !important;opacity:1 !important;width:100% !important;height:100% !important;position:relative !important;";
            }}
          }}
        }} catch (_) {{}}

        // If mainVid is stuck at readyState < 2 (currentTime === 0) or has an error or missing stream src, bind direct muxed MP4 stream
        if (videoId) {{
          const curSrcAttr = rawGetAttr.call(mainVid, "src") || mainVid.src || "";
          const directStreamUrl = location.origin + "/youtube/runtime/stream/" + videoId;
          let isYtAuthed = false;
          try {{
            const cDump = String(controller?.cookieJar?.dump?.() || localStorage.getItem("__sh_cookie_vault_yt") || "");
            isYtAuthed = /(?:^|[\\s;"])(?:SAPISID|__Secure-3PAPISID)=([^;\\s"]+)/.test(cDump);
          }} catch (_) {{}}
          if (mainVid.dataset.shActiveVid !== videoId) {{
            mainVid.dataset.shActiveVid = videoId;
            mainVid.dataset.shActiveSince = String(Date.now());
            mainVid.dataset.shDirectRetryCount = "0";
          }}
          const activeElapsed = Date.now() - Number(mainVid.dataset.shActiveSince || Date.now());
          const retryCount = Number(mainVid.dataset.shDirectRetryCount || 0);
          const hasValidDirectSrc = curSrcAttr.includes("/youtube/runtime/stream/" + videoId) || curSrcAttr.includes("/yts/stream/" + videoId);
          const hasBlobMseSrc = curSrcAttr.startsWith("blob:");
          const noSrcWaitMs = isYtAuthed ? 8000 : 2500;
          const blobWaitMs = isYtAuthed ? 15000 : 8500;
          if (
            mainVid.readyState < 2 &&
            Number(mainVid.currentTime || 0) === 0 &&
            retryCount < 3 &&
            (
              Boolean(mainVid.error) ||
              (!hasValidDirectSrc && !hasBlobMseSrc && activeElapsed > noSrcWaitMs) ||
              (hasBlobMseSrc && mainVid.readyState === 0 && activeElapsed > blobWaitMs) ||
              (hasValidDirectSrc && activeElapsed > 4500)
            )
          ) {{
            mainVid.dataset.shDirectRetryCount = String(retryCount + 1);
            mainVid.dataset.shActiveSince = String(Date.now());
            rawSetAttr.call(mainVid, "src", directStreamUrl);
            mainVid.load();
            mainVid.play().catch(() => {{
              mainVid.muted = true;
              mainVid.play().catch(() => {{}});
            }});
          }}
        }}

        // If audio is playing (currentTime > 0.5) but videoWidth === 0 (audio-only or broken video track), force muxed MP4 stream
        if (!mainVid.paused && mainVid.readyState >= 2 && mainVid.videoWidth === 0 && Number(mainVid.currentTime || 0) > 0.5 && videoId && mainVid.dataset.shFixedZeroWidth !== videoId) {{
          mainVid.dataset.shFixedZeroWidth = videoId;
          const savedT = Number(mainVid.currentTime || 0);
          const directStream = location.origin + "/youtube/runtime/stream/" + videoId;
          rawSetAttr.call(mainVid, "src", directStream);
          mainVid.load();
          const onFixMeta = () => {{
            mainVid.removeEventListener("loadedmetadata", onFixMeta);
            try {{ if (savedT > 0) mainVid.currentTime = savedT; }} catch (_) {{}}
            mainVid.play().catch(() => {{}});
          }};
          mainVid.addEventListener("loadedmetadata", onFixMeta);
        }}

        const nowMs = Date.now();
        const curTime = Number(mainVid.currentTime || 0);
        const lastT = Number(mainVid.dataset.shLastTime || -1);
        const lastCheckMs = Number(mainVid.dataset.shLastCheckMs || 0);
        if (Math.abs(curTime - lastT) > 0.05) {{
          mainVid.dataset.shLastTime = String(curTime);
          mainVid.dataset.shLastCheckMs = String(nowMs);
          mainVid.dataset.shStallCount = "0";
        }} else if (!mainVid.paused && !mainVid.ended && !mainVid.seeking && curTime > 0 && lastCheckMs > 0 && (nowMs - lastCheckMs) > 6500) {{
          mainVid.dataset.shLastCheckMs = String(nowMs);
          const stalls = Number(mainVid.dataset.shStallCount || 0) + 1;
          mainVid.dataset.shStallCount = String(stalls);
          if (mainVid.error && stalls <= 2) {{
            const savedTime = curTime;
            const curSrc = rawGetAttr.call(mainVid, "src") || mainVid.src || "";
            if (curSrc && (curSrc.includes("/youtube/runtime/stream/") || curSrc.includes("/yts/stream/"))) {{
              rawSetAttr.call(mainVid, "src", curSrc);
              mainVid.load();
              const onLoaded = () => {{
                mainVid.removeEventListener("loadedmetadata", onLoaded);
                try {{ mainVid.currentTime = savedTime; }} catch (_) {{}}
                mainVid.play().catch(() => {{}});
              }};
              mainVid.addEventListener("loadedmetadata", onLoaded);
            }}
          }}
        }}

        if (!mainVid.paused && mainVid.currentTime > 0 && mainVid.readyState >= 2) {{
          if (mainVid.dataset.shPlayingLogged !== autoKey) {{
            mainVid.dataset.shPlayingLogged = autoKey;
            clientLog("info", "video-playing", {{ videoId: autoKey, readyState: mainVid.readyState, videoWidth: mainVid.videoWidth || 0, videoHeight: mainVid.videoHeight || 0, currentTime: Number(mainVid.currentTime.toFixed(2)), duration: Math.round(mainVid.duration || 0), elapsed_ms: Math.round(performance.now() - startedAt) }});
          }}
          const stuckSpinner = doc.querySelector(".ytp-spinner");
          if (stuckSpinner && stuckSpinner.style.display !== "none") {{
            stuckSpinner.style.display = "none";
          }}
          const cuedOverlay = doc.querySelector("#movie_player .ytp-cued-thumbnail-overlay");
          if (cuedOverlay && cuedOverlay.style.display !== "none") {{
            cuedOverlay.style.display = "none";
          }}
        }} else if (mainVid.paused && mainVid.currentTime === 0 && mainVid.readyState >= 2 && mainVid.dataset.shAutoplayVid !== autoKey) {{
          mainVid.dataset.shAutoplayVid = autoKey;
          clientLog("info", "video-autoplay-ready", {{ videoId: autoKey, readyState: mainVid.readyState, duration: Math.round(mainVid.duration || 0) }});
          try {{
            const mp = doc.getElementById(isShorts ? "shorts-player" : "movie_player");
            if (mp && typeof mp.playVideo === "function") mp.playVideo();
          }} catch (_) {{}}
          mainVid.play().catch(() => {{
            mainVid.muted = true;
            mainVid.play().catch(() => {{}});
          }});
        }}
      }}
    }} catch (err) {{
      console.debug("[SchoolHub YouTube] ensureWatchPlayer error", err);
    }}
  }}

  function currentDocumentHealthy() {{
    try {{
      const win = frameEl.contentWindow;
      const href = String(win?.location?.href || "");
      const doc = frameEl.contentDocument;
      if (!doc || href === "about:blank") {{
        return {{ healthy: false, loading: false, title: "", textLength: 0, children: 0 }};
      }}
      const body = doc.body;
      const title = String(doc.title || "").trim();
      const text = String(body?.innerText || "").replace(/\\s+/g, " ").trim();
      const children = Number(body?.children?.length || 0);
      const hasYouTubeApp = Boolean(
        doc.querySelector("ytd-app, ytm-app, #movie_player, video, #content, #page-manager, input[type='email'], #identifierId, form")
      );
      const isProxied = href.includes(PREFIX);
      const loading = isProxied && (doc.readyState === "loading" || doc.readyState === "interactive");
      const healthy = Boolean(
        isProxied && (
          hasYouTubeApp ||
          title.length > 0 ||
          text.length > 16 ||
          children >= 1
        )
      );
      return {{
        healthy,
        loading,
        title,
        textLength: text.length,
        children,
      }};
    }} catch (_) {{
      return {{ healthy: false, loading: false, title: "", textLength: 0, children: 0 }};
    }}
  }}

  function showHealthy(url="") {{
    clearTimeout(healthTimer);
    clearTimeout(recoveryTimer);
    recoveryTimer = 0;
    recovery = 0;
    cover.classList.add("hidden");
    const detected = url || readFrameTarget();
    if (detected) observeUrl(detected);
    ensureWatchPlayer(detected || lastUrl);
    const snap = currentDocumentHealthy();
    if (snap.title) lastTitle = snap.title;
    log("FRAME HEALTHY:", {{ title: snap.title || lastTitle || "YouTube", url: detected || lastUrl, elapsed_ms: Math.round(performance.now() - startedAt) }});
    if (status && !status.classList.contains("hidden")) {{
      setTimeout(() => status.classList.add("hidden"), 1200);
    }}
  }}

  function armHealthCheck(token, waitMs=HEALTH_DELAY) {{
    clearTimeout(healthTimer);
    healthTimer = setTimeout(() => {{
      if (!frame || token !== navigationToken) return;
      const snap = currentDocumentHealthy();
      if (snap.healthy) {{
        showHealthy();
        return;
      }}
      if (snap.loading) {{
        armHealthCheck(token, 8000);
        return;
      }}
      recover("blank-or-unhydrated");
    }}, waitMs);
  }}

  async function createController(kind) {{
    log("STEP 5: constructing Controller (" + kind + ")");
    const C = globalThis.$scramjetController?.Controller;
    if (typeof C !== "function") throw new Error("Scramjet Controller not found");
    const client = await getTransport(kind);
    const sj = globalThis.$scramjet;
    const scramjetConfig = {{
      flags: {{
        ...(sj?.defaultConfig?.flags || {{}}),
        allowInvalidJs: true,
        allowFailedIntercepts: true,
        encapsulateWorkers: true,
      }},
    }};

    let instance = null;
    try {{
      instance = new C({{
        serviceworker: navigator.serviceWorker.controller,
        transport: client,
        config: {{
          prefix: PREFIX,
          scramjetPath: "/youtube/runtime/scramjet.js?v={RUNTIME_REV}",
          injectPath: "/youtube/runtime/controller.inject.js?v={RUNTIME_REV}",
          wasmPath: "/scramjet/scramjet.wasm",
          virtualWasmPath: "scramjet.wasm.js",
          codec: {{
            encode: (value) => value ? encodeURIComponent(value) : value,
            decode: (value) => value ? decodeURIComponent(value) : value,
          }},
        }},
        scramjetConfig,
      }});
      log("CONTROLLER CREATED:", "id=" + instance.id, "prefix=" + instance.prefix, "transport=" + kind);
      await withTimeout(instance.wait(), 20000, "Scramjet Controller");
      log("CONTROLLER READY:", "elapsed_ms=" + Math.round(performance.now() - startedAt));
      return {{ instance, client }};
    }} catch (error) {{
      try {{ client?.destroy?.(); }} catch (_) {{}}
      try {{ client?.close?.(); }} catch (_) {{}}
      throw error;
    }}
  }}

  async function switchTransport(kind) {{
    if (!controller || transportName === kind) return true;
    try {{
      const next = await getTransport(kind);
      if (typeof controller.setTransport !== "function") return false;
      const old = transport;
      controller.setTransport(next);
      transport = next;
      transportName = kind;
      try {{ old?.destroy?.(); }} catch (_) {{}}
      try {{ old?.close?.(); }} catch (_) {{}}
      message("通信方式を" + kind + "へ切り替えました");
      return true;
    }} catch (e) {{
      console.warn("[SchoolHub YouTube] transport switch failed", kind, e);
      return false;
    }}
  }}

  const isOurSw = (c) => Boolean(c && String(c.scriptURL || "").includes("/youtube/sw.js"));

  async function waitForWorkerControl(reg) {{
    if (isOurSw(navigator.serviceWorker.controller)) {{
      try {{ sessionStorage.removeItem(SW_GUARD); }} catch (_) {{}}
      return;
    }}
    try {{ reg?.active?.postMessage("claim"); }} catch (_) {{}}
    const pendingSw = reg?.installing || reg?.waiting;
    if (pendingSw) {{
      await new Promise((resolve) => {{
        let done = false;
        const finish = () => {{
          if (done) return;
          done = true;
          clearTimeout(t);
          navigator.serviceWorker.removeEventListener("controllerchange", finish);
          resolve();
        }};
        navigator.serviceWorker.addEventListener("controllerchange", finish);
        pendingSw.addEventListener("statechange", () => {{
          if (pendingSw.state === "activated") {{
            try {{ pendingSw.postMessage("claim"); }} catch (_) {{}}
            if (isOurSw(navigator.serviceWorker.controller)) finish();
          }} else if (pendingSw.state === "redundant") {{
            finish();
          }}
        }});
        const t = setTimeout(finish, 3500);
      }});
    }}
    if (isOurSw(navigator.serviceWorker.controller)) {{
      try {{ sessionStorage.removeItem(SW_GUARD); }} catch (_) {{}}
      return;
    }}
    try {{ reg?.active?.postMessage("claim"); }} catch (_) {{}}
    let settled = false;
    let timer = 0;
    let onChange = null;
    const waitForChange = new Promise(resolve => {{
      onChange = () => {{
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        navigator.serviceWorker.removeEventListener("controllerchange", onChange);
        resolve();
      }};
      navigator.serviceWorker.addEventListener("controllerchange", onChange);
      timer = setTimeout(() => {{
        if (settled) return;
        settled = true;
        navigator.serviceWorker.removeEventListener("controllerchange", onChange);
        resolve();
      }}, CONTROL_WAIT);
    }});
    await waitForChange;
    if (isOurSw(navigator.serviceWorker.controller)) {{
      try {{ sessionStorage.removeItem(SW_GUARD); }} catch (_) {{}}
      return;
    }}
    let reloaded = false;
    try {{ reloaded = sessionStorage.getItem(SW_GUARD) === "1"; }} catch (_) {{}}
    if (!reloaded) {{
      try {{ sessionStorage.setItem(SW_GUARD, "1"); }} catch (_) {{}}
      message("Service Worker制御を確立するため再読み込みします…");
      location.reload();
      throw new Error("Service Worker control reload scheduled");
    }}
    throw new Error("Service Worker did not take control");
  }}

  async function recover(reason) {{
    if (!frame || recoveryTimer) return;
    const snap = currentDocumentHealthy();
    if (snap.healthy) {{
      showHealthy();
      return;
    }}
    if (status) status.classList.remove("hidden");
    if (recovery >= MAX_RECOVERY) {{
      cover.classList.remove("hidden");
      cover.textContent = "YouTubeを安定して読み込めませんでした。ページを再読み込みしてください。";
      fail("自動復旧を終了 (" + reason + ")", reason);
      notify("Scramjet/YouTube側で復旧できない状態です。手動再読み込みで再試行できます。", 12000);
      return;
    }}

    recovery += 1;
    const attempt = recovery;
    log("RECOVERY START:", "attempt=" + attempt + "/" + MAX_RECOVERY, "reason=" + reason);
    clientLog("warn", "recovery-start", {{ attempt, reason, target: safeSourceUrl(readFrameTarget() || lastUrl || HOME) }});
    clearTimeout(healthTimer);
    navigationToken += 1;
    const recoveryToken = navigationToken;
    cover.classList.remove("hidden");
    cover.textContent = "YouTubeを復旧中… (" + attempt + "/" + MAX_RECOVERY + ")";
    message("自動復旧 " + attempt + "/" + MAX_RECOVERY);

    recoveryTimer = setTimeout(async () => {{
      recoveryTimer = 0;
      try {{
        try {{ controller?.setupMessagePort?.(); }} catch (_) {{}}
        if (attempt === 2 && transportName === "hybrid") await switchTransport("libcurl");
        else if (attempt === 3) await switchTransport("hybrid");
        const target = readFrameTarget() || lastUrl || HOME;
        lastUrl = target;
        remember(target);
        setAddress(target);
        frame.go(target);
        armHealthCheck(recoveryToken, HEALTH_DELAY);
      }} catch (e) {{
        console.warn("[SchoolHub YouTube] recovery failed", reason, e);
      }}
    }}, attempt === 1 ? 1000 : attempt === 2 ? 1500 : 2000);
  }}

  function startFrameMonitor() {{
    clearInterval(pollTimer);
    pollTimer = setInterval(() => {{
      if (!ready || !frame) return;
      const snap = currentDocumentHealthy();
      if (snap.healthy) {{
        const target = readFrameTarget();
        if (!cover.classList.contains("hidden")) {{
          showHealthy(target);
        }} else {{
          if (target && target !== lastUrl) observeUrl(target);
          ensureWatchPlayer(target || lastUrl);
          if (snap.title && snap.title !== lastTitle) {{
            lastTitle = snap.title;
          }}
        }}
      }}
    }}, 650);
  }}

  function bindFrame() {{
    frameEl.addEventListener("load", () => {{
      const token = navigationToken;
      log("FRAME LOAD EVENT:", frameEl.src);
      const snap = currentDocumentHealthy();
      log("FRAME DIAG:", {{ readyState: frameEl.contentDocument?.readyState || "", title: snap.title, healthy: snap.healthy, children: snap.children }});
      if (snap.healthy) {{
        showHealthy();
      }} else {{
        setTimeout(() => {{
          const s2 = currentDocumentHealthy();
          if (s2.healthy) showHealthy();
          else if (status && !status.classList.contains("hidden")) setTimeout(() => status.classList.add("hidden"), 1200);
        }}, 1000);
        armHealthCheck(token, 12000);
      }}
    }});
    frameEl.addEventListener("error", (ev) => {{
      log("FRAME ERROR EVENT:", textOf(ev), "src=" + frameEl.src);
      recover("iframe-error");
    }});
    startFrameMonitor();
  }}

  async function navigate(raw, why) {{
    if (!frame) throw new Error("frame not ready");
    const target = normalize(raw);
    log("STEP 7: navigating iframe ->", target, "(" + why + ")");
    clientLog("info", "navigate", {{ reason: why, target: safeSourceUrl(target) }});
    navigationToken += 1;
    recovery = 0;
    autoStartedVideoId = "";
    lastUrl = target;
    remember(target);
    setAddress(target);
    cover.classList.remove("hidden");
    cover.textContent = "YouTubeを読み込んでいます…";
    clearTimeout(healthTimer);
    frame.go(target);
    log("FRAME GO CALLED:", "src=" + (frameEl.src || "pending"));
    armHealthCheck(navigationToken, HEALTH_DELAY);
  }}

  function keepalive() {{
    clearInterval(keepaliveTimer);
    const ping = () => {{
      try {{ navigator.serviceWorker.controller?.postMessage("keepalive"); }} catch (_) {{}}
    }};
    ping();
    keepaliveTimer = setInterval(ping, 15000);
  }}

  async function boot() {{
    log("BOOT START elapsed_ms=" + Math.round(performance.now() - startedAt));
    const isolated = Boolean(globalThis.crossOriginIsolated);
    log("BROWSER:", {{ ua: navigator.userAgent, platform: navigator.platform, vendor: navigator.vendor, crossOriginIsolated: isolated }});
    try {{
      if (localStorage.getItem("sh:yt:rev") !== "{RUNTIME_REV}") {{
        localStorage.setItem("sh:yt:rev", "{RUNTIME_REV}");
        if (typeof caches !== "undefined") {{
          const keys = await caches.keys();
          await Promise.all(keys.filter(k => k.startsWith("sh-yt-") && k !== STATIC_CACHE_NAME && k !== MEDIA_CACHE_NAME).map(k => caches.delete(k)));
        }}
      }}
    }} catch (_) {{}}
    clientLog("info", "boot-start", {{
      userAgent: String(navigator.userAgent || "").slice(0, 500),
      crossOriginIsolated: isolated,
      wispMode: WISP_OVERRIDE ? "override" : "same-origin",
      wispEndpoint: WISP_OVERRIDE ? safeSourceUrl(WISP_OVERRIDE) : WISP_PATH,
    }});
    if (!("serviceWorker" in navigator)) throw new Error("Service Worker unavailable");

    log("STEP 1: registering Service Worker", SW, "scope=" + SW_SCOPE);
    if (isOurSw(navigator.serviceWorker.controller) && String(navigator.serviceWorker.controller?.scriptURL || "").includes("{RUNTIME_REV}")) {{
      try {{ sessionStorage.removeItem(SW_GUARD); }} catch (_) {{}}
      log("SW CONTROLLER ALREADY ACTIVE:", String(navigator.serviceWorker.controller?.scriptURL || ""));
      clientLog("info", "service-worker-already-controlled", {{
        scriptURL: String(navigator.serviceWorker.controller?.scriptURL || ""),
      }});
      (window.__shSwRegP || navigator.serviceWorker.getRegistration(SW_SCOPE)).then((r) => {{ if (r) registration = r; }}).catch(() => {{}});
    }} else {{
      registration = await withTimeout(
        window.__shSwRegP || navigator.serviceWorker.register(SW, {{ scope: SW_SCOPE, updateViaCache: "none", type: "classic" }}),
        4500,
        "SW register"
      ).catch(async () => await navigator.serviceWorker.getRegistration(SW_SCOPE));
      log("SW REGISTERED:", "scope=" + (registration?.scope || ""), "active=" + Boolean(registration?.active));
      clientLog("info", "service-worker-registered", {{ scope: registration?.scope || "", active: Boolean(registration?.active) }});
      await waitForWorkerControl(registration);
    }}
    navigator.serviceWorker.addEventListener("controllerchange", () => {{
      log("SW CONTROLLERCHANGE:", navigator.serviceWorker.controller?.scriptURL || "null");
      try {{ controller?.setupMessagePort?.(); }} catch (_) {{}}
    }});
    log("SW CONTROLLER OK:", navigator.serviceWorker.controller?.scriptURL || "active", "elapsed_ms=" + Math.round(performance.now() - startedAt));
    clientLog("info", "service-worker-controlled", {{ controller: Boolean(navigator.serviceWorker.controller) }});
    keepalive();

    log("STEP 2: Scramjet core loaded", "version={SCRAMJET_VERSION}");
    log("STEP 3: Controller API loaded", "version={CONTROLLER_VERSION}");
    log("STEP 4: initializing hybrid HTTP/2 + Wisp transport");
    let created;
    try {{
      created = await createController("hybrid");
      transportName = "hybrid";
      clientLog("info", "controller-created", {{ transport: "hybrid" }});
    }} catch (hybridErr) {{
      log("HYBRID TRANSPORT FAILED:", textOf(hybridErr?.message || hybridErr));
      clientLog("warn", "hybrid-failed", {{ error: safeError(hybridErr) }});
      try {{
        created = await createController("libcurl");
        transportName = "libcurl";
        clientLog("info", "controller-created", {{ transport: "libcurl" }});
      }} catch (e) {{
        log("LIBCURL TRANSPORT FAILED:", textOf(e?.message || e));
        clientLog("warn", "libcurl-failed", {{ error: safeError(e) }});
        created = await createController("epoxy");
        transportName = "epoxy";
        clientLog("info", "controller-created", {{ transport: "epoxy" }});
      }}
    }}

    controller = created.instance;
    window.__schoolhub_controller = controller;
    transport = created.client;
    log("STEP 6: creating Scramjet iframe");
    frame = controller.createFrame(frameEl, {{ plugins: buildPlugins() }});
    clientLog("info", "frame-created", {{ frameCount: Array.isArray(controller?.frames) ? controller.frames.length : null }});
    bindFrame();
    ready = true;

    const stored = restore();
    await navigate(stored, "YouTube起動");
  }}

  window.addEventListener("popstate", (ev) => {{
    if (!ready || !frame) return;
    const stTarget = ev.state?.shYtTarget;
    if (stTarget && stTarget !== lastUrl) {{
      navigate(stTarget, "popstate").catch(() => {{}});
    }}
  }});

  window.addEventListener("online", () => {{
    if (!ready || !frame) return;
    message("接続復旧。再読み込み中…");
    try {{ frame.reload(); }} catch (_) {{}}
    armHealthCheck(++navigationToken, HEALTH_DELAY);
  }});

  window.addEventListener("beforeunload", () => {{
    try {{ controller?.persistCookies?.(); }} catch (_) {{}}
    clearInterval(keepaliveTimer);
    clearInterval(pollTimer);
  }});

  boot().catch((error) => {{
    fail("boot failed", error);
    clientLog("error", "boot-failed", {{ error: safeError(error), crossOriginIsolated: Boolean(globalThis.crossOriginIsolated) }});
    console.error("[SchoolHub YouTube] boot failed", error);
    cover.classList.remove("hidden");
    cover.textContent = "YouTubeを起動できませんでした。ページを再読み込みしてください。";
  }});

  document.addEventListener("visibilitychange", () => {{
    if (document.visibilityState === "hidden") {{
      try {{ controller?.persistCookies?.(); }} catch (error) {{ console.warn("[SchoolHub YouTube] cookie persist", error); }}
    }} else if (document.visibilityState === "visible") {{
      if (ready && frame) armHealthCheck(navigationToken, 8000);
    }}
  }});
}})();
</script>
</body>
</html>"""


def _build_youtube_shell_response(target: str) -> HTMLResponse:
    parsed_target = urlparse(target)
    init_vid = _extract_request_video_id(parsed_target, b"")
    if init_vid:
        _PREWARM_REQUESTED_VIDS.add(init_vid)
        _ensure_stream_head_prewarm(
            init_vid,
            f"https://invid-api.poketube.fun/companion/latest_version?id={init_vid}&itag=18&local=true",
        )
        asyncio.create_task(_fetch_android_vr_player(init_vid))
        asyncio.create_task(_resolve_stream_info(init_vid))
        asyncio.create_task(_get_anon_visitor_cookie_header())
    response = HTMLResponse(_shell_html(target))
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self' data: blob:; "
        "base-uri 'self'; object-src 'none'; form-action 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' 'wasm-unsafe-eval' data: blob:; "
        "style-src 'self' 'unsafe-inline' data: blob:; "
        "img-src 'self' data: blob: https:; font-src 'self' data: blob: https:; "
        "connect-src 'self' data: blob: ws: wss: https:; "
        "frame-src 'self' data: blob: https:; "
        "worker-src 'self' data: blob:; "
        "media-src 'self' data: blob: https:; "
        "child-src 'self' data: blob: https:; "
        "frame-ancestors 'self'; report-uri /_diag/youtube"
    )
    return response


@router.get("/youtube-beta", include_in_schema=False)
@router.get("/youtube-beta/", include_in_schema=False)
@router.get("/youtube-beta/{subpath:path}", include_in_schema=False)
async def youtube_beta_legacy_redirect(request: Request, subpath: str = ""):
    dest = "/youtube/" + subpath.lstrip("/") if subpath else "/youtube/"
    if request.url.query:
        dest += "?" + request.url.query
    return RedirectResponse(dest, status_code=307)


@router.get("/youtube", include_in_schema=False)
async def youtube_redirect(request: Request):
    dest = "/youtube/" + ("?" + request.url.query if request.url.query else "")
    return RedirectResponse(dest, status_code=307)


@router.get("/youtube/", response_class=HTMLResponse, include_in_schema=False)
@router.get("/youtube/watch", response_class=HTMLResponse, include_in_schema=False)
@router.get("/youtube/results", response_class=HTMLResponse, include_in_schema=False)
@router.get("/youtube/shorts", response_class=HTMLResponse, include_in_schema=False)
@router.get("/youtube/shorts/{video_id}", response_class=HTMLResponse, include_in_schema=False)
async def youtube_home(
    request: Request,
    q: str = "",
    v: str = "",
    url: str = "",
    search_query: str = "",
    video_id: str = "",
):
    raw_in = q.strip() or url.strip()
    if not raw_in:
        if video_id.strip():
            raw_in = f"https://www.youtube.com/shorts/{video_id.strip()}"
        elif v.strip():
            qs_extra = "&".join(
                f"{quote(k)}={quote(val)}"
                for k, val in request.query_params.multi_items()
                if k not in ("v", "q", "url")
            )
            raw_in = f"https://www.youtube.com/watch?v={v.strip()}" + (f"&{qs_extra}" if qs_extra else "")
        elif search_query.strip():
            raw_in = "https://www.youtube.com/results?" + request.url.query
        elif request.url.path.rstrip("/") == "/youtube/shorts":
            raw_in = "https://www.youtube.com/shorts"
    target = _target_from_input(raw_in)
    return _build_youtube_shell_response(target)


@router.get("/youtube/health", include_in_schema=False)
async def youtube_health():
    return {
        "ok": True,
        "service": SERVICE_NAME,
        "router_path": SERVICE_PATH,
        "autoload_suffix": "_proxy.py",
        "proxy_module": PROXY_MODULE,
        "scramjet": SCRAMJET_VERSION,
        "controller": CONTROLLER_VERSION,
        "utils": UTILS_VERSION,
        "libcurl": LIBCURL_VERSION,
        "epoxy": EPOXY_VERSION,
        "service_worker": "/youtube/sw.js",
        "wisp": SCRAMJET_WISP_URL or "/wisp-youtube/",
        "wisp_mode": "official-override" if SCRAMJET_WISP_URL else "existing-project-endpoint",
        "virtual_prefix": "/youtube/~/sj/",
        "transports": ["hybrid", "libcurl", "epoxy"],
        "features": [
            "official-ui-only",
            "search",
            "direct-video-navigation",
            "deep-link-routing",
            "google-login",
            "hd-mse-streaming",
            "shorts",
            "comments-likes-subscriptions",
            "studio-uploads",
            "cookie-persistence",
            "bounded-auto-recovery",
            "wisp-stream-packet-buffer",
            "webkit-stream-transfer-compat",
            "webkit-sw-post-body-compat",
            "http2-chrome-tls-relay",
        ],
    }


@router.get("/youtube/{subpath:path}", response_class=HTMLResponse, include_in_schema=False)
async def youtube_subpath(request: Request, subpath: str):
    clean_sub = (subpath or "").lstrip("/")
    if (
        clean_sub.startswith(("runtime/", "~/sj/", "wisp"))
        or clean_sub in ("sw.js", "health", "google-login")
    ):
        return Response("Not Found", status_code=404)
    upstream_url = "https://www.youtube.com/" + clean_sub
    if request.url.query:
        upstream_url += "?" + request.url.query
    target = _target_from_input(upstream_url)
    return _build_youtube_shell_response(target)


async def prewarm_service_cache() -> None:
    try:
        await asyncio.gather(
            _ensure_patched_runtime("scramjet.js", _patch_scramjet_js),
            _ensure_patched_runtime("controller.api.js", _patch_controller_api),
            _ensure_patched_runtime("controller.sw.js", _patch_controller_sw),
            _ensure_patched_runtime("controller.inject.js", _patch_controller_inject),
            _get_curl_session(),
            _get_media_http_client(),
            return_exceptions=True,
        )
        asyncio.create_task(_get_anon_visitor_cookie_header())
        asyncio.create_task(_get_home_rich_items())
    except Exception:
        pass


