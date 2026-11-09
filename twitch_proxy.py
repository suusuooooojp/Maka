# twitch_proxy.py
import httpx
from fastapi import APIRouter, Request, Response, WebSocket
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
import re
import websockets
import asyncio
import ssl
import ipaddress
from urllib.parse import urlparse, parse_qs
from httpx import URL

# 東的検出用のメタ情報
SERVICE_NAME = "Twitch(安定)"
SERVICE_ICON = "📺"
SERVICE_PATH = "/twitch"

import os

router = APIRouter()
_HAS_PROXY_ENV = bool(os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY"))
_TWITCH_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=30, keepalive_expiry=30.0)
client = httpx.AsyncClient(
    follow_redirects=True,
    verify=False,
    timeout=httpx.Timeout(30.0, connect=8.0),
    limits=_TWITCH_LIMITS,
    headers={"Accept-Encoding": "identity"},
    transport=(
        None
        if _HAS_PROXY_ENV
        else httpx.AsyncHTTPTransport(
            local_address="0.0.0.0",
            verify=False,
            retries=2,
            limits=_TWITCH_LIMITS,
        )
    ),
)

NETWORK_HIJACK_SCRIPT_TWITCH = r"""
/* Twitch Proxy Hijack HTML with WebKit Drag & Draggable Lane-Matching Danmaku */
(function() {
    const _global = typeof window !== 'undefined' ? window : (typeof self !== 'undefined' ? self : this);
    if (!_global || _global.__network_hijacked) return;
    _global.__network_hijacked = true;

    let originalSetAttribute = null;
    if (_global.Element && _global.Element.prototype) originalSetAttribute = _global.Element.prototype.setAttribute;

    function proxyUrl(url) {
        if (typeof url !== 'string') return url;
        if (url.startsWith('ws://') || url.startsWith('wss://')) return url;
        
        // 二重変換（マトリョーシカ化）の完全防止ガード
        if (url.includes('/proxy_subdomain/')) return url; 
        
        const origin = _global.location ? _global.location.origin : '';
        if (url.includes('twitch.tv')) {
            let newUrl = url.replace(/https?:\/\/([a-zA-Z0-9\-\.]+)\.twitch\.tv/g, origin + '/proxy_subdomain/twitch/$1');
            newUrl = newUrl.replace(/https?:\/\/twitch\.tv/g, origin + '/twitch');
            return newUrl;
        }
        if (url.includes('ttvnw.net')) return url.replace(/https?:\/\/([a-zA-Z0-9\-\.]+)\.ttvnw\.net/g, origin + '/proxy_subdomain/ttvnw/$1');
        if (url.includes('jtvnw.net')) return url.replace(/https?:\/\/([a-zA-Z0-9\-\.]+)\.jtvnw\.net/g, origin + '/proxy_subdomain/jtvnw/$1');
        if (url.includes('twitchcdn.net')) return url.replace(/https?:\/\/([a-zA-Z0-9\-\.]+)\.twitchcdn\.net/g, origin + '/proxy_subdomain/twitchcdn/$1');
        return url;
    }

    function processSrcset(value) {
        if (typeof value !== 'string') return value;
        return value.split(',').map(part => {
            let parts = part.trim().split(/\s+/);
            if(parts.length > 0) parts[0] = proxyUrl(parts[0]);
            return parts.join(' ');
        }).join(', ');
    }

    // 🛡️ 弾幕システム v2: 安定検出 + 低遅延 + 滑らかなGPU移動
    function initDanmaku() {
        if (_global.__danmaku_inited) return;
        if (!_global.document || !_global.document.body) {
            setTimeout(initDanmaku, 50);
            return;
        }
        _global.__danmaku_inited = true;

        let danmakuEnabled = localStorage.getItem('schoolhub_danmaku_enabled') !== '0';
        let fontSize = Math.max(10, Math.min(48, parseInt(localStorage.getItem('schoolhub_danmaku_size') || '18', 10)));
        let speed = Math.max(80, Math.min(420, parseInt(localStorage.getItem('schoolhub_danmaku_speed') || '190', 10)));
        let opacity = Math.max(0.35, Math.min(1, parseFloat(localStorage.getItem('schoolhub_danmaku_opacity') || '0.96')));
        let spawnGap = Math.max(25, Math.min(250, parseInt(localStorage.getItem('schoolhub_danmaku_gap') || '70', 10)));
        let lastSpawn = 0;
        let layer = null;
        let layerRect = null;
        const laneState = [];
        const processed = new WeakSet();
        const recentKeys = new Map();

        const panel = document.createElement('div');
        panel.id = 'danmaku-control-panel';
        panel.style.cssText = 'position:fixed;right:12px;top:12px;z-index:2147483647;background:rgba(12,13,20,.88);backdrop-filter:blur(10px);color:#fff;padding:9px 11px;border-radius:10px;font:600 11px/1.2 system-ui,sans-serif;border:1px solid rgba(255,255,255,.14);box-shadow:0 8px 24px rgba(0,0,0,.32);user-select:none;min-width:190px;';
        panel.innerHTML = '<div style="display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:7px"><b>⚙️ 弾幕</b><button id="dm-min" style="border:0;background:rgba(255,255,255,.12);color:#fff;border-radius:6px;padding:3px 6px">収納</button></div>'+
            '<div id="dm-controls" style="display:grid;gap:6px">'+
            '<button id="dm-toggle" style="border:0;color:#fff;border-radius:7px;padding:6px;font-weight:700">弾幕モード</button>'+
            '<label style="display:flex;align-items:center;justify-content:space-between;gap:8px">文字 <input id="dm-size" type="range" min="10" max="48" step="1" value="'+fontSize+'"><span id="dm-size-v" style="width:28px;text-align:right">'+fontSize+'</span></label>'+
            '<label style="display:flex;align-items:center;justify-content:space-between;gap:8px">速度 <input id="dm-speed" type="range" min="80" max="420" step="5" value="'+speed+'"><span id="dm-speed-v" style="width:34px;text-align:right">'+speed+'</span></label>'+
            '<label style="display:flex;align-items:center;justify-content:space-between;gap:8px">濃さ <input id="dm-opacity" type="range" min="35" max="100" step="1" value="'+Math.round(opacity*100)+'"><span id="dm-opacity-v" style="width:34px;text-align:right">'+Math.round(opacity*100)+'%</span></label>'+
            '<label style="display:flex;align-items:center;justify-content:space-between;gap:8px">密度 <input id="dm-gap" type="range" min="25" max="250" step="5" value="'+spawnGap+'"><span id="dm-gap-v" style="width:34px;text-align:right">'+spawnGap+'ms</span></label>'+
            '</div>';
        _global.document.body.appendChild(panel);

        // Twitch絵文字・全画面弾幕強化CSS
        const dmStyle = document.createElement('style');
        dmStyle.textContent = `
        #danmaku-layer { position:fixed !important; inset:0 !important; width:100vw !important; height:100vh !important; }
        .schoolhub-emoji { display:inline-block; animation: schoolhubEmojiFloat .8s ease-in-out infinite alternate; }
        @keyframes schoolhubEmojiFloat {
          from { transform:translateY(0) scale(1); }
          to { transform:translateY(-6px) scale(1.15) rotate(5deg); }
        }`;
        document.head.appendChild(dmStyle);

        const $ = id => panel.querySelector(id);
        const toggle = $('#dm-toggle'), controls = $('#dm-controls');
        function refreshToggle(){
            toggle.textContent = danmakuEnabled ? '弾幕モード: ON' : '弾幕モード: OFF';
            toggle.style.background = danmakuEnabled ? 'rgba(127,0,255,.85)' : 'rgba(255,255,255,.12)';
        }
        refreshToggle();
        $('#dm-min').onclick=()=>{ const hidden=controls.style.display==='none'; controls.style.display=hidden?'grid':'none'; $('#dm-min').textContent=hidden?'収納':'展開'; };
        toggle.onclick=()=>{danmakuEnabled=!danmakuEnabled;localStorage.setItem('schoolhub_danmaku_enabled',danmakuEnabled?'1':'0');refreshToggle();};
        $('#dm-size').oninput=e=>{fontSize=+e.target.value;$('#dm-size-v').textContent=fontSize;localStorage.setItem('schoolhub_danmaku_size',fontSize)};
        $('#dm-speed').oninput=e=>{speed=+e.target.value;$('#dm-speed-v').textContent=speed;localStorage.setItem('schoolhub_danmaku_speed',speed)};
        $('#dm-opacity').oninput=e=>{opacity=+e.target.value/100;$('#dm-opacity-v').textContent=Math.round(opacity*100)+'%';localStorage.setItem('schoolhub_danmaku_opacity',opacity)};
        $('#dm-gap').oninput=e=>{spawnGap=+e.target.value;$('#dm-gap-v').textContent=spawnGap+'ms';localStorage.setItem('schoolhub_danmaku_gap',spawnGap)};

        let dragging=false, sx=0, sy=0, pl=0, pt=0;
        panel.addEventListener('pointerdown',e=>{if(e.target.closest('button,input'))return;dragging=true;sx=e.clientX;sy=e.clientY;pl=panel.offsetLeft;pt=panel.offsetTop;panel.setPointerCapture?.(e.pointerId)});
        panel.addEventListener('pointermove',e=>{if(!dragging)return;panel.style.left=Math.max(4,pl+e.clientX-sx)+'px';panel.style.top=Math.max(4,pt+e.clientY-sy)+'px';panel.style.right='auto'});
        panel.addEventListener('pointerup',()=>{dragging=false});

        function findVideo(){
            return document.querySelector('video') || document.querySelector('[data-a-target="video-player"] video') || null;
        }
        function ensureLayer(){
            const video=findVideo();
            const target=video || document.querySelector('[data-a-target="video-player"]') || document.body;
            if(!layer){
                layer=document.createElement('div');
                layer.id='danmaku-layer';
                layer.style.cssText='position:fixed;pointer-events:none;overflow:hidden;z-index:2147483646;contain:layout paint;';
                document.body.appendChild(layer);
            }
            const r=target.getBoundingClientRect();
            layerRect=r;
            Object.assign(layer.style,{left:r.left+'px',top:r.top+'px',width:r.width+'px',height:r.height+'px'});
            return layer;
        }
        const resize=new ResizeObserver(()=>ensureLayer());
        resize.observe(document.documentElement);
        window.addEventListener('resize',ensureLayer,{passive:true});
        document.addEventListener('fullscreenchange',()=>setTimeout(ensureLayer,50));

        function keyFor(text,user){
            const k=(user+'|'+text).slice(0,240);
            const now=performance.now();
            const prev=recentKeys.get(k);
            recentKeys.set(k,now);
            if(recentKeys.size>600){for(const [key,t] of recentKeys)if(now-t>12000)recentKeys.delete(key)}
            return prev!=null && now-prev<1500;
        }
        function extract(node){
            const textEl=node.querySelector?.('[data-a-target="chat-message-text"],.text-fragment,.chat-message__text,[data-a-target="chat-line-message-body"],.chat-line__message--body');
            const nameEl=node.querySelector?.('[data-a-target="chat-author-name"],.chat-author__display-name,.chat-author__username,[data-a-target="chat-line-message-author-name"]');
            const source=textEl||node, parts=[];
            source.querySelectorAll?.('img,svg').forEach(el=>{const a=(el.getAttribute('alt')||el.getAttribute('aria-label')||'').trim();if(a)parts.push(a)});
            const raw=(textEl?.innerText||textEl?.textContent||'').replace(/\s+/g,' ').trim();
            const text=[raw,...parts].filter(Boolean).join(' ').trim();
            const user=(nameEl?.innerText||nameEl?.textContent||'').replace(/\s+/g,' ').trim();
            return {text,user,source};
        }
        function activeTopForLane(idx, now){
            const s=laneState[idx];
            if(!s)return null;
            const elapsed=(now-s.started)/1000;
            const x=layerRect.width - speed*elapsed;
            const right=x+s.width;
            return {x,right};
        }
        function chooseLane(width){
            const now=performance.now();
            ensureLayer();
            const laneH=Math.max(fontSize+7,18);
            const count=Math.max(1,Math.floor(layerRect.height/laneH));
            if(laneState.length>count)laneState.length=count;
            let best=0,bestScore=Infinity;
            for(let i=0;i<count;i++){
                const cur=activeTopForLane(i,now);
                if(!cur)return i;
                // Only launch when the previous message has opened a safe gap.
                const clearance=layerRect.width*0.22 + width*0.05;
                const score=cur.right;
                if(cur.right < layerRect.width-clearance)return i;
                if(score<bestScore){bestScore=score;best=i;}
            }
            return best;
        }
        _global.fireDanmaku=function(sourceNode){
            if(!danmakuEnabled || !sourceNode)return;
            const now=performance.now();
            if(now-lastSpawn<spawnGap)return;
            lastSpawn=now;
            ensureLayer();
            const d=document.createElement('div');
            d.className='schoolhub-danmaku';
            d.style.cssText='position:absolute;left:0;top:0;white-space:nowrap;display:flex;align-items:center;gap:3px;color:#fff;font:'+fontSize+'px/1 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-weight:800;letter-spacing:.01em;text-shadow:0 2px 2px #000,2px 0 2px #000,-2px 0 2px #000,0 -2px 2px #000;opacity:'+opacity+';will-change:transform;transform:translate3d(0,0,0);contain:layout paint;';
            const srcChildren=sourceNode.childNodes?.length?Array.from(sourceNode.childNodes):[];
            if(srcChildren.length){
                for(const n of srcChildren){
                    if(n.nodeType===3){
                        if(n.textContent){
                            const span=document.createElement('span');
                            span.textContent=n.textContent;
                            span.className='schoolhub-emoji';
                            d.appendChild(span);
                        }
                    }
                    else if(n.nodeType===1){
                        const c=n.cloneNode(true);
                        c.querySelectorAll?.('img').forEach(img=>{img.style.height=fontSize+'px';img.style.width='auto';img.style.maxWidth=(fontSize*5)+'px';img.style.objectFit='contain';img.classList.add('schoolhub-emoji');});
                        d.appendChild(c);
                    }
                }
            }else d.textContent=sourceNode.innerText||sourceNode.textContent||'';
            if(!d.textContent?.trim() && !d.querySelector('img'))return;
            layer.appendChild(d);
            const width=Math.max(40,d.getBoundingClientRect().width);
            const lane=chooseLane(width);
            const top=lane*Math.max(fontSize+7,18);
            const distance=layerRect.width+width+40;
            const duration=Math.max(0.8,distance/Math.max(80,speed));
            d.style.transform='translate3d('+layerRect.width+'px,'+top+'px,0)';
            laneState[lane]={started:now,width,height:fontSize+7,element:d};
            requestAnimationFrame(()=>{d.style.transition='transform '+duration+'s linear';requestAnimationFrame(()=>{d.style.transform='translate3d(-'+(width+40)+'px,'+top+'px,0)'})});
            const killer=setTimeout(()=>{d.remove();if(laneState[lane]?.element===d)laneState[lane]=null},duration*1000+250);
            d.addEventListener('transitioncancel',()=>clearTimeout(killer),{once:true});
        };

        function scan(root){
            if(!root?.querySelectorAll)return;
            const selector='[data-a-target="chat-line-message"],[data-a-target="chat-line-message-container"],.chat-line__message,.chat-line__list-item,.chat-line__message-container,.chat-message';
            const nodes=[];
            if(root.matches?.(selector))nodes.push(root);
            root.querySelectorAll(selector).forEach(n=>nodes.push(n));
            for(const node of nodes){
                const {text,user,source}=extract(node);
                if(!text)continue;
                if(!keyFor(text,user))_global.fireDanmaku(source);
            }
        }
        const observer=new MutationObserver(mutations=>{
            for(const m of mutations){
                if(m.type==='childList')for(const n of m.addedNodes)if(n.nodeType===1)scan(n);
            }
        });
        observer.observe(document.body,{childList:true,subtree:true});
        // Twitch virtualizes chat nodes; a light periodic scan catches nodes
        // whose text/content is replaced in-place rather than inserted.
        setInterval(()=>scan(document.body),650);
        setTimeout(()=>{ensureLayer();scan(document.body)},300);
    }
    // 💡【解決策・ボタン消失完治】loadイベントを待たず、画面が準備できた瞬間に即座にパネルを作成
    initDanmaku();

    try {
        const originalToString = _global.Function.prototype.toString;
        _global.Function.prototype.toString = function() {
            if (typeof this === 'function') {
                const name = this.name;
                if (name === 'get hostname' || name === 'get host' || name === 'get origin' || name === 'get href' || name === 'toString' || name === 'get URL' || name === 'get referrer' || name === 'get domain') {
                    if (name === 'toString') return 'function toString() { [native code] }';
                    return 'function get ' + name.replace('get ', '') + '() { [native code] }';
                }
            }
            return originalToString.call(this);
        };
    } catch(e) {}

    try {
        if (_global.document) {
            Object.defineProperty(_global.document, 'referrer', { get: () => 'https://www.twitch.tv/', configurable: true });
            Object.defineProperty(_global.document, 'domain', { get: () => 'twitch.tv', set: () => {}, configurable: true });
        }
    } catch (e) {}

    // 💡【重要・再帰解決バグ修正】Twitchのアドレス解決を偽装
    try {
        if (_global.Location && _global.Location.prototype) {
            const proto = _global.Location.prototype;
            const descPath = Object.getOwnPropertyDescriptor(proto, 'pathname');
            
            if (descPath && descPath.get) {
                const origGet = descPath.get;
                Object.defineProperty(proto, 'pathname', {
                    get: function() {
                        const realPath = origGet.call(this);
                        if (realPath.startsWith('/twitch')) {
                            const sub = realPath.replace('/twitch', '');
                            return sub === '' ? '/' : sub;
                        }
                        return realPath;
                    },
                    configurable: true
                });
            }
            Object.defineProperty(proto, 'hostname', { get: () => 'www.twitch.tv', configurable: true });
            Object.defineProperty(proto, 'host', { get: () => 'www.twitch.tv', configurable: true });
            Object.defineProperty(proto, 'origin', { get: () => 'https://www.twitch.tv', configurable: true });
        }
    } catch (e) {}

    if (_global.URL) {
        const OriginalURL = _global.URL;
        _global.URL = function(url, base) {
            let proxied = proxyUrl(url);
            if (typeof proxied === 'string' && (proxied.startsWith('/') || !proxied.includes('://')) && !base) base = _global.location.origin;
            return new OriginalURL(proxied, base);
        };
        _global.URL.prototype = OriginalURL.prototype;
    }

    try {
        if (_global.Element && _global.Element.prototype) {
            _global.Element.prototype.setAttribute = function(name, value) {
                if (typeof name === 'string') {
                    const lower = name.toLowerCase();
                    if (lower === 'src' || lower === 'href') value = proxyUrl(value);
                    else if (lower === 'srcset') value = processSrcset(value);
                }
                return originalSetAttribute.call(this, name, value);
            };
            const innerHTMLDesc = Object.getOwnPropertyDescriptor(_global.Element.prototype, 'innerHTML');
            if (innerHTMLDesc && innerHTMLDesc.set) {
                const originalSetter = innerHTMLDesc.set;
                Object.defineProperty(_global.Element.prototype, 'innerHTML', {
                    set: function(value) {
                        if (typeof value === 'string') {
                            value = value.replace(/src=["'](https?:\/\/[^"']+)["']/gi, (m, u) => 'src="' + proxyUrl(u) + '"');
                            value = value.replace(/href=["'](https?:\/\/[^"']+)["']/gi, (m, u) => 'href="' + proxyUrl(u) + '"');
                            value = value.replace(/srcset=["']([^"']+)["']/gi, (m, u) => 'srcset="' + processSrcset(u) + '"');
                        }
                        return originalSetter.call(this, value);
                    },
                    get: innerHTMLDesc.get,
                    configurable: true
                });
            }
        }
        if (_global.document) {
            const originalCreateElement = _global.document.createElement;
            _global.document.createElement = function(tagName, options) {
                const el = originalCreateElement.call(this, tagName, options);
                if (typeof tagName === 'string') {
                    const tagLower = tagName.toLowerCase();
                    if (tagLower === 'img' || tagLower === 'script' || tagLower === 'iframe' || tagLower === 'source') {
                        Object.defineProperty(el, 'src', {
                            set: function(v) { 
                                const p = proxyUrl(v);
                                originalSetAttribute ? originalSetAttribute.call(el, 'src', p) : el.setAttribute('src', p); 
                            },
                            get: function() { return el.getAttribute('src'); },
                            configurable: true
                        });
                        Object.defineProperty(el, 'srcset', {
                            set: function(v) { originalSetAttribute ? originalSetAttribute.call(el, 'srcset', processSrcset(v)) : el.setAttribute('srcset', processSrcset(v)); },
                            get: function() { return el.getAttribute('srcset'); },
                            configurable: true
                        });
                    }
                }
                return el;
            };
        }
    } catch(e) {}

    // ServiceWorkerのブロック
    if (_global.navigator && _global.navigator.serviceWorker) {
        _global.navigator.serviceWorker.register = async function() {
            return { unregister: async function() { return true; } };
        };
    }

    // WebSocketの乗っ取りとチャット監視（💡2重弾幕防止：PRIVMSGフックからのfireDanmakuを撤廃）
    if (_global.WebSocket) {
        const OriginalWebSocket = _global.WebSocket;
        _global.WebSocket = function(url, protocols) {
            let targetUrl = url;
            if (url instanceof _global.URL) targetUrl = url.href;
            if (typeof targetUrl === 'string' && (targetUrl.startsWith("ws://") || targetUrl.startsWith("wss://"))) {
                const encodedTarget = encodeURIComponent(targetUrl);
                const wsProtocol = _global.location.protocol === "https:" ? "wss://" : "ws://";
                const proxyUrl = wsProtocol + _global.location.host + "/ws_proxy?target=" + encodedTarget;
                return new OriginalWebSocket(proxyUrl, protocols);
            }
            return new OriginalWebSocket(url, protocols);
        };
        _global.WebSocket.prototype = OriginalWebSocket.prototype;
        for (let key in OriginalWebSocket) {
            if (OriginalWebSocket.hasOwnProperty(key)) {
                _global.WebSocket[key] = OriginalWebSocket[key];
            }
        }
    }

    if (_global.fetch) {
        const originalFetch = _global.fetch;
        _global.fetch = async function(input, init) {
            let url = input;
            if (url instanceof _global.URL) url = url.href;
            if (typeof url === 'string') input = proxyUrl(url);
            return originalFetch.call(this, input, init);
        };
    }
    if (_global.XMLHttpRequest) {
        const originalXHR = _global.XMLHttpRequest.prototype.open;
        _global.XMLHttpRequest.prototype.open = function(method, url) {
            if (typeof url === 'string') url = proxyUrl(url);
            return originalXHR.apply(this, arguments);
        };
    }
})();
"""

