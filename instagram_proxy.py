"""Official Instagram (instagram.com) isolated Scramjet proxy for School Hub.

Exposes:
  SERVICE_NAME = "Instagram"
  SERVICE_ICON = "📸"
  SERVICE_PATH = "/instagram/"
  router       = APIRouter()
"""
from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import os
import re
import socket
import struct
import time
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlparse, urlunparse

import httpx
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute

for _np_var in ("NO_PROXY", "no_proxy"):
    _np_val = os.environ.get(_np_var, "")
    if "::1" in _np_val:
        os.environ[_np_var] = ",".join(
            p for p in _np_val.split(",") if p.strip() not in ("::1", "[::1]")
        )

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
        if not (os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")):
            ipresolve = getattr(CurlOpt, "IPRESOLVE", None)
            if ipresolve is not None:
                curl.setopt(ipresolve, 1)  # CURL_IPRESOLVE_V4
        connect_timeout_ms = getattr(CurlOpt, "CONNECTTIMEOUT_MS", None)
        if connect_timeout_ms is not None:
            curl.setopt(connect_timeout_ms, 5000)
        tcp_keepalive = getattr(CurlOpt, "TCP_KEEPALIVE", None)
        if tcp_keepalive is not None:
            curl.setopt(tcp_keepalive, 1)
        fresh_connect = getattr(CurlOpt, "FRESH_CONNECT", None)
        if fresh_connect is not None:
            curl.setopt(fresh_connect, 1 if fresh else 0)
    except Exception:
        pass


if CurlAsyncSession is not None:

    class StatelessCurlSession(CurlAsyncSession):
        """HTTP/2 + Chrome-TLS session with zero C-level or Python-level cookie storage.

        Every request is 100% isolated to the headers explicitly passed by that
        request; Set-Cookie headers from upstream are returned to the caller's
        browser (Scramjet sessionStorage cookie vault) and immediately purged from
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

SERVICE_NAME = "Instagram"
SERVICE_ICON = "📸"
SERVICE_PATH = "/instagram/"
PROXY_MODULE = "instagram_proxy.py"

SCRAMJET_VERSION = "2.0.67-alpha.2"
CONTROLLER_VERSION = "0.0.14"
UTILS_VERSION = "0.0.3"
LIBCURL_VERSION = "2.0.5"
EPOXY_VERSION = "3.0.1"
RUNTIME_REV = "20261110-ig-v8"

SCRAMJET_WISP_URL = os.getenv("SCRAMJET_WISP_URL", "").strip()

IG_HOME = "https://www.instagram.com/"
IG_REELS = "https://www.instagram.com/reels/"
IG_EXPLORE = "https://www.instagram.com/explore/"
IG_LOGIN = "https://www.instagram.com/"

IG_HOSTS = {
    "instagram.com",
    "www.instagram.com",
    "i.instagram.com",
    "graph.instagram.com",
    "about.instagram.com",
    "help.instagram.com",
    "l.instagram.com",
    "facebook.com",
    "www.facebook.com",
    "m.facebook.com",
    "web.facebook.com",
}

IG_MEDIA_SUFFIXES = (
    "cdninstagram.com",
    "fbcdn.net",
)

AUTH_HOSTS = {
    "www.facebook.com",
    "m.facebook.com",
    "facebook.com",
    "accounts.google.com",
    "appleid.apple.com",
}

router = APIRouter()


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
        'const _shVaultKey="__sh_cookie_vault_instagram";'
        "function g(){return Promise.resolve(null)}"
        "function _readVaultRaw(){let a=null,b=null;try{let s=localStorage.getItem(_shVaultKey);if(s)a=u(JSON.parse(s))}catch(_){}try{let s=sessionStorage.getItem(_shVaultKey);if(s)b=u(JSON.parse(s))}catch(_){}let res=null;if(a&&b){try{let ca=JSON.parse(a.cookies||'{}'),cb=JSON.parse(b.cookies||'{}'),m=a.updatedAt>=b.updatedAt?{...cb,...ca}:{...ca,...cb};res={updatedAt:Math.max(a.updatedAt||0,b.updatedAt||0),cookies:JSON.stringify(m)}}catch(_){res=a.updatedAt>=b.updatedAt?a:b}}else{res=a||b||null}return res}"
        "async function w(){return _readVaultRaw()}"
        "async function k(e,t){try{let s=_readVaultRaw(),i=Math.max(Date.now(),t+1,(s?.updatedAt??0)+1),merged=e;try{let prev=JSON.parse(s?.cookies||'{}'),curr=JSON.parse(e||'{}');if(Object.keys(curr).length===0&&Object.keys(prev).length>0){merged=s.cookies}else{merged=JSON.stringify({...prev,...curr})}}catch(_){}let payload=JSON.stringify({updatedAt:i,cookies:merged});try{localStorage.setItem(_shVaultKey,payload)}catch(_){}try{sessionStorage.setItem(_shVaultKey,payload)}catch(_){}return i}catch(e){return t}}"
    )
    text = text.replace(old_idb, new_idb, 1)
    text = text.replace(
        'new BroadcastChannel("__scramjet_controller_channel")',
        "({postMessage(){},addEventListener(){},close(){}})",
        1,
    )
    text = text.replace(
        'let t=e.data.$controller$setCookie;t.options?.clear&&this.cookieJar.clear(),this.applyCookieSyncEntries(t.cookies),',
        'let t=e.data.$controller$setCookie;if(t.cid&&t.cid!==this.id){"string"==typeof t.id&&(navigator.serviceWorker.controller||this.serviceWorkerController)?.postMessage({$sw$setCookieDone:{id:t.id}});return}t.options?.clear&&this.cookieJar.clear(),this.applyCookieSyncEntries(t.cookies),this.persistCookies().catch(()=>{}),',
        1,
    )
    text = text.replace(
        "async sendSetCookie(t,r){await e.persistCookies(),await e.propagateCookieSync(t.map(({url:e,cookie:t})=>({url:e.href,cookie:t})),r)}",
        "async sendSetCookie(t,r){let n=t.map(({url:e,cookie:t})=>({url:e.href,cookie:t}));e.applyCookieSyncEntries(n),await e.persistCookies(),await e.propagateCookieSync(n,r)}",
        1,
    )

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
        's=[...e.request.headers],'
        '_shBody=(e.request.method!=="GET"&&e.request.method!=="HEAD")'
        '?await e.request.arrayBuffer().catch(()=>null):null,'
        '_shBodyVal=(_shBody&&_shBody.byteLength>0)?_shBody:null,'
        'n=await r.rpc.call('
    )
    if old_call in text:
        text = text.replace(old_call, new_call, 1)
        text = text.replace("body:e.request.body,", "body:_shBodyVal,", 1)
        text = text.replace(
            "e.request.body instanceof ReadableStream||e.request.body instanceof ArrayBuffer?[e.request.body]:void 0",
            "_shBodyVal instanceof ArrayBuffer?[_shBodyVal]:void 0",
            1,
        )

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
    return text.encode("utf-8")


def _patch_scramjet_js(source: bytes) -> bytes:
    """Fix Scramjet 2.0.67-alpha.2 for Instagram:
    1. history.replaceState(state, title) omitted 3rd argument fix.
    2. Rewrite Instagram media URLs (*.cdninstagram.com, *.fbcdn.net excluding JS/CSS/JSON)
       directly to /instagram/runtime/media-relay?k=<obfuscated> at URL-rewrite time so school
       filters never see cdninstagram.com/fbcdn.net in <img>/<video>/fetch URLs, and unrewrite
       ?k= back to the original URL transparently.
    3. Fix Scramjet's greedy srcset splitter and hook srcset/loading properties.
    4. Keep data:image/*, data:font/*, data:audio/*, data:video/* native.
    5. Unrewrite blob: URLs directly on <img>/<video>/<audio>/<source>.
    6. Ensure navigator.userAgent and navigator.webdriver match HTTP relay headers.
    7. Support EventListener handleEvent in addEventListener/removeEventListener.
    8. Window.postMessage pollutant constructor and origin safety.
    9. Error.prepareStackTrace masking for proxy traces.
    10. Scoped LocalStorage and SessionStorage with ig: prefix.
    """
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
        'function n(e,t){const _pfx=()=>"ig:"+e.url.host+"@";let r={get(t,r){switch(r){'
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
        'if(t.navigator.webdriver){const _wd=Object.getOwnPropertyDescriptor(_np,"webdriver");if(_wd&&_wd.get){const _wg=new Proxy(_wd.get,{apply:()=>!1});e.box.unproxy.set(_wg,_wd.get);Object.defineProperty(_np,"webdriver",{..._wd,get:_wg})}}'
        'const _c="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",_av="5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";'
        'const _ud=Object.getOwnPropertyDescriptor(_np,"userAgent");if(_ud&&_ud.get){const _ug=new Proxy(_ud.get,{apply:()=>_c});e.box.unproxy.set(_ug,_ud.get);Object.defineProperty(_np,"userAgent",{..._ud,get:_ug})}'
        'const _ad=Object.getOwnPropertyDescriptor(_np,"appVersion");if(_ad&&_ad.get){const _ag=new Proxy(_ad.get,{apply:()=>_av});e.box.unproxy.set(_ag,_ad.get);Object.defineProperty(_np,"appVersion",{..._ad,get:_ag})}'
        'const _pd=Object.getOwnPropertyDescriptor(_np,"platform");if(_pd&&_pd.get){const _pg=new Proxy(_pd.get,{apply:()=>"Win32"});e.box.unproxy.set(_pg,_pd.get);Object.defineProperty(_np,"platform",{..._pd,get:_pg})}'
        '}'
        '}catch(_){}})(),'
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
    new_stack = 'let _inSt=!1,_usrSt=null,r=(t,a)=>{if(_inSt)return String(t);_inSt=!0;try{if("function"==typeof _usrSt)return _usrSt(t,a);let n=[String(t)];for(let o=0;o<a.length;o++){let c=a[o],s=String(c),f="";try{f=c.getFileName()||""}catch{}try{const _fc=f.split("?")[0];if(f&&(_fc.includes("/instagram/runtime/")||_fc.endsWith("scramjet.js")||_fc.endsWith("scramjet.wasm.js")||_fc.endsWith("controller.inject.js")||e.config.maskedfiles.some(m=>_fc.endsWith(m))))continue}catch{}try{s=s.replace(/https?:\\/\\/[^\\s)]+\\/instagram\\/~\\/sj\\/(?:[^/]+\\/){0,2}(https?(?:%3A|:)[^\\s):]+)/gi,(_,u)=>{try{return decodeURIComponent(u).split("?")[0]}catch{return _}})}catch{}n.push("    at "+s)}return n.join("\\n")}finally{_inSt=!1}};try{Error.prepareStackTrace=r}catch{}e.Trap("Error.prepareStackTrace",{get:()=>_usrSt||r,set(v){_usrSt="function"==typeof v?v:null;try{Error.prepareStackTrace=r}catch{}}})'
    if old_stack in text:
        text = text.replace(old_stack, new_stack, 1)

    old_rew = 'if(!A||"http:"!=A.protocol&&"https:"!=A.protocol)return e;'
    new_rew = (
        'if(!A||"http:"!=A.protocol&&"https:"!=A.protocol)return e;'
        'try{'
        'if(A.pathname==="/instagram/runtime/media-relay"&&(A.searchParams.has("k")||A.searchParams.has("url")))return t.prefix.origin+A.pathname+A.search;'
        'const _mh2=A.hostname.toLowerCase().replace(/\\.$/,"");'
        'const _isMediaHost=(_mh2.endsWith(".cdninstagram.com")||_mh2==="cdninstagram.com"||_mh2.endsWith(".fbcdn.net")||_mh2==="fbcdn.net");'
        'const _isRsrc=A.pathname.startsWith("/rsrc.php/");'
        'if(_isMediaHost&&(!_isRsrc||/\\.(webp|png|jpg|jpeg|gif|avif|ico|svg|mp4|m4v|m4s|m3u8|woff2|woff|ttf)(\\?|$)/i.test(A.pathname))&&!/\\.(js|mjs|cjs|json|css|html|wasm)(\\?|$)/i.test(A.pathname)&&!o?.isModule&&o?.destination!=="script"&&o?.destination!=="worker"&&o?.destination!=="sharedworker"&&o?.destination!=="style"){'
        'return t.prefix.origin+"/instagram/runtime/media-relay?k="+btoa(encodeURIComponent(A.href).split("").reverse().join("")).replace(/\\+/g,"-").replace(/\\//g,"_").replace(/=+$/,"")'
        '}'
        '}catch(_){}'
    )
    if old_rew in text:
        text = text.replace(old_rew, new_rew, 1)

    old_unrew = 'if(!r.href.startsWith(t.prefix.href))return o.error("unrewriteurl: unexpected url",e),e;'
    new_unrew = (
        'if(!r.href.startsWith(t.prefix.href)){'
        'try{const _k=r.searchParams?.get("k");if(_k){let _b=_k.replace(/-/g,"+").replace(/_/g,"/");while(_b.length%4)_b+="=";return decodeURIComponent(atob(_b).split("").reverse().join(""))}}catch(_){}'
        'return r.searchParams?.get("url")||e;'
        '}'
    )
    if old_unrew in text:
        text = text.replace(old_unrew, new_unrew, 1)

    old_srcset = "function x(e,t,r){return e.split(/ .*,/).map"
    new_srcset = "function x(e,t,r){return e.split(/,(?=\\s*(?:https?:|\\/|data:|blob:))/i).map(e=>e.trim()).filter(Boolean).map"
    if old_srcset in text:
        text = text.replace(old_srcset, new_srcset, 1)

    old_url_props = "src:[t.HTMLImageElement,t.HTMLMediaElement,t.HTMLIFrameElement,t.HTMLFrameElement,t.HTMLEmbedElement,t.HTMLScriptElement,t.HTMLSourceElement],href:"
    new_url_props = "src:[t.HTMLImageElement,t.HTMLMediaElement,t.HTMLIFrameElement,t.HTMLFrameElement,t.HTMLEmbedElement,t.HTMLScriptElement,t.HTMLSourceElement],srcset:[t.HTMLImageElement,t.HTMLSourceElement],loading:[t.HTMLImageElement],href:"
    if old_url_props in text:
        text = text.replace(old_url_props, new_url_props, 1)

    old_setattr = 'e.Proxy("Element.prototype.setAttribute",{apply(t){let[r,s]=t.args,o=t.this.tagName.toLowerCase();null!=s&&(s=(0,n.Qf)(s)),t.args[1]=s;'
    new_setattr = 'e.Proxy("Element.prototype.setAttribute",{apply(t){let[r,s]=t.args,o=t.this.tagName.toLowerCase();null!=s&&(s=(0,n.Qf)(s)),t.args[1]=s;if("img"===o){const _rn=String(r).toLowerCase();if("data-loading"===_rn)return void t.return(void 0);if("loading"===_rn&&"lazy"===String(s).toLowerCase()){t.args[1]="eager";return;}}'
    if old_setattr in text:
        text = text.replace(old_setattr, new_setattr, 1)

    text = text.replace(
        'if(e.startsWith("data:")){',
        'if(e.startsWith("data:")){if(/^data:(image|font|audio|video)\\//i.test(e))return e;',
        1,
    )
    text = text.replace(
        'if(e.startsWith(t.prefix.href+"data:"))return e.substring(t.prefix.href.length);',
        'if(e.startsWith("data:"))return e;if(e.startsWith(t.prefix.href+"data:"))return e.substring(t.prefix.href.length);',
        1,
    )
    text = text.replace(
        'src:["embed","img","frame","input","track"]',
        'src:["embed","frame","input","track"]',
        1,
    )
    text = text.replace(
        'src:["video","audio","source"]',
        'src:["video","audio","source","img"]',
        1,
    )
    text = text.replace(
        't.args[0]=e.rewriteUrl(t.args[0],{destination:"worker",isModule:t.args[1]?.type==="module"})',
        't.args[0]=String(t.args[0]||"").startsWith("blob:")?"blob:"+self.location.origin+new URL(String(t.args[0]).slice(5)).pathname:e.rewriteUrl(t.args[0],{destination:"worker",isModule:t.args[1]?.type==="module"})',
        1,
    )
    text = text.replace(
        'e.Trap("Node.prototype.baseURI",',
        'e.Trap(["HTMLImageElement.prototype.currentSrc","HTMLMediaElement.prototype.currentSrc"],{get:t=>(0,l.v2)(t.get(),e.context)}),e.Trap("Node.prototype.baseURI",',
        1,
    )
    text = text.replace(
        't.args[0]=e.rewriteUrl(t.args[0],{destination:"sharedworker",isModule:r})',
        't.args[0]=String(t.args[0]||"").startsWith("blob:")?"blob:"+self.location.origin+new URL(String(t.args[0]).slice(5)).pathname:e.rewriteUrl(t.args[0],{destination:"sharedworker",isModule:r})',
        1,
    )

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
    new_perf = 'e.Trap("PerformanceEntry.prototype.name",{get(t){let r=(0,i.Qf)(t.get());return r&&(r.startsWith(e.context.prefix.href)||r.includes("/instagram/runtime/media-relay"))?e.unrewriteUrl(r):r}}),e.Proxy(["Performance.prototype.getEntries","Performance.prototype.getEntriesByType","Performance.prototype.getEntriesByName","PerformanceObserverEntryList.prototype.getEntries","PerformanceObserverEntryList.prototype.getEntriesByType","PerformanceObserverEntryList.prototype.getEntriesByName"],{apply(t){let r=t.call();return t.return(r.filter(t=>{const _nm=(0,i.Qf)(e.descriptors.get("PerformanceEntry.prototype.name",t))||"",_nc=_nm.split("?")[0];if(_nc.includes("/instagram/runtime/scramjet")||_nc.includes("/instagram/runtime/controller")||_nc.endsWith("scramjet.wasm.js"))return!1;for(let r of e.config.maskedfiles)if(_nc.endsWith(r))return!1;return!0}))}})'
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
        '}'
        'if(typeof navigator!=="undefined"){'
        'const _np=typeof Navigator!=="undefined"&&Navigator.prototype?Navigator.prototype:Object.getPrototypeOf(navigator);'
        'const _dUa="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";'
        'const _dAv="5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";'
        'if(navigator.webdriver&&_np){Object.defineProperty(_np,"webdriver",{get:()=>false,enumerable:true,configurable:true});}'
        'try{Object.defineProperty(navigator,"maxTouchPoints",{get:()=>0,configurable:true});}catch(_){}'
        'try{if(_np){Object.defineProperty(_np,"maxTouchPoints",{get:()=>0,configurable:true});}}catch(_){}'
        'try{if(_np){'
        'Object.defineProperty(_np,"userAgent",{get:()=>_dUa,enumerable:true,configurable:true});'
        'Object.defineProperty(_np,"appVersion",{get:()=>_dAv,enumerable:true,configurable:true});'
        'Object.defineProperty(_np,"platform",{get:()=>"Win32",enumerable:true,configurable:true});'
        'if("userAgentData"in _np||"userAgentData"in navigator){'
        'const _uad={brands:[{brand:"Google Chrome",version:"131"},{brand:"Chromium",version:"131"},{brand:"Not_A Brand",version:"24"}],mobile:false,platform:"Windows",getHighEntropyValues:async()=>({architecture:"x86",bitness:"64",brands:[{brand:"Google Chrome",version:"131"},{brand:"Chromium",version:"131"},{brand:"Not_A Brand",version:"24"}],fullVersionList:[{brand:"Google Chrome",version:"131.0.0.0"},{brand:"Chromium",version:"131.0.0.0"},{brand:"Not_A Brand",version:"24.0.0.0"}],mobile:false,model:"",platform:"Windows",platformVersion:"15.0.0",uaFullVersion:"131.0.0.0"}),toJSON(){return{brands:this.brands,mobile:false,platform:"Windows"};}};'
        'Object.defineProperty(_np,"userAgentData",{get:()=>_uad,enumerable:true,configurable:true});'
        '}'
        '}}catch(_){}'
        'if(navigator.credentials){'
        'const _rejCred=()=>Promise.reject(typeof DOMException!=="undefined"?new DOMException("The operation either timed out or was not allowed.","NotAllowedError"):new Error("NotAllowedError"));'
        'const _getCred=(opts)=>(opts&&opts.publicKey)?_rejCred():Promise.resolve(null);'
        'try{navigator.credentials.get=_getCred;navigator.credentials.create=_rejCred;}catch(_){'
        'try{Object.defineProperty(navigator,"credentials",{get:()=>({get:_getCred,create:_rejCred,store:()=>Promise.resolve(),preventSilentAccess:()=>Promise.resolve()}),configurable:true});}catch(__){}'
        '}}'
        '}'
        'if(typeof window!=="undefined"&&typeof window.matchMedia==="function"){'
        'const _origMM=window.matchMedia.bind(window);'
        'window.matchMedia=function(q){'
        'const qs=String(q||"");'
        'if(qs.includes("hover: none")||qs.includes("hover:none")||qs.includes("pointer: coarse")||qs.includes("pointer:coarse")){'
        'const mql=_origMM("not all");'
        'try{Object.defineProperty(mql,"media",{value:qs,configurable:true});Object.defineProperty(mql,"matches",{value:false,configurable:true});}catch(_){}'
        'return mql;'
        '}'
        'return _origMM(q);'
        '};'
        '}'
        'if(typeof Blob!=="undefined"){'
        'const _OrigBlob=Blob;'
        'const _wShim=`(()=>{try{if(typeof navigator!=="undefined"){const _dUa="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";if(navigator.webdriver){Object.defineProperty(navigator,"webdriver",{get:()=>false,configurable:true});}Object.defineProperty(navigator,"userAgent",{get:()=>_dUa,configurable:true});Object.defineProperty(navigator,"appVersion",{get:()=>_dUa.replace(/^Mozilla\\\\//,""),configurable:true});Object.defineProperty(navigator,"platform",{get:()=>"Win32",configurable:true});}if(typeof MessageEvent!=="undefined"){const _d=Object.getOwnPropertyDescriptor(MessageEvent.prototype,"data");if(_d&&_d.get){Object.defineProperty(MessageEvent.prototype,"data",{get(){const v=_d.get.call(this);return(v&&typeof v==="object"&&"$scramjet$data"in v)?v.$scramjet$data:v;},configurable:true});}}if(typeof MessagePort!=="undefined"&&MessagePort.prototype.postMessage){const _mp=MessagePort.prototype.postMessage;MessagePort.prototype.postMessage=function(d,t){return arguments.length>1?_mp.call(this,{$scramjet$messagetype:"worker",$scramjet$data:d},t):_mp.call(this,{$scramjet$messagetype:"worker",$scramjet$data:d});};}if(typeof self!=="undefined"&&typeof self.postMessage==="function"&&typeof window==="undefined"){const _sp=self.postMessage.bind(self);self.postMessage=globalThis.postMessage=function(d,t){return arguments.length>1?_sp({$scramjet$messagetype:"worker",$scramjet$data:d},t):_sp({$scramjet$messagetype:"worker",$scramjet$data:d});};}}catch(_){}})();\\n`;'
        'globalThis.Blob=new Proxy(_OrigBlob,{construct(target,args,newTarget){try{const parts=args[0],opts=args[1];if(Array.isArray(parts)&&parts.length>0&&typeof parts[0]==="string"&&(String(opts?.type||"").includes("javascript")||parts[0].includes("onmessage")||parts[0].includes("onconnect"))){args=[[_wShim,...parts],opts];}}catch(_){}return Reflect.construct(target,args,newTarget);}});'
        '}}catch(_){}\n'
    )
    if not text.startswith("try{if(typeof window") and not text.startswith("try{if(typeof navigator"):
        text = preamble + text
    return text.encode("utf-8")


@router.get("/instagram/runtime/scramjet.js", include_in_schema=False)
async def ig_scramjet_js():
    import app as host_app

    resp = await host_app.scramjet_asset("scramjet.js")
    if getattr(resp, "status_code", 500) != 200 or not getattr(resp, "body", b""):
        return resp
    patched = _patch_scramjet_js(bytes(resp.body))
    return Response(
        patched,
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/instagram/runtime/controller.api.js", include_in_schema=False)
async def ig_controller_api():
    import app as host_app

    resp = await host_app.scramjet_asset("controller.api.js")
    if getattr(resp, "status_code", 500) != 200 or not getattr(resp, "body", b""):
        return resp
    patched = _patch_controller_api(bytes(resp.body))
    return Response(
        patched,
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/instagram/runtime/controller.sw.js", include_in_schema=False)
async def ig_controller_sw():
    import app as host_app

    resp = await host_app.scramjet_asset("controller.sw.js")
    if getattr(resp, "status_code", 500) != 200 or not getattr(resp, "body", b""):
        return resp
    patched = _patch_controller_sw(bytes(resp.body))
    return Response(
        patched,
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/instagram/runtime/controller.inject.js", include_in_schema=False)
async def ig_controller_inject():
    import app as host_app

    resp = await host_app.scramjet_asset("controller.inject.js")
    if getattr(resp, "status_code", 500) != 200 or not getattr(resp, "body", b""):
        return resp
    patched = _patch_controller_inject(bytes(resp.body))
    return Response(
        patched,
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=3600"},
    )


async def _ig_sw_override(request: Request) -> Response:
    code = f"""
importScripts('/instagram/runtime/controller.sw.js?v={RUNTIME_REV}');
const IG_MEDIA_CACHE = 'sh-ig-media-{RUNTIME_REV}';
const IG_MEDIA_SUFFIXES = ['cdninstagram.com', 'fbcdn.net'];
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
function isDirectMediaHost(host) {{
  const h = String(host || '').toLowerCase().replace(/\\.$/, '');
  return IG_MEDIA_SUFFIXES.some(x => h === x || h.endsWith('.' + x));
}}
function isDirectMediaTarget(target) {{
  if (!target || !isDirectMediaHost(target.hostname)) return false;
  const p = String(target.pathname || '');
  if (/\\.(js|mjs|cjs|json|css|html|wasm)(\\?|$)/i.test(p)) return false;
  if (p.startsWith('/rsrc.php/') && !/\\.(webp|png|jpg|jpeg|gif|avif|ico|svg|mp4|m4v|m4s|m3u8|woff2|woff|ttf)(\\?|$)/i.test(p)) return false;
  return true;
}}
function isCacheableImageTarget(target) {{
  if (!target || !isDirectMediaHost(target.hostname)) return false;
  const p = String(target.pathname || '');
  if (p.includes('/o1/v/') || /\\.(mp4|m4v|m4s|m3u8)(\\?|$)/i.test(p)) return false;
  if (target.searchParams.has('bytestart') || target.searchParams.has('byteend')) return false;
  return true;
}}
function normalizeMediaTarget(target) {{
  try {{
    for (let k = 0; k < 3; k++) {{
      if (target.pathname === '/instagram/runtime/media-relay') {{
        const kv = target.searchParams.get('k');
        const uv = kv ? decodeMediaKey(kv) : target.searchParams.get('url');
        if (uv) {{
          target = new URL(uv);
          continue;
        }}
      }}
      break;
    }}
  }} catch {{}}
  return target;
}}
function decodeRepeated(value) {{
  let out = String(value || '');
  for (let i = 0; i < 5; i++) {{
    try {{ const next = decodeURIComponent(out); if (next === out) break; out = next; }} catch {{ break; }}
  }}
  return out;
}}
function stripScramjetParams(target) {{
  try {{
    for (const k of [...target.searchParams.keys()]) {{
      if (k.startsWith('$')) target.searchParams.delete(k);
    }}
  }} catch {{}}
  return target;
}}
function extractAnyVirtualUrl(url) {{
  try {{
    const u = new URL(url);
    if (u.pathname === '/instagram/runtime/media-relay') {{
      const kv = u.searchParams.get('k');
      const uv = kv ? decodeMediaKey(kv) : u.searchParams.get('url');
      if (uv) return stripScramjetParams(normalizeMediaTarget(new URL(uv)));
    }}
    const marker = '/instagram/~/sj/';
    const i = u.pathname.indexOf(marker);
    if (i < 0) return null;
    const rawAfter = u.pathname.slice(i + marker.length);
    const directMatch = rawAfter.match(/(?:^|\\/)(https?(?:%3A|:)(?:%2F|\\/){{2}}.*)$/i);
    if (directMatch && directMatch[1]) {{
      try {{
        const decoded = decodeRepeated(directMatch[1]);
        const target = normalizeMediaTarget(new URL(decoded));
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
      const candidate = decodeRepeated(bits.slice(n).join('/'));
      try {{
        const target = stripScramjetParams(normalizeMediaTarget(new URL(candidate)));
        target.hash = '';
        return target;
      }} catch {{}}
    }}
  }} catch {{}}
  return null;
}}
function checkShortCircuit(target) {{
  if (!target) return null;
  const h = String(target.hostname || '').toLowerCase().replace(/\\.$/, '');
  const p = target.pathname || '';
  if (
    h === 'graph.instagram.com' && p.includes('logging_client_events') ||
    p.startsWith('/ajax/bz') ||
    p.startsWith('/ajax/qm') ||
    (h === 'www.facebook.com' && p.startsWith('/ig_xsite_user_info'))
  ) {{
    return new Response('{{}}', {{
      status: 200,
      headers: {{'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*'}},
    }});
  }}
  return null;
}}
async function fetchViaHttpRelay(targetHref, method, fwdHeaders) {{
  const hdrList = Object.entries(fwdHeaders || {{}}).map(([k, v]) => [String(k), String(v)]);
  const metaBytes = new TextEncoder().encode(JSON.stringify({{
    url: targetHref,
    method,
    headers: hdrList,
  }}));
  const frameBuf = new Uint8Array(4 + metaBytes.byteLength);
  new DataView(frameBuf.buffer).setUint32(0, metaBytes.byteLength, false);
  frameBuf.set(metaBytes, 4);
  const resp = await fetch('/instagram/runtime/http-relay', {{
    method: 'POST',
    credentials: 'omit',
    cache: 'no-store',
    headers: {{'content-type': 'application/octet-stream'}},
    body: frameBuf,
  }});
  if (!resp.ok) throw new Error('HTTP relay status ' + resp.status);
  const rawBuf = await resp.arrayBuffer();
  if (rawBuf.byteLength < 4) throw new Error('HTTP relay short buffer');
  const metaLen = new DataView(rawBuf).getUint32(0, false);
  const metaJson = new TextDecoder().decode(new Uint8Array(rawBuf, 4, metaLen));
  const meta = JSON.parse(metaJson);
  const payloadBytes = new Uint8Array(rawBuf, 4 + metaLen);
  const statusNum = Number(meta.status || 200);
  const hObj = new Headers({{'Access-Control-Allow-Origin': '*', 'Cross-Origin-Resource-Policy': 'cross-origin'}});
  if (Array.isArray(meta.headers)) {{
    for (const [k, v] of meta.headers) {{
      if (k && k.toLowerCase() !== 'set-cookie') {{
        try {{ hObj.append(k, v); }} catch {{}}
      }}
    }}
  }}
  const bodyOut = (method === 'HEAD' || [101, 204, 205, 304].includes(statusNum))
    ? null
    : payloadBytes.buffer.slice(payloadBytes.byteOffset, payloadBytes.byteOffset + payloadBytes.byteLength);
  return new Response(bodyOut, {{
    status: statusNum,
    statusText: String(meta.statusText || 'OK'),
    headers: hObj,
  }});
}}
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', event => {{
  try {{
    const reqUrlObj = new URL(event.request.url);
    if ((reqUrlObj.pathname.startsWith('/instagram/runtime/') && reqUrlObj.pathname !== '/instagram/runtime/media-relay') || reqUrlObj.pathname.startsWith('/_diag/')) {{
      return;
    }}
    const sjIdx = reqUrlObj.pathname.indexOf('/instagram/~/sj/');
    if (sjIdx >= 0) {{
      const rawTail = reqUrlObj.pathname.slice(sjIdx + 16);
      const afterSj = decodeRepeated(rawTail);
      const blobM = afterSj.match(/(?:^|\\/)(blob:https?:\\/\\/[^?#]+)/i);
      if (blobM && blobM[1]) {{
        let blobTarget = blobM[1];
        try {{
          const inner = new URL(blobTarget.slice(5));
          blobTarget = 'blob:' + self.location.origin + inner.pathname;
        }} catch {{}}
        event.respondWith(fetch(blobTarget).catch(() => new Response(null, {{status: 204}})));
        return;
      }}
      const dataM = rawTail.match(/(?:^|\\/)(data:.*)$/i) || afterSj.match(/(?:^|\\/)(data:.*)$/i);
      if (dataM && dataM[1]) {{
        let dataUrl = dataM[1];
        if (dataUrl.startsWith('data%3A') || dataUrl.startsWith('data%3a')) {{
          try {{ dataUrl = decodeURIComponent(dataUrl); }} catch {{}}
        }}
        event.respondWith(fetch(dataUrl).catch(() => new Response(null, {{status: 204}})));
        return;
      }}
    }}
    const anyTarget = extractAnyVirtualUrl(event.request.url);
    if (anyTarget) {{
      const sc = checkShortCircuit(anyTarget);
      if (sc) {{
        event.respondWith(sc);
        return;
      }}
    }}
    if (event.request.method === 'GET' || event.request.method === 'HEAD') {{
      const isAlreadyRelay = reqUrlObj.pathname === '/instagram/runtime/media-relay' && reqUrlObj.searchParams.has('k');
      const asOut = (r, m) => (isAlreadyRelay && m === 'GET' && ![101, 204, 205, 304].includes(r.status))
        ? r
        : new Response((m === 'HEAD' || [101, 204, 205, 304].includes(r.status)) ? null : r.body, {{
            status: r.status,
            statusText: r.statusText,
            headers: r.headers,
          }});
      if (anyTarget && isDirectMediaTarget(anyTarget) && event.request.destination !== 'script' && event.request.destination !== 'worker' && event.request.destination !== 'sharedworker' && event.request.destination !== 'style' && !event.request.url.includes('%24module=module') && !event.request.url.includes('$module=module')) {{
        const relay = new URL('/instagram/runtime/media-relay', self.location.origin);
        relay.searchParams.set('k', encodeMediaKey(anyTarget.href));
        if (reqUrlObj.searchParams.get('native') === '1') relay.searchParams.set('native', '1');
        const method = event.request.method;
        const rng = event.request.headers.get('range');
        const acc = event.request.headers.get('accept');
        const canCache = method === 'GET' && !rng && isCacheableImageTarget(anyTarget);
        const fwdHeaders = {{}};
        if (rng) fwdHeaders['Range'] = rng;
        if (acc) fwdHeaders['Accept'] = acc;
        event.respondWith((async () => {{
          let cache = null;
          if (canCache && typeof caches !== 'undefined') {{
            try {{
              cache = await caches.open(IG_MEDIA_CACHE);
              const hit = await cache.match(relay.href);
              if (hit) return asOut(hit, method);
            }} catch {{}}
          }}
          try {{
            const resp = await fetch(relay.href, {{
              method: canCache ? 'GET' : method,
              headers: fwdHeaders,
              credentials: 'omit',
            }});
            if (resp && resp.status < 400) {{
              if (cache && resp.status === 200) {{
                try {{ await cache.put(relay.href, resp.clone()); }} catch {{}}
              }}
              return asOut(resp, method);
            }}
          }} catch {{}}
          const fallbackResp = await fetchViaHttpRelay(anyTarget.href, canCache ? 'GET' : method, fwdHeaders);
          if (cache && fallbackResp && fallbackResp.status === 200) {{
            try {{ await cache.put(relay.href, fallbackResp.clone()); }} catch {{}}
          }}
          return asOut(fallbackResp, method);
        }})());
        return;
      }}
    }}
    if (typeof $scramjetController !== 'undefined' && $scramjetController.shouldRoute(event)) {{
      event.respondWith($scramjetController.route(event));
      return;
    }}
  }} catch (error) {{
    console.error('[SchoolHub isolated SW:instagram]', error);
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
            "Service-Worker-Allowed": "/instagram/",
        },
    )


# ---------------------------------------------------------------------------
# Fast HTTP/2 + Chrome-TLS Relay & Media Relay for Instagram
# ---------------------------------------------------------------------------
_CURL_SESSION: StatelessCurlSession | None = None
_CURL_SESSION_LOCK = asyncio.Lock()
_MEDIA_HTTP_CLIENT: httpx.AsyncClient | None = None
_MEDIA_HTTP_CLIENT_LOCK = asyncio.Lock()
_IMAGE_CACHE: dict[str, tuple[float, int, str, bytes, str]] = {}
_IMAGE_CACHE_TTL = 3600.0
_IMAGE_CACHE_MAX_ITEMS = 512
_IMAGE_CACHE_MAX_BYTES = 2 * 1024 * 1024
_STATIC_ASSET_CACHE: dict[str, tuple[float, int, str, list[list[str]], bytes]] = {}
_STATIC_ASSET_TTL = 3600.0 * 6
_STATIC_ASSET_MAX_ITEMS = 2048
_STATIC_ASSET_MAX_BYTES = 16 * 1024 * 1024

_RELAY_STRIP_REQ_HEADERS = {
    "host",
    "connection",
    "keep-alive",
    "proxy-connection",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "accept-encoding",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
    "sec-ch-ua-arch",
    "sec-ch-ua-full-version",
    "sec-ch-ua-full-version-list",
    "sec-ch-ua-model",
    "sec-ch-ua-platform-version",
    "sec-ch-ua-bitness",
    "sec-ch-ua-wow64",
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
    "document-policy",
    "permissions-policy",
    "cross-origin-opener-policy",
    "cross-origin-embedder-policy",
    "cross-origin-resource-policy",
    "reporting-endpoints",
    "report-to",
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
    """Shared cookieless persistent IPv4 client for Instagram images and Reel videos."""
    global _MEDIA_HTTP_CLIENT
    if _MEDIA_HTTP_CLIENT is not None and not _MEDIA_HTTP_CLIENT.is_closed:
        return _MEDIA_HTTP_CLIENT
    async with _MEDIA_HTTP_CLIENT_LOCK:
        if _MEDIA_HTTP_CLIENT is None or _MEDIA_HTTP_CLIENT.is_closed:
            limits = httpx.Limits(max_connections=120, max_keepalive_connections=60)
            timeout = httpx.Timeout(connect=7.0, read=45.0, write=10.0, pool=10.0)
            if not (os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")):
                transport = httpx.AsyncHTTPTransport(
                    local_address="0.0.0.0",
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
        "instagram.com",
        "cdninstagram.com",
        "fbcdn.net",
        "facebook.com",
        "facebook.net",
        "fbsbx.com",
        "meta.com",
        "meta.ai",
        "threads.net",
        "google.com",
        "gstatic.com",
        "googleapis.com",
        "googleusercontent.com",
        "apple.com",
        "cdn-apple.com",
    )
    if any(host == sfx or host.endswith("." + sfx) for sfx in _trusted_suffixes):
        _PUBLIC_HOST_CACHE[key] = now
        return
    if not (os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")):
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
        if (host.endswith(".cdninstagram.com") or host == "cdninstagram.com" or host.endswith(".fbcdn.net")) and (
            path.startswith("/rsrc.php/")
            or path.endswith((".js", ".css", ".woff2", ".woff", ".ttf", ".svg", ".png", ".jpg", ".webp"))
        ):
            return True
    except Exception:
        pass
    return False


def _pack_relay_response(
    status: int, status_text: str, headers_list: list[list[str]], body: bytes
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
    return Response(
        payload,
        media_type="application/octet-stream",
        headers={
            "Cache-Control": "private, no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "Vary": "*",
        },
    )


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


def _strip_dollar_query(raw_url: str) -> str:
    if "?" not in raw_url:
        return raw_url
    try:
        p = urlparse(raw_url)
        if not p.query:
            return raw_url
        clean_parts = [
            part for part in p.query.split("&")
            if part and not unquote(part.split("=", 1)[0]).startswith("$")
        ]
        return urlunparse((p.scheme, p.netloc, p.path, p.params, "&".join(clean_parts), ""))
    except Exception:
        return raw_url


async def _serve_ig_media_url(raw_url: str, request: Request) -> Response:
    if not raw_url:
        return Response(b"missing url", status_code=400)
    try:
        for _ in range(3):
            p_check = urlparse(raw_url)
            if p_check.path == "/instagram/runtime/media-relay":
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
        if parsed.scheme not in ("http", "https") or not any(
            host == s or host.endswith("." + s) for s in IG_MEDIA_SUFFIXES
        ):
            return Response(b"disallowed media host", status_code=403)
    except Exception:
        return Response(b"bad media url", status_code=400)

    path_lower = (parsed.path or "").lower()
    is_video = "/o1/v/" in path_lower or path_lower.endswith((".mp4", ".m4v", ".m4s", ".m3u8"))
    has_byte_params = "bytestart=" in (parsed.query or "") or "byteend=" in (parsed.query or "")
    client_range = request.headers.get("range")
    is_cacheable_img = (not is_video) and (not has_byte_params) and (not client_range)

    now = time.time()
    if is_cacheable_img and request.method in ("GET", "HEAD"):
        cached = _IMAGE_CACHE.get(raw_url)
        if cached and (now - cached[0]) < _IMAGE_CACHE_TTL:
            _, c_status, c_media_type, c_body, c_cc = cached
            body_out = b"" if request.method == "HEAD" else c_body
            return Response(
                body_out,
                status_code=c_status,
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Cross-Origin-Resource-Policy": "cross-origin",
                    "Cache-Control": c_cc,
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(len(body_out)),
                },
                media_type=c_media_type,
            )

    req_headers = {
        "User-Agent": _DESKTOP_CHROME_UA,
        "Accept": request.headers.get("accept") or "*/*",
        "Accept-Encoding": "identity",
        "Referer": "https://www.instagram.com/",
        "Origin": "https://www.instagram.com",
    }
    if client_range:
        req_headers["Range"] = client_range

    client = await _get_media_http_client()
    try:
        req = client.build_request(request.method, raw_url, headers=req_headers)
        resp = await client.send(req, stream=True)
        out_headers = {
            "Access-Control-Allow-Origin": "*",
            "Cross-Origin-Resource-Policy": "cross-origin",
            "Cache-Control": resp.headers.get("cache-control") or "public, max-age=86400",
            "Accept-Ranges": resp.headers.get("accept-ranges") or "bytes",
        }
        for h_name in ("content-type", "content-length", "content-range", "etag", "last-modified"):
            val = resp.headers.get(h_name)
            if val:
                out_headers[h_name.title()] = val

        media_type = resp.headers.get("content-type") or "application/octet-stream"

        if request.method == "HEAD":
            await resp.aclose()
            return Response(b"", status_code=resp.status_code, headers=out_headers, media_type=media_type)

        cl_val = resp.headers.get("content-length") or ""
        if is_cacheable_img or has_byte_params or resp.headers.get("content-encoding") or (cl_val.isdigit() and int(cl_val) <= 2 * 1024 * 1024):
            body_bytes = await resp.aread()
            await resp.aclose()
            status_code = resp.status_code
            if (
                is_cacheable_img
                and request.method == "GET"
                and status_code == 200
                and 0 < len(body_bytes) <= _IMAGE_CACHE_MAX_BYTES
            ):
                if len(_IMAGE_CACHE) >= _IMAGE_CACHE_MAX_ITEMS:
                    oldest_k = next(iter(_IMAGE_CACHE))
                    _IMAGE_CACHE.pop(oldest_k, None)
                _IMAGE_CACHE[raw_url] = (
                    now,
                    status_code,
                    media_type,
                    body_bytes,
                    out_headers["Cache-Control"],
                )
            out_headers["Content-Length"] = str(len(body_bytes))
            return Response(body_bytes, status_code=status_code, headers=out_headers, media_type=media_type)

        async def _stream_media(upstream_resp=resp):
            try:
                async for chunk in upstream_resp.aiter_bytes(64 * 1024):
                    if chunk:
                        yield chunk
            except Exception:
                pass
            finally:
                try:
                    await upstream_resp.aclose()
                except Exception:
                    pass

        return StreamingResponse(
            _stream_media(),
            status_code=resp.status_code,
            headers=out_headers,
            media_type=media_type,
        )
    except Exception as exc:
        return Response(f"media error: {exc}".encode("utf-8", "ignore"), status_code=502)


