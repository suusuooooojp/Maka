"""Official TVer (tver.jp) isolated Scramjet proxy for School Hub.

100% genuine official tver.jp UI and behavior (zero custom UI / 独自UIなし):
- Full Next.js SPA routing (Home, Search, Live, Rankings, Categories, Series, Episodes, MyPage, TVer ID)
- Streaks Player + HLS (.m3u8 / AES-128 / fMP4 / TS) VOD & Live playback
- Per-browser isolated cookie & storage vault (__sh_cookie_vault_tver)

Exposes:
  SERVICE_NAME = "TVer"
  SERVICE_ICON = "📺"
  SERVICE_PATH = "/tver/"
  router       = APIRouter()
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

import httpx
from fastapi import APIRouter, Request, WebSocket
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
        """HTTP/2 + Chrome-TLS session with zero server-side cookie storage."""

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
                return await super().request(method, url, *args, **kwargs)
            finally:
                try:
                    self._cookies.clear()
                except Exception:
                    pass

else:
    StatelessCurlSession = None

SERVICE_NAME = "TVer"
SERVICE_ICON = "📺"
SERVICE_PATH = "/tver/"
PROXY_MODULE = "tver_proxy.py"

SCRAMJET_VERSION = "2.0.67-alpha.2"
CONTROLLER_VERSION = "0.0.14"
UTILS_VERSION = "0.0.3"
LIBCURL_VERSION = "2.0.5"
EPOXY_VERSION = "3.0.1"
RUNTIME_REV = "20261112-tver14"

SCRAMJET_WISP_URL = os.getenv("SCRAMJET_WISP_URL", "").strip()
TVER_UPSTREAM_PROXY = (
    os.getenv("TVER_UPSTREAM_PROXY")
    or os.getenv("JAPAN_PROXY_URL")
    or ""
).strip()
TVER_STREAKS_RELAY_URL = (os.getenv("TVER_STREAKS_RELAY_URL") or "").strip()

TVER_HOME = "https://tver.jp/"

AUTH_HOSTS = {
    "id.tver.jp",
    "tverid.tver.jp",
    "auth.tver.jp",
    "accounts.google.com",
    "appleid.apple.com",
}

router = APIRouter()


# ---------------------------------------------------------------------------
# WebKit / iPadOS / Chromium compatibility patches for Scramjet controller
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
        'const _shVaultKey="__sh_cookie_vault_tver";'
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
    old_cookie_wait = (
        'r=setTimeout(()=>{if(!n){let r=s.filter(e=>void 0!==t[e]);'
        'console.error(`timed out waiting for set cookie response (deadlock?): cookies=${e.length} clients=${o.length} pending=${r.length}/${s.length} clientUrls=${o.map(e=>e.url).join(",")}`)}i()},1e3)'
    )
    text = text.replace(old_cookie_wait, "r=setTimeout(()=>{i()},350)", 1)
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
    """Patch Scramjet 2.0.67-alpha.2 for TVer Next.js SPA & Streaks HLS Player."""
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
        'function n(e,t){const _pfx=()=>"tver:"+e.url.host+"@";let r={get(t,r){switch(r){'
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
        'const _u=String(t.navigator.userAgent||"");if(_u.includes("HeadlessChrome/")){'
        'const _c=_u.replace(/HeadlessChrome\\//g,"Chrome/"),_av=_c.replace(/^Mozilla\\//,"");'
        'const _ud=Object.getOwnPropertyDescriptor(_np,"userAgent");if(_ud&&_ud.get){const _ug=new Proxy(_ud.get,{apply:()=>_c});e.box.unproxy.set(_ug,_ud.get);Object.defineProperty(_np,"userAgent",{..._ud,get:_ug})}'
        'const _ad=Object.getOwnPropertyDescriptor(_np,"appVersion");if(_ad&&_ad.get){const _ag=new Proxy(_ad.get,{apply:()=>_av});e.box.unproxy.set(_ag,_ad.get);Object.defineProperty(_np,"appVersion",{..._ad,get:_ag})}'
        '}}'
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
        '{value:function(r){return r&&"function"==typeof r.postMessage?{postMessage:function(...a){try{const _isW=(()=>{try{return r.window===r||Object.prototype.toString.call(r).includes("Window")}catch(_){return!1}})();if(_isW){const _u=e?.url?.href||"",_sb="about:srcdoc"===_u||"about:blank"===_u,_og=(_sb?e?.global?.parent?.[n.p]?.url?.origin:e?.url?.origin)||"";if(!(a[0]&&"object"==typeof a[0]&&a[0].$scramjet$messagetype==="window")){a[0]={$scramjet$messagetype:"window",$scramjet$origin:_og,$scramjet$data:a[0]}}"string"==typeof a[1]&&(a[1]="*"),a[1]&&"object"==typeof a[1]&&(a[1].targetOrigin="*");try{top.__sh_last_pm_inv=(fn,th,ar)=>fn.apply(th,ar)}catch(_){}}}catch(_){}try{return r.postMessage(...a)}finally{try{top.__sh_last_pm_inv=null}catch(_){}}}}:r},'
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
        'i.iswindow&&e.Proxy("window.postMessage",{apply(c){if(c.args[0]&&"object"==typeof c.args[0]&&c.args[0].$scramjet$messagetype==="window"){"string"==typeof c.args[1]&&(c.args[1]="*"),"object"==typeof c.args[1]&&null!==c.args[1]&&(c.args[1].targetOrigin="*");let _inv=null;try{_inv=top.__sh_last_pm_inv;top.__sh_last_pm_inv=null}catch(_){}c.return(_inv?_inv(c.fn,c.this||t,c.args):c.fn.apply(c.this||t,c.args));return}let r=null;try{let{constructor:{constructor:f}}="object"==typeof c.args[0]&&null!==c.args[0]?c.args[0]:"object"==typeof c.args[2]&&null!==c.args[2]?c.args[2]:c.this&&o.POLLUTANT in c.this&&"object"==typeof c.this[o.POLLUTANT]&&null!==c.this[o.POLLUTANT]?c.this[o.POLLUTANT]:{};r=f("return globalThis")()?.[n.p]}catch(_){}r||(r=c.this?.[n.p]||t?.[n.p]||e);let u=r?.url?.href||"",s="about:srcdoc"===u||"about:blank"===u,g=(s?r?.global?.parent?.[n.p]?.url?.origin:r?.url?.origin)||e?.url?.origin||"";c.args[0]={$scramjet$messagetype:"window",$scramjet$origin:g,$scramjet$data:c.args[0]},"string"==typeof c.args[1]&&(c.args[1]="*"),"object"==typeof c.args[1]&&null!==c.args[1]&&(c.args[1].targetOrigin="*"),c.return(c.fn.apply(c.this||t,c.args))}})'
    )
    if old_win_pm in text:
        text = text.replace(old_win_pm, new_win_pm, 1)

    old_smap = "let e=globalThis[r.config.globals.pushsourcemapfn];"
    new_smap = "let e=(n?.base?.defaultView||globalThis)[r.config.globals.pushsourcemapfn]||globalThis[r.config.globals.pushsourcemapfn];"
    if old_smap in text:
        text = text.replace(old_smap, new_smap, 1)

    old_stack = 'let r=(t,r)=>{let n=t.stack;for(let t=0;t<r.length;t++){let s=r[t].getFileName();try{if(e.config.maskedfiles.some(e=>s.endsWith(e))){let e=n.split("\\n"),t=e.find(e=>e.includes(s));e.splice(t,1),n=e.join("\\n");continue}}catch{}try{n=n.replaceAll(s,(0,i.v2)(s,e.context))}catch{}}return n};e.Trap("Error.prepareStackTrace",{get:e=>r,set(e){}})'
    new_stack = 'let _inSt=!1,_usrSt=null,r=(t,a)=>{if(_inSt)return String(t);_inSt=!0;try{if("function"==typeof _usrSt)return _usrSt(t,a);let n=[String(t)];for(let o=0;o<a.length;o++){let c=a[o],s=String(c),f="";try{f=c.getFileName()||""}catch{}try{const _fc=f.split("?")[0];if(f&&(_fc.includes("/tver/runtime/")||_fc.endsWith("scramjet.js")||_fc.endsWith("scramjet.wasm.js")||_fc.endsWith("controller.inject.js")||e.config.maskedfiles.some(m=>_fc.endsWith(m))))continue}catch{}try{s=s.replace(/https?:\\/\\/[^\\s)]+\\/tver\\/~\\/sj\\/(?:[^/]+\\/){0,2}(https?(?:%3A|:)[^\\s):]+)/gi,(_,u)=>{try{return decodeURIComponent(u).split("?")[0]}catch{return _}})}catch{}n.push("    at "+s)}return n.join("\\n")}finally{_inSt=!1}};try{Error.prepareStackTrace=r}catch{}e.Trap("Error.prepareStackTrace",{get:()=>_usrSt||r,set(v){_usrSt="function"==typeof v?v:null;try{Error.prepareStackTrace=r}catch{}}})'
    if old_stack in text:
        text = text.replace(old_stack, new_stack, 1)

    old_rew = 'if(!A||"http:"!=A.protocol&&"https:"!=A.protocol)return e;'
    new_rew = (
        'if(!A||"http:"!=A.protocol&&"https:"!=A.protocol)return e;'
        'try{'
        'if(A.pathname==="/tver/runtime/media-relay"&&(A.searchParams.has("k")||A.searchParams.has("url")))return t.prefix.origin+A.pathname+A.search;'
        'const _mh=A.hostname.toLowerCase().replace(/\\.$/,""),_mp=String(A.pathname||"");'
        'const _isImg=(_mh==="image-cdn.tver.jp"||(_mh==="statics.tver.jp"&&/\\.(jpg|jpeg|png|webp|gif|svg|avif|ico)(\\?|$)/i.test(_mp)));'
        'const _isHls=(!_mh.endsWith(".api.streaks.jp")&&(/\\.(m3u8|ts|m4s|cmfv|cmfa|aac|vtt|webvtt|key)(\\?|$)/i.test(_mp)||((_mh.endsWith(".tver.jp")||_mh.endsWith(".cloudfront.net")||_mh.endsWith(".akamaized.net")||_mh.endsWith(".akamaihd.net")||_mh.endsWith(".fastly.net")||_mh.endsWith(".boltdns.net")||_mh.endsWith(".brightcove.com")||_mh.endsWith(".brightcove.net")||_mh.endsWith(".streaks.jp")||_mh.endsWith(".uliza.jp")||_mh.endsWith(".stream.ne.jp")||_mh.endsWith(".jstream.jp"))&&/\\.(mp4|m4a|m4v)(\\?|$)/i.test(_mp))));'
        'if((_isImg||_isHls)&&!o?.isModule&&o?.destination!=="script"&&o?.destination!=="worker"&&o?.destination!=="sharedworker"&&o?.destination!=="document"){'
        'return t.prefix.origin+"/tver/runtime/media-relay?k="+btoa(encodeURIComponent(A.href).split("").reverse().join("")).replace(/\\+/g,"-").replace(/\\//g,"_").replace(/=+$/,"")'
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
    new_url_props = "src:[t.HTMLImageElement,t.HTMLMediaElement,t.HTMLIFrameElement,t.HTMLFrameElement,t.HTMLEmbedElement,t.HTMLScriptElement,t.HTMLSourceElement],srcset:[t.HTMLImageElement,t.HTMLSourceElement],href:"
    if old_url_props in text:
        text = text.replace(old_url_props, new_url_props, 1)

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
    new_perf = 'e.Trap("PerformanceEntry.prototype.name",{get(t){let r=(0,i.Qf)(t.get());return r&&(r.startsWith(e.context.prefix.href)||r.includes("/tver/runtime/media-relay"))?e.unrewriteUrl(r):r}}),e.Proxy(["Performance.prototype.getEntries","Performance.prototype.getEntriesByType","Performance.prototype.getEntriesByName","PerformanceObserverEntryList.prototype.getEntries","PerformanceObserverEntryList.prototype.getEntriesByType","PerformanceObserverEntryList.prototype.getEntriesByName"],{apply(t){let r=t.call();return t.return(r.filter(t=>{const _nm=(0,i.Qf)(e.descriptors.get("PerformanceEntry.prototype.name",t))||"",_nc=_nm.split("?")[0];if(_nc.includes("/tver/runtime/scramjet")||_nc.includes("/tver/runtime/controller")||_nc.endsWith("scramjet.wasm.js"))return!1;for(let r of e.config.maskedfiles)if(_nc.endsWith(r))return!1;return!0}))}})'
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
        'if(navigator.webdriver&&_np){Object.defineProperty(_np,"webdriver",{get:()=>false,enumerable:true,configurable:true});}'
        'const _rawUa=String(navigator.userAgent||"").replace(/HeadlessChrome\\//g,"Chrome/");'
        'let _tvUa=_rawUa,_tvPlat=navigator.platform||"Win32",_tvChPlat="Windows";'
        'if(/(iPhone|iPad|iPod)/i.test(_rawUa)&&!/Chrome\\/|CriOS\\//i.test(_rawUa)){'
        '_tvUa="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15";'
        '_tvPlat="MacIntel";_tvChPlat="macOS";'
        '}else if(!/Windows NT|Macintosh/i.test(_rawUa)||/Android|Mobile|CrOS|Linux/i.test(_rawUa)){'
        'const _cm=_rawUa.match(/Chrome\\/([\\d.]+)/);'
        'const _cv=_cm?_cm[1]:"131.0.0.0";'
        '_tvUa=`Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/${_cv} Safari/537.36`;'
        '_tvPlat="Win32";_tvChPlat="Windows";'
        '}'
        'if(_np&&_tvUa!==navigator.userAgent){'
        'Object.defineProperty(_np,"userAgent",{get:()=>_tvUa,enumerable:true,configurable:true});'
        'Object.defineProperty(_np,"appVersion",{get:()=>_tvUa.replace(/^Mozilla\\//,""),enumerable:true,configurable:true});'
        'Object.defineProperty(_np,"platform",{get:()=>_tvPlat,enumerable:true,configurable:true});'
        'if("maxTouchPoints"in _np&&!/MacIntel/.test(_tvPlat)){try{Object.defineProperty(_np,"maxTouchPoints",{get:()=>0,enumerable:true,configurable:true})}catch(_){}}'
        'if("userAgentData"in _np&&navigator.userAgentData){'
        'const _uad=navigator.userAgentData,_brands=_uad.brands||[];'
        'try{Object.defineProperty(_np,"userAgentData",{get:()=>({brands:_brands,mobile:false,platform:_tvChPlat,getHighEntropyValues:async()=>({brands:_brands,mobile:false,platform:_tvChPlat,platformVersion:"15.0.0",architecture:"x86",bitness:"64",model:"",uaFullVersion:"131.0.0.0"}),toJSON:()=>({brands:_brands,mobile:false,platform:_tvChPlat})}),enumerable:true,configurable:true})}catch(_){}'
        '}}}'
        'if(typeof document!=="undefined"){'
        'const _oGei=typeof Document!=="undefined"&&Document.prototype?Document.prototype.getElementById:null;'
        'const _ensureTvAd=()=>{try{const existing=_oGei?_oGei.call(document,"chee4au4yieYohs9"):null;if(!existing){const d=document.createElement("div");d.id="chee4au4yieYohs9";d.style.display="none";(document.body||document.documentElement)?.appendChild(d);}}catch(_){}};'
        '_ensureTvAd();'
        'document.addEventListener("DOMContentLoaded",_ensureTvAd,{once:true});'
        'document.addEventListener("readystatechange",_ensureTvAd);'
        'if(_oGei&&typeof Document!=="undefined"&&Document.prototype){try{Document.prototype.getElementById=function(id){if(id==="chee4au4yieYohs9")_ensureTvAd();return _oGei.call(this,id);};}catch(_){}}'
        'if(typeof Node!=="undefined"&&Node.prototype){'
        'const _isDs=(n)=>{try{if(!n||n.nodeType!==1||String(n.tagName||"").toUpperCase()!=="SCRIPT")return false;const s=String(n.getAttribute?.("src")||n.src||"");return /doubleserve\\.js/i.test(s);}catch(_){return false;}};'
        'const _shimDs=(n)=>{try{_ensureTvAd();n.removeAttribute("src");setTimeout(()=>{try{_ensureTvAd();n.dispatchEvent(new Event("load"));}catch(_){}},0);}catch(_){}};'
        'const _oAp=Node.prototype.appendChild,_oIb=Node.prototype.insertBefore;'
        'if(_oAp){Node.prototype.appendChild=function(c){if(_isDs(c)){_shimDs(c);}return _oAp.call(this,c);};}'
        'if(_oIb){Node.prototype.insertBefore=function(c,r){if(_isDs(c)){_shimDs(c);}return _oIb.call(this,c,r);};}'
        '}}'
        'if(typeof Blob!=="undefined"){'
        'const _OrigBlob=Blob;'
        'const _wShim=`(()=>{try{const _shOrig=(()=>{try{if(self.location&&self.location.origin&&self.location.origin!=="null")return self.location.origin;const m=String(self.location&&self.location.href||"").replace(/^blob:/i,"").match(/^(https?:\\\\/\\\\/[^\\\\/?#]+)/i);return m?m[1]:"";}catch(_){return "";}})();const _fixWUrl=(u)=>{if(typeof u==="string"&&u.startsWith("/")&&!u.startsWith("//")&&_shOrig)return _shOrig+u;return u;};if(typeof window==="undefined"&&typeof self!=="undefined"){if(typeof self.fetch==="function"){const _of=self.fetch.bind(self);self.fetch=globalThis.fetch=function(inp,init){try{if(typeof inp==="string")inp=_fixWUrl(inp);else if(inp&&typeof URL!=="undefined"&&inp instanceof URL)inp=_fixWUrl(inp.href);}catch(_){}return _of(inp,init);};}if(typeof XMLHttpRequest!=="undefined"&&XMLHttpRequest.prototype&&XMLHttpRequest.prototype.open){const _xo=XMLHttpRequest.prototype.open;XMLHttpRequest.prototype.open=function(m,u){try{if(typeof u==="string")arguments[1]=_fixWUrl(u);}catch(_){}return _xo.apply(this,arguments);};}}if(typeof navigator!=="undefined"){if(navigator.webdriver){Object.defineProperty(navigator,"webdriver",{get:()=>false,configurable:true});}const _ru=String(navigator.userAgent||"").replace(/HeadlessChrome\\\\//g,"Chrome/");if(_ru!==navigator.userAgent||/CrOS|Linux|Android|iPhone|iPad/i.test(_ru)){const _cm=_ru.match(/Chrome\\\\/([\\\\d.]+)/);const _u=/(iPhone|iPad|iPod)/i.test(_ru)&&!/Chrome\\\\//i.test(_ru)?"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15":("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/"+(_cm?_cm[1]:"131.0.0.0")+" Safari/537.36");Object.defineProperty(navigator,"userAgent",{get:()=>_u,configurable:true});Object.defineProperty(navigator,"appVersion",{get:()=>_u.replace(/^Mozilla\\\\//,""),configurable:true});}}if(typeof MessageEvent!=="undefined"){const _d=Object.getOwnPropertyDescriptor(MessageEvent.prototype,"data");if(_d&&_d.get){Object.defineProperty(MessageEvent.prototype,"data",{get(){const v=_d.get.call(this);return(v&&typeof v==="object"&&"$scramjet$data"in v)?v.$scramjet$data:v;},configurable:true});}}if(typeof MessagePort!=="undefined"&&MessagePort.prototype.postMessage){const _mp=MessagePort.prototype.postMessage;MessagePort.prototype.postMessage=function(d,t){return arguments.length>1?_mp.call(this,{$scramjet$messagetype:"worker",$scramjet$data:d},t):_mp.call(this,{$scramjet$messagetype:"worker",$scramjet$data:d});};}if(typeof self!=="undefined"&&typeof self.postMessage==="function"&&typeof window==="undefined"){const _sp=self.postMessage.bind(self);self.postMessage=globalThis.postMessage=function(d,t){return arguments.length>1?_sp({$scramjet$messagetype:"worker",$scramjet$data:d},t):_sp({$scramjet$messagetype:"worker",$scramjet$data:d});};}}catch(_){}})();\\n`;'
        'globalThis.Blob=new Proxy(_OrigBlob,{construct(target,args,newTarget){try{const parts=args[0],opts=args[1];if(Array.isArray(parts)&&parts.length>0&&typeof parts[0]==="string"&&(String(opts?.type||"").includes("javascript")||parts[0].includes("onmessage")||parts[0].includes("onconnect"))){args=[[_wShim,...parts],opts];}}catch(_){}return Reflect.construct(target,args,newTarget);}});'
        '}}catch(_){}\n'
    )
    if not text.startswith("try{if(typeof window") and not text.startswith("try{if(typeof navigator"):
        text = preamble + text
    return text.encode("utf-8")


_PATCHED_RUNTIME_CACHE: dict[str, bytes] = {}
_PATCHED_RUNTIME_GZIP_CACHE: dict[str, bytes] = {}


async def _ensure_patched_runtime(asset_name: str, patcher) -> tuple[bytes, bytes] | None:
    cached = _PATCHED_RUNTIME_CACHE.get(asset_name)
    cached_gz = _PATCHED_RUNTIME_GZIP_CACHE.get(asset_name)
    if cached is not None and cached_gz is not None:
        return cached, cached_gz
    import app as host_app

    resp = await host_app.scramjet_asset(asset_name)
    if getattr(resp, "status_code", 500) != 200 or not getattr(resp, "body", b""):
        return None
    patched = patcher(bytes(resp.body))
    gz = gzip.compress(patched, compresslevel=6)
    _PATCHED_RUNTIME_CACHE[asset_name] = patched
    _PATCHED_RUNTIME_GZIP_CACHE[asset_name] = gz
    return patched, gz


async def _serve_patched_runtime(asset_name: str, patcher, request: Request) -> Response:
    pair = await _ensure_patched_runtime(asset_name, patcher)
    if pair is None:
        import app as host_app

        return await host_app.scramjet_asset(asset_name)
    patched, gz = pair
    ae = (request.headers.get("accept-encoding") or "").lower()
    if "gzip" in ae:
        return Response(
            gz,
            media_type="application/javascript",
            headers={
                "Cache-Control": "public, max-age=86400, immutable",
                "Content-Encoding": "gzip",
                "Vary": "Accept-Encoding",
            },
        )
    return Response(
        patched,
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )


@router.get("/tver/runtime/scramjet.js", include_in_schema=False)
async def tver_scramjet_js(request: Request):
    return await _serve_patched_runtime("scramjet.js", _patch_scramjet_js, request)


@router.get("/tver/runtime/controller.api.js", include_in_schema=False)
async def tver_controller_api(request: Request):
    return await _serve_patched_runtime("controller.api.js", _patch_controller_api, request)


@router.get("/tver/runtime/controller.sw.js", include_in_schema=False)
async def tver_controller_sw(request: Request):
    return await _serve_patched_runtime("controller.sw.js", _patch_controller_sw, request)


@router.get("/tver/runtime/controller.inject.js", include_in_schema=False)
async def tver_controller_inject(request: Request):
    return await _serve_patched_runtime("controller.inject.js", _patch_controller_inject, request)


async def _tver_sw_override(request: Request) -> Response:
    code = f"""