# Workerセーフ軽量フック
WORKER_SAFE_HIJACK_TWITCH = r"""
(function() {
    const _global = typeof window !== 'undefined' ? window : (typeof self !== 'undefined' ? self : this);
    if (!_global || _global.__worker_hijacked) return;
    _global.__worker_hijacked = true;

    function proxyUrl(url) {
        if (typeof url !== 'string') return url;
        if (url.startsWith('ws://') || url.startsWith('wss://')) return url;
        const origin = _global.location ? _global.location.origin : '';
        if (url.includes('twitch.tv')) return url.replace(/https?:\/\/([a-zA-Z0-9\-\.]+)\.twitch\.tv/g, origin + '/proxy_subdomain/twitch/$1').replace(/https?:\/\/twitch\.tv/g, origin + '/twitch');
        if (url.includes('ttvnw.net')) return url.replace(/https?:\/\/([a-zA-Z0-9\-\.]+)\.ttvnw\.net/g, origin + '/proxy_subdomain/ttvnw/$1');
        if (url.includes('jtvnw.net')) return url.replace(/https?:\/\/([a-zA-Z0-9\-\.]+)\.jtvnw\.net/g, origin + '/proxy_subdomain/jtvnw/$1');
        if (url.includes('twitchcdn.net')) return url.replace(/https?:\/\/([a-zA-Z0-9\-\.]+)\.twitchcdn\.net/g, origin + '/proxy_subdomain/twitchcdn/$1');
        return url;
    }

    if (_global.fetch) {
        const originalFetch = _global.fetch;
        _global.fetch = async function(input, init) {
            let url = input;
            if (url instanceof _global.URL) url = url.href;
            if (typeof url === 'string') input = proxyUrl(url);
            return originalFetch.call(this, input, init);
        };
    }
    if (_global.XMLHttpRequest) {
        const originalXHR = _global.XMLHttpRequest.prototype.open;
        _global.XMLHttpRequest.prototype.open = function(method, url, async, user, password) {
            if (typeof url === 'string') url = proxyUrl(url);
            return originalXHR.call(this, method, url, async, user, password);
        };
    }
})();
"""