@router.api_route(
    "/instagram/runtime/media-relay",
    methods=["GET", "HEAD"],
    include_in_schema=False,
)
async def ig_media_relay(request: Request):
    k_val = (request.query_params.get("k") or "").strip()
    raw_url = _decode_media_key(k_val) if k_val else (request.query_params.get("url") or "").strip()
    return await _serve_ig_media_url(raw_url, request)


def _decode_repeated_str(val: str) -> str:
    out = val or ""
    for _ in range(5):
        nxt = unquote(out)
        if nxt == out:
            break
        out = nxt
    return out


def _extract_sj_virtual_url(raw_rest: str, query_str: str) -> str:
    decoded_rest = _decode_repeated_str(raw_rest)
    m = re.search(r"(https?://.*)$", decoded_rest, re.I)
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
    "/instagram/~/sj/{rest:path}",
    methods=["GET", "HEAD", "POST", "OPTIONS"],
    include_in_schema=False,
)
async def ig_sj_direct_fallback(rest: str, request: Request):
    """Server-side fallback when iOS/iPadOS WebKit AVFoundation or early resource
    fetches bypass the Service Worker and request /instagram/~/sj/... directly."""
    if request.method == "OPTIONS":
        return Response(
            b"",
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": "*",
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
            raw_bytes = base64.b64decode(m_data.group(3))
            return Response(
                b"" if request.method == "HEAD" else raw_bytes,
                media_type=m_data.group(2),
                headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=86400"},
            )
        except Exception:
            pass
    m_raw_data = re.search(r"data:([A-Za-z0-9.+/-]+)?(?:;charset=[^,;]+)?,(.*)$", rest, re.S)
    if m_raw_data:
        try:
            mime = (m_raw_data.group(1) or "text/plain").strip()
            payload_bytes = unquote(m_raw_data.group(2)).encode("utf-8")
            return Response(
                b"" if request.method == "HEAD" else payload_bytes,
                media_type=mime,
                headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=86400"},
            )
        except Exception:
            pass
    virtual_url = _extract_sj_virtual_url(rest, request.url.query)
    if not virtual_url:
        return Response(b"invalid virtual url", status_code=404)
    try:
        parsed = urlparse(virtual_url)
        if parsed.path == "/instagram/runtime/media-relay":
            qs = parse_qs(parsed.query)
            k_nested = (qs.get("k") or [""])[0]
            nested = _decode_media_key(k_nested) if k_nested else (qs.get("url") or [""])[0]
            if nested:
                return await _serve_ig_media_url(nested, request)
        host = (parsed.hostname or "").lower().rstrip(".")
        path = parsed.path or ""
        if (
            (host == "graph.instagram.com" and "logging_client_events" in path)
            or path.startswith(("/ajax/bz", "/ajax/qm"))
            or (host == "www.facebook.com" and path.startswith("/ig_xsite_user_info"))
        ):
            return Response(
                b"{}",
                status_code=200,
                media_type="application/json",
                headers={"Access-Control-Allow-Origin": "*"},
            )
        if any(host == s or host.endswith("." + s) for s in IG_MEDIA_SUFFIXES):
            return await _serve_ig_media_url(virtual_url, request)
    except Exception:
        return Response(b"bad virtual url", status_code=400)
    if request.method == "POST":
        return Response(
            b"{}",
            status_code=200,
            media_type="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )
    return Response(b"Service Worker route required", status_code=404)


def _normalize_ig_set_cookie(cookie_val: str, host: str) -> str:
    """Ensure cookies set by instagram.com / www.instagram.com / i.instagram.com
    share Domain=.instagram.com in Scramjet's CookieJar."""
    if not cookie_val or not (host == "instagram.com" or host.endswith(".instagram.com")):
        return cookie_val
    first_semi = cookie_val.find(";")
    name_val = cookie_val if first_semi < 0 else cookie_val[:first_semi]
    cookie_name = name_val.split("=", 1)[0].strip()
    if cookie_name.startswith("__Host-"):
        return cookie_val
    if re.search(r";\s*domain\s*=", cookie_val, re.I):
        return re.sub(
            r"(;\s*domain\s*=\s*)(?:www\.)?instagram\.com\b",
            r"\1.instagram.com",
            cookie_val,
            flags=re.I,
        )
    return cookie_val.rstrip("; ") + "; Domain=.instagram.com"


def _select_curl_impersonate(ua: str) -> str:
    return "chrome131"


def _inline_ig_data_scripts(html_bytes: bytes) -> bytes:
    """Convert <script src="data:application/x-javascript;base64,..."></script> and
    <script src="data:text/javascript;base64,..."></script> into inline <script>...</script>
    so Scramjet's HTML rewriter rewrites them directly in-place without extra SW roundtrips
    or data-URL origin check failures. Also disable lox_pow_challenge so password reset
    search never freezes the UI for 8 seconds solving a 500k-iteration BigInt puzzle."""
    if b'"lox_pow_challenge":{' in html_bytes:
        html_bytes = re.sub(
            rb'("lox_pow_challenge":\{[^{}]*"iterations":)\d+',
            rb"\g<1>1",
            html_bytes,
        )
    if b"data:" not in html_bytes:
        return html_bytes
    try:
        text = html_bytes.decode("utf-8")
    except Exception:
        return html_bytes

    def _repl(m: re.Match) -> str:
        full_tag = m.group(0)
        b64_part = m.group(1) or m.group(2) or ""
        try:
            raw_b64 = unquote(b64_part).strip()
            pad = (-len(raw_b64)) % 4
            if pad:
                raw_b64 += "=" * pad
            js_code = base64.b64decode(raw_b64).decode("utf-8", "replace")
            open_end = full_tag.find(">")
            if open_end < 0:
                return full_tag
            open_tag = full_tag[: open_end + 1]
            open_tag = re.sub(
                r"""\s+src=(?:"data:[^"]+"|'data:[^']+')""",
                "",
                open_tag,
                flags=re.I,
            )
            return f"{open_tag}{js_code}</script>"
        except Exception:
            return full_tag

    patched = re.sub(
        r"""<script\b[^>]*\bsrc=(?:"data:(?:text|application)/(?:x-)?javascript[^,]*;base64,([^"]+)"|'data:(?:text|application)/(?:x-)?javascript[^,]*;base64,([^']+)')[^>]*>\s*</script>""",
        lambda m: _repl(m),
        text,
        flags=re.I,
    )
    return patched.encode("utf-8")


async def _handle_ig_account_recovery_search(
    req_body: bytes,
    header_dict: dict[str, str],
    cookie_str: str,
    client_ua: str,
    impersonate_profile: str,
) -> Response | None:
    """Bridge CAAIGAccountSearchViewQuery on /accounts/password/reset/ to
    /api/v1/web/accounts/account_recovery_send_ajax/ so shared IPs never receive
    'エラーが発生しました。しばらくしてからもう一度お試しください。'."""
    try:
        body_text = req_body.decode("utf-8", "ignore")
        form = parse_qs(body_text, keep_blank_values=True)
        vars_raw = (form.get("variables") or [""])[0]
        if not vars_raw:
            return None
        vars_obj = json.loads(vars_raw)
        if not isinstance(vars_obj, dict):
            return None
        params = vars_obj.get("params") if isinstance(vars_obj.get("params"), dict) else vars_obj
        search_query = str(params.get("search_query") or "").strip()
        if not search_query:
            return None

        csrf_token = ""
        m_csrf = re.search(r"(?:^|[;\s])csrftoken=([^;\s]+)", cookie_str)
        if m_csrf:
            csrf_token = m_csrf.group(1).strip()
        if not csrf_token:
            for hk, hv in header_dict.items():
                if hk.lower() == "x-csrftoken" and hv.strip():
                    csrf_token = hv.strip()
                    break
        if not csrf_token:
            csrf_token = "igcsrf" + str(int(time.time()))
        v1_cookie = cookie_str
        if "csrftoken=" not in v1_cookie:
            v1_cookie = f"{v1_cookie}; csrftoken={csrf_token}" if v1_cookie else f"csrftoken={csrf_token}"

        v1_headers: dict[str, str] = {
            "User-Agent": client_ua or _DESKTOP_CHROME_UA,
            "Accept": "*/*",
            "Accept-Language": header_dict.get("Accept-Language") or "ja,en-US;q=0.9,en;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded",
            "X-CSRFToken": csrf_token,
            "X-IG-App-ID": "936619743392459",
            "X-Instagram-AJAX": "1051002150",
            "X-ASBD-ID": "359341",
            "X-IG-WWW-Claim": "0",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://www.instagram.com",
            "Referer": "https://www.instagram.com/accounts/password/reset/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Cookie": v1_cookie,
        }
        v1_body = urlencode({"email_or_username": search_query}).encode("utf-8")
        v1_url = "https://www.instagram.com/api/v1/web/accounts/account_recovery_send_ajax/"
        ephemeral: StatelessCurlSession | None = None
        try:
            if StatelessCurlSession is not None:
                ephemeral = StatelessCurlSession(
                    impersonate=impersonate_profile,
                    default_headers=False,
                    allow_redirects=False,
                    timeout=25,
                    max_clients=2,
                )
                upstream = await ephemeral.request(
                    method="POST",
                    url=v1_url,
                    headers=v1_headers,
                    data=v1_body,
                    allow_redirects=False,
                    timeout=20,
                )
                v1_status = int(upstream.status_code)
                v1_resp_bytes = bytes(upstream.content or b"")
                raw_multi = list(upstream.headers.multi_items())
            else:
                client = await _get_media_http_client()
                upstream = await client.request(
                    method="POST",
                    url=v1_url,
                    headers=v1_headers,
                    content=v1_body,
                    follow_redirects=False,
                )
                v1_status = int(upstream.status_code)
                v1_resp_bytes = bytes(upstream.content or b"")
                raw_multi = list(upstream.headers.multi_items())
        finally:
            if ephemeral is not None:
                try:
                    await ephemeral.close()
                except Exception:
                    pass

        v1_json = {}
        try:
            parsed_j = json.loads(v1_resp_bytes.decode("utf-8", "ignore"))
            if isinstance(parsed_j, dict):
                v1_json = parsed_j
        except Exception:
            pass

        print(
            f"[IG-RECOVERY] v1_status={v1_status} status={v1_json.get('status')} "
            f"title={v1_json.get('title')} msg={str(v1_json.get('body') or v1_json.get('message') or '')[:80]}",
            flush=True,
        )

        out_headers: list[list[str]] = [
            ["content-type", "application/json; charset=utf-8"],
            ["access-control-allow-origin", "https://www.instagram.com"],
            ["access-control-allow-credentials", "true"],
        ]
        for k, v in raw_multi:
            if k.lower() == "set-cookie":
                out_headers.append(["set-cookie", _normalize_ig_set_cookie(v, "www.instagram.com")])

        search_type = "EMAIL" if "@" in search_query else "USERNAME"
        msg_raw = str(v1_json.get("body") or v1_json.get("message") or "").strip()
        title_raw = str(v1_json.get("title") or "ログインリンクを送信しました").strip()

        if v1_json.get("status") == "ok" or v1_status == 429 or not msg_raw or "エラーが発生しました" in msg_raw:
            final_msg = (
                msg_raw
                if (msg_raw and "エラーが発生しました" not in msg_raw)
                else "このアカウントが存在する場合、ログイン用のリンクが送信されます。メールまたはSMSをご確認ください。"
            )
            ar_payload = {
                "search_type": search_type,
                "feta_account_returned": False,
                "eligible_for_ar_code": False,
                "cipher": None,
                "redirect_uri": None,
                "recaptcha_required": False,
                "contact_points": [],
                "profiles": [],
                "notification_info": {
                    "title": title_raw,
                    "message": final_msg,
                    "secondary_message": None,
                },
                "error_content": None,
                "persist_data": None,
                "account_type_shown": None,
                "allow_display": False,
                "cuid": None,
            }
        else:
            ar_payload = {
                "search_type": search_type,
                "feta_account_returned": False,
                "eligible_for_ar_code": False,
                "cipher": None,
                "redirect_uri": None,
                "recaptcha_required": False,
                "contact_points": [],
                "profiles": [],
                "notification_info": None,
                "error_content": {
                    "description": msg_raw,
                    "description_localized_text": msg_raw,
                    "style": "PAGE_LEVEL_BANNER",
                },
                "persist_data": None,
                "account_type_shown": None,
                "allow_display": False,
                "cuid": None,
            }

        gql_body = json.dumps(
            {
                "data": {"caa_ar_ig_account_search": ar_payload},
                "extensions": {"is_final": True},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        return _pack_relay_response(200, "OK", out_headers, gql_body)
    except Exception as exc:
        print(f"[IG-RECOVERY] bridge exception: {exc}", flush=True)
        return None


async def _handle_ig_caa_login(
    req_body: bytes,
    header_dict: dict[str, str],
    cookie_str: str,
    client_ua: str,
    impersonate_profile: str,
) -> Response | None:
    """Bridge Instagram's CAA GraphQL login mutation (useCDSWebLoginMutation) to
    /api/v1/web/accounts/login/ajax/ (with #PWD_INSTAGRAM_BROWSER tag conversion) AND
    also try native /api/graphql when v1 does not authenticate, while suppressing
    GraphQL's 1675004 Rate Limit error ('エラーが発生しました。もう一度お試しください。')."""
    try:
        body_text = req_body.decode("utf-8", "ignore")
        form = parse_qs(body_text, keep_blank_values=True)
        vars_raw = (form.get("variables") or [""])[0]
        if not vars_raw:
            return None
        vars_obj = json.loads(vars_raw)
        inp = vars_obj.get("input") if isinstance(vars_obj, dict) else None
        if not isinstance(inp, dict):
            return None
        identifier = str(inp.get("identifier") or "").strip()
        enc_pw_field = inp.get("enc_password")
        if isinstance(enc_pw_field, dict):
            enc_password = str(enc_pw_field.get("sensitive_string_value") or "").strip()
        else:
            enc_password = str(enc_pw_field or "").strip()
        if not identifier or not enc_password:
            return None

        # Convert CAA's #PWD_BROWSER:10:<ts>:<b64> prefix to #PWD_INSTAGRAM_BROWSER:10:<ts>:<b64>
        # which is what PolarisEncryptionUtils produces for /api/v1/web/accounts/login/ajax/
        v1_enc_password = enc_password
        if v1_enc_password.startswith("#PWD_BROWSER:"):
            v1_enc_password = "#PWD_INSTAGRAM_BROWSER:" + v1_enc_password.split(":", 1)[1]

        query_params_raw = inp.get("query_params")
        if not isinstance(query_params_raw, str) or not query_params_raw.strip():
            query_params_raw = "{}"
        next_uri = "/"
        try:
            qp_obj = json.loads(query_params_raw)
            if isinstance(qp_obj, dict) and qp_obj.get("next"):
                cand = str(qp_obj["next"]).strip()
                if cand.startswith("/"):
                    next_uri = cand
        except Exception:
            pass

        td_raw = inp.get("trusted_device_records")
        if not isinstance(td_raw, str) or not td_raw.strip():
            td_raw = "{}"

        stop_del = inp.get("stop_deletion_token")

        csrf_token = ""
        m_csrf = re.search(r"(?:^|[;\s])csrftoken=([^;\s]+)", cookie_str)
        if m_csrf:
            csrf_token = m_csrf.group(1).strip()
        if not csrf_token:
            for hk, hv in header_dict.items():
                if hk.lower() == "x-csrftoken" and hv.strip():
                    csrf_token = hv.strip()
                    break
        if not csrf_token:
            csrf_token = "igcsrf" + str(int(time.time()))
        v1_cookie = cookie_str
        if "csrftoken=" not in v1_cookie:
            v1_cookie = f"{v1_cookie}; csrftoken={csrf_token}" if v1_cookie else f"csrftoken={csrf_token}"

        m_did = re.search(r"(?:^|[;\s])ig_did=([^;\s]+)", v1_cookie)
        ig_did = m_did.group(1).strip() if m_did else str(inp.get("ig_web_device_id") or "").strip()
        m_mid = re.search(r"(?:^|[;\s])mid=([^;\s]+)", v1_cookie)
        ig_mid = m_mid.group(1).strip() if m_mid else ""

        v1_params: dict[str, str] = {
            "enc_password": v1_enc_password,
            "optIntoOneTap": "false",
            "queryParams": query_params_raw,
            "trustedDeviceRecords": td_raw,
            "username": identifier,
        }
        if stop_del:
            v1_params["stopDeletionNonce"] = str(stop_del)

        v1_body = urlencode(v1_params).encode("utf-8")
        v1_headers: dict[str, str] = {
            "User-Agent": client_ua or _DESKTOP_CHROME_UA,
            "Accept": "*/*",
            "Accept-Language": header_dict.get("Accept-Language") or "ja,en-US;q=0.9,en;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded",
            "X-CSRFToken": csrf_token,
            "X-IG-App-ID": "936619743392459",
            "X-Instagram-AJAX": "1051002150",
            "X-ASBD-ID": "359341",
            "X-IG-WWW-Claim": "0",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://www.instagram.com",
            "Referer": "https://www.instagram.com/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Cookie": v1_cookie,
        }
        if ig_did:
            v1_headers["X-Web-Device-Id"] = ig_did
        if ig_mid:
            v1_headers["X-Mid"] = ig_mid

        v1_url = "https://www.instagram.com/api/v1/web/accounts/login/ajax/"
        ephemeral: StatelessCurlSession | None = None
        gql_fallback_resp: Response | None = None
        try:
            if StatelessCurlSession is not None:
                ephemeral = StatelessCurlSession(
                    impersonate=impersonate_profile,
                    default_headers=False,
                    allow_redirects=False,
                    timeout=25,
                    max_clients=2,
                )
                upstream = await ephemeral.request(
                    method="POST",
                    url=v1_url,
                    headers=v1_headers,
                    data=v1_body,
                    allow_redirects=False,
                    timeout=22,
                )
                v1_status = int(upstream.status_code)
                v1_resp_bytes = bytes(upstream.content or b"")
                raw_multi = list(upstream.headers.multi_items())
                if v1_status == 403 and b"/__rd_verify_" in v1_resp_bytes:
                    m_rd = re.search(rb"""fetch\(['"](/__rd_verify_[^'"]+)['"]""", v1_resp_bytes)
                    if m_rd:
                        rd_path = m_rd.group(1).decode("utf-8", "ignore")
                        rd_resp = await ephemeral.request(
                            method="POST",
                            url="https://www.instagram.com" + rd_path,
                            headers=v1_headers,
                            allow_redirects=False,
                            timeout=12,
                        )
                        rd_cookies: list[str] = []
                        for rk, rv in rd_resp.headers.multi_items():
                            if rk.lower() == "set-cookie":
                                raw_multi.append((rk, rv))
                                c_nv = rv.split(";", 1)[0].strip()
                                if c_nv:
                                    rd_cookies.append(c_nv)
                        if rd_cookies:
                            v1_headers["Cookie"] = v1_headers["Cookie"] + "; " + "; ".join(rd_cookies)
                        upstream2 = await ephemeral.request(
                            method="POST",
                            url=v1_url,
                            headers=v1_headers,
                            data=v1_body,
                            allow_redirects=False,
                            timeout=22,
                        )
                        v1_status = int(upstream2.status_code)
                        v1_resp_bytes = bytes(upstream2.content or b"")
                        raw_multi.extend(upstream2.headers.multi_items())
            else:
                client = await _get_media_http_client()
                upstream = await client.request(
                    method="POST",
                    url=v1_url,
                    headers=v1_headers,
                    content=v1_body,
                    follow_redirects=False,
                )
                v1_status = int(upstream.status_code)
                v1_resp_bytes = bytes(upstream.content or b"")
                raw_multi = list(upstream.headers.multi_items())

            v1_json = {}
            try:
                parsed_v1 = json.loads(v1_resp_bytes.decode("utf-8", "ignore"))
                if isinstance(parsed_v1, dict):
                    v1_json = parsed_v1
            except Exception:
                pass

            print(
                f"[IG-LOGIN] v1_status={v1_status} status={v1_json.get('status')} "
                f"auth={v1_json.get('authenticated')} user={v1_json.get('user')} "
                f"2fa={v1_json.get('two_factor_required')} chk={bool(v1_json.get('checkpoint_url'))} "
                f"err_type={v1_json.get('error_type')} msg={str(v1_json.get('message') or '')[:80]}",
                flush=True,
            )

            # If v1 did not return a valid JSON response (e.g. non-200 or empty), attempt native /api/graphql
            # (which uses #PWD_BROWSER) in case the account requires CAA/FETA GraphQL login,
            # while suppressing 1675004 rate-limit errors.
            if (
                (v1_status != 200 or not v1_json)
                and not v1_json.get("authenticated")
                and not v1_json.get("two_factor_required")
                and not v1_json.get("checkpoint_url")
                and v1_json.get("message") not in ("checkpoint_required", "challenge_required")
                and ephemeral is not None
            ):
                try:
                    gql_headers = dict(header_dict)
                    gql_headers["Cookie"] = v1_headers["Cookie"]
                    gql_headers["X-CSRFToken"] = csrf_token
                    gql_headers["X-IG-App-ID"] = "936619743392459"
                    gql_headers["Origin"] = "https://www.instagram.com"
                    gql_headers["Referer"] = "https://www.instagram.com/"
                    gql_headers["Sec-Fetch-Dest"] = "empty"
                    gql_headers["Sec-Fetch-Mode"] = "cors"
                    gql_headers["Sec-Fetch-Site"] = "same-origin"
                    gql_up = await ephemeral.request(
                        method="POST",
                        url="https://www.instagram.com/api/graphql",
                        headers=gql_headers,
                        data=req_body,
                        allow_redirects=False,
                        timeout=18,
                    )
                    gql_bytes = bytes(gql_up.content or b"")
                    gql_j = json.loads(gql_bytes.decode("utf-8", "ignore"))
                    caa_w = (
                        gql_j.get("data", {}).get("caa_login_web")
                        if isinstance(gql_j, dict) and isinstance(gql_j.get("data"), dict)
                        else None
                    )
                    if isinstance(caa_w, dict) and (
                        caa_w.get("ig_authenticated") is True
                        or caa_w.get("redirect_uri")
                        or caa_w.get("two_factor_result")
                        or (
                            isinstance(caa_w.get("stop_deletion_payload"), dict)
                            and caa_w["stop_deletion_payload"].get("stop_deletion_nonce")
                        )
                    ):
                        print(
                            f"[IG-LOGIN] gql_fallback succeeded: auth={caa_w.get('ig_authenticated')} "
                            f"redir={caa_w.get('redirect_uri')}",
                            flush=True,
                        )
                        gql_out_headers: list[list[str]] = [
                            ["content-type", "application/json; charset=utf-8"],
                            ["access-control-allow-origin", "https://www.instagram.com"],
                            ["access-control-allow-credentials", "true"],
                        ]
                        for k, v in raw_multi + list(gql_up.headers.multi_items()):
                            if k.lower() == "set-cookie":
                                gql_out_headers.append(
                                    ["set-cookie", _normalize_ig_set_cookie(v, "www.instagram.com")]
                                )
                        gql_fallback_resp = _pack_relay_response(200, "OK", gql_out_headers, gql_bytes)
                except Exception as gql_exc:
                    print(f"[IG-LOGIN] gql_fallback check exception: {gql_exc}", flush=True)
        finally:
            if ephemeral is not None:
                try:
                    await ephemeral.close()
                except Exception:
                    pass

        if gql_fallback_resp is not None:
            return gql_fallback_resp

        if not v1_json:
            return None

        msg_raw = str(v1_json.get("message") or "").strip()

        out_headers: list[list[str]] = [
            ["content-type", "application/json; charset=utf-8"],
            ["access-control-allow-origin", "https://www.instagram.com"],
            ["access-control-allow-credentials", "true"],
        ]
        cookie_uid = ""
        for k, v in raw_multi:
            if k.lower() == "set-cookie":
                m_duid = re.search(r"(?:^|[;\s])ds_user_id=([^;\s]+)", v)
                if m_duid and m_duid.group(1).strip():
                    cookie_uid = m_duid.group(1).strip()
                out_headers.append(["set-cookie", _normalize_ig_set_cookie(v, "www.instagram.com")])

        caa_payload: dict[str, object] = {
            "error_code": None,
            "error_message": None,
            "error_style": None,
            "redirect_uri": None,
            "two_factor_result": None,
            "reg_nta_context": None,
            "is_3pl": False,
            "is_xapp_login": False,
            "recaptcha_needed": False,
            "is_ig_login_recaptcha": False,
            "should_show_profile_selector": False,
            "should_show_save_password_dialog": False,
            "captcha_persist_data": None,
            "crypted_uid": None,
            "ig_nonce_refresh": None,
            "ig_trusted_device_nonce_refresh": None,
            "stop_deletion_payload": None,
            "ig_authenticated": False,
            "ig_login_upsell_ar": None,
            "should_show_google_oauth_after_failure": False,
            "google_oauth_uri": None,
            "lara_recovery_redirect_uri": None,
            "lara_recovery_captcha_persist_data": None,
            "lara_recovery_captcha_cipher": None,
            "lara_recovery_method": None,
            "lara_recovery_cuid": None,
        }

        if v1_json.get("authenticated") is True:
            uid = str(v1_json.get("userId") or cookie_uid or "1")
            nonce = str(v1_json.get("trustedDeviceNonce") or "")
            caa_payload["ig_authenticated"] = True
            caa_payload["redirect_uri"] = next_uri
            caa_payload["crypted_uid"] = uid
            if uid and nonce:
                caa_payload["ig_trusted_device_nonce_refresh"] = {"uid": uid, "nonce": nonce}
        elif v1_json.get("two_factor_required") is True:
            tf_info = v1_json.get("two_factor_info") if isinstance(v1_json.get("two_factor_info"), dict) else {}
            enc_ctx = str(tf_info.get("encrypted_context") or "").strip()
            caa_payload["two_factor_result"] = json.dumps(v1_json, ensure_ascii=False)
            if enc_ctx:
                caa_payload["redirect_uri"] = (
                    f"/accounts/login/two_step_verification/?encrypted_context={quote(enc_ctx, safe='')}"
                    f"&flow=two_factor_login&next={quote(next_uri, safe='')}"
                )
            else:
                caa_payload["redirect_uri"] = f"/accounts/login/two_factor?next={quote(next_uri, safe='')}"
        elif v1_json.get("message") in ("checkpoint_required", "challenge_required") or v1_json.get("checkpoint_url"):
            caa_payload["redirect_uri"] = str(v1_json.get("checkpoint_url") or "/challenge/")
        else:
            msg = msg_raw
            if not msg or msg in ("", "fail") or "エラーが発生しました" in msg or "リクエストにエラー" in msg:
                if v1_json.get("user") is False:
                    msg = "入力されたユーザーネームはアカウントにリンクされていません。ユーザーネームをご確認の上、もう一度お試しください。"
                else:
                    msg = "入力されたパスワードが正しくありません。ご確認の上、もう一度お試しください。"
            caa_payload["error_code"] = 1348092
            caa_payload["error_style"] = "GENERIC_BANNER"
            caa_payload["error_message"] = {
                "text": msg,
                "inline_style_ranges": [],
                "image_ranges": [],
                "ranges": [],
                "color_ranges": [],
            }

        gql_body = json.dumps(
            {
                "data": {"caa_login_web": caa_payload},
                "extensions": {"is_final": True},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        return _pack_relay_response(200, "OK", out_headers, gql_body)
    except Exception as exc:
        print(f"[IG-LOGIN] bridge exception: {exc}", flush=True)
        return None


@router.post("/instagram/runtime/http-relay", include_in_schema=False)
async def ig_http_relay(request: Request):
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

    try:
        for _ in range(3):
            p_check = urlparse(url)
            if p_check.path == "/instagram/runtime/media-relay":
                qs = parse_qs(p_check.query)
                k_nested = (qs.get("k") or [""])[0]
                nested = _decode_media_key(k_nested) if k_nested else (qs.get("url") or [""])[0]
                if nested:
                    url = nested
                    continue
            break
        url = _strip_dollar_query(url)
        parsed = urlparse(url)
        if (
            parsed.hostname
            and parsed.hostname.lower().rstrip(".") in ("www.instagram.com", "instagram.com")
            and parsed.path.rstrip("/") == "/accounts/login"
            and parsed.query
        ):
            qs_login = parse_qs(parsed.query, keep_blank_values=True)
            if "mtn" in qs_login:
                qs_login.pop("mtn", None)
                new_q = urlencode(qs_login, doseq=True)
                url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", new_q, ""))
                parsed = urlparse(url)
    except Exception:
        return Response(b"bad url", status_code=400)

    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return Response(b"unsupported scheme", status_code=400)

    host = parsed.hostname.lower().rstrip(".")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port not in (80, 443):
        return Response(b"unsupported port", status_code=403)

    try:
        await _ensure_public_host(host, port)
    except Exception:
        return Response(b"blocked host", status_code=403)

    req_path = parsed.path or "/"
    if (
        (host == "graph.instagram.com" and "logging_client_events" in req_path)
        or req_path.startswith(("/ajax/bz", "/ajax/qm"))
        or (host == "www.facebook.com" and req_path.startswith("/ig_xsite_user_info"))
    ):
        return _pack_relay_response(
            200,
            "OK",
            [
                ["content-type", "application/json; charset=utf-8"],
                ["access-control-allow-origin", "https://www.instagram.com"],
                ["access-control-allow-credentials", "true"],
            ],
            b"{}",
        )

    req_headers: list[tuple[str, str]] = []
    seen_ua = False
    seen_al = False
    client_ua = _DESKTOP_CHROME_UA
    if isinstance(raw_headers, list):
        items = raw_headers
    elif isinstance(raw_headers, dict):
        items = list(raw_headers.items())
    else:
        items = []

    for item in items:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        k = str(item[0]).strip()
        v = str(item[1]).strip()
        kl = k.lower()
        if not k or kl in _RELAY_STRIP_REQ_HEADERS or kl.startswith(":"):
            continue
        if kl == "user-agent":
            seen_ua = True
            v = _DESKTOP_CHROME_UA
            client_ua = v
        elif kl == "sec-ch-ua-mobile":
            v = "?0"
        elif kl == "sec-ch-ua-platform":
            v = '"Windows"'
        elif kl == "accept-language":
            seen_al = True
        req_headers.append((k, v))

    if not seen_ua:
        req_headers.append(("User-Agent", _DESKTOP_CHROME_UA))
    if not seen_al:
        req_headers.append(("Accept-Language", "ja,en-US;q=0.9,en;q=0.8"))

    header_dict: dict[str, str] = {}
    cookie_parts: list[str] = []
    for k, v in req_headers:
        if k.lower() == "cookie":
            cookie_parts.append(v)
        else:
            header_dict[k] = v
    cookie_str = "; ".join(cookie_parts) if cookie_parts else ""
    if cookie_str:
        header_dict["Cookie"] = cookie_str

    impersonate_profile = _select_curl_impersonate(client_ua)

    # Inject standard Sec-Fetch-* headers when missing so Instagram's SSR and GraphQL endpoints
    # treat requests identically to direct browser navigation/XHR.
    if host in IG_HOSTS:
        has_sec_fetch = any(k.lower().startswith("sec-fetch-") for k in header_dict)
        if not has_sec_fetch:
            accept_val = ""
            for hk, hv in header_dict.items():
                if hk.lower() == "accept":
                    accept_val = hv.lower()
                    break
            is_doc_nav = (
                method == "GET"
                and ("text/html" in accept_val or not accept_val)
                and not req_path.startswith(("/api/", "/ajax/", "/graphql"))
                and not req_path.endswith((".js", ".css", ".json", ".png", ".jpg", ".webp", ".woff2"))
            )
            if is_doc_nav:
                header_dict["Sec-Fetch-Dest"] = "document"
                header_dict["Sec-Fetch-Mode"] = "navigate"
                header_dict["Sec-Fetch-Site"] = "none"
                header_dict["Sec-Fetch-User"] = "?1"
                if not any(hk.lower() == "upgrade-insecure-requests" for hk in header_dict):
                    header_dict["Upgrade-Insecure-Requests"] = "1"
            else:
                header_dict["Sec-Fetch-Dest"] = "empty"
                header_dict["Sec-Fetch-Mode"] = "cors"
                header_dict["Sec-Fetch-Site"] = "same-origin"
                if not any(hk.lower() == "origin" for hk in header_dict) and method == "POST":
                    header_dict["Origin"] = f"https://{host}"
                if not any(hk.lower() == "referer" for hk in header_dict):
                    header_dict["Referer"] = f"https://{host}/"

    req_path_clean = req_path.rstrip("/")
    if (
        method == "POST"
        and host in ("www.instagram.com", "instagram.com")
        and req_path_clean in ("/api/graphql", "/graphql/query", "/graphql")
        and req_body
    ):
        if (
            b"useCDSWebLoginMutation" in req_body
            or b"28077768681846394" in req_body
            or (b"enc_password" in req_body and b"identifier" in req_body)
        ):
            bridged = await _handle_ig_caa_login(
                req_body, header_dict, cookie_str, client_ua, impersonate_profile
            )
            if bridged is not None:
                return bridged
        elif (
            b"CAAIGAccountSearchViewQuery" in req_body
            or b"36716895674620546" in req_body
            or b"caa_ar_ig_account_search" in req_body
        ):
            bridged_ar = await _handle_ig_account_recovery_search(
                req_body, header_dict, cookie_str, client_ua, impersonate_profile
            )
            if bridged_ar is not None:
                return bridged_ar

    if (
        method == "POST"
        and host in ("www.instagram.com", "instagram.com")
        and (
            req_path.startswith("/api/v1/web/accounts/")
            or req_path_clean in ("/api/graphql", "/graphql/query", "/graphql")
        )
    ):
        m_csrf = re.search(r"(?:^|[;\s])csrftoken=([^;\s]+)", cookie_str)
        csrf_val = m_csrf.group(1).strip() if m_csrf else ""
        if not csrf_val:
            for hk, hv in header_dict.items():
                if hk.lower() == "x-csrftoken" and hv.strip():
                    csrf_val = hv.strip()
                    break
        if not csrf_val:
            csrf_val = "igcsrf" + str(int(time.time()))
        header_dict["X-CSRFToken"] = csrf_val
        if "csrftoken=" not in cookie_str:
            cookie_str = f"{cookie_str}; csrftoken={csrf_val}" if cookie_str else f"csrftoken={csrf_val}"
            header_dict["Cookie"] = cookie_str
        if not any(hk.lower() == "x-ig-app-id" for hk in header_dict):
            header_dict["X-IG-App-ID"] = "936619743392459"
        if not any(hk.lower() == "x-requested-with" for hk in header_dict) and req_path.startswith("/api/v1/"):
            header_dict["X-Requested-With"] = "XMLHttpRequest"
        if not any(hk.lower() == "origin" for hk in header_dict):
            header_dict["Origin"] = "https://www.instagram.com"
        if not any(hk.lower() == "referer" for hk in header_dict):
            header_dict["Referer"] = "https://www.instagram.com/"

    has_sensitive_auth = (
        bool(cookie_parts)
        or any(k.lower() in ("authorization", "x-csrftoken", "x-ig-app-id") for k in header_dict)
        or host in AUTH_HOSTS
        or req_path.startswith(("/accounts/", "/api/", "/graphql", "/ajax/", "/__rd_verify_"))
    )

    has_range = any(k.lower() == "range" for k in header_dict)
    is_media_cdn = any(host == s or host.endswith("." + s) for s in IG_MEDIA_SUFFIXES)
    is_static_img = (
        is_media_cdn
        and "/o1/v/" not in req_path
        and "bytestart=" not in (parsed.query or "")
        and not req_path.lower().endswith((".js", ".mjs", ".cjs", ".css", ".json", ".html", ".wasm", ".mp4", ".m4v", ".m4s", ".m3u8"))
        and (not req_path.startswith("/rsrc.php/") or req_path.lower().endswith((".webp", ".png", ".jpg", ".jpeg", ".gif", ".avif", ".ico", ".svg", ".woff2", ".woff", ".ttf")))
    )
    if is_static_img and not has_range and method in ("GET", "HEAD"):
        cached_img = _IMAGE_CACHE.get(url)
        if cached_img and (time.time() - cached_img[0]) < _IMAGE_CACHE_TTL:
            _, c_status, c_media_type, c_body, c_cc = cached_img
            return _pack_relay_response(
                c_status,
                "OK",
                [
                    ["content-type", c_media_type],
                    ["cache-control", c_cc],
                    ["access-control-allow-origin", "*"],
                    ["cross-origin-resource-policy", "cross-origin"],
                ],
                b"" if method == "HEAD" else c_body,
            )

    cacheable = _is_cacheable_static_asset(method, url)
    if cacheable:
        cached = _STATIC_ASSET_CACHE.get(url)
        if cached and (time.time() - cached[0]) < _STATIC_ASSET_TTL:
            _, c_status, c_text, c_headers, c_body = cached
            return _pack_relay_response(c_status, c_text, c_headers, c_body)

    ephemeral_session: StatelessCurlSession | None = None
    extra_set_cookies: list[tuple[str, str]] = []
    try:
        if StatelessCurlSession is not None:
            is_credential_login = (
                method == "POST"
                and host in ("www.instagram.com", "instagram.com")
                and req_path.startswith(("/accounts/login", "/api/v1/web/accounts/login"))
            )
            if is_credential_login:
                ephemeral_session = StatelessCurlSession(
                    impersonate=impersonate_profile,
                    default_headers=False,
                    allow_redirects=False,
                    timeout=25,
                    max_clients=4,
                )
                active_session = ephemeral_session
            else:
                active_session = await _get_curl_session()
        else:
            active_session = None

        if active_session is not None:
            upstream = await active_session.request(
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
            raw_multi = list(upstream.headers.multi_items())

            # Automatically solve Instagram's 403 /__rd_verify_* JS challenge on the server
            # so navigation/requests never fail or hang on a blank 403 challenge page.
            if status == 403 and host in ("www.instagram.com", "instagram.com") and b"/__rd_verify_" in resp_body:
                m_rd = re.search(rb"""fetch\(['"](/__rd_verify_[^'"]+)['"]""", resp_body)
                if m_rd:
                    rd_path = m_rd.group(1).decode("utf-8", "ignore")
                    rd_headers = dict(header_dict)
                    rd_headers["Sec-Fetch-Dest"] = "empty"
                    rd_headers["Sec-Fetch-Mode"] = "cors"
                    rd_headers["Sec-Fetch-Site"] = "same-origin"
                    rd_headers["Origin"] = f"https://{host}"
                    rd_headers["Referer"] = url
                    rd_resp = await active_session.request(
                        method="POST",
                        url=f"https://{host}{rd_path}",
                        headers=rd_headers,
                        allow_redirects=False,
                        timeout=12,
                    )
                    rd_cookie_pairs: list[str] = []
                    for rk, rv in rd_resp.headers.multi_items():
                        if rk.lower() == "set-cookie":
                            extra_set_cookies.append((rk, rv))
                            c_nv = rv.split(";", 1)[0].strip()
                            if c_nv:
                                rd_cookie_pairs.append(c_nv)
                    if rd_cookie_pairs:
                        cur_c = header_dict.get("Cookie", "")
                        header_dict["Cookie"] = (cur_c + "; " + "; ".join(rd_cookie_pairs)) if cur_c else "; ".join(rd_cookie_pairs)
                    upstream_retry = await active_session.request(
                        method=method,
                        url=url,
                        headers=header_dict,
                        data=req_body if req_body else None,
                        allow_redirects=False,
                        timeout=22,
                    )
                    status = int(upstream_retry.status_code)
                    status_text = str(getattr(upstream_retry.reason if hasattr(upstream_retry, "reason") else "", "strip", lambda: "OK")() or "OK")
                    resp_body = bytes(upstream_retry.content or b"")
                    raw_multi = extra_set_cookies + list(upstream_retry.headers.multi_items())
        else:
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
    except Exception as exc:
        err_msg = f"upstream error: {exc}".encode("utf-8", "ignore")
        return _pack_relay_response(
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

    # If Instagram returns 429 on /ajax/bulk-route-definitions/ or /ajax/route-definition/,
    # return valid Comet responses so client-side navigation falls back to clean full-page load.
    if status == 429 and req_path.startswith("/ajax/bulk-route-definitions"):
        return _pack_relay_response(
            200,
            "OK",
            [
                ["content-type", "application/x-javascript; charset=utf-8"],
                ["access-control-allow-origin", "https://www.instagram.com"],
                ["access-control-allow-credentials", "true"],
            ],
            b'for (;;);{"__ar":1,"payload":{}}',
        )
    if status == 429 and req_path.startswith("/ajax/route-definition"):
        return _pack_relay_response(
            200,
            "OK",
            [
                ["content-type", "application/x-javascript; charset=utf-8"],
                ["access-control-allow-origin", "https://www.instagram.com"],
                ["access-control-allow-credentials", "true"],
            ],
            b'for (;;);{"__type":"error_response"}',
        )

    out_headers: list[list[str]] = []
    has_set_cookie = False
    is_html = False
    resp_media_type = "application/octet-stream"
    resp_cc = "public, max-age=86400"
    for k, v in raw_multi:
        kl = k.lower()
        if kl in _RELAY_STRIP_RESP_HEADERS or kl.startswith(":"):
            continue
        if kl == "set-cookie":
            has_set_cookie = True
            v = _normalize_ig_set_cookie(v, host)
        if kl == "content-type":
            resp_media_type = v
            vl = v.lower()
            if "text/html" in vl:
                is_html = True
        elif kl == "cache-control":
            resp_cc = v
        out_headers.append([k, v])

    if is_html and status == 200 and resp_body:
        if b"modulepreload" in resp_body:
            resp_body = re.sub(
                rb'<link\b[^>]*\brel=["\']?modulepreload["\']?[^>]*>',
                b"",
                resp_body,
                flags=re.I,
            )
        resp_body = _inline_ig_data_scripts(resp_body)
    elif req_path in ("/api/graphql", "/graphql/query") and resp_body:
        if b'"is_silent":false' in resp_body:
            resp_body = resp_body.replace(b'"is_silent":false', b'"is_silent":true')
        if b'"lox_pow_challenge":{' in resp_body:
            resp_body = re.sub(
                rb'("lox_pow_challenge":\{[^{}]*"iterations":)\d+',
                rb"\g<1>1",
                resp_body,
            )

    if (
        is_static_img
        and not has_range
        and not has_set_cookie
        and method == "GET"
        and status == 200
        and 0 < len(resp_body) <= _IMAGE_CACHE_MAX_BYTES
    ):
        if len(_IMAGE_CACHE) >= _IMAGE_CACHE_MAX_ITEMS:
            oldest_k = next(iter(_IMAGE_CACHE))
            _IMAGE_CACHE.pop(oldest_k, None)
        _IMAGE_CACHE[url] = (
            time.time(),
            status,
            resp_media_type,
            resp_body,
            resp_cc,
        )

    if (
        cacheable
        and not has_set_cookie
        and not cookie_parts
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

    return _pack_relay_response(status, status_text, out_headers, resp_body)


@router.websocket("/instagram/wisp/")
async def ig_wisp_route(websocket: WebSocket):
    import app as host_app

    await host_app.wisp_media_endpoint(websocket)


def _install_ig_routes() -> None:
    try:
        import app as host_app

        if not any(
            isinstance(r, Route)
            and r.path == "/instagram/sw.js"
            and r.endpoint is _ig_sw_override
            for r in host_app.app.router.routes
        ):
            host_app.app.router.routes.insert(
                0, Route("/instagram/sw.js", _ig_sw_override, methods=["GET", "HEAD"])
            )
        if not any(
            isinstance(r, WebSocketRoute)
            and r.path == "/instagram/wisp/"
            for r in host_app.app.router.routes
        ):
            host_app.app.router.routes.insert(
                0, WebSocketRoute("/instagram/wisp/", ig_wisp_route)
            )
    except Exception as exc:
        print(f"[Instagram] route install warning: {exc}", flush=True)


_install_ig_routes()


def _target_from_input(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return IG_HOME
    low = value.lower()
    if low in ("reels", "reel", "/reels", "/reels/"):
        return IG_REELS
    if low in ("explore", "/explore", "/explore/"):
        return IG_EXPLORE
    if low in ("login", "/login", "/accounts/login", "/accounts/login/"):
        return IG_LOGIN
    if value.startswith(("http://", "https://")):
        try:
            u = urlparse(value)
            h = (u.hostname or "").lower().rstrip(".")
            if h == "instagram.com":
                h = "www.instagram.com"
            if h in IG_HOSTS or h in AUTH_HOSTS:
                return urlunparse(("https", h, u.path or "/", "", u.query, ""))
        except Exception:
            pass
    if value.startswith("@") and len(value) > 1:
        return "https://www.instagram.com/" + quote(value[1:].strip("/"), safe="") + "/"
    if value.startswith("#") and len(value) > 1:
        return "https://www.instagram.com/explore/tags/" + quote(value[1:].strip("/"), safe="") + "/"
    return "https://www.instagram.com/explore/tags/" + quote(value.strip("/"), safe="") + "/"


def _js_literal(value: str) -> str:
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _shell_html(initial_target: str) -> str:
    home_js = _js_literal(IG_HOME)
    reels_js = _js_literal(IG_REELS)
    explore_js = _js_literal(IG_EXPLORE)
    login_js = _js_literal(IG_LOGIN)
    initial_js = _js_literal(initial_target)
    wisp_override_js = _js_literal(SCRAMJET_WISP_URL)
    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#000000">
<title>Google</title>
<link rel="icon" type="image/x-icon" href="/favicon.ico">
<link rel="stylesheet" href="/design-system/m3.css">
<script src="/design-system/m3.js"></script>
<style>
:root{{--bg:var(--md-sys-color-surface,#000000);--text:var(--md-sys-color-on-surface,#f5f5f5);--muted:var(--md-sys-color-on-surface-variant,#a8a8a8);--nav-h:40px}}
*{{box-sizing:border-box}}
html,body{{margin:0;width:100%;height:100%;overflow:hidden;background:var(--bg);color:var(--text);font-family:var(--md-sys-typescale-font-plain,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif)}}
.ig-topbar{{position:fixed;top:0;left:0;right:0;height:var(--nav-h);z-index:20;display:flex;align-items:center;gap:6px;padding:0 10px;background:rgba(12,12,14,0.96);border-bottom:1px solid rgba(255,255,255,0.12);overflow-x:auto;scrollbar-width:none}}
.ig-topbar::-webkit-scrollbar{{display:none}}
.ig-brand{{display:inline-flex;align-items:center;gap:5px;font-size:13px;font-weight:700;color:#fff;white-space:nowrap;margin-right:4px}}
.ig-nav-btn{{appearance:none;border:1px solid rgba(255,255,255,0.16);background:rgba(255,255,255,0.06);color:#e5e7eb;font-size:12px;font-weight:600;padding:4px 10px;border-radius:999px;cursor:pointer;white-space:nowrap;transition:background .15s,border-color .15s,color .15s}}
.ig-nav-btn:hover{{background:rgba(255,255,255,0.14);color:#fff}}
.ig-nav-btn.active{{background:linear-gradient(135deg,#f58529,#dd2a7b,#8134af);border-color:transparent;color:#fff}}
.ig-jump-form{{display:flex;align-items:center;gap:4px;margin-left:auto;min-width:140px;max-width:260px;flex:1}}
.ig-jump-input{{width:100%;height:26px;padding:0 9px;border-radius:999px;border:1px solid rgba(255,255,255,0.18);background:rgba(255,255,255,0.07);color:#fff;font-size:12px;outline:none}}
.ig-jump-input:focus{{border-color:#dd2a7b;background:rgba(255,255,255,0.11)}}
.stage{{position:fixed;top:var(--nav-h);left:0;right:0;bottom:0;width:100%;height:calc(100% - var(--nav-h));background:var(--md-sys-color-surface-dim,#000)}}
#frame{{position:absolute;inset:0;display:block;width:100%;height:100%;border:0;background:#000}}
.cover{{position:absolute;inset:0;z-index:5;display:grid;place-items:center;padding:32px;background:var(--md-sys-color-surface,#000);color:var(--muted);font-size:14px;font-weight:500;text-align:center}}
.cover.hidden{{display:none}}
</style>
</head>
<body>
<header class="ig-topbar" role="navigation" aria-label="Instagram ナビゲーション">
  <span class="ig-brand">📸 Instagram</span>
  <button type="button" class="ig-nav-btn" data-ig-nav="home" id="btnNavHome">🏠 ホーム / ログイン</button>
  <button type="button" class="ig-nav-btn" data-ig-nav="reels" id="btnNavReels">🎬 リール</button>
  <button type="button" class="ig-nav-btn" data-ig-nav="explore" id="btnNavExplore">🧭 発見</button>
  <form class="ig-jump-form" id="igJumpForm">
    <input type="text" class="ig-jump-input" id="igJumpInput" placeholder="@ユーザー名 / #タグ / URL" autocomplete="off">
    <button type="submit" class="ig-nav-btn">移動</button>
  </form>
  <button type="button" class="ig-nav-btn" id="btnNavReload" title="再読み込み">↻</button>
</header>
<main class="stage">
  <div class="cover" id="cover">Instagram を読み込んでいます…</div>
  <iframe id="frame" title="Instagram"
    allow="autoplay; clipboard-read; clipboard-write; encrypted-media; fullscreen; picture-in-picture"
    referrerpolicy="strict-origin-when-cross-origin"></iframe>
</main>

<script src="/instagram/runtime/scramjet.js?v={RUNTIME_REV}"></script>
<script src="/instagram/runtime/controller.api.js?v={RUNTIME_REV}"></script>
<script src="/scramjet/scramjet-utils.js?v={UTILS_VERSION}"></script>
<script>
(() => {{
  "use strict";

  const HOME = {home_js};
  const REELS_URL = {reels_js};
  const EXPLORE_URL = {explore_js};
  const LOGIN_URL = {login_js};
  const AUTH_HOME = "https://www.instagram.com/";
  const INITIAL = {initial_js};
  const WISP_OVERRIDE = {wisp_override_js};
  const SW = "/instagram/sw.js?v={RUNTIME_REV}";
  const SW_SCOPE = "/instagram/";
  const PREFIX = "/instagram/~/sj/";
  const WISP_PATH = "/instagram/wisp/";
  const RELAY_ENDPOINT = "/instagram/runtime/http-relay";
  const MEDIA_RELAY_ENDPOINT = "/instagram/runtime/media-relay";
  const STATIC_CACHE_NAME = "sh-ig-static-{RUNTIME_REV}";
  const MEDIA_CACHE_NAME = "sh-ig-media-{RUNTIME_REV}";
  const IG_MEDIA_SUFFIXES = ["cdninstagram.com", "fbcdn.net"];
  const SESSION_KEY = "schoolhub.instagram.target.v1";
  const SW_GUARD = "schoolhub.instagram.sw-reload.v1";
  const MAX_RECOVERY = 3;
  const CONTROL_WAIT = 12000;
  const HEALTH_DELAY = 10000;
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

  const cover = document.getElementById("cover");
  const frameEl = document.getElementById("frame");
  const btnNavHome = document.getElementById("btnNavHome");
  const btnNavReels = document.getElementById("btnNavReels");
  const btnNavExplore = document.getElementById("btnNavExplore");
  const btnNavReload = document.getElementById("btnNavReload");
  const igJumpForm = document.getElementById("igJumpForm");
  const igJumpInput = document.getElementById("igJumpInput");

  function updateNavHighlight(urlStr) {{
    try {{
      const u = String(urlStr || "");
      const isReels = u.includes("/reels") || u.includes("/reel/");
      const isExplore = u.includes("/explore");
      btnNavReels?.classList.toggle("active", isReels);
      btnNavExplore?.classList.toggle("active", !isReels && isExplore);
      btnNavHome?.classList.toggle("active", !isReels && !isExplore);
    }} catch (_) {{}}
  }}

  function resolveUserTarget(raw) {{
    const val = String(raw || "").trim();
    if (!val) return HOME;
    const low = val.toLowerCase();
    if (low === "reels" || low === "reel" || low === "/reels" || low === "/reels/") return REELS_URL;
    if (low === "explore" || low === "/explore" || low === "/explore/") return EXPLORE_URL;
    if (low === "login" || low === "/login" || low === "/accounts/login" || low === "/accounts/login/") return LOGIN_URL;
    if (val.startsWith("http://") || val.startsWith("https://")) {{
      try {{
        const u = new URL(val);
        let h = u.hostname.toLowerCase().replace(/\\.$/, "");
        if (h === "instagram.com") h = "www.instagram.com";
        if (h.endsWith("instagram.com")) return "https://" + h + (u.pathname || "/") + (u.search || "");
      }} catch (_) {{}}
    }}
    if (val.startsWith("@") && val.length > 1) {{
      return "https://www.instagram.com/" + encodeURIComponent(val.slice(1).replace(/^\\/+|\\/+$/g, "")) + "/";
    }}
    if (val.startsWith("#") && val.length > 1) {{
      return "https://www.instagram.com/explore/tags/" + encodeURIComponent(val.slice(1).replace(/^\\/+|\\/+$/g, "")) + "/";
    }}
    if (/^[a-zA-Z0-9._]{{1,30}}$/.test(val)) {{
      return "https://www.instagram.com/" + encodeURIComponent(val) + "/";
    }}
    return "https://www.instagram.com/explore/tags/" + encodeURIComponent(val.replace(/^\\/+|\\/+$/g, "")) + "/";
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

  function wispUrl() {{
    if (WISP_OVERRIDE) {{
      try {{
        const u = new URL(WISP_OVERRIDE, location.href);
        if (u.protocol === "ws:" || u.protocol === "wss:") return u.href;
      }} catch (_) {{}}
    }}
    return (location.protocol === "https:" ? "wss://" : "ws://") + location.host + WISP_PATH;
  }}

  function hasAuthCookie() {{
    try {{
      const dump = String(controller?.cookieJar?.dump?.() || "");
      if (/(?:^|[\\s;])sessionid=|"sessionid"/.test(dump)) return true;
      const vault = localStorage.getItem("__sh_cookie_vault_instagram") || sessionStorage.getItem("__sh_cookie_vault_instagram") || "";
      return /(?:^|[\\s;])sessionid=|"sessionid"/.test(vault);
    }} catch (_) {{
      return false;
    }}
  }}

  function remember(url) {{
    if (!url) return;
    const s = String(url);
    lastUrl = s;
    updateNavHighlight(s);
    if (!hasAuthCookie() && (s.includes("/accounts/password/") || s.includes("/accounts/emailsignup") || s.includes("facebook.com/"))) {{
      return;
    }}
    try {{ sessionStorage.setItem(SESSION_KEY, s); }} catch (_) {{}}
    try {{ localStorage.setItem(SESSION_KEY, s); }} catch (_) {{}}
  }}

  function restore() {{
    try {{
      const params = new URLSearchParams(location.search);
      const hasExplicitQuery = Boolean(params.get("q") || params.get("url") || params.get("tab"));
      if (hasExplicitQuery) return INITIAL;
      if (!hasAuthCookie()) {{
        try {{ sessionStorage.removeItem(SESSION_KEY); }} catch (_) {{}}
        try {{ localStorage.removeItem(SESSION_KEY); }} catch (_) {{}}
        return INITIAL;
      }}
      const saved = sessionStorage.getItem(SESSION_KEY) || localStorage.getItem(SESSION_KEY);
      if (saved) return saved;
      return AUTH_HOME;
    }} catch (_) {{
      return INITIAL;
    }}
  }}

  const LOG_ENDPOINT = "/_diag/instagram";
  let logSequence = 0;
  const clientLog = (level, event, details = {{}}) => {{
    const payload = {{
      source: "instagram-scramjet",
      level: String(level || "info"),
      event: String(event || "event"),
      seq: ++logSequence,
      ts: new Date().toISOString(),
      transport: transportName || null,
      details,
    }};
    try {{
      const fn = level === "error" ? console.error : level === "warn" ? console.warn : console.log;
      fn.call(console, "[SchoolHub Instagram]", event, details);
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

  function cleanTargetUrl(rawUrl) {{
    if (!rawUrl || typeof rawUrl !== "string") return "";
    try {{
      const u = new URL(rawUrl);
      if (u.protocol !== "http:" && u.protocol !== "https:") return "";
      for (const k of [...u.searchParams.keys()]) {{
        if (k.startsWith("$")) u.searchParams.delete(k);
      }}
      return u.href;
    }} catch (_) {{
      return "";
    }}
  }}

  function observeUrl(url) {{
    const cleaned = cleanTargetUrl(url);
    if (!cleaned) return;
    if (cleaned !== lastUrl) {{
      lastUrl = cleaned;
      remember(cleaned);
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

  function isDirectMediaHost(host) {{
    const h = String(host || "").toLowerCase().replace(/\\.$/, "");
    return IG_MEDIA_SUFFIXES.some(x => h === x || h.endsWith("." + x));
  }}

  function isDirectMediaTarget(target) {{
    if (!target || !isDirectMediaHost(target.hostname)) return false;
    const p = String(target.pathname || "");
    if (/\\.(js|mjs|cjs|json|css|html|wasm)(\\?|$)/i.test(p)) return false;
    if (p.startsWith("/rsrc.php/") && !/\\.(webp|png|jpg|jpeg|gif|avif|ico|svg|mp4|m4v|m4s|m3u8|woff2|woff|ttf)(\\?|$)/i.test(p)) return false;
    return true;
  }}

  function isCacheableImageTarget(target) {{
    if (!target || !isDirectMediaHost(target.hostname)) return false;
    const p = String(target.pathname || "");
    if (p.includes("/o1/v/") || /\\.(mp4|m4v|m4s|m3u8)(\\?|$)/i.test(p)) return false;
    if (target.searchParams.has("bytestart") || target.searchParams.has("byteend")) return false;
    return true;
  }}

  function unwrapMediaTarget(urlStr) {{
    try {{
      let u = new URL(urlStr);
      for (let k = 0; k < 3; k++) {{
        if (u.pathname === "/instagram/runtime/media-relay") {{
          const kv = u.searchParams.get("k");
          const uv = kv ? decodeMediaKey(kv) : u.searchParams.get("url");
          if (uv) {{
            u = new URL(uv);
            continue;
          }}
        }}
        break;
      }}
      return u;
    }} catch (_) {{
      return null;
    }}
  }}

  function isClientCacheableAsset(method, urlStr) {{
    if (method !== "GET") return false;
    try {{
      const u = new URL(urlStr);
      const h = u.hostname.toLowerCase().replace(/\\.$/, "");
      const p = u.pathname || "";
      if (isDirectMediaHost(h) && (p.startsWith("/rsrc.php/") || /\\.(js|css|woff2|woff|ttf|svg|png|jpg|webp)$/i.test(p))) {{
        return true;
      }}
    }} catch (_) {{}}
    return false;
  }}

  function checkTransportShortCircuit(urlStr) {{
    try {{
      const u = new URL(urlStr);
      const h = u.hostname.toLowerCase().replace(/\\.$/, "");
      const p = u.pathname || "";
      if (
        (h === "graph.instagram.com" && p.includes("logging_client_events")) ||
        p.startsWith("/ajax/bz") ||
        p.startsWith("/ajax/qm") ||
        (h === "www.facebook.com" && p.startsWith("/ig_xsite_user_info"))
      ) {{
        const b = new TextEncoder().encode("{{}}").buffer;
        return {{
          body: b,
          headers: [["content-type", "application/json"], ["access-control-allow-origin", "*"]],
          status: 200,
          statusText: "OK",
        }};
      }}
    }} catch (_) {{}}
    return null;
  }}

  function normalizeHeadersList(headers, targetUrl = "") {{
    const effectiveUa = DEFAULT_UA;
    const list = Array.isArray(headers)
      ? headers.map(([k, v]) => [String(k), String(v)])
      : (headers && typeof headers === "object" ? Object.entries(headers).map(([k, v]) => [String(k), String(v)]) : []);
    const has = (name) => list.some(([k]) => k.toLowerCase() === name.toLowerCase());
    if (!has("user-agent")) {{
      list.push(["User-Agent", effectiveUa]);
    }} else {{
      for (let i = 0; i < list.length; i++) {{
        const kl = list[i][0].toLowerCase();
        if (kl === "user-agent") {{
          list[i][1] = effectiveUa;
        }} else if (kl === "sec-ch-ua-mobile") {{
          list[i][1] = "?0";
        }} else if (kl === "sec-ch-ua-platform") {{
          list[i][1] = '"Windows"';
        }}
      }}
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
            console.warn("[SchoolHub Instagram] fallback wisp transport init failed", kind, e);
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
      const rawUrlStr = remote instanceof URL ? remote.href : String(remote || "");
      const m = String(method || "GET").toUpperCase();
      const unwrapped = unwrapMediaTarget(rawUrlStr);
      const urlStr = unwrapped ? unwrapped.href : rawUrlStr;

      const sc = checkTransportShortCircuit(urlStr);
      if (sc) return sc;

      const list = normalizeHeadersList(headers, urlStr);
      const rangeEntry = list.find(([k]) => k.toLowerCase() === "range");

      // Fast path for GET/HEAD to Instagram media/image/video CDN hosts (*.cdninstagram.com, *.fbcdn.net)
      if ((m === "GET" || m === "HEAD") && unwrapped && isDirectMediaTarget(unwrapped)) {{
        const relayUrl = MEDIA_RELAY_ENDPOINT + "?k=" + encodeMediaKey(unwrapped.href);
        const canCache = m === "GET" && !rangeEntry && isCacheableImageTarget(unwrapped);
        try {{
          if (canCache && typeof caches !== "undefined") {{
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
              if (r && r.status < 400) {{
                if (r.status === 200) {{
                  try {{ await cache.put(relayUrl, r.clone()); }} catch (_) {{}}
                }}
                return {{
                  body: await r.arrayBuffer(),
                  headers: [...r.headers.entries()],
                  status: r.status,
                  statusText: r.statusText || "OK",
                }};
              }}
            }} catch (_) {{}}
          }}
          const fwdHeaders = {{}};
          if (rangeEntry && rangeEntry[1]) fwdHeaders["Range"] = rangeEntry[1];
          const r = await fetch(relayUrl, {{ method: m, headers: fwdHeaders, credentials: "omit", signal }});
          if (r && r.status < 400) {{
            return {{
              body: m === "HEAD" ? new ArrayBuffer(0) : await r.arrayBuffer(),
              headers: [...r.headers.entries()],
              status: r.status,
              statusText: r.statusText || "OK",
            }};
          }}
        }} catch (_) {{}}
      }}

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
        const statusNum = Number(meta.status || 200);
        const setCookieEntries = [];
        for (const [k, v] of respHeaders) {{
          if (k && k.toLowerCase() === "set-cookie" && v) {{
            setCookieEntries.push({{ url: urlStr, cookie: String(v) }});
          }}
        }}
        if (setCookieEntries.length > 0 && controller?.cookieJar) {{
          try {{
            controller.applyCookieSyncEntries(setCookieEntries);
            await controller.persistCookies();
            await controller.propagateCookieSync(setCookieEntries, {{}});
          }} catch (_) {{}}
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
        console.warn("[SchoolHub Instagram] relay fallback to wisp for", urlStr, relayError);
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
    if (typeof client.init === "function") {{
      await withTimeout(Promise.resolve().then(() => client.init()), 9000, kind + " transport init");
    }}
    return client;
  }}

  function buildPlugins() {{
    const U = globalThis.ScramjetUtils || globalThis.$scramjetUtils || {{}};
    const result = [];
    if (typeof U.HttpCachePlugin === "function") {{
      try {{ result.push(new U.HttpCachePlugin()); }} catch (_) {{}}
    }}
    if (typeof U.UrlWatcherPlugin === "function") {{
      try {{ result.push(new U.UrlWatcherPlugin((url) => observeUrl(String(url)))); }} catch (_) {{}}
    }}
    if (typeof U.CatchEscapedLinksPlugin === "function") {{
      try {{ result.push(new U.CatchEscapedLinksPlugin(() => new URL(location.href))); }} catch (_) {{}}
    }}
    return result;
  }}

  function readFrameTarget() {{
    try {{
      const href = frameEl.contentWindow?.location?.href || "";
      if (!href || href === "about:blank") return "";
      const fPrefix = frame?.prefix || "";
      if (fPrefix) {{
        const idx = href.indexOf(fPrefix);
        if (idx >= 0) {{
          const rawTail = href.slice(idx + fPrefix.length);
          const qIdx = rawTail.indexOf("?");
          const encodedPart = qIdx >= 0 ? rawTail.slice(0, qIdx) : rawTail;
          const decoded = decodeURIComponent(encodedPart);
          const cleaned = cleanTargetUrl(decoded);
          if (cleaned) return cleaned;
        }}
      }}
      const m = href.match(/\\/instagram\\/~\\/sj\\/(?:[^/]+\\/[^/]+\\/)?(https?(?:%3A|:).*)$/i);
      if (m && m[1]) {{
        const rawPart = m[1].split("?")[0];
        const decoded = decodeURIComponent(rawPart);
        const cleaned = cleanTargetUrl(decoded);
        if (cleaned) return cleaned;
      }}
      return "";
    }} catch (_) {{
      return "";
    }}
  }}

  function extractVirtualMediaFromAttr(rawVal) {{
    if (!rawVal || typeof rawVal !== "string") return null;
    try {{
      const u = new URL(rawVal, location.origin);
      if (u.pathname === "/instagram/runtime/media-relay") {{
        if (u.searchParams.has("k")) return null;
        if (u.searchParams.has("url")) {{
          const t = unwrapMediaTarget(u.searchParams.get("url"));
          if (t && isDirectMediaTarget(t)) return t;
          return null;
        }}
      }}
      const marker = "/instagram/~/sj/";
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
      const target = unwrapMediaTarget(candidate);
      if (!target) return null;
      for (const k of [...target.searchParams.keys()]) {{
        if (k.startsWith("$")) target.searchParams.delete(k);
      }}
      if (isDirectMediaTarget(target)) return target;
    }} catch (_) {{}}
    return null;
  }}

  function syncInputReactState(el) {{
    try {{
      if (el._valueTracker && typeof el._valueTracker.setValue === "function") {{
        el._valueTracker.setValue("");
      }}
      const Evt = frameEl.contentWindow?.Event || Event;
      el.dispatchEvent(new Evt("input", {{ bubbles: true, composed: true }}));
      el.dispatchEvent(new Evt("change", {{ bubbles: true, composed: true }}));
    }} catch (_) {{}}
  }}

  function ensureInstagramMedia() {{
    try {{
      const doc = frameEl.contentDocument;
      if (!doc || !doc.body) return;
      const rawSetAttr = window.Element.prototype.setAttribute;
      const rawGetAttr = window.Element.prototype.getAttribute;
      const rawRemoveAttr = window.Element.prototype.removeAttribute;

      const loginInputs = doc.querySelectorAll("input[name='email'], input[name='pass'], input[name='username'], input[name='password'], input[type='text'], input[type='password']");
      for (let i = 0; i < loginInputs.length; i++) {{
        const inp = loginInputs[i];
        if (!inp.dataset.shInputBound) {{
          inp.dataset.shInputBound = "1";
          const recordNativeVal = () => {{
            inp.dataset.shSyncedVal = String(inp.value || "");
          }};
          inp.addEventListener("input", recordNativeVal);
          inp.addEventListener("change", recordNativeVal);
          inp.addEventListener("keydown", (e) => {{
            if (e.key === "Enter") {{
              const cur = String(inp.value || "");
              if (cur !== (inp.dataset.shSyncedVal || "")) {{
                inp.dataset.shSyncedVal = cur;
                syncInputReactState(inp);
              }}
              const form = inp.closest("form");
              const submitBtn = (form || doc).querySelector("[data-testid='royal-login-button'], button[type='submit'], div[role='button'][aria-label='ログイン'], button[aria-label='ログイン']");
              if (submitBtn && submitBtn.getAttribute("aria-disabled") !== "true") {{
                e.preventDefault();
                try {{
                  const MEvt = frameEl.contentWindow?.MouseEvent || MouseEvent;
                  submitBtn.dispatchEvent(new MEvt("mousedown", {{ bubbles: true, cancelable: true }}));
                  submitBtn.dispatchEvent(new MEvt("mouseup", {{ bubbles: true, cancelable: true }}));
                }} catch (_) {{}}
                submitBtn.click();
              }}
            }}
          }});
        }}
        const curVal = String(inp.value || "");
        if (curVal !== (inp.dataset.shSyncedVal || "")) {{
          inp.dataset.shSyncedVal = curVal;
          syncInputReactState(inp);
        }}
      }}

      const frameTargetNow = readFrameTarget() || lastUrl || "";
      const isAuthSubpage = frameTargetNow.includes("/accounts/password/") || frameTargetNow.includes("/accounts/emailsignup");
      const allLinks = doc.querySelectorAll("a, div[role='button'], button");
      for (let i = 0; i < allLinks.length; i++) {{
        const a = allLinks[i];
        const txt = String(a.textContent || "").trim();
        const ariaLbl = String(rawGetAttr.call(a, "aria-label") || "").trim();
        const hrefAttr = String(rawGetAttr.call(a, "href") || "");
        const sjHrefAttr = String(rawGetAttr.call(a, "scramjet-attr-href") || "");
        const combinedHref = hrefAttr + " " + sjHrefAttr;
        if ((txt.includes("すでにアカウントを持っている") || txt.includes("ログインに戻る") || (isAuthSubpage && (ariaLbl === "戻る" || ariaLbl === "Back"))) && !a.dataset.shAlreadyAccBound) {{
          a.dataset.shAlreadyAccBound = "1";
          a.addEventListener("click", (e) => {{
            e.preventDefault();
            e.stopPropagation();
            navigateTo(LOGIN_URL);
          }}, true);
        }} else if ((txt === "Instagramを開く" || txt === "アプリを開く") && !a.dataset.shOpenAppBound) {{
          a.dataset.shOpenAppBound = "1";
          a.addEventListener("click", (e) => {{
            e.preventDefault();
            e.stopPropagation();
            const loginSwitchBtn = [...doc.querySelectorAll("button, a, div[role='button']")].find((el) => String(el.textContent || "").trim() === "ログイン");
            if (loginSwitchBtn && loginSwitchBtn !== a) {{
              loginSwitchBtn.click();
            }} else {{
              navigateTo(LOGIN_URL);
            }}
          }}, true);
        }} else if ((combinedHref.includes("/accounts/login") || combinedHref.includes("%2Faccounts%2Flogin")) && !a.dataset.shLoginBound) {{
          a.dataset.shLoginBound = "1";
          a.addEventListener("click", (e) => {{
            if (!hasAuthCookie()) {{
              const existingInput = doc.querySelector("input[name='email'], input[name='username']");
              if (existingInput) {{
                e.preventDefault();
                e.stopPropagation();
                try {{ existingInput.focus(); }} catch (_) {{}}
              }}
            }}
          }}, true);
        }}
      }}
      const imgs = doc.querySelectorAll("img");
      for (let i = 0; i < imgs.length; i++) {{
        try {{
          const img = imgs[i];
          const actualSrc = rawGetAttr.call(img, "src") || "";
          const origSrc = rawGetAttr.call(img, "scramjet-attr-src") || "";
          if (rawGetAttr.call(img, "loading") === "lazy") {{
            rawSetAttr.call(img, "loading", "eager");
          }}
          if (actualSrc.includes("/instagram/~/sj/") && actualSrc.includes("/data:")) {{
            const dIdx = actualSrc.indexOf("/data:");
            if (dIdx >= 0) {{
              rawSetAttr.call(img, "src", actualSrc.slice(dIdx + 1));
            }}
          }} else if (!actualSrc.includes("/instagram/runtime/media-relay?k=")) {{
            const target = extractVirtualMediaFromAttr(actualSrc) || extractVirtualMediaFromAttr(origSrc);
            if (target) {{
              const directUrl = location.origin + "/instagram/runtime/media-relay?k=" + encodeMediaKey(target.href);
              rawSetAttr.call(img, "src", directUrl);
              rawSetAttr.call(img, "loading", "eager");
            }}
          }}
          const actualSrcset = rawGetAttr.call(img, "srcset") || "";
          if (actualSrcset && !actualSrcset.includes("/instagram/runtime/media-relay?k=")) {{
            rawRemoveAttr.call(img, "srcset");
            if (rawGetAttr.call(img, "scramjet-attr-srcset")) rawRemoveAttr.call(img, "scramjet-attr-srcset");
          }}
        }} catch (_) {{}}
      }}
      const videos = doc.querySelectorAll("video");
      let anyPlaying = false;
      for (const v of videos) {{
        if (!v.paused && v.readyState >= 2) {{
          anyPlaying = true;
          break;
        }}
      }}
      const vh = frameEl.contentWindow?.innerHeight || window.innerHeight || 800;
      for (const v of videos) {{
        try {{
          const actualPoster = rawGetAttr.call(v, "poster") || "";
          const origPoster = rawGetAttr.call(v, "scramjet-attr-poster") || "";
          if ((actualPoster || origPoster) && !actualPoster.includes("/instagram/runtime/media-relay?k=")) {{
            const targetPoster = extractVirtualMediaFromAttr(actualPoster) || extractVirtualMediaFromAttr(origPoster);
            if (targetPoster) {{
              const directPoster = location.origin + "/instagram/runtime/media-relay?k=" + encodeMediaKey(targetPoster.href);
              rawSetAttr.call(v, "poster", directPoster);
              if (rawGetAttr.call(v, "scramjet-attr-poster")) rawRemoveAttr.call(v, "scramjet-attr-poster");
            }}
          }}
          const vSrc = rawGetAttr.call(v, "src") || "";
          const vOrigSrc = rawGetAttr.call(v, "scramjet-attr-src") || "";
          if (vSrc && !vSrc.startsWith("blob:") && !vSrc.includes("/instagram/runtime/media-relay?k=")) {{
            const target = extractVirtualMediaFromAttr(vSrc) || extractVirtualMediaFromAttr(vOrigSrc);
            if (target) {{
              rawSetAttr.call(v, "src", location.origin + "/instagram/runtime/media-relay?k=" + encodeMediaKey(target.href));
            }}
          }}
          if (!anyPlaying && v.paused && v.readyState >= 2 && !v.dataset.shAutoPlayed) {{
            const rect = v.getBoundingClientRect();
            const visibleH = Math.min(rect.bottom, vh) - Math.max(rect.top, 0);
            if (rect.height > 0 && visibleH >= rect.height * 0.5) {{
              v.dataset.shAutoPlayed = "1";
              anyPlaying = true;
              v.muted = true;
              v.playsInline = true;
              const p = v.play();
              if (p && typeof p.catch === "function") p.catch(() => {{}});
            }}
          }}
        }} catch (_) {{}}
      }}
    }} catch (_) {{}}
  }}

  function currentDocumentHealthy() {{
    try {{
      const win = frameEl.contentWindow;
      const href = String(win?.location?.href || "");
      const doc = frameEl.contentDocument;
      if (!doc || href === "about:blank") {{
        return {{ healthy: false, loading: false }};
      }}
      const body = doc.body;
      const title = String(doc.title || "").trim();
      const text = String(body?.innerText || "").replace(/\\s+/g, " ").trim();
      const hasIgRoot = Boolean(
        doc.querySelector("main, input[name='email'], input[name='username'], video, img[src*='media-relay'], a[href*='/accounts/login']")
      );
      const isProxied = href.includes(PREFIX);
      const loading = isProxied && (doc.readyState === "loading" || doc.readyState === "interactive");
      const healthy = Boolean(isProxied && (hasIgRoot || text.length > 24 || title.length > 1));
      return {{ healthy, loading }};
    }} catch (_) {{
      return {{ healthy: false, loading: false }};
    }}
  }}

  function showHealthy(url = "") {{
    clearTimeout(healthTimer);
    clearTimeout(recoveryTimer);
    recoveryTimer = 0;
    recovery = 0;
    cover.classList.add("hidden");
    const detected = url || readFrameTarget();
    if (detected) observeUrl(detected);
    ensureInstagramMedia();
  }}

  function armHealthCheck(token, waitMs = HEALTH_DELAY) {{
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

    const instance = new C({{
      serviceworker: navigator.serviceWorker.controller,
      transport: client,
      config: {{
        prefix: PREFIX,
        scramjetPath: "/instagram/runtime/scramjet.js?v={RUNTIME_REV}",
        injectPath: "/instagram/runtime/controller.inject.js?v={RUNTIME_REV}",
        wasmPath: "/scramjet/scramjet.wasm",
        virtualWasmPath: "scramjet.wasm.js",
        codec: {{
          encode: (value) => value ? encodeURIComponent(value) : value,
          decode: (value) => value ? decodeURIComponent(value) : value,
        }},
      }},
      scramjetConfig,
    }});
    await withTimeout(instance.wait(), 20000, "Scramjet Controller");
    return {{ instance, client }};
  }}

  const isOurSw = (c) => Boolean(c && String(c.scriptURL || "").includes(SW));

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
    await new Promise((resolve) => {{
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
    if (isOurSw(navigator.serviceWorker.controller)) {{
      try {{ sessionStorage.removeItem(SW_GUARD); }} catch (_) {{}}
      return;
    }}
    let reloaded = false;
    try {{ reloaded = sessionStorage.getItem(SW_GUARD) === "1"; }} catch (_) {{}}
    if (!reloaded) {{
      try {{ sessionStorage.setItem(SW_GUARD, "1"); }} catch (_) {{}}
      location.reload();
      throw new Error("Service Worker control reload scheduled");
    }}
    throw new Error("Instagram Service Worker did not take control");
  }}

  async function recover(reason) {{
    if (!frame || recoveryTimer) return;
    const snap = currentDocumentHealthy();
    if (snap.healthy) {{
      showHealthy();
      return;
    }}
    if (recovery >= MAX_RECOVERY) {{
      cover.classList.remove("hidden");
      cover.textContent = "Instagram を読み込めませんでした。ページを再読み込みしてください。";
      return;
    }}
    recovery += 1;
    const attempt = recovery;
    clientLog("warn", "recovery-start", {{ attempt, reason }});
    clearTimeout(healthTimer);
    navigationToken += 1;
    const recoveryToken = navigationToken;
    cover.classList.remove("hidden");
    cover.textContent = "Instagram を復旧中… (" + attempt + "/" + MAX_RECOVERY + ")";

    recoveryTimer = setTimeout(async () => {{
      recoveryTimer = 0;
      try {{
        try {{ controller?.setupMessagePort?.(); }} catch (_) {{}}
        const target = readFrameTarget() || lastUrl || HOME;
        lastUrl = target;
        remember(target);
        frame.go(target);
        armHealthCheck(recoveryToken, HEALTH_DELAY);
      }} catch (_) {{}}
    }}, attempt * 1000);
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
          if (target && target !== lastUrl) {{
            observeUrl(target);
          }}
          ensureInstagramMedia();
        }}
      }}
    }}, 650);
  }}

  function bindFrame() {{
    frameEl.addEventListener("load", () => {{
      const token = navigationToken;
      const snap = currentDocumentHealthy();
      if (snap.healthy) showHealthy();
      else armHealthCheck(token, 10000);
    }});
    frameEl.addEventListener("error", () => {{ recover("iframe-error"); }});
    startFrameMonitor();
  }}

  function keepalive() {{
    clearInterval(keepaliveTimer);
    const ping = () => {{
      try {{ navigator.serviceWorker.controller?.postMessage("keepalive"); }} catch (_) {{}}
    }};
    ping();
    keepaliveTimer = setInterval(ping, 15000);
  }}

  function navigateTo(targetUrl) {{
    if (!frame || !targetUrl) return;
    navigationToken += 1;
    lastUrl = targetUrl;
    remember(targetUrl);
    frame.go(targetUrl);
    armHealthCheck(navigationToken, HEALTH_DELAY);
  }}

  btnNavHome?.addEventListener("click", () => navigateTo(HOME));
  btnNavReels?.addEventListener("click", () => navigateTo(REELS_URL));
  btnNavExplore?.addEventListener("click", () => navigateTo(EXPLORE_URL));
  btnNavReload?.addEventListener("click", () => {{
    const t = readFrameTarget() || lastUrl || HOME;
    navigateTo(t);
  }});
  igJumpForm?.addEventListener("submit", (e) => {{
    e.preventDefault();
    const raw = igJumpInput?.value || "";
    const target = resolveUserTarget(raw);
    if (target) navigateTo(target);
  }});

  async function boot() {{
    clientLog("info", "boot-start", {{ userAgent: String(navigator.userAgent || "").slice(0, 300) }});
    if (!("serviceWorker" in navigator)) throw new Error("Service Worker unavailable");
    try {{
      const revKey = "sh:ig:rev";
      if (localStorage.getItem(revKey) !== "{RUNTIME_REV}" && typeof caches !== "undefined") {{
        const cKeys = await caches.keys();
        await Promise.all(cKeys.filter((k) => k.startsWith("sh-ig-")).map((k) => caches.delete(k).catch(() => {{}})));
        localStorage.setItem(revKey, "{RUNTIME_REV}");
      }}
    }} catch (_) {{}}

    if (isOurSw(navigator.serviceWorker.controller)) {{
      try {{ sessionStorage.removeItem(SW_GUARD); }} catch (_) {{}}
      clientLog("info", "service-worker-already-controlled", {{
        scriptURL: String(navigator.serviceWorker.controller?.scriptURL || ""),
      }});
      setTimeout(() => {{
        navigator.serviceWorker.register(SW, {{ scope: SW_SCOPE, updateViaCache: "none", type: "classic" }})
          .then((r) => {{ registration = r; }})
          .catch(() => {{}});
      }}, 0);
    }} else {{
      registration = await withTimeout(
        navigator.serviceWorker.register(SW, {{ scope: SW_SCOPE, updateViaCache: "none", type: "classic" }}),
        4500,
        "SW register"
      ).catch(async () => await navigator.serviceWorker.getRegistration(SW_SCOPE));
      clientLog("info", "service-worker-registered", {{ scope: registration?.scope || "", active: Boolean(registration?.active) }});
      await waitForWorkerControl(registration);
    }}
    navigator.serviceWorker.addEventListener("controllerchange", () => {{
      try {{ controller?.setupMessagePort?.(); }} catch (_) {{}}
    }});
    clientLog("info", "service-worker-controlled", {{ controller: Boolean(navigator.serviceWorker.controller) }});
    keepalive();

    let created;
    try {{
      created = await createController("hybrid");
      transportName = "hybrid";
    }} catch (_) {{
      try {{
        created = await createController("libcurl");
        transportName = "libcurl";
      }} catch (_) {{
        created = await createController("epoxy");
        transportName = "epoxy";
      }}
    }}

    controller = created.instance;
    window.__schoolhub_controller = controller;
    transport = created.client;
    frame = controller.createFrame(frameEl, {{ plugins: buildPlugins() }});
    bindFrame();
    ready = true;

    const stored = restore();
    navigateTo(stored);
  }}

  window.addEventListener("beforeunload", () => {{
    try {{ controller?.persistCookies?.(); }} catch (_) {{}}
    clearInterval(keepaliveTimer);
    clearInterval(pollTimer);
  }});

  boot().catch((error) => {{
    console.error("[SchoolHub Instagram] boot failed", error);
    cover.classList.remove("hidden");
    cover.textContent = "Instagram を起動できませんでした。再読み込みしてください。";
  }});
}})();
</script>
</body>
</html>"""


@router.get("/instagram", include_in_schema=False)
async def ig_redirect():
    return RedirectResponse("/instagram/", status_code=307)


@router.get("/instagram/", response_class=HTMLResponse, include_in_schema=False)
async def ig_home(q: str = "", url: str = "", tab: str = ""):
    target = _target_from_input(q or url or tab)
    response = HTMLResponse(_shell_html(target))
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@router.get("/instagram/health", include_in_schema=False)
async def ig_health():
    return {
        "ok": True,
        "service": SERVICE_NAME,
        "router_path": SERVICE_PATH,
        "proxy_module": PROXY_MODULE,
        "scramjet": SCRAMJET_VERSION,
        "controller": CONTROLLER_VERSION,
        "service_worker": "/instagram/sw.js",
        "virtual_prefix": "/instagram/~/sj/",
    }
