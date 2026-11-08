"""Compatibility shim for Interstellar same-origin navigation, service workers, and tabs."""

from fastapi import Request
from fastapi.responses import Response
from fastapi.routing import APIRoute

from app import app
from interstellar_proxy import SHIM_JS


_NAV_SHIM = r'''
;(()=>{"use strict";const P="/interstellar";const ROOT=/^\/(?:a|b|c|d)(?:\/|[?#]|$)/i;const map=(href)=>{try{const u=new URL(href,location.href);if(u.origin!==location.origin)return href;if(u.pathname===P||u.pathname.startsWith(P+"/"))return u.href;if(u.pathname==="/")u.pathname=P+"/";else if(ROOT.test(u.pathname))u.pathname=P+u.pathname;else return href;return u.href}catch{return href}};const fix=()=>{for(const a of document.querySelectorAll("a[href]")){const n=map(a.href);if(n!==a.href)a.href=n}};const fixFrames=()=>{for(const el of document.querySelectorAll("iframe[src], [data-original-tag=\"iframe\"][src]")){try{const u=new URL(el.getAttribute("src"),location.href);if(u.origin===location.origin&&(u.pathname==="/"||u.pathname==="/interstellar")){el.setAttribute("src",P+"/")}}catch{}}};const boot=()=>{fix();fixFrames()};if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",boot,{once:true});else boot();try{new MutationObserver(()=>{fix();fixFrames()}).observe(document.documentElement,{subtree:true,childList:true,attributes:true,attributeFilter:["href","src"]})}catch{}document.addEventListener("click",e=>{const a=e.target?.closest?.("a[href]");if(!a||e.defaultPrevented||e.button!==0||e.metaKey||e.ctrlKey||e.shiftKey||e.altKey)return;const n=map(a.href);if(n!==a.href){e.preventDefault();location.href=n}},true);try{if(typeof HTMLImageElement!=="undefined"){const d=Object.getOwnPropertyDescriptor(HTMLImageElement.prototype,"src");if(d&&d.set){Object.defineProperty(HTMLImageElement.prototype,"src",{configurable:true,enumerable:d.enumerable,get:d.get,set:function(v){if(typeof v==="string"&&v.startsWith("/assets/")&&!v.startsWith(P+"/"))v=P+v;return d.set.call(this,v)}})}}}catch{}try{if(navigator.serviceWorker){if(navigator.serviceWorker.addEventListener){const _swAdd=navigator.serviceWorker.addEventListener.bind(navigator.serviceWorker);navigator.serviceWorker.addEventListener=function(t,l,o){const res=_swAdd(t,l,o);if(t==="message"&&typeof navigator.serviceWorker.startMessages==="function"){try{navigator.serviceWorker.startMessages()}catch{}}return res}}if(typeof navigator.serviceWorker.startMessages==="function"){try{navigator.serviceWorker.startMessages()}catch{}}if(navigator.serviceWorker.register){const r=navigator.serviceWorker.register.bind(navigator.serviceWorker);navigator.serviceWorker.register=(scriptURL,options={})=>{try{const u=new URL(scriptURL,location.href);if(u.origin===location.origin&&location.pathname.startsWith(P+"/")&&(u.pathname==="/sw.js"||u.pathname.endsWith("/sw.js"))){u.pathname=P+"/sw.js";scriptURL=u.href}if(options&&options.scope==="/a/")options={...options,scope:P+"/a/"}}catch{}return r(scriptURL,options)}}}}catch(e){console.debug("Interstellar service worker isolation shim",e)}})();
'''

_NAV_JS = SHIM_JS + _NAV_SHIM


def _shim(request: Request):
    return Response(
        _NAV_JS.encode("utf-8"),
        status_code=200,
        media_type="application/javascript",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "X-Interstellar-Isolated": "1",
        },
    )


app.add_api_route(
    "/interstellar/__shim.js",
    _shim,
    methods=["GET", "HEAD"],
    name="interstellar_nav_shim",
    include_in_schema=False,
)


@app.on_event("startup")
async def _install_navigation_shim():
    exact_index = next(
        (
            index
            for index, route in enumerate(app.router.routes)
            if isinstance(route, APIRoute)
            and route.path == "/interstellar/__shim.js"
            and route.name == "interstellar_nav_shim"
        ),
        None,
    )
    if exact_index is None:
        print("[Interstellar compat] navigation shim route missing", flush=True)
        return
    route = app.router.routes.pop(exact_index)
    app.router.routes.insert(0, route)
    print("[Interstellar compat] navigation shim installed", flush=True)