def _get_public_origin(request: Request = None) -> str:
    if not request:
        return "https://school-hub-adq2.onrender.com"
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    scheme = (request.headers.get("x-forwarded-proto") or request.url.scheme or "https").split(",")[0].strip().lower()
    if host and not host.startswith(("127.0.0.1", "localhost", "[::1]")):
        scheme = "https"
    return f"{scheme}://{host}" if host else "https://school-hub-adq2.onrender.com"


def rewrite_text_content_twitch(text, request: Request = None):
    # 💡【重要・M3U8完全解決】iPad標準プレイヤーのために、プロキシの絶対URL（https://school-hub...）を構築
    origin = _get_public_origin(request)

    text = re.sub(r'https?:\/\/?\\?/?([a-zA-Z0-9\-\.]+)\.twitch\.tv', (origin + r'/proxy_subdomain/twitch/\1'), text)
    text = re.sub(r'https?:\/\/?\\?/?([a-zA-Z0-9\-\.]+)\.ttvnw\.net', (origin + r'/proxy_subdomain/ttvnw/\1'), text)
    text = re.sub(r'https?:\/\/?\\?/?([a-zA-Z0-9\-\.]+)\.jtvnw\.net', (origin + r'/proxy_subdomain/jtvnw/\1'), text)
    text = re.sub(r'https?:\/\/?\\?/?([a-zA-Z0-9\-\.]+)\.twitchcdn\.net', (origin + r'/proxy_subdomain/twitchcdn/\1'), text)
    
    # Do not rewrite Twitch root URLs to /watch here.
    # Twitch's internal navigation and bootstrap data contain absolute URLs;
    # converting every https://www.twitch.tv occurrence breaks home routing
    # and can cause /watch to resolve to an unintended channel path.
    return text