importScripts('/tver/runtime/controller.sw.js?v={RUNTIME_REV}');
const TVER_MEDIA_CACHE = 'sh-tver-media-{RUNTIME_REV}';
const TVER_REWRITTEN_CACHE = 'sh-tver-rewritten-{RUNTIME_REV}';
const EMPTY_VAST_XML = '<?xml version="1.0" encoding="UTF-8"?><VAST version="3.0"/>';
const EMPTY_AFFINITY_JSON = '{{"api_version":"","code":0,"message":"","type":"hash","result":{{"exists":false,"customParams":""}}}}';
const DOUBLESERVE_JS = '(()=>{{try{{var d=document,add=function(){{if(!d.getElementById("chee4au4yieYohs9")){{var e=d.createElement("div");e.id="chee4au4yieYohs9";e.style.display="none";(d.body||d.documentElement).appendChild(e);}}}};add();if(!d.body)d.addEventListener("DOMContentLoaded",add,{{once:true}});}}catch(_){{}}}})();';
const blockedDirectHosts = new Set();
function fetchWithFastTimeout(url, opts, ms) {{
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), ms);
  return fetch(url, Object.assign({{}}, opts, {{ signal: ac.signal }})).finally(() => clearTimeout(timer));
}}
const MAX_CONCURRENT_IMAGE_FETCHES = 4;
let _activeImageFetches = 0;
const _imageFetchWaiters = [];
function acquireImageSlot() {{
  if (_activeImageFetches < MAX_CONCURRENT_IMAGE_FETCHES) {{
    _activeImageFetches += 1;
    return Promise.resolve();
  }}
  return new Promise(resolve => _imageFetchWaiters.push(resolve));
}}
function releaseImageSlot() {{
  const next = _imageFetchWaiters.shift();
  if (next) {{
    next();
  }} else if (_activeImageFetches > 0) {{
    _activeImageFetches -= 1;
  }}
}}
async function withImageSlot(fn) {{
  await acquireImageSlot();
  try {{
    return await fn();
  }} finally {{
    releaseImageSlot();
  }}
}}
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
function isStreamCdnTarget(target) {{
  if (!target) return false;
  const h = String(target.hostname || '').toLowerCase().replace(/\\.$/, '');
  const p = String(target.pathname || '');
  if (/\\.(js|mjs|cjs|json|css|html|wasm)(\\?|$)/i.test(p)) return false;
  if (h.endsWith('.api.streaks.jp')) return false;
  if (/\\.(m3u8|ts|m4s|cmfv|cmfa|aac|vtt|webvtt|key)(\\?|$)/i.test(p)) return true;
  if ((
      h.endsWith('.tver.jp') || h.endsWith('.cloudfront.net') || h.endsWith('.akamaized.net') ||
      h.endsWith('.akamaihd.net') || h.endsWith('.fastly.net') || h.endsWith('.boltdns.net') ||
      h.endsWith('.brightcove.com') || h.endsWith('.brightcove.net') || h.endsWith('.streaks.jp') ||
      h.endsWith('.uliza.jp') || h.endsWith('.stream.ne.jp') || h.endsWith('.jstream.jp') ||
      h.endsWith('.llnwd.net') || h.endsWith('.edgekey.net') || h.endsWith('.edgesuite.net') ||
      h.endsWith('.cdngc.net') || h.endsWith('.footprint.net') || h === 'localhost' || h === '127.0.0.1'
  ) && /\\.(mp4|m4a|m4v)(\\?|$)/i.test(p)) {{
    return true;
  }}
  return false;
}}
function isDirectMediaTarget(target) {{
  if (!target) return false;
  const h = String(target.hostname || '').toLowerCase().replace(/\\.$/, '');
  const p = String(target.pathname || '');
  if (/\\.(js|mjs|cjs|json|css|html|wasm)(\\?|$)/i.test(p)) return false;
  if (h === 'image-cdn.tver.jp') return true;
  if (h === 'statics.tver.jp' && /\\.(jpg|jpeg|png|webp|gif|svg|avif|ico)(\\?|$)/i.test(p)) return true;
  return isStreamCdnTarget(target);
}}
function isCacheableImageTarget(target) {{
  if (!target) return false;
  const h = String(target.hostname || '').toLowerCase().replace(/\\.$/, '');
  const p = String(target.pathname || '');
  return h === 'image-cdn.tver.jp' || (h === 'statics.tver.jp' && /\\.(jpg|jpeg|png|webp|gif|svg|avif|ico)(\\?|$)/i.test(p));
}}
function isImmutableRewrittenTarget(target, req) {{
  if (!target || !req || req.method !== 'GET' || req.headers.get('range')) return false;
  const dest = String(req.destination || '');
  if (dest === 'document' || dest === 'iframe') return false;
  const h = String(target.hostname || '').toLowerCase().replace(/\\.$/, '');
  const p = String(target.pathname || '');
  if ((h === 'tver.jp' || h === 'www.tver.jp') && p.startsWith('/_next/static/') && /\\.(js|mjs|css)(\\?|$)/i.test(p)) return true;
  if (h === 's.tver.jp' && /^\\/(psdk|appsdk)\\.js$/i.test(p)) return true;
  if (h === 'imasdk.googleapis.com' && p === '/js/sdkloader/ima3.js') return true;
  return false;
}}
function rewriteM3u8Text(text, manifestUrl, forNative) {{
  const baseOrigin = String(self.location.origin || '').replace(/\\/+$/, '');
  const toTarget = (rawRef) => {{
    const cleanRef = String(rawRef || '').trim();
    if (!cleanRef || /^(data:|skd:|blob:)/i.test(cleanRef)) return rawRef;
    let absHref = '';
    try {{
      absHref = new URL(cleanRef, manifestUrl).href;
    }} catch {{
      return rawRef;
    }}
    if (!/^https?:\\/\\//i.test(absHref)) return rawRef;
    if (forNative) {{
      return baseOrigin + '/tver/runtime/media-relay?native=1&k=' + encodeMediaKey(absHref);
    }}
    return baseOrigin + '/tver/runtime/media-relay?k=' + encodeMediaKey(absHref);
  }};
  const withUris = String(text || '').replace(/URI="([^"]+)"/g, (_, u) => 'URI="' + toTarget(u) + '"');
  return withUris.split(/\\r?\\n/).map((line) => {{
    const s = line.trim();
    return (s && !s.startsWith('#')) ? toTarget(s) : line;
  }}).join('\\n');
}}
function normalizeMediaTarget(target) {{
  try {{
    for (let k = 0; k < 3; k++) {{
      if (target.pathname === '/tver/runtime/media-relay') {{
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
function stripDollarRaw(s) {{
  return String(s || '').replace(/([?&])\\$[A-Za-z0-9_]+=[^&#]*/g, '$1').replace(/\\?&/g, '?').replace(/[?&]+$/g, '');
}}
function stripScramjetParams(target) {{
  try {{
    const cleaned = stripDollarRaw(target.href);
    if (cleaned !== target.href) target = new URL(cleaned);
    for (const k of [...target.searchParams.keys()]) {{
      if (k.startsWith('$')) target.searchParams.delete(k);
    }}
  }} catch {{}}
  return target;
}}
function extractAnyVirtualUrl(url) {{
  try {{
    const u = new URL(url);
    if (u.pathname === '/tver/runtime/media-relay') {{
      const kv = u.searchParams.get('k');
      const uv = kv ? decodeMediaKey(kv) : u.searchParams.get('url');
      if (uv) return stripScramjetParams(normalizeMediaTarget(new URL(stripDollarRaw(uv))));
    }}
    const marker = '/tver/~/sj/';
    const i = u.pathname.indexOf(marker);
    if (i < 0) return null;
    const rawAfter = u.pathname.slice(i + marker.length);
    const directMatch = rawAfter.match(/(?:^|\\/)(https?(?:%3A|:)(?:%2F|\\/){{2}}.*)$/i);
    if (directMatch && directMatch[1]) {{
      try {{
        const decoded = stripDollarRaw(decodeRepeated(directMatch[1]));
        let target = normalizeMediaTarget(new URL(decoded));
        if (u.search) {{
          const extra = new URLSearchParams(u.search);
          for (const [k, v] of extra.entries()) {{
            if (!k.startsWith('$') && !target.searchParams.has(k)) target.searchParams.set(k, v);
          }}
        }}
        target = stripScramjetParams(target);
        target.hash = '';
        return target;
      }} catch {{}}
    }}
    const tail = decodeRepeated(rawAfter);
    const bits = tail.split('/');
    for (let n = Math.min(3, bits.length); n >= 0; n--) {{
      const candidate = stripDollarRaw(decodeRepeated(bits.slice(n).join('/')));
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
  if (h === 'platform-api.tver.jp' && p === '/service/api/v1/callAffinity') {{
    return new Response(EMPTY_AFFINITY_JSON, {{
      status: 200,
      headers: {{
        'Content-Type': 'application/json; charset=utf-8',
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Credentials': 'true',
      }},
    }});
  }}
  if (((h === 'tver.jp' || h === 'www.tver.jp') && p === '/doubleserve.js') || (h === 's.tver.jp' && p === '/ads/doubleserve.js')) {{
    return new Response(DOUBLESERVE_JS, {{
      status: 200,
      headers: {{
        'Content-Type': 'application/javascript; charset=utf-8',
        'Access-Control-Allow-Origin': '*',
      }},
    }});
  }}
  if (
    (h === 'adsvr.tver.jp' && p.startsWith('/ad-server')) ||
    ((h === 'pubads.g.doubleclick.net' || h === 'securepubads.g.doubleclick.net') && (p.includes('/gampad/ads') || p.includes('/pagead/ads')))
  ) {{
    return new Response(EMPTY_VAST_XML, {{
      status: 200,
      headers: {{
        'Content-Type': 'application/xml; charset=utf-8',
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Credentials': 'true',
      }},
    }});
  }}
  if (
    h === 'www.googletagmanager.com' || h === 'googletagmanager.com' ||
    ((h === 'imasdk.googleapis.com' || h === 'pagead2.googlesyndication.com' || h === 'securepubads.g.doubleclick.net') &&
      (p.includes('omweb-v1.js') || p.includes('omsdk-v1.js') || p.includes('/tag/js/gpt.js') || p.includes('/gpt/pubads_impl')))
  ) {{
    return new Response('window.dataLayer=window.dataLayer||[];window.googletag=window.googletag||{{cmd:[]}};', {{
      status: 200,
      headers: {{
        'Content-Type': 'application/javascript; charset=utf-8',
        'Access-Control-Allow-Origin': '*',
      }},
    }});
  }}
  if (
    h === 'm-tag.tver.jp' || h === 's-tag.tver.jp' || h === 'ce-api.tver.jp' || h === 'log.tver.jp' ||
    h === 'js-agent.newrelic.com' || h === 'bam.nr-data.net' || h.endsWith('.nr-data.net') || h.endsWith('.newrelic.com') ||
    h === 'analytics.tiktok.com' || h === 'analytics.twitter.com' ||
    h === 'ads-twitter.com' || h.endsWith('.ads-twitter.com') ||
    h === 'b99.yahoo.co.jp' || h === 'yjtag.yahoo.co.jp' || h === 'yads.c.yimg.jp' || h === 's.yimg.jp' ||
    h === 'dmp.im-apps.net' || h === 'connect.facebook.net' ||
    (h === 'www.facebook.com' && p.startsWith('/tr')) ||
    h === 'gum.criteo.com' || h === 'static.criteo.net' || h === 'bidder.criteo.com' ||
    h === 'cnt.fout.jp' || h === 'sync.fout.jp' || h === 'js.fout.jp' || h.endsWith('.fout.jp') ||
    h === 'tg.socdm.com' || h.endsWith('.socdm.com') ||
    h.endsWith('.adtrafficquality.google') || h === 's0.2mdn.net' ||
    h === 'pagead2.googlesyndication.com' || h === 'tpc.googlesyndication.com' ||
    h === 'www.google-analytics.com' || h === 'region1.google-analytics.com' ||
    h === 'analytics.google.com' || h === 'stats.g.doubleclick.net' ||
    h.endsWith('.karte.io') || h.endsWith('.braze.com') ||
    h === 'js-sec.indexww.com' || h === 'c.amazon-adsystem.com' || h.endsWith('.amazon-adsystem.com')
  ) {{
    const isJs = /\\.js(\\?|$)/i.test(p) || p.includes('/js');
    return new Response(isJs ? '' : '{{}}', {{
      status: 200,
      headers: {{
        'Content-Type': isJs ? 'application/javascript; charset=utf-8' : 'application/json',
        'Access-Control-Allow-Origin': '*',
      }},
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
  const resp = await fetch('/tver/runtime/http-relay', {{
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
    if (reqUrlObj.origin !== self.location.origin) {{
      return;
    }}
    if (reqUrlObj.searchParams.get('via') === 'server') {{
      return;
    }}
    if ((reqUrlObj.pathname.startsWith('/tver/runtime/') && reqUrlObj.pathname !== '/tver/runtime/media-relay') || reqUrlObj.pathname.startsWith('/_diag/')) {{
      return;
    }}
    const sjIdx = reqUrlObj.pathname.indexOf('/tver/~/sj/');
    if (sjIdx >= 0) {{
      const rawTail = reqUrlObj.pathname.slice(sjIdx + 11);
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
      const isAlreadyRelay = reqUrlObj.pathname === '/tver/runtime/media-relay' && Boolean(anyTarget);
      const asOut = (r, m) => (isAlreadyRelay && m === 'GET' && ![101, 204, 205, 304].includes(r.status))
        ? r
        : new Response((m === 'HEAD' || [101, 204, 205, 304].includes(r.status)) ? null : r.body, {{
            status: r.status,
            statusText: r.statusText,
            headers: r.headers,
          }});
      if (anyTarget && (isAlreadyRelay || isDirectMediaTarget(anyTarget)) && event.request.destination !== 'script' && event.request.destination !== 'worker' && event.request.destination !== 'sharedworker' && event.request.destination !== 'style' && !event.request.url.includes('%24module=module') && !event.request.url.includes('$module=module')) {{
        const forNative = reqUrlObj.searchParams.get('native') === '1' || event.request.destination === 'video' || event.request.destination === 'audio';
        const targetHost = String(anyTarget.hostname || '').toLowerCase().replace(/\\.$/, '');
        const isTverDomain = targetHost === 'tver.jp' || targetHost.endsWith('.tver.jp');
        const relay = new URL('/tver/runtime/media-relay', self.location.origin);
        relay.searchParams.set('k', encodeMediaKey(anyTarget.href));
        relay.searchParams.set('via', 'server');
        if (forNative) relay.searchParams.set('native', '1');
        const method = event.request.method;
        const rng = event.request.headers.get('range');
        const acc = event.request.headers.get('accept');
        const isImg = isCacheableImageTarget(anyTarget);
        const canCache = method === 'GET' && !rng && isImg;
        const isM3u8Path = /\\.m3u8(\\?|$)/i.test(anyTarget.pathname || '');
        const fwdHeaders = {{}};
        if (rng && !isM3u8Path) fwdHeaders['Range'] = rng;
        if (acc) fwdHeaders['Accept'] = acc;
        event.respondWith((async () => {{
          let cache = null;
          if (canCache && typeof caches !== 'undefined') {{
            try {{
              cache = await caches.open(TVER_MEDIA_CACHE);
              const hit = await cache.match(relay.href);
              if (hit) return asOut(hit, method);
            }} catch {{}}
          }}
          if (!isTverDomain && !canCache && !blockedDirectHosts.has(targetHost)) {{
            try {{
              const cdnResp = await fetchWithFastTimeout(anyTarget.href, {{
                method,
                headers: fwdHeaders,
                mode: 'cors',
                credentials: 'omit',
              }}, 2500);
              if (cdnResp && (cdnResp.ok || cdnResp.status === 206)) {{
                const ct = String(cdnResp.headers.get('content-type') || '').toLowerCase();
                if (ct.includes('text/html') || ct.includes('application/xhtml')) {{
                  blockedDirectHosts.add(targetHost);
                }} else if ((isM3u8Path || ct.includes('mpegurl')) && method === 'GET') {{
                  const rawTxt = await cdnResp.text();
                  if (!rawTxt.trimStart().startsWith('#EXTM3U')) {{
                    blockedDirectHosts.add(targetHost);
                  }} else {{
                    const rewritten = rewriteM3u8Text(rawTxt, cdnResp.url || anyTarget.href, forNative);
                    return new Response(rewritten, {{
                      status: 200,
                      headers: {{
                        'Content-Type': 'application/vnd.apple.mpegurl',
                        'Access-Control-Allow-Origin': '*',
                        'Cross-Origin-Resource-Policy': 'cross-origin',
                        'Cache-Control': 'no-cache',
                      }},
                    }});
                  }}
                }} else {{
                  const hCopy = new Headers(cdnResp.headers);
                  hCopy.set('Access-Control-Allow-Origin', '*');
                  hCopy.set('Cross-Origin-Resource-Policy', 'cross-origin');
                  return new Response(method === 'HEAD' ? null : cdnResp.body, {{
                    status: cdnResp.status,
                    statusText: cdnResp.statusText,
                    headers: hCopy,
                  }});
                }}
              }} else {{
                blockedDirectHosts.add(targetHost);
              }}
            }} catch {{
              blockedDirectHosts.add(targetHost);
            }}
          }}
          const fetchRelayAsset = async () => {{
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
          }};
          return isImg ? await withImageSlot(fetchRelayAsset) : await fetchRelayAsset();
        }})());
        return;
      }}
    }}
    if (typeof $scramjetController !== 'undefined' && $scramjetController.shouldRoute(event)) {{
      if (anyTarget && isImmutableRewrittenTarget(anyTarget, event.request) && typeof caches !== 'undefined') {{
        const cacheKey = reqUrlObj.origin + reqUrlObj.pathname + reqUrlObj.search;
        event.respondWith((async () => {{
          let rwCache = null;
          try {{
            rwCache = await caches.open(TVER_REWRITTEN_CACHE);
            const hit = await rwCache.match(cacheKey);
            if (hit) return hit;
          }} catch {{}}
          const routed = await $scramjetController.route(event);
          if (rwCache && routed && routed.status === 200) {{
            try {{ await rwCache.put(cacheKey, routed.clone()); }} catch {{}}
          }}
          return routed;
        }})());
        return;
      }}
      event.respondWith($scramjetController.route(event));
      return;
    }}
  }} catch (error) {{
    console.error('[SchoolHub isolated SW:tver]', error);
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
            "Service-Worker-Allowed": "/tver/",
        },
    )


# ---------------------------------------------------------------------------
# Fast HTTP/2 + Chrome-TLS Relay & HLS Media Relay for TVer
# ---------------------------------------------------------------------------
_CURL_SESSIONS: dict[str, StatelessCurlSession] = {}
_CURL_SESSION_LOCK = asyncio.Lock()
_CURL_DISABLED_UNTIL: float = 0.0
_RELAY_HTTP_CLIENT: httpx.AsyncClient | None = None
_RELAY_HTTP_CLIENT_LOCK = asyncio.Lock()
_MEDIA_HTTP_CLIENT: httpx.AsyncClient | None = None
_MEDIA_HTTP_CLIENT_LOCK = asyncio.Lock()
_IMAGE_CACHE: dict[str, tuple[float, int, str, bytes, str]] = {}
_IMAGE_CACHE_TTL = 3600.0
_IMAGE_CACHE_MAX_ITEMS = 1024
_IMAGE_CACHE_MAX_BYTES = 1024 * 1024
_STATIC_ASSET_CACHE: dict[str, tuple[float, int, str, list[list[str]], bytes]] = {}
_STATIC_ASSET_TTL = 3600.0
_STATIC_ASSET_MAX_ITEMS = 1024
_STATIC_ASSET_MAX_BYTES = 6 * 1024 * 1024
_PUBLIC_API_CACHE: dict[str, tuple[float, float, int, str, list[list[str]], bytes]] = {}
_PUBLIC_API_MAX_ITEMS = 64
_PUBLIC_API_INFLIGHT: dict[str, asyncio.Future] = {}

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
    "cross-origin-opener-policy",
    "cross-origin-embedder-policy",
    "cross-origin-resource-policy",
}

_DESKTOP_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


async def _get_curl_session(impersonate_profile: str = "chrome131"):
    if StatelessCurlSession is None or time.time() < _CURL_DISABLED_UNTIL:
        return None
    profile = impersonate_profile or "chrome131"
    existing = _CURL_SESSIONS.get(profile)
    if existing is not None:
        return existing
    async with _CURL_SESSION_LOCK:
        existing = _CURL_SESSIONS.get(profile)
        if existing is None:
            existing = StatelessCurlSession(
                impersonate=profile,
                default_headers=False,
                allow_redirects=False,
                timeout=8,
                max_clients=32,
            )
            _CURL_SESSIONS[profile] = existing
    return existing


async def _get_relay_http_client() -> httpx.AsyncClient:
    """Fast persistent non-redirecting HTTP client for TVer SSR HTML, Next.js chunks, and APIs."""
    global _RELAY_HTTP_CLIENT
    if _RELAY_HTTP_CLIENT is not None and not _RELAY_HTTP_CLIENT.is_closed:
        return _RELAY_HTTP_CLIENT
    async with _RELAY_HTTP_CLIENT_LOCK:
        if _RELAY_HTTP_CLIENT is None or _RELAY_HTTP_CLIENT.is_closed:
            limits = httpx.Limits(max_connections=160, max_keepalive_connections=80)
            timeout = httpx.Timeout(connect=6.0, read=22.0, write=10.0, pool=8.0)
            if TVER_UPSTREAM_PROXY:
                _RELAY_HTTP_CLIENT = httpx.AsyncClient(
                    proxy=TVER_UPSTREAM_PROXY,
                    follow_redirects=False,
                    limits=limits,
                    timeout=timeout,
                )
            elif not (os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or os.getenv("https_proxy") or os.getenv("http_proxy")):
                transport = httpx.AsyncHTTPTransport(
                    retries=1,
                    limits=limits,
                )
                _RELAY_HTTP_CLIENT = httpx.AsyncClient(
                    transport=transport,
                    follow_redirects=False,
                    timeout=timeout,
                )
            else:
                _RELAY_HTTP_CLIENT = httpx.AsyncClient(
                    follow_redirects=False,
                    limits=limits,
                    timeout=timeout,
                )
    return _RELAY_HTTP_CLIENT


async def _get_media_http_client() -> httpx.AsyncClient:
    """Shared cookieless persistent HTTP client for TVer thumbnails and HLS segments."""
    global _MEDIA_HTTP_CLIENT
    if _MEDIA_HTTP_CLIENT is not None and not _MEDIA_HTTP_CLIENT.is_closed:
        return _MEDIA_HTTP_CLIENT
    async with _MEDIA_HTTP_CLIENT_LOCK:
        if _MEDIA_HTTP_CLIENT is None or _MEDIA_HTTP_CLIENT.is_closed:
            limits = httpx.Limits(max_connections=120, max_keepalive_connections=60)
            timeout = httpx.Timeout(connect=8.0, read=45.0, write=10.0, pool=10.0)
            if TVER_UPSTREAM_PROXY:
                _MEDIA_HTTP_CLIENT = httpx.AsyncClient(
                    proxy=TVER_UPSTREAM_PROXY,
                    follow_redirects=True,
                    limits=limits,
                    timeout=timeout,
                )
            elif not (os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or os.getenv("https_proxy") or os.getenv("http_proxy")):
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
        "tver.jp",
        "tver.co.jp",
        "streaks.jp",
        "brightcove.com",
        "brightcove.net",
        "boltdns.net",
        "cloudfront.net",
        "akamaized.net",
        "akamaihd.net",
        "fastly.net",
        "interactive-circle.jp",
        "google.com",
        "gstatic.com",
        "googleapis.com",
        "googleusercontent.com",
        "googlesyndication.com",
        "doubleclick.net",
        "apple.com",
        "cdn-apple.com",
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
        host = (parsed.hostname or "").lower().rstrip(".")
        path = parsed.path or ""
        if host == "imasdk.googleapis.com" and (
            path == "/js/sdkloader/ima3.js"
            or (path.startswith("/js/core/bridge3") and path.endswith(".html"))
        ):
            return True
        if host in ("tver.jp", "www.tver.jp", "s.tver.jp", "player.tver.jp", "statics.tver.jp"):
            if (
                path.startswith("/_next/static/")
                or path.startswith("/streaksplayer/")
                or path.startswith("/player/")
                or path in (
                    "/config/streaks_info_v2.json",
                    "/player/streaks_info_v2.json",
                    "/player/tver_thv_config2.json",
                    "/app/config/web.json",
                    "/setting.js",
                    "/setting.json",
                    "/data/string/s.json",
                )
                or (host == "s.tver.jp" and path.endswith("/psdkplf.html"))
                or path.endswith((".js", ".css", ".woff2", ".woff", ".ttf", ".svg", ".png", ".jpg", ".webp", ".ico"))
            ):
                return True
    except Exception:
        pass
    return False


def _public_api_cache_spec(method: str, url: str, has_auth: bool = False) -> tuple[str, float]:
    """Return (normalized_cache_key, ttl_seconds) for public, user-independent TVer APIs."""
    if method != "GET" or has_auth:
        return ("", 0.0)
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        path = parsed.path or ""
        if host == "contents-api.tver.jp" and path in (
            "/contents/api/v1/callHome",
            "/contents/api/v1/news/latest",
            "/contents/api/v1/news",
        ):
            # Strip per-browser platform_uid / platform_token so all visitors share the public catalog cache
            if parsed.query:
                kept = [
                    p
                    for p in parsed.query.split("&")
                    if p and p.split("=", 1)[0] not in ("platform_uid", "platform_token")
                ]
                norm = urlunparse(("https", host, path, "", "&".join(kept), ""))
            else:
                norm = f"https://{host}{path}"
            return (f"pubapi:{norm}", 90.0)
        if host == "service-api.tver.jp" and path == "/api/v1/callHeader":
            return (f"pubapi:https://{host}{path}", 90.0)
        if host == "platform-api.tver.jp" and path == "/v2/api/public/current_time":
            return (f"pubapi:https://{host}{path}", 4.0)
    except Exception:
        pass
    return ("", 0.0)


_PACKED_RELAY_FRAMES: dict[str, tuple[float, bytes, bytes | None]] = {}


def _prebuild_packed_relay_frame(
    cache_key: str,
    status: int,
    status_text: str,
    headers_list: list[list[str]],
    body: bytes,
) -> None:
    if not cache_key:
        return
    try:
        meta_bytes = json.dumps(
            {
                "status": status,
                "statusText": status_text,
                "headers": headers_list,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        raw_frame = struct.pack(">I", len(meta_bytes)) + meta_bytes + body
        if len(raw_frame) > _STATIC_ASSET_MAX_BYTES + 8192:
            return
        gz_frame = gzip.compress(raw_frame, compresslevel=5) if len(raw_frame) >= 1024 else None
        if len(_PACKED_RELAY_FRAMES) >= _STATIC_ASSET_MAX_ITEMS:
            _PACKED_RELAY_FRAMES.pop(next(iter(_PACKED_RELAY_FRAMES)), None)
        _PACKED_RELAY_FRAMES[cache_key] = (time.time(), raw_frame, gz_frame)
    except Exception:
        pass


def _pack_relay_response(
    status: int,
    status_text: str,
    headers_list: list[list[str]],
    body: bytes,
    request: Request | None = None,
    cache_key: str = "",
    cache_ttl: float = 0.0,
) -> Response:
    resp_headers = {
        "Cache-Control": "private, no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
        "Vary": "Accept-Encoding",
    }
    wants_gzip = (
        request is not None
        and "gzip" in (request.headers.get("accept-encoding") or "").lower()
    )
    if cache_key:
        now = time.time()
        ttl = cache_ttl if cache_ttl > 0 else (300.0 if cache_key.endswith(("/setting.js", "/setting.json")) else _STATIC_ASSET_TTL)
        packed = _PACKED_RELAY_FRAMES.get(cache_key)
        if packed and (now - packed[0]) < ttl:
            _, raw_frame, gz_frame = packed
            if wants_gzip and gz_frame is not None:
                resp_headers["Content-Encoding"] = "gzip"
                return Response(gz_frame, media_type="application/octet-stream", headers=resp_headers)
            return Response(raw_frame, media_type="application/octet-stream", headers=resp_headers)

    meta_bytes = json.dumps(
        {
            "status": status,
            "statusText": status_text,
            "headers": headers_list,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    raw_frame = struct.pack(">I", len(meta_bytes)) + meta_bytes + body
    gz_frame: bytes | None = None
    if len(raw_frame) >= 1024 and (wants_gzip or cache_key):
        gz_frame = gzip.compress(raw_frame, compresslevel=5)
    if cache_key and len(raw_frame) <= _STATIC_ASSET_MAX_BYTES + 8192:
        if len(_PACKED_RELAY_FRAMES) >= _STATIC_ASSET_MAX_ITEMS:
            _PACKED_RELAY_FRAMES.pop(next(iter(_PACKED_RELAY_FRAMES)), None)
        _PACKED_RELAY_FRAMES[cache_key] = (time.time(), raw_frame, gz_frame)
    if wants_gzip and gz_frame is not None:
        resp_headers["Content-Encoding"] = "gzip"
        return Response(gz_frame, media_type="application/octet-stream", headers=resp_headers)
    return Response(
        raw_frame,
        media_type="application/octet-stream",
        headers=resp_headers,
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


def _rewrite_hls_manifest(
    body: bytes,
    manifest_url: str,
    *,
    for_native_player: bool = False,
    proxy_origin: str = "",
) -> bytes:
    """Resolve relative HLS .m3u8 URIs to full-origin /tver/runtime/media-relay?k=... URLs
    for Video.js/MSE (including inside blob: Workers), or native=1&via=server for native <video>."""
    try:
        text = body.decode("utf-8")
    except Exception:
        return body

    origin_prefix = proxy_origin.rstrip("/")

    def _to_target(raw_ref: str) -> str:
        clean_ref = raw_ref.strip()
        if not clean_ref or clean_ref.lower().startswith(("data:", "skd:", "blob:")):
            return raw_ref
        abs_url = urljoin(manifest_url, clean_ref)
        if not abs_url.lower().startswith(("http://", "https://")):
            return raw_ref
        if for_native_player:
            return f"{origin_prefix}/tver/runtime/media-relay?native=1&via=server&k={_encode_media_key(abs_url)}"
        if origin_prefix:
            return f"{origin_prefix}/tver/runtime/media-relay?k={_encode_media_key(abs_url)}"
        return abs_url

    text = re.sub(
        r'URI="([^"]+)"',
        lambda m: f'URI="{_to_target(m.group(1))}"',
        text,
    )
    out_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            out_lines.append(_to_target(stripped))
        else:
            out_lines.append(line)
    return "\n".join(out_lines).encode("utf-8")


_EMBEDDED_DOLLAR_RE = re.compile(r"([?&])\$[A-Za-z0-9_]+=[^&#]*")


def _strip_dollar_query(raw_url: str) -> str:
    if "$" not in raw_url:
        return raw_url
    cleaned = _EMBEDDED_DOLLAR_RE.sub(r"\1", raw_url).replace("?&", "?").rstrip("?&")
    if "?" not in cleaned:
        return cleaned
    try:
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


async def _serve_tver_media_url(
    raw_url: str, request: Request, *, force_native: bool = False
) -> Response:
    if not raw_url:
        return Response(b"missing url", status_code=400)
    try:
        for _ in range(3):
            p_check = urlparse(raw_url)
            if p_check.path == "/tver/runtime/media-relay":
                qs = parse_qs(p_check.query)
                k_nested = (qs.get("k") or [""])[0]
                nested = _decode_media_key(k_nested) if k_nested else (qs.get("url") or [""])[0]
                if nested:
                    raw_url = nested
                    continue
            break
        raw_url = _strip_dollar_query(raw_url)
        parsed = urlparse(raw_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return Response(b"bad media url", status_code=400)
        host = parsed.hostname.lower().rstrip(".")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if port not in (80, 443):
            return Response(b"unsupported port", status_code=403)
        await _ensure_public_host(host, port)
    except PermissionError:
        return Response(b"disallowed media host", status_code=403)
    except Exception:
        return Response(b"bad media url", status_code=400)

    path_lower = (parsed.path or "").lower()
    is_m3u8 = path_lower.endswith(".m3u8")
    sec_dest = (request.headers.get("sec-fetch-dest") or "").lower()
    ua_lower = (request.headers.get("user-agent") or "").lower()
    for_native_player = (
        force_native
        or request.query_params.get("native") == "1"
        or sec_dest in ("video", "audio")
        or "applecoremedia" in ua_lower
    )
    client_range = request.headers.get("range") if not is_m3u8 else None
    is_static_img_host = host == "image-cdn.tver.jp" or (
        host == "statics.tver.jp"
        and path_lower.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".avif", ".ico"))
    )

    now = time.time()
    if is_static_img_host and not client_range and request.method in ("GET", "HEAD"):
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
        "Accept": request.headers.get("accept")
        or ("image/avif,image/webp,image/apng,image/*,*/*;q=0.8" if is_static_img_host else "*/*"),
        "Accept-Encoding": "identity",
        "Referer": "https://tver.jp/",
        "Origin": "https://tver.jp",
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
            "Cache-Control": resp.headers.get("cache-control") or ("no-cache" if is_m3u8 else "public, max-age=86400"),
            "Accept-Ranges": resp.headers.get("accept-ranges") or "bytes",
        }
        for h_name in ("content-type", "content-length", "content-range", "etag", "last-modified"):
            val = resp.headers.get(h_name)
            if val:
                out_headers[h_name.title()] = val

        media_type = resp.headers.get("content-type") or "application/octet-stream"
        if "mpegurl" in media_type.lower():
            is_m3u8 = True

        if request.method == "HEAD" and not is_m3u8:
            await resp.aclose()
            return Response(b"", status_code=resp.status_code, headers=out_headers, media_type=media_type)

        cl_val = resp.headers.get("content-length") or ""
        if is_m3u8 or is_static_img_host or resp.headers.get("content-encoding") or (cl_val.isdigit() and int(cl_val) <= 1024 * 1024):
            body_bytes = await resp.aread()
            await resp.aclose()
            status_code = resp.status_code
            if is_m3u8 and status_code in (200, 206):
                req_proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "https").split(",")[0].strip()
                req_host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc or "").split(",")[0].strip()
                proxy_origin = f"{req_proto}://{req_host}" if req_host else ""
                body_bytes = _rewrite_hls_manifest(
                    body_bytes,
                    str(resp.url or raw_url),
                    for_native_player=for_native_player,
                    proxy_origin=proxy_origin,
                )
                status_code = 200
                out_headers.pop("Content-Range", None)
                media_type = "application/vnd.apple.mpegurl"
                out_headers["Content-Type"] = media_type
            elif (
                is_static_img_host
                and not client_range
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
            out_body = b"" if request.method == "HEAD" else body_bytes
            out_headers["Content-Length"] = str(len(out_body))
            return Response(out_body, status_code=status_code, headers=out_headers, media_type=media_type)

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
    "/tver/runtime/media-relay",
    methods=["GET", "HEAD"],
    include_in_schema=False,
)
async def tver_media_relay(request: Request):
    k_val = (request.query_params.get("k") or "").strip()
    raw_url = _decode_media_key(k_val) if k_val else (request.query_params.get("url") or "").strip()
    return await _serve_tver_media_url(raw_url, request)


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


_EMPTY_VAST_BYTES = b'<?xml version="1.0" encoding="UTF-8"?><VAST version="3.0"/>'
_TAG_STUB_BYTES = b"window.dataLayer=window.dataLayer||[];window.googletag=window.googletag||{cmd:[]};"
_DOUBLESERVE_BYTES = (
    b'(()=>{try{var d=document,add=function(){if(!d.getElementById("chee4au4yieYohs9")){'
    b'var e=d.createElement("div");e.id="chee4au4yieYohs9";e.style.display="none";'
    b'(d.body||d.documentElement).appendChild(e);}};add();'
    b'if(!d.body)d.addEventListener("DOMContentLoaded",add,{once:true});}catch(_){}})();'
)
_EMPTY_AFFINITY_BYTES = (
    b'{"api_version":"","code":0,"message":"","type":"hash","result":{"exists":false,"customParams":""}}'
)
_SHORT_CIRCUIT_HOSTS = frozenset(
    {
        "m-tag.tver.jp",
        "s-tag.tver.jp",
        "ce-api.tver.jp",
        "log.tver.jp",
        "js-agent.newrelic.com",
        "bam.nr-data.net",
        "analytics.tiktok.com",
        "analytics.twitter.com",
        "ads-twitter.com",
        "b99.yahoo.co.jp",
        "yjtag.yahoo.co.jp",
        "yads.c.yimg.jp",
        "s.yimg.jp",
        "dmp.im-apps.net",
        "connect.facebook.net",
        "gum.criteo.com",
        "static.criteo.net",
        "bidder.criteo.com",
        "cnt.fout.jp",
        "sync.fout.jp",
        "sync.rfp.fout.jp",
        "js.fout.jp",
        "tg.socdm.com",
        "s0.2mdn.net",
        "pagead2.googlesyndication.com",
        "tpc.googlesyndication.com",
        "www.google-analytics.com",
        "region1.google-analytics.com",
        "analytics.google.com",
        "stats.g.doubleclick.net",
        "js-sec.indexww.com",
        "c.amazon-adsystem.com",
    }
)


def _check_server_short_circuit(host: str, raw_path: str) -> tuple[int, str, bytes] | None:
    p = raw_path or ""
    pl = p.lower()
    if host == "platform-api.tver.jp" and p == "/service/api/v1/callAffinity":
        return (200, "application/json; charset=utf-8", _EMPTY_AFFINITY_BYTES)
    if (host in ("tver.jp", "www.tver.jp") and pl == "/doubleserve.js") or (
        host == "s.tver.jp" and pl == "/ads/doubleserve.js"
    ):
        return (200, "application/javascript; charset=utf-8", _DOUBLESERVE_BYTES)
    if (host == "adsvr.tver.jp" and pl.startswith("/ad-server")) or (
        host in ("pubads.g.doubleclick.net", "securepubads.g.doubleclick.net")
        and ("/gampad/ads" in pl or "/pagead/ads" in pl)
    ):
        return (200, "application/xml; charset=utf-8", _EMPTY_VAST_BYTES)
    if host in ("www.googletagmanager.com", "googletagmanager.com") or (
        host in ("imasdk.googleapis.com", "pagead2.googlesyndication.com", "securepubads.g.doubleclick.net")
        and (
            "omweb-v1.js" in pl
            or "omsdk-v1.js" in pl
            or "/tag/js/gpt.js" in pl
            or "/gpt/pubads_impl" in pl
        )
    ):
        return (200, "application/javascript; charset=utf-8", _TAG_STUB_BYTES)
    if (
        host in _SHORT_CIRCUIT_HOSTS
        or host.endswith(
            (
                ".ads-twitter.com",
                ".karte.io",
                ".braze.com",
                ".amazon-adsystem.com",
                ".fout.jp",
                ".socdm.com",
                ".adtrafficquality.google",
                ".nr-data.net",
                ".newrelic.com",
            )
        )
        or (host == "www.facebook.com" and pl.startswith("/tr"))
    ):
        if pl.endswith(".js") or "/js" in pl:
            return (200, "application/javascript; charset=utf-8", b"")
        return (200, "application/json", b"{}")
    return None


_GUEST_HTML_CACHE: dict[str, tuple[float, int, str, list[list[str]], bytes]] = {}
_GUEST_HTML_TTL = 45.0
_GUEST_HTML_MAX_ITEMS = 32
_PREWARM_INFLIGHT: set[str] = set()
_PREFETCH_ASSET_SEMAPHORE: asyncio.Semaphore | None = None
_HTML_STATIC_REF_RE = re.compile(
    rb"""(?:href|src)=["'](https://(?:tver\.jp|s\.tver\.jp|statics\.tver\.jp|player\.tver\.jp)/[^"'\s<>]+|/_next/static/[^"'\s<>]+)["']""",
    re.I,
)
_MANIFEST_CHUNK_RE = re.compile(r'"(static/(?:chunks|css)/[^"]+\.(?:js|css))"')


async def _prefetch_single_static_asset(asset_url: str) -> bytes:
    global _PREFETCH_ASSET_SEMAPHORE
    if not _is_cacheable_static_asset("GET", asset_url):
        return b""
    now = time.time()
    ttl = 300.0 if asset_url.endswith(("/setting.js", "/setting.json")) else _STATIC_ASSET_TTL
    cached = _STATIC_ASSET_CACHE.get(asset_url)
    if cached and (now - cached[0]) < ttl:
        return cached[4]
    if _PREFETCH_ASSET_SEMAPHORE is None:
        _PREFETCH_ASSET_SEMAPHORE = asyncio.Semaphore(14)
    async with _PREFETCH_ASSET_SEMAPHORE:
        cached = _STATIC_ASSET_CACHE.get(asset_url)
        if cached and (time.time() - cached[0]) < ttl:
            return cached[4]
        try:
            client = await _get_relay_http_client()
            resp = await client.get(
                asset_url,
                headers={
                    "User-Agent": _DESKTOP_CHROME_UA,
                    "Accept": "*/*",
                    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
                    "Referer": "https://tver.jp/",
                    "Origin": "https://tver.jp",
                },
            )
            if resp.status_code != 200:
                return b""
            body = bytes(resp.content or b"")
            if not (0 < len(body) <= _STATIC_ASSET_MAX_BYTES):
                return b""
            has_sc = False
            out_hdrs: list[list[str]] = []
            for k, v in resp.headers.multi_items():
                kl = k.lower()
                if kl in _RELAY_STRIP_RESP_HEADERS or kl.startswith(":"):
                    continue
                if kl == "set-cookie":
                    has_sc = True
                    break
                out_hdrs.append([k, v])
            if has_sc:
                return b""
            reason = str(resp.reason_phrase or "OK")
            if len(_STATIC_ASSET_CACHE) >= _STATIC_ASSET_MAX_ITEMS:
                _STATIC_ASSET_CACHE.pop(next(iter(_STATIC_ASSET_CACHE)), None)
            _STATIC_ASSET_CACHE[asset_url] = (
                time.time(),
                200,
                reason,
                out_hdrs,
                body,
            )
            _prebuild_packed_relay_frame(asset_url, 200, reason, out_hdrs, body)
            return body
        except Exception:
            return b""


async def _prefetch_public_api(api_url: str) -> bytes:
    cache_key, ttl = _public_api_cache_spec("GET", api_url, False)
    if not cache_key or ttl <= 0:
        return b""
    now = time.time()
    cached = _PUBLIC_API_CACHE.get(cache_key)
    if cached and (now - cached[0]) < cached[1]:
        return cached[5]
    try:
        client = await _get_relay_http_client()
        resp = await client.get(
            api_url,
            headers={
                "User-Agent": _DESKTOP_CHROME_UA,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
                "x-tver-platform-type": "web",
                "Referer": "https://tver.jp/",
                "Origin": "https://tver.jp",
            },
        )
        if resp.status_code != 200:
            return b""
        body = bytes(resp.content or b"")
        if not (0 < len(body) <= _STATIC_ASSET_MAX_BYTES):
            return b""
        out_hdrs: list[list[str]] = []
        for k, v in resp.headers.multi_items():
            kl = k.lower()
            if kl in _RELAY_STRIP_RESP_HEADERS or kl == "set-cookie" or kl.startswith(":"):
                continue
            out_hdrs.append([k, v])
        reason = str(resp.reason_phrase or "OK")
        if len(_PUBLIC_API_CACHE) >= _PUBLIC_API_MAX_ITEMS:
            _PUBLIC_API_CACHE.pop(next(iter(_PUBLIC_API_CACHE)), None)
        _PUBLIC_API_CACHE[cache_key] = (time.time(), ttl, 200, reason, out_hdrs, body)
        _prebuild_packed_relay_frame(cache_key, 200, reason, out_hdrs, body)
        return body
    except Exception:
        return b""


async def _prewarm_single_image(img_url: str) -> None:
    if not img_url or img_url in _IMAGE_CACHE:
        return
    try:
        client = await _get_media_http_client()
        resp = await client.get(
            img_url,
            headers={
                "User-Agent": _DESKTOP_CHROME_UA,
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                "Accept-Encoding": "identity",
                "Referer": "https://tver.jp/",
                "Origin": "https://tver.jp",
            },
        )
        if resp.status_code != 200:
            return
        body = bytes(resp.content or b"")
        if not (0 < len(body) <= _IMAGE_CACHE_MAX_BYTES):
            return
        if len(_IMAGE_CACHE) >= _IMAGE_CACHE_MAX_ITEMS:
            _IMAGE_CACHE.pop(next(iter(_IMAGE_CACHE)), None)
        _IMAGE_CACHE[img_url] = (
            time.time(),
            200,
            resp.headers.get("content-type") or "image/webp",
            body,
            resp.headers.get("cache-control") or "public, max-age=86400",
        )
    except Exception:
        pass


async def _prewarm_home_thumbnails(home_body: bytes) -> None:
    if not home_body:
        return
    try:
        data = json.loads(home_body.decode("utf-8"))
        comps = (data.get("result") if isinstance(data.get("result"), dict) else data).get("components") or []
        thumb_urls: list[str] = []
        seen: set[str] = set()
        for comp in comps[:6]:
            comp_id = str(comp.get("componentID") or "")
            is_hero = "billboard" in comp_id
            for entry in (comp.get("contents") or [])[:8]:
                c_obj = entry.get("content") if isinstance(entry.get("content"), dict) else entry
                if not isinstance(c_obj, dict):
                    continue
                ctype = str(entry.get("type") or c_obj.get("type") or "episode")
                cid = str(c_obj.get("id") or "")
                ver = c_obj.get("version")
                if not cid or ctype not in ("episode", "series", "live", "specialMain"):
                    continue
                v_q = f"?v={ver}" if ver is not None else ""
                if is_hero and ctype == "episode":
                    candidates = [
                        f"https://image-cdn.tver.jp/w=1200,dpr=1/images/content/thumbnail/episode/xlarge/{cid}.jpg{v_q}",
                        f"https://image-cdn.tver.jp/w=800/images/content/thumbnail/episode/large/{cid}.jpg{v_q}",
                    ]
                else:
                    folder = "live" if ctype == "live" else ("series" if ctype == "series" else "episode")
                    candidates = [
                        f"https://image-cdn.tver.jp/w=400/images/content/thumbnail/{folder}/small/{cid}.jpg{v_q}",
                        f"https://image-cdn.tver.jp/w=400,dpr=2/images/content/thumbnail/{folder}/small/{cid}.jpg{v_q}",
                    ]
                for cu in candidates:
                    if cu not in seen:
                        seen.add(cu)
                        thumb_urls.append(cu)
                if len(thumb_urls) >= 28:
                    break
            if len(thumb_urls) >= 28:
                break
        if thumb_urls:
            sem = asyncio.Semaphore(6)

            async def _fetch_t(u: str) -> None:
                async with sem:
                    await _prewarm_single_image(u)

            await asyncio.gather(*(_fetch_t(u) for u in thumb_urls), return_exceptions=True)
    except Exception:
        pass


def _extract_webpack_dynamic_assets(webpack_bytes: bytes) -> list[str]:
    out: list[str] = []
    try:
        text = webpack_bytes.decode("utf-8", "ignore")
        m_u = re.search(
            r'static/chunks/"\+\(\(\{([^}]*)\}\)\[\w+\]\|\|\w+\)\+"\."\+\{([^}]*)\}\[\w+\]\+"\.js"',
            text,
        )
        if m_u:
            name_map = dict(re.findall(r'(\d+):"([^"]+)"', m_u.group(1)))
            for cid, chash in re.findall(r'(\d+):"([0-9a-f]{8,32})"', m_u.group(2)):
                cname = name_map.get(cid, cid)
                out.append(f"https://tver.jp/_next/static/chunks/{cname}.{chash}.js")
        else:
            m_u2 = re.search(r'static/chunks/"\+\w+\+"\."\+\{([^}]*)\}\[\w+\]\+"\.js"', text)
            if m_u2:
                for cid, chash in re.findall(r'(\d+):"([0-9a-f]{8,32})"', m_u2.group(1)):
                    out.append(f"https://tver.jp/_next/static/chunks/{cid}.{chash}.js")
        m_css = re.search(r'static/css/"\+\{([^}]*)\}\[\w+\]\+"\.css"', text)
        if m_css:
            for _, chash in re.findall(r'(\d+):"([0-9a-f]{8,32})"', m_css.group(1)):
                out.append(f"https://tver.jp/_next/static/css/{chash}.css")
    except Exception:
        pass
    return out


async def _prefetch_static_from_html(html_bytes: bytes, page_url: str) -> None:
    if not html_bytes:
        return
    urls: list[str] = []
    seen: set[str] = set()
    manifest_url = ""
    webpack_urls: list[str] = []
    for m in _HTML_STATIC_REF_RE.finditer(html_bytes):
        try:
            ref = m.group(1).decode("utf-8", "ignore")
            abs_u = urljoin(page_url, ref)
            if "_buildManifest.js" in abs_u:
                manifest_url = abs_u
            if abs_u not in seen and _is_cacheable_static_asset("GET", abs_u):
                seen.add(abs_u)
                urls.append(abs_u)
                if "/_next/static/chunks/webpack-" in abs_u:
                    webpack_urls.append(abs_u)
        except Exception:
            continue
    for extra_u in (
        "https://s.tver.jp/setting.js",
        "https://s.tver.jp/setting.json",
        "https://s.tver.jp/data/string/s.json",
        "https://s.tver.jp/psdk.js",
        "https://s.tver.jp/appsdk.js",
        "https://statics.tver.jp/app/config/web.json",
        "https://player.tver.jp/player/streaks_info_v2.json",
        "https://player.tver.jp/player/tver_thv_config2.json",
        "https://imasdk.googleapis.com/js/sdkloader/ima3.js",
    ):
        if extra_u not in seen:
            seen.add(extra_u)
            urls.append(extra_u)
    if urls:
        await asyncio.gather(*(_prefetch_single_static_asset(u) for u in urls[:48]), return_exceptions=True)
    dyn_urls: list[str] = []
    for wp_u in webpack_urls:
        wp_cached = _STATIC_ASSET_CACHE.get(wp_u)
        if wp_cached and wp_cached[4]:
            for du in _extract_webpack_dynamic_assets(wp_cached[4]):
                if du not in seen:
                    seen.add(du)
                    dyn_urls.append(du)
    if manifest_url:
        m_cached = _STATIC_ASSET_CACHE.get(manifest_url)
        if m_cached and m_cached[4]:
            try:
                m_txt = m_cached[4].decode("utf-8", "ignore")
                idx_pos = m_txt.find('"/":')
                slice_txt = m_txt[idx_pos : idx_pos + 1800] if idx_pos >= 0 else m_txt[:2500]
                for cm in _MANIFEST_CHUNK_RE.finditer(slice_txt):
                    cu = f"https://tver.jp/_next/{cm.group(1)}"
                    if cu not in seen:
                        seen.add(cu)
                        dyn_urls.append(cu)
            except Exception:
                pass
    if dyn_urls:
        await asyncio.gather(*(_prefetch_single_static_asset(u) for u in dyn_urls[:48]), return_exceptions=True)


async def _prewarm_tver_target(target_url: str) -> None:
    clean_url = _strip_dollar_query(target_url or TVER_HOME)
    if clean_url in _PREWARM_INFLIGHT:
        return
    _PREWARM_INFLIGHT.add(clean_url)
    try:
        import app as host_app

        parsed = urlparse(clean_url)
        host = (parsed.hostname or "").lower().rstrip(".")
        is_home = host in ("tver.jp", "www.tver.jp") and (parsed.path or "/") == "/"

        base_tasks = [
            _ensure_patched_runtime("scramjet.js", _patch_scramjet_js),
            _ensure_patched_runtime("controller.api.js", _patch_controller_api),
            _ensure_patched_runtime("controller.sw.js", _patch_controller_sw),
            _ensure_patched_runtime("controller.inject.js", _patch_controller_inject),
            host_app.scramjet_asset("scramjet-utils.js"),
            _serve_virtual_wasm_js(),
        ]
        if is_home:
            base_tasks.append(_prefetch_public_api("https://contents-api.tver.jp/contents/api/v1/callHome"))
            base_tasks.append(_prefetch_public_api("https://contents-api.tver.jp/contents/api/v1/news/latest"))
            base_tasks.append(_prefetch_public_api("https://service-api.tver.jp/api/v1/callHeader"))
        results = await asyncio.gather(*base_tasks, return_exceptions=True)
        if is_home and len(results) >= 7 and isinstance(results[6], (bytes, bytearray)) and results[6]:
            try:
                asyncio.get_running_loop().create_task(_prewarm_home_thumbnails(bytes(results[6])))
            except Exception:
                pass

        if host not in ("tver.jp", "www.tver.jp"):
            return
        cached_html = _GUEST_HTML_CACHE.get(clean_url)
        if cached_html and (time.time() - cached_html[0]) < _GUEST_HTML_TTL:
            await _prefetch_static_from_html(cached_html[4], clean_url)
            return
        client = await _get_relay_http_client()
        resp = await client.get(
            clean_url,
            headers={
                "User-Agent": _DESKTOP_CHROME_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
                "Referer": "https://tver.jp/",
            },
        )
        if resp.status_code != 200:
            return
        body = bytes(resp.content or b"")
        if not body:
            return
        if b"modulepreload" in body:
            body = re.sub(
                rb'<link\b[^>]*\brel=["\']?modulepreload["\']?[^>]*>',
                b"",
                body,
                flags=re.I,
            )
        has_sc = False
        is_html = False
        out_hdrs: list[list[str]] = []
        for k, v in resp.headers.multi_items():
            kl = k.lower()
            if kl in _RELAY_STRIP_RESP_HEADERS or kl.startswith(":"):
                continue
            if kl == "set-cookie":
                has_sc = True
                break
            if kl == "content-type" and "text/html" in v.lower():
                is_html = True
            out_hdrs.append([k, v])
        if is_html and not has_sc and len(body) <= 512 * 1024:
            reason = str(resp.reason_phrase or "OK")
            if len(_GUEST_HTML_CACHE) >= _GUEST_HTML_MAX_ITEMS:
                _GUEST_HTML_CACHE.pop(next(iter(_GUEST_HTML_CACHE)), None)
            _GUEST_HTML_CACHE[clean_url] = (
                time.time(),
                200,
                reason,
                out_hdrs,
                body,
            )
            _prebuild_packed_relay_frame(f"guesthtml:{clean_url}", 200, reason, out_hdrs, body)
        if is_html:
            await _prefetch_static_from_html(body, clean_url)
    except Exception:
        pass
    finally:
        _PREWARM_INFLIGHT.discard(clean_url)


def _schedule_tver_prewarm(target_url: str = TVER_HOME) -> None:
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_prewarm_tver_target(target_url))
    except Exception:
        pass


@router.api_route(
    "/tver/~/sj/{rest:path}",
    methods=["GET", "HEAD", "POST", "OPTIONS"],
    include_in_schema=False,
)
async def tver_sj_direct_fallback(rest: str, request: Request):
    """Server-side fallback when iOS/iPadOS WebKit AVFoundation or early resource
    fetches bypass the Service Worker and request /tver/~/sj/... directly."""
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
        if parsed.path == "/tver/runtime/media-relay":
            qs = parse_qs(parsed.query)
            k_nested = (qs.get("k") or [""])[0]
            nested = _decode_media_key(k_nested) if k_nested else (qs.get("url") or [""])[0]
            if nested:
                return await _serve_tver_media_url(nested, request)
        host = (parsed.hostname or "").lower().rstrip(".")
        path = (parsed.path or "").lower()
        sc = _check_server_short_circuit(host, parsed.path or "")
        if sc is not None:
            sc_status, sc_ct, sc_body = sc
            return Response(
                b"" if request.method == "HEAD" else sc_body,
                status_code=sc_status,
                media_type=sc_ct,
                headers={"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Credentials": "true"},
            )
        if host == "image-cdn.tver.jp" or path.endswith(
            (".m3u8", ".ts", ".m4s", ".mp4", ".m4a", ".aac", ".vtt", ".key", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg")
        ):
            return await _serve_tver_media_url(virtual_url, request)
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


def _normalize_tver_set_cookie(cookie_val: str, host: str) -> str:
    """Ensure cookies set on tver.jp / *.tver.jp share cleanly across TVer subdomains."""
    if not cookie_val:
        return cookie_val
    first_semi = cookie_val.find(";")
    name_val = cookie_val if first_semi < 0 else cookie_val[:first_semi]
    cookie_name = name_val.split("=", 1)[0].strip()
    if cookie_name.startswith("__Host-"):
        return cookie_val
    if host == "tver.jp" or host.endswith(".tver.jp"):
        if re.search(r";\s*domain\s*=", cookie_val, re.I):
            return re.sub(
                r"(;\s*domain\s*=\s*)tver\.jp\b",
                r"\1.tver.jp",
                cookie_val,
                flags=re.I,
            )
        if host in ("tver.jp", "www.tver.jp", "platform-api.tver.jp", "service-api.tver.jp", "member-api.tver.jp"):
            return cookie_val.rstrip("; ") + "; Domain=.tver.jp"
    if host == "google.com" or host.endswith(".google.com"):
        if re.search(r";\s*domain\s*=", cookie_val, re.I):
            return re.sub(
                r"(;\s*domain\s*=\s*)google\.com\b",
                r"\1.google.com",
                cookie_val,
                flags=re.I,
            )
        return cookie_val.rstrip("; ") + "; Domain=.google.com"
    return cookie_val


def _select_curl_impersonate(ua: str) -> str:
    if not ua:
        return "chrome131"
    if ("iPad" in ua or "iPhone" in ua or "Mobile/" in ua) and "AppleWebKit" in ua and "Android" not in ua:
        return "safari17_2_ios"
    if "Macintosh" in ua and "Safari/" in ua and "Chrome/" not in ua and "Chromium/" not in ua:
        return "safari18_0"
    return "chrome131"


def _is_leaked_proxy_url(ref_val: str) -> bool:
    if not ref_val:
        return True
    rl = ref_val.lower()
    return (
        "/~/sj/" in rl
        or ".onrender.com" in rl
        or ".cloud.run" in rl
        or ".run.app" in rl
        or ".ai.studio" in rl
        or "localhost" in rl
        or "127.0.0.1" in rl
    )


@router.post("/tver/runtime/http-relay", include_in_schema=False)
async def tver_http_relay(request: Request):
    global _CURL_DISABLED_UNTIL

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
            if p_check.path == "/tver/runtime/media-relay":
                qs = parse_qs(p_check.query)
                k_nested = (qs.get("k") or [""])[0]
                nested = _decode_media_key(k_nested) if k_nested else (qs.get("url") or [""])[0]
                if nested:
                    url = nested
                    continue
            break
        url = _strip_dollar_query(url)
        parsed = urlparse(url)
    except Exception:
        return Response(b"bad url", status_code=400)

    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return Response(b"unsupported scheme", status_code=400)

    host = parsed.hostname.lower().rstrip(".")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port not in (80, 443):
        return Response(b"unsupported port", status_code=403)

    sc = _check_server_short_circuit(host, parsed.path or "")
    if sc is not None:
        sc_status, sc_ct, sc_body = sc
        return _pack_relay_response(
            sc_status,
            "OK",
            [
                ["content-type", sc_ct],
                ["access-control-allow-origin", "*"],
                ["access-control-allow-credentials", "true"],
            ],
            b"" if method == "HEAD" else sc_body,
        )

    try:
        await _ensure_public_host(host, port)
    except Exception:
        return Response(b"blocked host", status_code=403)

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
            v = v.replace("HeadlessChrome/", "Chrome/")
            if any(tok in v for tok in ("iPhone", "iPad", "iPod")) and "Chrome/" not in v and "CriOS/" not in v:
                v = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15"
            elif ("Windows NT" not in v and "Macintosh" not in v) or any(tok in v for tok in ("Android", "Mobile", "CrOS", "Linux")):
                m_cv = re.search(r"Chrome/([\d.]+)", v)
                cv = m_cv.group(1) if m_cv else "131.0.0.0"
                v = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{cv} Safari/537.36"
            client_ua = v
        elif kl == "sec-ch-ua-mobile":
            v = "?0"
        elif kl == "sec-ch-ua-platform":
            if "mac" not in v.lower():
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

    if host in AUTH_HOSTS or host.endswith(".google.com") or host == "google.com":
        if (parsed.path or "").startswith("/gsi/"):
            header_dict["Origin"] = "https://tver.jp"
            header_dict["Referer"] = "https://tver.jp/"
        else:
            clean_ref = f"https://{host}/" if _is_leaked_proxy_url(orig_ref) else orig_ref
            header_dict["Referer"] = clean_ref
            if has_origin or method in ("POST", "PUT", "PATCH", "DELETE"):
                try:
                    ru = urlparse(clean_ref)
                    header_dict["Origin"] = f"{ru.scheme}://{ru.netloc}" if ru.netloc else f"https://{host}"
                except Exception:
                    header_dict["Origin"] = f"https://{host}"
    elif host == "tver.jp" or host.endswith((".tver.jp", ".streaks.jp", ".brightcove.com", ".brightcove.net", ".boltdns.net")):
        clean_ref = "https://tver.jp/" if _is_leaked_proxy_url(orig_ref) else orig_ref
        header_dict["Referer"] = clean_ref
        if has_origin or method in ("POST", "PUT", "PATCH", "DELETE") or host != "tver.jp":
            if orig_origin and not _is_leaked_proxy_url(orig_origin) and "tver.jp" in orig_origin:
                header_dict["Origin"] = orig_origin
            else:
                header_dict["Origin"] = "https://tver.jp"
    else:
        if not _is_leaked_proxy_url(orig_ref):
            header_dict["Referer"] = orig_ref
        else:
            header_dict["Referer"] = "https://tver.jp/"
        if has_origin:
            header_dict["Origin"] = "https://tver.jp"

    path_lower = (parsed.path or "").lower()
    has_range = any(k.lower() == "range" for k in header_dict)
    is_static_img_host = host == "image-cdn.tver.jp" or (
        host == "statics.tver.jp"
        and path_lower.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".avif", ".ico"))
    )
    if is_static_img_host and not has_range and method in ("GET", "HEAD"):
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
                request=request,
            )

    has_auth = any(k.lower() == "authorization" for k in header_dict)
    pub_cache_key, pub_ttl = ("", 0.0) if has_range else _public_api_cache_spec(method, url, has_auth)
    if pub_cache_key and pub_ttl > 0:
        cached_pub = _PUBLIC_API_CACHE.get(pub_cache_key)
        if cached_pub and (time.time() - cached_pub[0]) < cached_pub[1]:
            _, p_ttl, p_status, p_text, p_headers, p_body = cached_pub
            return _pack_relay_response(
                p_status,
                p_text,
                p_headers,
                b"" if method == "HEAD" else p_body,
                request=request,
                cache_key=pub_cache_key,
                cache_ttl=p_ttl,
            )

    cacheable = (not has_range) and _is_cacheable_static_asset(method, url)
    if cacheable:
        cached = _STATIC_ASSET_CACHE.get(url)
        static_ttl = 300.0 if path_lower in ("/setting.js", "/setting.json") else _STATIC_ASSET_TTL
        if cached and (time.time() - cached[0]) < static_ttl:
            _, c_status, c_text, c_headers, c_body = cached
            return _pack_relay_response(
                c_status,
                c_text,
                c_headers,
                b"" if method == "HEAD" else c_body,
                request=request,
                cache_key=url,
                cache_ttl=static_ttl,
            )
        header_dict.pop("Cookie", None)

    is_guest_html_req = (
        not cacheable
        and not cookie_str
        and not has_range
        and method in ("GET", "HEAD")
        and host in ("tver.jp", "www.tver.jp")
    )
    if is_guest_html_req:
        cached_guest = _GUEST_HTML_CACHE.get(url)
        if cached_guest and (time.time() - cached_guest[0]) < _GUEST_HTML_TTL:
            _, g_status, g_text, g_headers, g_body = cached_guest
            return _pack_relay_response(
                g_status,
                g_text,
                g_headers,
                b"" if method == "HEAD" else g_body,
                request=request,
                cache_key=f"guesthtml:{url}",
                cache_ttl=_GUEST_HTML_TTL,
            )

    impersonate_profile = _select_curl_impersonate(client_ua)
    if impersonate_profile.startswith("safari"):
        for k_del in [k for k in header_dict if k.lower().startswith("sec-ch-ua")]:
            header_dict.pop(k_del, None)
    else:
        has_ch_ua = any(k.lower() == "sec-ch-ua" for k in header_dict)
        if not has_ch_ua:
            m_ver = re.search(r"Chrome/(\d+)", client_ua)
            c_maj = m_ver.group(1) if m_ver else "131"
            header_dict["sec-ch-ua"] = f'"Google Chrome";v="{c_maj}", "Chromium";v="{c_maj}", "Not_A Brand";v="24"'
            header_dict["sec-ch-ua-mobile"] = "?0"
            if "Macintosh" in client_ua or "Mac OS X" in client_ua:
                ch_plat = '"macOS"'
            else:
                ch_plat = '"Windows"'
            header_dict["sec-ch-ua-platform"] = ch_plat

    try:
        client = await _get_relay_http_client()
        httpx_err: Exception | None = None
        status = 502
        status_text = "Bad Gateway"
        resp_body = b""
        raw_multi: list[tuple[str, str]] = []
        try:
            upstream = await client.request(
                method=method,
                url=url,
                headers=header_dict,
                content=req_body if req_body else None,
            )
            status = int(upstream.status_code)
            status_text = str(upstream.reason_phrase or "OK")
            resp_body = bytes(upstream.content or b"")
            raw_multi = list(upstream.headers.multi_items())
        except Exception as exc_http:
            httpx_err = exc_http

        need_curl_fallback = httpx_err is not None or (status == 403 and host in AUTH_HOSTS)
        if need_curl_fallback:
            curl_sess = await _get_curl_session(impersonate_profile)
            if curl_sess is not None:
                try:
                    c_up = await curl_sess.request(
                        method=method,
                        url=url,
                        headers=header_dict,
                        data=req_body if req_body else None,
                        allow_redirects=False,
                        timeout=7,
                    )
                    status = int(c_up.status_code)
                    status_text = str(getattr(c_up, "reason", "") or "OK")
                    resp_body = bytes(c_up.content or b"")
                    raw_multi = list(c_up.headers.multi_items())
                    httpx_err = None
                except Exception as c_exc:
                    if "28" in str(c_exc) or "timed out" in str(c_exc).lower():
                        _CURL_DISABLED_UNTIL = time.time() + 120.0
                    if httpx_err is not None:
                        raise httpx_err
            elif httpx_err is not None:
                raise httpx_err

        if (
            host == "playback.api.streaks.jp"
            and status == 403
            and b"P01002" in resp_body
            and TVER_STREAKS_RELAY_URL
        ):
            try:
                sep = "&" if "?" in TVER_STREAKS_RELAY_URL else "?"
                relay_target = f"{TVER_STREAKS_RELAY_URL}{sep}url={quote(url, safe='')}"
                fwd_hdrs = {
                    k: v
                    for k, v in header_dict.items()
                    if k.lower() in ("x-streaks-api-key", "accept", "user-agent", "origin", "referer")
                }
                r_up = await client.request(
                    method=method,
                    url=relay_target,
                    headers=fwd_hdrs,
                    content=req_body if req_body else None,
                )
                if r_up.status_code < 400:
                    status = int(r_up.status_code)
                    status_text = str(r_up.reason_phrase or "OK")
                    resp_body = bytes(r_up.content or b"")
                    raw_multi = list(r_up.headers.multi_items())
            except Exception:
                pass
    except Exception as exc:
        err_msg = f"upstream error: {exc}".encode("utf-8", "ignore")
        return _pack_relay_response(
            502,
            "Bad Gateway",
            [["content-type", "text/plain; charset=utf-8"]],
            err_msg,
        )

    out_headers: list[list[str]] = []
    has_set_cookie = False
    is_m3u8 = path_lower.endswith(".m3u8")
    is_html = False
    is_json = False
    resp_media_type = "application/octet-stream"
    resp_cc = "public, max-age=86400"
    for k, v in raw_multi:
        kl = k.lower()
        if kl in _RELAY_STRIP_RESP_HEADERS or kl.startswith(":"):
            continue
        if kl == "set-cookie":
            has_set_cookie = True
            v = _normalize_tver_set_cookie(v, host)
        if kl == "content-type":
            resp_media_type = v
            vl = v.lower()
            if "mpegurl" in vl:
                is_m3u8 = True
            elif "text/html" in vl:
                is_html = True
            elif "json" in vl:
                is_json = True
        elif kl == "cache-control":
            resp_cc = v
        out_headers.append([k, v])

    if host == "playback.api.streaks.jp" and status == 200 and is_json and resp_body and b'"ads"' in resp_body:
        try:
            pb_data = json.loads(resp_body.decode("utf-8"))
            if isinstance(pb_data, dict) and "ads" in pb_data:
                pb_data.pop("ads", None)
                resp_body = json.dumps(pb_data, ensure_ascii=False).encode("utf-8")
        except Exception:
            pass

    if is_html and status == 200 and resp_body and b"modulepreload" in resp_body:
        resp_body = re.sub(
            rb'<link\b[^>]*\brel=["\']?modulepreload["\']?[^>]*>',
            b"",
            resp_body,
            flags=re.I,
        )

    if is_m3u8 and status in (200, 206) and resp_body:
        req_proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "https").split(",")[0].strip()
        req_host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc or "").split(",")[0].strip()
        proxy_origin = f"{req_proto}://{req_host}" if req_host else ""
        resp_body = _rewrite_hls_manifest(resp_body, url, proxy_origin=proxy_origin)
        status = 200
        out_headers = [h for h in out_headers if h[0].lower() != "content-range"]

    out_cache_key = ""
    out_cache_ttl = 0.0

    if (
        pub_cache_key
        and pub_ttl > 0
        and not has_set_cookie
        and status == 200
        and 0 < len(resp_body) <= _STATIC_ASSET_MAX_BYTES
    ):
        if len(_PUBLIC_API_CACHE) >= _PUBLIC_API_MAX_ITEMS:
            _PUBLIC_API_CACHE.pop(next(iter(_PUBLIC_API_CACHE)), None)
        _PUBLIC_API_CACHE[pub_cache_key] = (
            time.time(),
            pub_ttl,
            status,
            status_text,
            out_headers,
            resp_body,
        )
        out_cache_key = pub_cache_key
        out_cache_ttl = pub_ttl
        if host == "contents-api.tver.jp" and path_lower == "/contents/api/v1/callhome":
            try:
                asyncio.get_running_loop().create_task(_prewarm_home_thumbnails(resp_body))
            except Exception:
                pass

    if (
        is_static_img_host
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
        out_cache_key = url
        out_cache_ttl = 300.0 if path_lower in ("/setting.js", "/setting.json") else _STATIC_ASSET_TTL

    if is_html and status == 200 and resp_body:
        if (
            not cookie_str
            and not has_range
            and not has_set_cookie
            and method == "GET"
            and host in ("tver.jp", "www.tver.jp")
            and len(resp_body) <= 512 * 1024
        ):
            if len(_GUEST_HTML_CACHE) >= _GUEST_HTML_MAX_ITEMS:
                _GUEST_HTML_CACHE.pop(next(iter(_GUEST_HTML_CACHE)), None)
            _GUEST_HTML_CACHE[url] = (
                time.time(),
                status,
                status_text,
                out_headers,
                resp_body,
            )
            out_cache_key = f"guesthtml:{url}"
            out_cache_ttl = _GUEST_HTML_TTL
        try:
            asyncio.get_running_loop().create_task(_prefetch_static_from_html(resp_body, url))
        except Exception:
            pass

    return _pack_relay_response(
        status,
        status_text,
        out_headers,
        resp_body,
        request=request,
        cache_key=out_cache_key,
        cache_ttl=out_cache_ttl,
    )


@router.websocket("/tver/wisp/")
async def tver_wisp_route(websocket: WebSocket):
    import app as host_app

    await host_app.wisp_media_endpoint(websocket)


def _install_tver_routes() -> None:
    try:
        import app as host_app

        if not any(
            isinstance(r, Route)
            and r.path == "/tver/sw.js"
            and r.endpoint is _tver_sw_override
            for r in host_app.app.router.routes
        ):
            host_app.app.router.routes.insert(
                0, Route("/tver/sw.js", _tver_sw_override, methods=["GET", "HEAD"])
            )
        if not any(
            isinstance(r, WebSocketRoute)
            and r.path == "/tver/wisp/"
            for r in host_app.app.router.routes
        ):
            host_app.app.router.routes.insert(
                0, WebSocketRoute("/tver/wisp/", tver_wisp_route)
            )
    except Exception as exc:
        print(f"[TVer] route install warning: {exc}", flush=True)


_install_tver_routes()


def _target_from_input(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return TVER_HOME
    if value.startswith(("http://", "https://")):
        try:
            u = urlparse(value)
            h = (u.hostname or "").lower().rstrip(".")
            if h == "tver.jp" or h.endswith(".tver.jp") or h in AUTH_HOSTS:
                return urlunparse(("https", h, u.path or "/", "", u.query, ""))
        except Exception:
            pass
    if value.startswith("/"):
        return "https://tver.jp" + value
    return "https://tver.jp/search/" + quote(value, safe="")


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
    """Full-bleed 100vw x 100vh official TVer container (zero custom UI / 独自UIなし)."""
    home_js = _js_literal(TVER_HOME)
    initial_js = _js_literal(initial_target)
    wisp_override_js = _js_literal(SCRAMJET_WISP_URL)
    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#ffffff">
<title>Google</title>
<link rel="icon" type="image/x-icon" href="/favicon.ico">
<link rel="preload" href="/tver/runtime/scramjet.js?v={RUNTIME_REV}" as="script">
<link rel="preload" href="/tver/runtime/controller.api.js?v={RUNTIME_REV}" as="script">
<link rel="preload" href="/scramjet/scramjet-utils.js?v={UTILS_VERSION}" as="script">
<link rel="preload" href="/tver/runtime/controller.inject.js?v={RUNTIME_REV}" as="script">
<link rel="preload" href="/scramjet/scramjet.wasm" as="fetch" crossorigin>
<script>
if("serviceWorker"in navigator){{window.__shSwRegP=navigator.serviceWorker.register("/tver/sw.js?v={RUNTIME_REV}",{{scope:"/tver/",updateViaCache:"none",type:"classic"}}).catch(function(){{return null;}});}}
</script>
<style>
*{{box-sizing:border-box}}
html,body{{margin:0;width:100%;height:100%;overflow:hidden;background:#ffffff;color:#111827;font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans","Noto Sans JP","Segoe UI",Roboto,sans-serif}}
.stage{{position:fixed;inset:0;width:100%;height:100%;background:#ffffff}}
#frame{{position:fixed;inset:0;display:block;width:100%;height:100%;border:0;background:#ffffff}}
.cover{{position:fixed;inset:0;z-index:5;display:grid;place-items:center;padding:32px;background:#ffffff;color:#6b7280;font-size:14px;font-weight:500;text-align:center}}
.cover.hidden{{display:none}}
</style>
</head>
<body>
<main class="stage">
  <div class="cover" id="cover">読み込み中…</div>
  <iframe id="frame" title="TVer"
    allow="autoplay; clipboard-read; clipboard-write; encrypted-media; fullscreen; picture-in-picture"
    referrerpolicy="strict-origin-when-cross-origin"></iframe>
</main>

<script src="/tver/runtime/scramjet.js?v={RUNTIME_REV}"></script>
<script src="/tver/runtime/controller.api.js?v={RUNTIME_REV}"></script>
<script src="/scramjet/scramjet-utils.js?v={UTILS_VERSION}"></script>
<script>
(() => {{
  "use strict";

  const HOME = {home_js};
  const INITIAL = {initial_js};
  const WISP_OVERRIDE = {wisp_override_js};
  const SW = "/tver/sw.js?v={RUNTIME_REV}";
  const SW_SCOPE = "/tver/";
  const PREFIX = "/tver/~/sj/";
  const WISP_PATH = "/tver/wisp/";
  const RELAY_ENDPOINT = "/tver/runtime/http-relay";
  const MEDIA_RELAY_ENDPOINT = "/tver/runtime/media-relay";
  const STATIC_CACHE_NAME = "sh-tver-static-{RUNTIME_REV}";
  const MEDIA_CACHE_NAME = "sh-tver-media-{RUNTIME_REV}";
  const REWRITTEN_CACHE_NAME = "sh-tver-rewritten-{RUNTIME_REV}";
  const EMPTY_VAST_XML = '<?xml version="1.0" encoding="UTF-8"?><VAST version="3.0"/>';
  const EMPTY_AFFINITY_JSON = '{{"api_version":"","code":0,"message":"","type":"hash","result":{{"exists":false,"customParams":""}}}}';
  const SESSION_KEY = "schoolhub.tver.target.v1";
  const SW_GUARD = "schoolhub.tver.sw-reload.v1";
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

  const cover = document.getElementById("cover");
  const frameEl = document.getElementById("frame");

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

  function isLoginOrAuthUrl(urlStr) {{
    if (!urlStr) return false;
    try {{
      const u = new URL(urlStr);
      const h = u.hostname.toLowerCase().replace(/\\.$/, "");
      return h === "id.tver.jp" || h === "tverid.tver.jp" || h === "auth.tver.jp" ||
             h === "accounts.google.com" || h.endsWith(".google.com") || h === "appleid.apple.com";
    }} catch (_) {{
      return false;
    }}
  }}

  function remember(url) {{
    if (!url) return;
    const s = String(url);
    lastUrl = s;
    if (isLoginOrAuthUrl(s)) return;
    try {{ sessionStorage.setItem(SESSION_KEY, s); }} catch (_) {{}}
  }}

  function restore() {{
    try {{
      const params = new URLSearchParams(location.search);
      const hasExplicitQuery = Boolean(params.get("q") || params.get("url"));
      const hasDeepPath = location.pathname !== "/tver/" && location.pathname !== "/tver";
      if (hasExplicitQuery || hasDeepPath) return INITIAL;
      const saved = sessionStorage.getItem(SESSION_KEY) || INITIAL;
      if (isLoginOrAuthUrl(saved)) return HOME;
      return saved || HOME;
    }} catch (_) {{
      return INITIAL;
    }}
  }}

  function cleanTargetUrl(rawUrl) {{
    if (!rawUrl || typeof rawUrl !== "string") return "";
    try {{
      const preCleaned = rawUrl.replace(/([?&])\\$[A-Za-z0-9_]+=[^&#]*/g, "$1").replace(/\\?&/g, "?").replace(/[?&]+$/g, "");
      const u = new URL(preCleaned);
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

  function isStreamCdnTarget(target) {{
    if (!target) return false;
    const h = String(target.hostname || "").toLowerCase().replace(/\\.$/, "");
    const p = String(target.pathname || "");
    if (/\\.(js|mjs|cjs|json|css|html|wasm)(\\?|$)/i.test(p)) return false;
    if (h.endsWith(".api.streaks.jp")) return false;
    if (/\\.(m3u8|ts|m4s|cmfv|cmfa|aac|vtt|webvtt|key)(\\?|$)/i.test(p)) return true;
    if ((
        h.endsWith(".tver.jp") || h.endsWith(".cloudfront.net") || h.endsWith(".akamaized.net") ||
        h.endsWith(".akamaihd.net") || h.endsWith(".fastly.net") || h.endsWith(".boltdns.net") ||
        h.endsWith(".brightcove.com") || h.endsWith(".brightcove.net") || h.endsWith(".streaks.jp") ||
        h.endsWith(".uliza.jp") || h.endsWith(".stream.ne.jp") || h.endsWith(".jstream.jp") ||
        h.endsWith(".llnwd.net") || h.endsWith(".edgekey.net") || h.endsWith(".edgesuite.net") ||
        h.endsWith(".cdngc.net") || h.endsWith(".footprint.net") || h === "localhost" || h === "127.0.0.1"
    ) && /\\.(mp4|m4a|m4v)(\\?|$)/i.test(p)) {{
      return true;
    }}
    return false;
  }}

  function isDirectMediaTarget(target) {{
    if (!target) return false;
    const h = String(target.hostname || "").toLowerCase().replace(/\\.$/, "");
    const p = String(target.pathname || "");
    if (/\\.(js|mjs|cjs|json|css|html|wasm)(\\?|$)/i.test(p)) return false;
    if (h === "image-cdn.tver.jp") return true;
    if (h === "statics.tver.jp" && /\\.(jpg|jpeg|png|webp|gif|svg|avif|ico)(\\?|$)/i.test(p)) return true;
    return isStreamCdnTarget(target);
  }}

  function isCacheableImageTarget(target) {{
    if (!target) return false;
    const h = String(target.hostname || "").toLowerCase().replace(/\\.$/, "");
    const p = String(target.pathname || "");
    return h === "image-cdn.tver.jp" || (h === "statics.tver.jp" && /\\.(jpg|jpeg|png|webp|gif|svg|avif|ico)(\\?|$)/i.test(p));
  }}

  function rewriteM3u8Text(text, manifestUrl) {{
    const baseOrigin = String(location.origin || "").replace(/\\/+$/, "");
    const toTarget = (rawRef) => {{
      const cleanRef = String(rawRef || "").trim();
      if (!cleanRef || /^(data:|skd:|blob:)/i.test(cleanRef)) return rawRef;
      try {{
        const absHref = new URL(cleanRef, manifestUrl).href;
        if (!/^https?:\\/\\//i.test(absHref)) return rawRef;
        return baseOrigin + MEDIA_RELAY_ENDPOINT + "?k=" + encodeMediaKey(absHref);
      }} catch (_) {{
        return rawRef;
      }}
    }};
    const withUris = String(text || "").replace(/URI="([^"]+)"/g, (_, u) => 'URI="' + toTarget(u) + '"');
    return withUris.split(/\\r?\\n/).map((line) => {{
      const s = line.trim();
      return (s && !s.startsWith("#")) ? toTarget(s) : line;
    }}).join("\\n");
  }}

  function isClientDirectApiTarget(urlStr) {{
    try {{
      const u = new URL(urlStr);
      const h = u.hostname.toLowerCase().replace(/\\.$/, "");
      return h.endsWith(".api.streaks.jp") || h === "edge.api.brightcove.com";
    }} catch (_) {{
      return false;
    }}
  }}

  function unwrapMediaTarget(urlStr) {{
    try {{
      let u = new URL(urlStr);
      for (let k = 0; k < 3; k++) {{
        if (u.pathname === "/tver/runtime/media-relay") {{
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
      if (h === "imasdk.googleapis.com" && (p === "/js/sdkloader/ima3.js" || (p.startsWith("/js/core/bridge3") && p.endsWith(".html")))) {{
        return true;
      }}
      if ((h === "tver.jp" || h === "www.tver.jp" || h === "s.tver.jp" || h === "player.tver.jp" || h === "statics.tver.jp") &&
          p !== "/setting.js" && p !== "/setting.json" && !p.startsWith("/api/") &&
          (p.startsWith("/_next/static/") || p.startsWith("/streaksplayer/") || p.startsWith("/player/") ||
           p === "/config/streaks_info_v2.json" || p === "/player/tver_thv_config2.json" ||
           p === "/app/config/web.json" || p === "/data/string/s.json" ||
           (h === "s.tver.jp" && p.endsWith("/psdkplf.html")) ||
           /\\.(js|css|woff2|woff|ttf|otf|svg|png|jpg|jpeg|webp|ico)$/i.test(p))) {{
        return true;
      }}
    }} catch (_) {{}}
    return false;
  }}

  const DOUBLESERVE_JS = '(()=>{{try{{var d=document,add=function(){{if(!d.getElementById("chee4au4yieYohs9")){{var e=d.createElement("div");e.id="chee4au4yieYohs9";e.style.display="none";(d.body||d.documentElement).appendChild(e);}}}};add();if(!d.body)d.addEventListener("DOMContentLoaded",add,{{once:true}});}}catch(_){{}}}})();';
  const blockedDirectHosts = new Set();

  function fetchDirectWithTimeout(url, init, timeoutMs, outerSignal) {{
    const ac = new AbortController();
    let timedOut = false;
    const timer = setTimeout(() => {{
      timedOut = true;
      ac.abort();
    }}, timeoutMs);
    const onOuterAbort = () => ac.abort();
    if (outerSignal) {{
      if (outerSignal.aborted) {{
        clearTimeout(timer);
        ac.abort();
      }} else {{
        outerSignal.addEventListener("abort", onOuterAbort, {{ once: true }});
      }}
    }}
    return fetch(url, Object.assign({{}}, init, {{ signal: ac.signal }})).finally(() => {{
      clearTimeout(timer);
      if (outerSignal) outerSignal.removeEventListener("abort", onOuterAbort);
      if (timedOut) ac._timedOut = true;
    }});
  }}

  function checkTransportShortCircuit(urlStr) {{
    try {{
      const u = new URL(urlStr);
      const h = u.hostname.toLowerCase().replace(/\\.$/, "");
      const p = u.pathname || "";
      if (h === "platform-api.tver.jp" && p === "/service/api/v1/callAffinity") {{
        return {{
          body: new TextEncoder().encode(EMPTY_AFFINITY_JSON).buffer,
          headers: [
            ["content-type", "application/json; charset=utf-8"],
            ["access-control-allow-origin", "*"],
            ["access-control-allow-credentials", "true"],
          ],
          status: 200,
          statusText: "OK",
        }};
      }}
      if (((h === "tver.jp" || h === "www.tver.jp") && p === "/doubleserve.js") || (h === "s.tver.jp" && p === "/ads/doubleserve.js")) {{
        return {{
          body: new TextEncoder().encode(DOUBLESERVE_JS).buffer,
          headers: [
            ["content-type", "application/javascript; charset=utf-8"],
            ["access-control-allow-origin", "*"],
          ],
          status: 200,
          statusText: "OK",
        }};
      }}
      if (
        (h === "adsvr.tver.jp" && p.startsWith("/ad-server")) ||
        ((h === "pubads.g.doubleclick.net" || h === "securepubads.g.doubleclick.net") && (p.includes("/gampad/ads") || p.includes("/pagead/ads")))
      ) {{
        return {{
          body: new TextEncoder().encode(EMPTY_VAST_XML).buffer,
          headers: [
            ["content-type", "application/xml; charset=utf-8"],
            ["access-control-allow-origin", "*"],
            ["access-control-allow-credentials", "true"],
          ],
          status: 200,
          statusText: "OK",
        }};
      }}
      if (
        h === "www.googletagmanager.com" || h === "googletagmanager.com" ||
        ((h === "imasdk.googleapis.com" || h === "pagead2.googlesyndication.com" || h === "securepubads.g.doubleclick.net") &&
          (p.includes("omweb-v1.js") || p.includes("omsdk-v1.js") || p.includes("/tag/js/gpt.js") || p.includes("/gpt/pubads_impl")))
      ) {{
        return {{
          body: new TextEncoder().encode("window.dataLayer=window.dataLayer||[];window.googletag=window.googletag||{{cmd:[]}};").buffer,
          headers: [
            ["content-type", "application/javascript; charset=utf-8"],
            ["access-control-allow-origin", "*"],
          ],
          status: 200,
          statusText: "OK",
        }};
      }}
      if (
        h === "m-tag.tver.jp" || h === "s-tag.tver.jp" || h === "ce-api.tver.jp" || h === "log.tver.jp" ||
        h === "bam.nr-data.net" || h === "js-agent.newrelic.com" || h.endsWith(".nr-data.net") || h.endsWith(".newrelic.com") ||
        h === "analytics.tiktok.com" || h === "analytics.twitter.com" ||
        h === "ads-twitter.com" || h.endsWith(".ads-twitter.com") ||
        h === "b99.yahoo.co.jp" || h === "yjtag.yahoo.co.jp" || h === "yads.c.yimg.jp" || h === "s.yimg.jp" ||
        h === "dmp.im-apps.net" || h === "connect.facebook.net" ||
        (h === "www.facebook.com" && p.startsWith("/tr")) ||
        h === "gum.criteo.com" || h === "static.criteo.net" || h === "bidder.criteo.com" ||
        h === "cnt.fout.jp" || h === "sync.fout.jp" || h === "js.fout.jp" || h.endsWith(".fout.jp") ||
        h.endsWith(".socdm.com") || h.endsWith(".adtrafficquality.google") ||
        h === "s0.2mdn.net" || h === "pagead2.googlesyndication.com" || h === "tpc.googlesyndication.com" ||
        h === "www.google-analytics.com" || h === "region1.google-analytics.com" ||
        h === "analytics.google.com" || h === "stats.g.doubleclick.net" ||
        h.endsWith(".karte.io") || h.endsWith(".braze.com") ||
        h === "js-sec.indexww.com" || h === "c.amazon-adsystem.com" || h.endsWith(".amazon-adsystem.com")
      ) {{
        const isJs = /\\.js(\\?|$)/i.test(p) || p.includes("/js");
        const b = new TextEncoder().encode(isJs ? "" : "{{}}").buffer;
        return {{
          body: b,
          headers: [
            ["content-type", isJs ? "application/javascript" : "application/json"],
            ["access-control-allow-origin", "*"],
          ],
          status: 200,
          statusText: "OK",
        }};
      }}
    }} catch (_) {{}}
    return null;
  }}

  function normalizeHeadersList(headers) {{
    const effectiveUa = String(navigator.userAgent || DEFAULT_UA).replace(/HeadlessChrome\\//g, "Chrome/");
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
      const list = normalizeHeadersList(headers);
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
      this.staticInflight = new Map();
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
            console.warn("[SchoolHub TVer] fallback wisp transport init failed", kind, e);
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
      const wasMediaRelay = rawUrlStr.includes("/tver/runtime/media-relay");
      const unwrapped = unwrapMediaTarget(rawUrlStr);
      const urlStr = unwrapped ? unwrapped.href : rawUrlStr;

      const sc = checkTransportShortCircuit(urlStr);
      if (sc) return sc;

      const list = normalizeHeadersList(headers);
      const rangeEntry = list.find(([k]) => k.toLowerCase() === "range");

      if ((m === "GET" || m === "HEAD") && unwrapped && (wasMediaRelay || isDirectMediaTarget(unwrapped))) {{
        const uHost = String(unwrapped.hostname || "").toLowerCase().replace(/\\.$/, "");
        const isTverHost = uHost === "tver.jp" || uHost.endsWith(".tver.jp");
        const relayUrl = MEDIA_RELAY_ENDPOINT + "?via=server&k=" + encodeMediaKey(unwrapped.href);
        const canCache = m === "GET" && !rangeEntry && isCacheableImageTarget(unwrapped);
        const isM3u8Path = /\\.m3u8(\\?|$)/i.test(unwrapped.pathname || "");
        try {{
          if (!isTverHost && !canCache && !blockedDirectHosts.has(uHost)) {{
            try {{
              const cdnHdrs = {{}};
              if (rangeEntry && rangeEntry[1] && !isM3u8Path) cdnHdrs["Range"] = rangeEntry[1];
              const cdnResp = await fetchDirectWithTimeout(unwrapped.href, {{
                method: m,
                headers: cdnHdrs,
                mode: "cors",
                credentials: "omit",
              }}, 2500, signal);
              if (cdnResp && (cdnResp.ok || cdnResp.status === 206)) {{
                const ct = String(cdnResp.headers.get("content-type") || "").toLowerCase();
                if (ct.includes("text/html") || ct.includes("application/xhtml")) {{
                  blockedDirectHosts.add(uHost);
                }} else if ((isM3u8Path || ct.includes("mpegurl")) && m === "GET") {{
                  const rawTxt = await cdnResp.text();
                  if (!rawTxt.trimStart().startsWith("#EXTM3U")) {{
                    blockedDirectHosts.add(uHost);
                  }} else {{
                    const rewritten = rewriteM3u8Text(rawTxt, cdnResp.url || unwrapped.href);
                    return {{
                      body: new TextEncoder().encode(rewritten).buffer,
                      headers: [
                        ["content-type", "application/vnd.apple.mpegurl"],
                        ["access-control-allow-origin", "*"],
                        ["cross-origin-resource-policy", "cross-origin"],
                        ["cache-control", "no-cache"],
                      ],
                      status: 200,
                      statusText: "OK",
                    }};
                  }}
                }} else {{
                  return {{
                    body: m === "HEAD" ? new ArrayBuffer(0) : await cdnResp.arrayBuffer(),
                    headers: [...cdnResp.headers.entries()],
                    status: cdnResp.status,
                    statusText: cdnResp.statusText || "OK",
                  }};
                }}
              }} else {{
                blockedDirectHosts.add(uHost);
              }}
            }} catch (_) {{
              if (!signal?.aborted) blockedDirectHosts.add(uHost);
            }}
          }}
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
          if (rangeEntry && rangeEntry[1] && !isM3u8Path) fwdHeaders["Range"] = rangeEntry[1];
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

      if (isClientDirectApiTarget(urlStr)) {{
        const apiHost = (() => {{
          try {{ return new URL(urlStr).hostname.toLowerCase().replace(/\\.$/, ""); }} catch (_) {{ return ""; }}
        }})();
        if (apiHost && !blockedDirectHosts.has(apiHost)) {{
          try {{
            const isSsai = apiHost === "ssai.api.streaks.jp";
            const corsHeaders = {{}};
            for (const [hk, hv] of list) {{
              const kl = String(hk || "").toLowerCase();
              if (!kl || !hv) continue;
              if (
                kl === "accept" || kl === "content-type" || kl === "authorization" ||
                kl === "x-requested-with" || kl === "if-modified-since" ||
                (!isSsai && (kl.startsWith("x-streaks-") || kl === "bcov-policy" || kl === "policy-key"))
              ) {{
                corsHeaders[hk] = hv;
              }}
            }}
            const directBody = (m === "GET" || m === "HEAD") ? undefined : await serializeRequestBody(body);
            const apiResp = await fetchDirectWithTimeout(urlStr, {{
              method: m,
              headers: corsHeaders,
              body: directBody && directBody.byteLength > 0 ? directBody : undefined,
              mode: "cors",
              credentials: "omit",
              cache: "no-store",
            }}, 3000, signal);
            const apiCt = String(apiResp?.headers?.get("content-type") || "").toLowerCase();
            if (apiCt.includes("text/html") || apiCt.includes("application/xhtml")) {{
              blockedDirectHosts.add(apiHost);
            }} else if (apiResp && apiResp.status >= 200 && apiResp.status < 400) {{
              let outBuf = m === "HEAD" ? new ArrayBuffer(0) : await apiResp.arrayBuffer();
              if (m === "GET" && outBuf.byteLength > 0) {{
                const parsedPb = JSON.parse(new TextDecoder().decode(outBuf));
                if (!parsedPb || typeof parsedPb !== "object") {{
                  throw new Error("invalid streaks json");
                }}
                if (apiHost === "playback.api.streaks.jp" && "ads" in parsedPb) {{
                  delete parsedPb.ads;
                  outBuf = new TextEncoder().encode(JSON.stringify(parsedPb)).buffer;
                }}
              }}
              return {{
                body: outBuf,
                headers: [...apiResp.headers.entries()],
                status: apiResp.status,
                statusText: apiResp.statusText || "OK",
              }};
            }}
          }} catch (_) {{
            if (!signal?.aborted) blockedDirectHosts.add(apiHost);
          }}
        }}
      }}

      const canClientCache = !rangeEntry && isClientCacheableAsset(m, urlStr);
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
        const inflight = this.staticInflight.get(urlStr);
        if (inflight) {{
          try {{
            const shared = await inflight;
            return {{
              body: shared.body.slice(0),
              headers: shared.headers,
              status: shared.status,
              statusText: shared.statusText,
            }};
          }} catch (_) {{}}
        }}
      }}

      const runRelayFetch = async () => {{
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
            const setCookieEntries = [];
            for (const [k, v] of respHeaders) {{
              if (String(k || "").toLowerCase() === "set-cookie" && v) {{
                let scStr = String(v);
                scStr = scStr.replace(/;\\s*domain=tver\\.jp\\b/gi, "; Domain=.tver.jp");
                scStr = scStr.replace(/;\\s*domain=google\\.com\\b/gi, "; Domain=.google.com");
                setCookieEntries.push({{ url: urlStr, cookie: scStr }});
              }}
            }}
            if (setCookieEntries.length > 0) {{
              controller.applyCookieSyncEntries?.(setCookieEntries);
              Promise.resolve(controller.persistCookies?.()).catch(() => {{}});
              Promise.resolve(controller.propagateCookieSync?.(setCookieEntries, {{}})).catch(() => {{}});
            }}
          }} catch (_) {{}}
        }}
        const statusNum = Number(meta.status || 200);
        if (canClientCache && statusNum === 200 && respBody.byteLength > 0 && typeof caches !== "undefined") {{
          const cachedClone = respBody.slice(0);
          caches.open(STATIC_CACHE_NAME).then((cache) => {{
            const hObj = new Headers();
            for (const [k, v] of respHeaders) {{
              if (k && k.toLowerCase() !== "set-cookie") {{
                try {{ hObj.append(k, v); }} catch (_) {{}}
              }}
            }}
            return cache.put(urlStr, new Response(cachedClone, {{ status: 200, headers: hObj }}));
          }}).catch(() => {{}});
        }}
        return {{
          body: respBody,
          headers: respHeaders,
          status: statusNum,
          statusText: String(meta.statusText || "OK"),
        }};
      }};
      try {{
        if (canClientCache) {{
          const pRelay = runRelayFetch();
          this.staticInflight.set(urlStr, pRelay);
          try {{
            const res = await pRelay;
            return {{
              body: res.body.slice(0),
              headers: res.headers,
              status: res.status,
              statusText: res.statusText,
            }};
          }} finally {{
            if (this.staticInflight.get(urlStr) === pRelay) {{
              this.staticInflight.delete(urlStr);
            }}
          }}
        }}
        return await runRelayFetch();
      }} catch (relayError) {{
        console.warn("[SchoolHub TVer] relay fallback to wisp for", urlStr, relayError);
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
      const m = href.match(/\\/tver\\/~\\/sj\\/(?:[^/]+\\/[^/]+\\/)?(https?(?:%3A|:).*)$/i);
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

  function ensurePageMedia() {{
    try {{
      const doc = frameEl.contentDocument;
      if (!doc || !doc.body) return;
      const rawSetAttr = window.Element.prototype.setAttribute;
      const rawGetAttr = window.Element.prototype.getAttribute;
      const imgs = doc.querySelectorAll("img");
      for (let i = 0; i < imgs.length; i++) {{
        try {{
          const img = imgs[i];
          if (i >= 10) {{
            if (!rawGetAttr.call(img, "loading")) rawSetAttr.call(img, "loading", "lazy");
            if (!rawGetAttr.call(img, "decoding")) rawSetAttr.call(img, "decoding", "async");
          }}
          const actualSrc = rawGetAttr.call(img, "src") || "";
          if (actualSrc.includes("/tver/~/sj/") && actualSrc.includes("/data:")) {{
            const dIdx = actualSrc.indexOf("/data:");
            if (dIdx >= 0) {{
              rawSetAttr.call(img, "src", actualSrc.slice(dIdx + 1));
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
      const text = String(body?.innerText || "").replace(/\\s+/g, " ").trim();
      const hasTVerRoot = Boolean(
        doc.querySelector("#__next > *, header, main, nav, video, [class*='Header'], [class*='Layout']")
      );
      const isProxied = href.includes(PREFIX);
      const loading = isProxied && (doc.readyState === "loading" || doc.readyState === "interactive");
      const healthy = Boolean(isProxied && (hasTVerRoot || text.length > 20));
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
    ensurePageMedia();
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
        scramjetPath: "/tver/runtime/scramjet.js?v={RUNTIME_REV}",
        injectPath: "/tver/runtime/controller.inject.js?v={RUNTIME_REV}",
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
    throw new Error("TVer Service Worker did not take control");
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
      cover.textContent = "ページを読み込めませんでした。再読み込みしてください。";
      return;
    }}
    recovery += 1;
    const attempt = recovery;
    clearTimeout(healthTimer);
    navigationToken += 1;
    const recoveryToken = navigationToken;
    cover.classList.remove("hidden");

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
          ensurePageMedia();
        }}
      }}
    }}, 120);
  }}

  function bindFrame() {{
    frameEl.addEventListener("load", () => {{
      const token = navigationToken;
      const snap = currentDocumentHealthy();
      if (snap.healthy) showHealthy();
      else armHealthCheck(token, 12000);
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

  async function boot() {{
    if (!("serviceWorker" in navigator)) throw new Error("Service Worker unavailable");
    try {{
      const revKey = "sh:tver:rev";
      if (localStorage.getItem(revKey) !== "{RUNTIME_REV}" && typeof caches !== "undefined") {{
        localStorage.setItem(revKey, "{RUNTIME_REV}");
        caches.keys().then((cKeys) => Promise.all(
          cKeys.filter((k) => k.startsWith("sh-tver-") && k !== STATIC_CACHE_NAME && k !== MEDIA_CACHE_NAME && k !== REWRITTEN_CACHE_NAME).map((k) => caches.delete(k).catch(() => {{}}))
        )).catch(() => {{}});
      }}
    }} catch (_) {{}}

    const initialRegP = window.__shSwRegP || navigator.serviceWorker.register(SW, {{ scope: SW_SCOPE, updateViaCache: "none", type: "classic" }});
    if (isOurSw(navigator.serviceWorker.controller)) {{
      try {{ sessionStorage.removeItem(SW_GUARD); }} catch (_) {{}}
      Promise.resolve(initialRegP).then((r) => {{ if (r) registration = r; }}).catch(() => {{}});
    }} else {{
      registration = await withTimeout(
        Promise.resolve(initialRegP).then((r) => r || navigator.serviceWorker.getRegistration(SW_SCOPE)),
        4500,
        "SW register"
      ).catch(async () => await navigator.serviceWorker.getRegistration(SW_SCOPE));
      await waitForWorkerControl(registration);
    }}
    navigator.serviceWorker.addEventListener("controllerchange", () => {{
      try {{ controller?.setupMessagePort?.(); }} catch (_) {{}}
    }});
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
    navigationToken += 1;
    lastUrl = stored;
    remember(stored);
    frame.go(stored);
    armHealthCheck(navigationToken, HEALTH_DELAY);
  }}

  window.addEventListener("beforeunload", () => {{
    try {{ controller?.persistCookies?.(); }} catch (_) {{}}
    clearInterval(keepaliveTimer);
    clearInterval(pollTimer);
  }});

  boot().catch((error) => {{
    console.error("[SchoolHub TVer] boot failed", error);
    cover.classList.remove("hidden");
    cover.textContent = "ページを読み込めませんでした。再読み込みしてください。";
  }});
}})();
</script>
</body>
</html>"""


async def prewarm_service_cache() -> None:
    await _prewarm_tver_target(TVER_HOME)


@router.on_event("startup")
async def _prewarm_tver_on_startup() -> None:
    _schedule_tver_prewarm(TVER_HOME)


@router.get("/tver", include_in_schema=False)
async def tver_redirect():
    return RedirectResponse("/tver/", status_code=307)


@router.get("/tver/", response_class=HTMLResponse, include_in_schema=False)
async def tver_home(q: str = "", url: str = ""):
    target = _target_from_input(url or q)
    _schedule_tver_prewarm(target)
    response = HTMLResponse(_shell_html(target))
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@router.get("/tver/health", include_in_schema=False)
async def tver_health():
    return {
        "ok": True,
        "service": SERVICE_NAME,
        "router_path": SERVICE_PATH,
        "proxy_module": PROXY_MODULE,
        "scramjet": SCRAMJET_VERSION,
        "controller": CONTROLLER_VERSION,
        "service_worker": "/tver/sw.js",
        "virtual_prefix": "/tver/~/sj/",
    }


@router.get("/tver/{subpath:path}", response_class=HTMLResponse, include_in_schema=False)
async def tver_deep_link(subpath: str, request: Request):
    clean = (subpath or "").lstrip("/")
    if clean in ("sw.js", "wisp", "wisp/") or clean.startswith(("runtime/", "~/sj/", "openchat/")):
        return Response(b"not found", status_code=404)
    q = f"?{request.url.query}" if request.url.query else ""
    target = f"https://tver.jp/{clean}{q}"
    _schedule_tver_prewarm(target)
    response = HTMLResponse(_shell_html(target))
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response