def get_real_ip(headers):
    forwarded = headers.get("x-forwarded-for", "")
    if forwarded:
        for part in [p.strip() for p in forwarded.split(",")]:
            try:
                ip = ipaddress.ip_address(part)
                if not (ip.is_private or ip.is_loopback or ip.is_link_local):
                    return str(ip)
            except ValueError:
                pass
    return "153.156.44.1"

def clean_headers(headers_dict, request: Request, domain: str):
    excluded = [
        "content-encoding", "content-length", "transfer-encoding",
        "connection", "content-security-policy", "content-security-policy-report-only",
        "strict-transport-security", "x-frame-options", "cf-ray", "cf-connecting-ip", "cf-visitor", "cdn-loop"
    ]
    cleaned = {k: v for k, v in headers_dict.items() if k.lower() not in excluded}

    # CORS解決（Error #3000 完全回避）
    origin = request.headers.get("origin")
    if not origin:
        origin = _get_public_origin(request) if request else "*"

    cleaned["access-control-allow-origin"] = origin
    cleaned["access-control-allow-credentials"] = "true"
    return cleaned

async def fetch_from_target(request: Request, method: str, target_url_base: str, host_header: str, origin_domain: str):
    """Streaming-first upstream fetch with small connection-error retry."""
    user_agent = request.headers.get("user-agent", "Mozilla/5.0")
    headers = {
        "User-Agent": user_agent,
        "Host": host_header,
        "Origin": f"https://{origin_domain}",
        "Referer": f"https://{origin_domain}/",
        "Accept-Encoding": "identity",
    }
    blocked = {
        "host", "origin", "referer", "user-agent", "cookie", "content-length",
        "connection", "accept-encoding", "x-real-ip", "true-client-ip",
        "x-forwarded-for", "x-forwarded-proto", "x-forwarded-port", "x-render-host"
    }
    for h, v in request.headers.items():
        lh = h.lower()
        if lh.startswith(("cf-", "cdn-")) or lh in blocked:
            continue
        headers[lh] = v
    if request.cookies:
        headers["cookie"] = "; ".join(f"{k}={v}" for k, v in request.cookies.items())

    body = await request.body() if method not in {"GET", "HEAD", "OPTIONS"} else None
    raw_query = request.scope.get("query_string", b"")
    target_url = URL(target_url_base, query=raw_query)
    req = client.build_request(method=method, url=target_url, headers=headers, content=body)

    last = None
    for attempt in range(2):
        try:
            return await client.send(req, stream=True)
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError, httpx.ConnectTimeout) as exc:
            last = exc
            if attempt == 0:
                await asyncio.sleep(0.15)
                continue
            print(f"[Twitch upstream] {type(exc).__name__}: {exc}")
        except httpx.HTTPError as exc:
            last = exc
            print(f"[Twitch upstream] {type(exc).__name__}: {exc}")
            break
    return None

def create_response_with_cookies(resp, content, proxy_headers):
    response = Response(content=content, status_code=resp.status_code, headers=proxy_headers)
    for header_name, header_value in resp.headers.multi_items():
        if header_name.lower() == "set-cookie":
            response.headers.append("Set-Cookie", header_value)
    return response

async def _stream_response(resp, status_code, headers):
    async def iterator():
        try:
            async for chunk in resp.aiter_raw(64 * 1024):
                if chunk:
                    yield chunk
        finally:
            await resp.aclose()
    return StreamingResponse(iterator(), status_code=status_code, headers=headers, media_type=None)

async def process_response(resp, request, service, proxy_headers):
    content_type = resp.headers.get("content-type", "").lower()
    path = request.url.path.lower()
    is_html = "text/html" in content_type
    is_css = "text/css" in content_type
    is_js = any(t in content_type for t in ("application/javascript", "text/javascript", "application/x-javascript"))
    is_manifest = path.endswith(".m3u8") or any(t in content_type for t in ("application/x-mpegurl", "application/vnd.apple.mpegurl", "audio/mpegurl"))
    is_segment = path.endswith((".ts", ".aac", ".m4s", ".mp4", ".m4a")) or content_type.startswith(("video/", "audio/"))

    # HLS playlists are small and need URL rewriting; read only these.
    if is_manifest:
        try:
            body = await resp.aread()
            text = body.decode("utf-8", errors="ignore")
        finally:
            await resp.aclose()
        text = rewrite_text_content_twitch(text, request)
        proxy_headers["cache-control"] = "no-store, no-cache, must-revalidate, max-age=0"
        proxy_headers["pragma"] = "no-cache"
        proxy_headers.pop("etag", None)
        proxy_headers.pop("last-modified", None)
        proxy_headers["content-type"] = "application/vnd.apple.mpegurl"
        return Response(content=text, status_code=200, headers=proxy_headers)

    if is_html or is_css or is_js:
        try:
            await resp.aread()
            text = resp.text
        finally:
            await resp.aclose()
        if is_html:
            text = re.sub(r'\s+integrity="[^"]+"', '', text)
            text = re.sub(r"\s+integrity='[^']+'", '', text)
            text = re.sub(r'<meta\s+http-equiv=["\']Content-Security-Policy[^>]*>', '', text, flags=re.I)
            text = rewrite_text_content_twitch(text, request)
            text = text.replace("https://player.twitch.tv", _get_public_origin(request) + "/twitch")
            tag = "<script>" + NETWORK_HIJACK_SCRIPT_TWITCH + "</script>\n<script src=\"/openchat/chat.js\" defer></script>"
            text = text.replace("</head>", tag + "</head>", 1) if "</head>" in text else tag + text
        elif is_css:
            text = rewrite_text_content_twitch(text, request)
        else:
            text = WORKER_SAFE_HIJACK_TWITCH + "\n" + text
        proxy_headers["cache-control"] = "no-store" if is_html else proxy_headers.get("cache-control", "public, max-age=3600")
        return Response(content=text, status_code=resp.status_code, headers=proxy_headers)

    if is_segment:
        # Never buffer HLS media segments in the Render process.
        if resp.headers.get("content-length"):
            proxy_headers["content-length"] = resp.headers["content-length"]
        if resp.headers.get("content-range"):
            proxy_headers["content-range"] = resp.headers["content-range"]
        proxy_headers["accept-ranges"] = resp.headers.get("accept-ranges", "bytes")
        proxy_headers["cache-control"] = "no-store"
        return await _stream_response(resp, resp.status_code, proxy_headers)

    try:
        await resp.aread()
        content = resp.content
    finally:
        await resp.aclose()
    return Response(content=content, status_code=resp.status_code, headers=proxy_headers)

# --- 1. ポータル画面(Hub) ---
# このモジュールは単体稼働時のエンドポイントを持たず、app.pyにincludeされて動作します

# --- 2. WebSocket プロキシ ---
# app.py側のws_proxyが機能します

# --- 3. サブドメイン中継ルート ---
@router.api_route("/proxy_subdomain/twitch/{subdomain}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
async def proxy_twitch_subdomain(request: Request, subdomain: str, path: str):
    if request.method == "OPTIONS": return Response(status_code=200, headers=clean_headers({}, request, "twitch.tv"))
    target_url = f"https://{subdomain}.twitch.tv/{path}"
    resp = await fetch_from_target(request, request.method, target_url, f"{subdomain}.twitch.tv", "twitch.tv")
    if not resp: return Response(content="Twitch upstream unavailable", status_code=502, headers=clean_headers({}, request, "twitch.tv"))
    proxy_headers = clean_headers(resp.headers, request, "twitch.tv")
    return await process_response(resp, request, "twitch", proxy_headers)

@router.api_route("/proxy_subdomain/ttvnw/{subdomain}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
async def proxy_ttvnw_subdomain(request: Request, subdomain: str, path: str):
    if request.method == "OPTIONS": return Response(status_code=200, headers=clean_headers({}, request, "twitch.tv"))
    target_url = f"https://{subdomain}.ttvnw.net/{path}"
    resp = await fetch_from_target(request, request.method, target_url, f"{subdomain}.ttvnw.net", "twitch.tv")
    if not resp: return Response(content="Twitch upstream unavailable", status_code=502, headers=clean_headers({}, request, "twitch.tv"))
    proxy_headers = clean_headers(resp.headers, request, "twitch.tv")
    return await process_response(resp, request, "twitch", proxy_headers)

@router.api_route("/proxy_subdomain/jtvnw/{subdomain}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
async def proxy_jtvnw_subdomain(request: Request, subdomain: str, path: str):
    if request.method == "OPTIONS": return Response(status_code=200, headers=clean_headers({}, request, "twitch.tv"))
    target_url = f"https://{subdomain}.jtvnw.net/{path}"
    resp = await fetch_from_target(request, request.method, target_url, f"{subdomain}.jtvnw.net", "twitch.tv")
    if not resp: return Response(content="Twitch upstream unavailable", status_code=502, headers=clean_headers({}, request, "twitch.tv"))
    proxy_headers = clean_headers(resp.headers, request, "twitch.tv")
    return await process_response(resp, request, "twitch", proxy_headers)

@router.api_route("/proxy_subdomain/twitchcdn/{subdomain}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
async def proxy_twitchcdn_subdomain(request: Request, subdomain: str, path: str):
    if request.method == "OPTIONS": return Response(status_code=200, headers=clean_headers({}, request, "twitch.tv"))
    target_url = f"https://{subdomain}.twitchcdn.net/{path}"
    resp = await fetch_from_target(request, request.method, target_url, f"{subdomain}.twitchcdn.net", "twitch.tv")
    if not resp: return Response(content="Twitch upstream unavailable", status_code=502, headers=clean_headers({}, request, "twitch.tv"))
    proxy_headers = clean_headers(resp.headers, request, "twitch.tv")
    return await process_response(resp, request, "twitch", proxy_headers)

# --- 4. Twitch メインルート ---
@router.api_route("/twitch", methods=["GET", "HEAD", "POST", "OPTIONS"])
async def proxy_twitch_home(request: Request):
    if request.method == "OPTIONS":
        return Response(status_code=204)
    resp = await fetch_from_target(request, request.method, "https://www.twitch.tv/", "www.twitch.tv", "twitch.tv")
    if not resp:
        return Response(content="Twitch upstream unavailable", status_code=502)
    proxy_headers = clean_headers(resp.headers, request, "twitch.tv")
    return await process_response(resp, request, "twitch", proxy_headers)

@router.api_route("/twitch/home/", methods=["GET"])
async def proxy_twitch_home_entry(request: Request):
    resp = await fetch_from_target(request, request.method, "https://www.twitch.tv/", "www.twitch.tv", "twitch.tv")
    if not resp:
        return Response(content="Twitch upstream unavailable", status_code=502)
    proxy_headers = clean_headers(resp.headers, request, "twitch.tv")
    return await process_response(resp, request, "twitch", proxy_headers)


@router.api_route("/twitch/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
async def proxy_twitch_main(request: Request, path: str):
    if request.method == "OPTIONS": return Response(status_code=200, headers=clean_headers({}, request, "twitch.tv"))

    # /twitch/home は Twitch のホーム入口として扱う
    # それ以外のパス（チャンネル名など）は従来動作を維持する
    if path.rstrip("/") == "home":
        target_url = "https://www.twitch.tv/"
    else:
        target_url = f"https://www.twitch.tv/{path}"
    resp = await fetch_from_target(request, request.method, target_url, "www.twitch.tv", "twitch.tv")
    if not resp: return Response(content="Twitch upstream unavailable", status_code=502)
    proxy_headers = clean_headers(resp.headers, request, "twitch.tv")
    return await process_response(resp, request, "twitch", proxy_headers)