"""Isolated Interstellar compatibility routes for SW, Scramjet, Bare-Mux, and UI scripts."""

from __future__ import annotations

import re

from fastapi import APIRouter, Request
from starlette.responses import Response

import interstellar_proxy as _interstellar

router = APIRouter()

_NO_STORE_JS_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
    "X-Interstellar-Isolated": "1",
}

_SW_JS = r"""// SchoolHub isolated Interstellar Service Worker
const DEFAULT_SJ_CONFIG = {
  prefix: "/interstellar/a/sj/",
  globals: {
    wrapfn: "$scramjet$wrap",
    wrappropertybase: "$scramjet__",
    wrappropertyfn: "$scramjet$prop",
    cleanrestfn: "$scramjet$clean",
    importfn: "$scramjet$import",
    rewritefn: "$scramjet$rewrite",
    metafn: "$scramjet$meta",
    setrealmfn: "$scramjet$setrealm",
    pushsourcemapfn: "$scramjet$pushsourcemap",
    trysetfn: "$scramjet$tryset",
    templocid: "$scramjet$temploc",
    tempunusedid: "$scramjet$tempunused",
  },
  files: {
    wasm: "/interstellar/assets/languagearts/sj.wasm.wasm",
    all: "/interstellar/assets/languagearts/sj.all.js",
    sync: "/interstellar/assets/languagearts/sj.sync.js",
  },
  flags: {
    serviceworkers: false,
    syncxhr: false,
    strictRewrites: true,
    rewriterLogs: false,
    captureErrors: true,
    cleanErrors: true,
    scramitize: false,
    sourcemaps: true,
    destructureRewrites: false,
    interceptDownloads: false,
    allowInvalidJs: true,
    allowFailedIntercepts: true,
  },
  siteFlags: {},
  codec: {
    encode: "e=>e?encodeURIComponent(e):e",
    decode: "e=>e?decodeURIComponent(e):e",
  },
};

try {
  if (typeof indexedDB !== "undefined" && indexedDB.open) {
    const _open = indexedDB.open.bind(indexedDB);
    indexedDB.open = function (name, ver) {
      const isSj = name === "$scramjet";
      const req = _open(name, isSj ? 1 : ver);
      if (isSj) {
        req.addEventListener("upgradeneeded", () => {
          try {
            const db = req.result;
            for (const s of ["config", "cookies", "redirectTrackers", "referrerPolicies", "publicSuffixList"]) {
              if (!db.objectStoreNames.contains(s)) {
                db.createObjectStore(s);
              }
            }
            try {
              const tx = req.transaction;
              if (tx) {
                tx.objectStore("config").put(DEFAULT_SJ_CONFIG, "config");
              }
            } catch (e) {}
          } catch (e) {}
        });
      }
      return req;
    };
  }
} catch (e) {}

async function ensureScramjetConfigInIDB() {
  const initDb = () =>
    new Promise((resolve, reject) => {
      const req = indexedDB.open("$scramjet", 1);
      req.onupgradeneeded = () => {
        const db = req.result;
        for (const s of ["config", "cookies", "redirectTrackers", "referrerPolicies", "publicSuffixList"]) {
          if (!db.objectStoreNames.contains(s)) {
            db.createObjectStore(s);
          }
        }
      };
      req.onsuccess = () => {
        try {
          const db = req.result;
          const tx = db.transaction("config", "readwrite");
          const store = tx.objectStore("config");
          const getReq = store.get("config");
          getReq.onsuccess = () => {
            const cur = getReq.result;
            if (!cur || !cur.prefix || cur.prefix === "/a/sj/" || !cur.files || String(cur.codec?.encode || "").includes("charCodeAt")) {
              store.put(DEFAULT_SJ_CONFIG, "config");
            }
          };
          tx.oncomplete = () => {
            try { db.close(); } catch (e) {}
            resolve();
          };
          tx.onerror = () => {
            try { db.close(); } catch (e) {}
            resolve();
          };
        } catch (e) {
          resolve();
        }
      };
      req.onerror = () => reject(req.error);
      req.onblocked = () => resolve();
    });
  try {
    await initDb();
  } catch (e) {
    try {
      await new Promise((res) => {
        const del = indexedDB.deleteDatabase("$scramjet");
        del.onsuccess = del.onerror = del.onblocked = () => res();
      });
      await initDb();
    } catch (e2) {}
  }
}

importScripts("/interstellar/assets/history/config.js?v=2025-04-15");
importScripts("/interstellar/assets/history/worker.js?v=2025-04-15");
importScripts("/interstellar/assets/mathematics/bundle.js?v=2025-04-15");
importScripts("/interstellar/assets/mathematics/config.js?v=2025-04-15");
importScripts(__uv$config.sw || "/interstellar/assets/mathematics/sw.js?v=2025-04-15");
importScripts("/interstellar/assets/languagearts/sj.all.js?v=2025-04-15");

const { ScramjetServiceWorker } = $scramjetLoadWorker();
const uv = new UVServiceWorker();
const dynamic = new Dynamic();
const sj = new ScramjetServiceWorker();

const userKey = new URL(location).searchParams.get("userkey");
self.dynamic = dynamic;

let _idbReadyPromise = ensureScramjetConfigInIDB();

self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", event => {
  event.waitUntil(
    Promise.all([
      self.clients.claim(),
      _idbReadyPromise,
    ]),
  );
});

self.addEventListener("fetch", event => {
  event.respondWith(
    (async () => {
      try {
        if (!sj.config) {
          await _idbReadyPromise;
          await sj.loadConfig();
        }
        if (sj.config && sj.config.prefix === "/a/sj/") {
          sj.config.prefix = "/interstellar/a/sj/";
        }
      } catch (err) {
        console.warn("Scramjet loadConfig warning:", err);
      }

      if (sj.config && (await sj.route(event))) {
        return await sj.fetch(event);
      }

      if (await dynamic.route(event)) {
        return await dynamic.fetch(event);
      }

      if (
        event.request.url.startsWith(`${location.origin}/interstellar/a/`) ||
        event.request.url.startsWith(`${location.origin}/a/`)
      ) {
        return await uv.fetch(event);
      }

      return await fetch(event.request);
    })(),
  );
});
"""

_SJ_TP_JS = r"""(() => {
  if (window.__isSjTpReady) {
    return;
  }

  window.__isSjTpReady = true;

  const sjConfig = {
    prefix: "/interstellar/a/sj/",
    files: {
      wasm: "/interstellar/assets/languagearts/sj.wasm.wasm",
      all: "/interstellar/assets/languagearts/sj.all.js",
      sync: "/interstellar/assets/languagearts/sj.sync.js",
    },
    flags: {
      rewriterLogs: false,
      scramitize: false,
      cleanErrors: true,
      sourcemaps: true,
    },
  };

  function getWispUrl() {
    const protocol = location.protocol === "https:" ? "wss" : "ws";
    return `${protocol}://${location.host}/interstellar/wisp/`;
  }

  async function ensureServiceWorker() {
    if (!("serviceWorker" in navigator)) return null;
    try {
      if (typeof navigator.serviceWorker.startMessages === "function") {
        try { navigator.serviceWorker.startMessages(); } catch (e) {}
      }
      const reg = await navigator.serviceWorker.register("/interstellar/sw.js?v=2025-04-15", {
        scope: "/interstellar/",
      });
      await navigator.serviceWorker.ready;
      if (!navigator.serviceWorker.controller) {
        await new Promise(resolve => {
          let done = false;
          const finish = () => {
            if (!done) {
              done = true;
              resolve();
            }
          };
          navigator.serviceWorker.addEventListener("controllerchange", finish, { once: true });
          setTimeout(finish, 1200);
        });
      }
      return reg;
    } catch (e) {
      try {
        return await navigator.serviceWorker.register("/interstellar/sw.js?v=2025-04-15", {
          scope: "/interstellar/a/",
        });
      } catch (err) {
        console.warn("SW registration fallback:", err);
        return null;
      }
    }
  }

  async function initSjTransport() {
    if (typeof window.$scramjetLoadController !== "function") {
      throw new Error("Sj bundle did not load.");
    }

    if ("serviceWorker" in navigator && typeof navigator.serviceWorker.startMessages === "function") {
      try { navigator.serviceWorker.startMessages(); } catch (e) {}
    }

    const [{ BareMuxConnection }, { ScramjetController }] = await Promise.all([
      import("/interstellar/bm/index.mjs"),
      Promise.resolve(window.$scramjetLoadController()),
    ]);

    const sj = new ScramjetController(sjConfig);
    window.__isSj = {
      connection: null,
      controller: sj,
      encodeUrl: url => sj.encodeUrl(url),
      decodeUrl: url => sj.decodeUrl(url),
      pxyUrl: url => sj.encodeUrl(url),
    };

    try {
      const connection = new BareMuxConnection("/interstellar/bm/worker.js");
      if ("serviceWorker" in navigator && typeof navigator.serviceWorker.startMessages === "function") {
        try { navigator.serviceWorker.startMessages(); } catch (e) {}
      }
      await connection.setTransport("/interstellar/ep/index.mjs", [{ wisp: getWispUrl() }]);
      window.__isSj.connection = connection;
    } catch (err) {
      console.warn("BareMux transport setup warning:", err);
    }

    try {
      await sj.init();
    } catch (err) {
      console.warn("Scramjet IDB init warning (recovering):", err);
      try {
        await new Promise(res => {
          const del = indexedDB.deleteDatabase("$scramjet");
          del.onsuccess = del.onerror = del.onblocked = () => res();
        });
        await sj.init();
      } catch (err2) {
        console.warn("Scramjet IDB retry failed:", err2);
      }
    }

    await ensureServiceWorker();
  }

  async function boot() {
    try {
      await initSjTransport();
    } catch (error) {
      console.error("Failed to initialize sj transport:", error);
    }
  }

  if (document.readyState === "loading") {
    window.__isSjReady = new Promise(resolve => {
      document.addEventListener(
        "DOMContentLoaded",
        () => {
          void boot().finally(resolve);
        },
        { once: true },
      );
    });
  } else {
    window.__isSjReady = boot();
  }
})();
"""

_INDEX3_JS = r"""// index-3.js (isolated under /interstellar)
window.addEventListener("load", () => {
  if ("serviceWorker" in navigator) {
    if (typeof navigator.serviceWorker.startMessages === "function") {
      try { navigator.serviceWorker.startMessages(); } catch (e) {}
    }
    navigator.serviceWorker
      .register("/interstellar/sw.js?v=2025-04-15", { scope: "/interstellar/" })
      .catch(() =>
        navigator.serviceWorker.register("/interstellar/sw.js?v=2025-04-15", {
          scope: "/interstellar/a/",
        }),
      );
  }
});

function isInsideTabsView() {
  try {
    if (window.parent && window.parent !== window) {
      const pp = window.parent.location.pathname;
      if (pp === "/interstellar/d" || pp === "/d") return true;
    }
  } catch {}
  try {
    if (window.top && window.top !== window) {
      const tp = window.top.location.pathname;
      if (tp === "/interstellar/d" || tp === "/d") return true;
    }
  } catch {}
  return false;
}

let xl = isInsideTabsView();

function resolveSearchEngine() {
  const raw = (localStorage.getItem("engine") || "").trim();
  if (
    !raw ||
    raw.includes("google.com/search") ||
    raw.includes("search.brave.com") ||
    raw.includes("duckduckgo.com")
  ) {
    return "https://search.yahoo.co.jp/search?p=";
  }
  return raw;
}

const form = document.getElementById("fv");
const input = document.getElementById("input");

if (form && input) {
  form.addEventListener("submit", async event => {
    event.preventDefault();
    try {
      if (isInsideTabsView()) await processUrl(input.value, "");
      else await processUrl(input.value, "/interstellar/d");
    } catch {
      await processUrl(input.value, "/interstellar/d");
    }
  });
}

function useScramjetPxy() {
  const p = localStorage.getItem("pchoice") || "sj";
  return p === "sj";
}

function fallbackXorEncode(str) {
  if (!str) return str;
  let out = "";
  for (let i = 0; i < str.length; i++) {
    out += i % 2 ? String.fromCharCode(str.charCodeAt(i) ^ 2) : str[i];
  }
  return encodeURIComponent(out);
}

async function getPxyUrl(url) {
  const p = localStorage.getItem("pchoice") || "sj";
  if (p === "dy") {
    const enc =
      typeof __uv$config !== "undefined" && __uv$config?.encodeUrl
        ? __uv$config.encodeUrl(url)
        : fallbackXorEncode(url);
    return `/interstellar/a/q/${enc}`;
  }
  if (useScramjetPxy()) {
    try {
      if (window.__isSjReady) {
        await window.__isSjReady;
      }
      if (window.__isSj?.encodeUrl) {
        return window.__isSj.encodeUrl(url);
      }
    } catch (e) {
      console.warn("Scramjet encode fallback:", e);
    }
    return `/interstellar/a/sj/${encodeURIComponent(url)}`;
  }

  const enc =
    typeof __uv$config !== "undefined" && __uv$config?.encodeUrl
      ? __uv$config.encodeUrl(url)
      : fallbackXorEncode(url);
  return `/interstellar/a/${enc}`;
}

async function processUrl(value, path) {
  let url = (value || "").trim();
  if (!url) return;
  const searchUrl = resolveSearchEngine();

  if (!isUrl(url)) {
    url = searchUrl + encodeURIComponent(url);
  } else if (!(url.startsWith("https://") || url.startsWith("http://"))) {
    url = `https://${url}`;
  }

  sessionStorage.setItem("GoRawUrl", url);
  const pxyUrl = await getPxyUrl(url);
  sessionStorage.setItem("GoUrl", pxyUrl);

  if (path) {
    location.href = path.startsWith("/interstellar")
      ? path
      : `/interstellar/${path.replace(/^\/+/, "")}`;
  } else {
    window.location.href = pxyUrl;
  }
}

function go(value) {
  processUrl(value, "/interstellar/d");
}

function blank(value) {
  processUrl(value, "");
}

function dy(value) {
  const enc =
    typeof __uv$config !== "undefined" && __uv$config?.encodeUrl
      ? __uv$config.encodeUrl(value)
      : fallbackXorEncode(value);
  processUrl(value, `/interstellar/a/q/${enc}`);
}

function isUrl(val = "") {
  if (/^http(s?):\/\//.test(val) || (val.includes(".") && val.substr(0, 1) !== " ")) {
    return true;
  }
  return false;
}

function handleInitialQuery() {
  try {
    const params = new URLSearchParams(window.location.search);
    const initial = (params.get("url") || params.get("q") || "").trim();
    if (initial) {
      if (input) input.value = initial;
      const cleanUrl = window.location.pathname;
      window.history.replaceState({}, "", cleanUrl);
      setTimeout(() => {
        if (isInsideTabsView()) void processUrl(initial, "");
        else void processUrl(initial, "/interstellar/d");
      }, 60);
    }
  } catch (e) {}
}
if (document.readyState === "loading") {
  window.addEventListener("DOMContentLoaded", handleInitialQuery, { once: true });
} else {
  handleInitialQuery();
}
"""

_T3_JS = r"""// t3.js (isolated under /interstellar)
function fallbackXorEncode(str) {
  if (!str) return str;
  let out = "";
  for (let i = 0; i < str.length; i++) {
    out += i % 2 ? String.fromCharCode(str.charCodeAt(i) ^ 2) : str[i];
  }
  return encodeURIComponent(out);
}

function decodeXor(input) {
  if (!input) {
    return input;
  }
  const [str, ...search] = input.split("?");
  return (
    decodeURIComponent(str)
      .split("")
      .map((char, ind) => (ind % 2 ? String.fromCharCode(char.charCodeAt(Number.NaN) ^ 2) : char))
      .join("") + (search.length ? `?${search.join("?")}` : "")
  );
}

function useScramjetPxy() {
  const p = localStorage.getItem("pchoice") || "sj";
  return p === "sj";
}

function resolveSearchEngine() {
  const raw = (localStorage.getItem("engine") || "").trim();
  if (
    !raw ||
    raw.includes("google.com/search") ||
    raw.includes("search.brave.com") ||
    raw.includes("duckduckgo.com")
  ) {
    return "https://search.yahoo.co.jp/search?p=";
  }
  return raw;
}

async function ensureTransportReady() {
  try {
    if ("serviceWorker" in navigator) {
      if (typeof navigator.serviceWorker.startMessages === "function") {
        try { navigator.serviceWorker.startMessages(); } catch (e) {}
      }
      await navigator.serviceWorker
        .register("/interstellar/sw.js?v=2025-04-15", { scope: "/interstellar/" })
        .catch(() =>
          navigator.serviceWorker.register("/interstellar/sw.js?v=2025-04-15", {
            scope: "/interstellar/a/",
          }),
        );
      await navigator.serviceWorker.ready;
      if (!navigator.serviceWorker.controller) {
        await new Promise(resolve => {
          let done = false;
          const finish = () => {
            if (!done) {
              done = true;
              resolve();
            }
          };
          navigator.serviceWorker.addEventListener("controllerchange", finish, { once: true });
          setTimeout(finish, 1200);
        });
      }
    }
  } catch (e) {}
  try {
    if (window.__isSjReady) {
      await window.__isSjReady;
    }
  } catch (e) {}
}

async function getPxyUrl(url) {
  await ensureTransportReady();
  const p = localStorage.getItem("pchoice") || "sj";
  if (p === "dy") {
    const enc =
      typeof __uv$config !== "undefined" && __uv$config?.encodeUrl
        ? __uv$config.encodeUrl(url)
        : fallbackXorEncode(url);
    return `/interstellar/a/q/${enc}`;
  }
  if (p === "sj") {
    if (window.__isSj?.encodeUrl) {
      return window.__isSj.encodeUrl(url);
    }
    return `/interstellar/a/sj/${encodeURIComponent(url)}`;
  }
  const enc =
    typeof __uv$config !== "undefined" && __uv$config?.encodeUrl
      ? __uv$config.encodeUrl(url)
      : fallbackXorEncode(url);
  return `/interstellar/a/${enc}`;
}

function getPxyUrlSync(url) {
  const p = localStorage.getItem("pchoice") || "sj";
  if (p === "dy") {
    const enc =
      typeof __uv$config !== "undefined" && __uv$config?.encodeUrl
        ? __uv$config.encodeUrl(url)
        : fallbackXorEncode(url);
    return `/interstellar/a/q/${enc}`;
  }
  if (p === "sj") {
    if (window.__isSj?.encodeUrl) {
      return window.__isSj.encodeUrl(url);
    }
    return `/interstellar/a/sj/${encodeURIComponent(url)}`;
  }
  const enc =
    typeof __uv$config !== "undefined" && __uv$config?.encodeUrl
      ? __uv$config.encodeUrl(url)
      : fallbackXorEncode(url);
  return `/interstellar/a/${enc}`;
}

window.__isGetPxyUrl = getPxyUrlSync;

function isUrl(val = "") {
  if (/^http(s?):\/\//.test(val) || (val.includes(".") && val.substr(0, 1) !== " ")) {
    return true;
  }
  return false;
}

function prependHttps(url) {
  if (!url.startsWith("http://") && !url.startsWith("https://")) {
    return `https://${url}`;
  }
  return url;
}

async function processUrl(url) {
  sessionStorage.setItem("GoRawUrl", url);
  const pxyUrl = await getPxyUrl(url);
  sessionStorage.setItem("GoUrl", pxyUrl);
  const iframeContainer = document.getElementById("frame-container");
  const activeIframe = Array.from(iframeContainer.querySelectorAll("iframe")).find(iframe =>
    iframe.classList.contains("active"),
  );
  if (activeIframe) {
    if (useScramjetPxy() && window.__isSj?.controller?.createFrame && !activeIframe.name) {
      try {
        window.__isSj.controller.createFrame(activeIframe);
      } catch (e) {}
    }
    activeIframe.src = pxyUrl;
    activeIframe.dataset.tabUrl = url;
  }
  const input = document.getElementById("input");
  if (input) input.value = url;
}

document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("fv");
  const input = document.getElementById("input");
  if (form && input) {
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const formValue = input.value.trim();
      if (!formValue) return;
      const engine = resolveSearchEngine();
      const url = isUrl(formValue)
        ? prependHttps(formValue)
        : `${engine}${encodeURIComponent(formValue)}`;
      await processUrl(url);
    });
  }

  const addTabButton = document.getElementById("add-tab");
  const tabList = document.getElementById("tab-list");
  const iframeContainer = document.getElementById("frame-container");
  let tabCounter = 1;

  if (addTabButton) {
    addTabButton.addEventListener("click", () => {
      createNewTab();
      Load();
    });
  }

  function normalizeProxyPath(value) {
    if (!value) return "/interstellar/";
    if (value === "/" || value === "./") return "/interstellar/";
    if (value.startsWith("/a/")) return "/interstellar" + value;
    if (value.startsWith("/e/")) return "/interstellar" + value;
    if (value.startsWith("/")) return window.location.origin + value;
    return value;
  }

  function createNewTab() {
    const newTab = document.createElement("li");
    const tabTitle = document.createElement("span");
    const newIframe = document.createElement("iframe");
    newIframe.name = `sj-tab-${tabCounter}-${Math.random().toString(36).slice(2, 8)}`;
    newIframe.sandbox =
      "allow-same-origin allow-scripts allow-forms allow-pointer-lock allow-modals allow-orientation-lock allow-presentation allow-storage-access-by-user-activation";
    tabTitle.textContent = `New Tab ${tabCounter}`;
    tabTitle.className = "t";
    newTab.dataset.tabId = tabCounter;
    newTab.addEventListener("click", switchTab);
    newTab.setAttribute("draggable", true);
    const closeButton = document.createElement("button");
    closeButton.classList.add("close-tab");
    closeButton.innerHTML = "&#10005;";
    closeButton.addEventListener("click", closeTab);
    newTab.appendChild(tabTitle);
    newTab.appendChild(closeButton);
    tabList.appendChild(newTab);

    for (const tab of Array.from(tabList.querySelectorAll("li"))) {
      tab.classList.remove("active");
    }
    for (const iframe of Array.from(iframeContainer.querySelectorAll("iframe"))) {
      iframe.classList.remove("active");
    }
    newTab.classList.add("active");
    newIframe.dataset.tabId = tabCounter;
    newIframe.classList.add("active");

    newIframe.addEventListener("load", () => {
      try {
        const title = newIframe.contentDocument?.title || "";
        tabTitle.textContent = title.length <= 1 ? "Tab" : title;
        if (newIframe.contentWindow) {
          newIframe.contentWindow.open = url => {
            const pxyUrl = window.__isGetPxyUrl
              ? window.__isGetPxyUrl(url)
              : getPxyUrlSync(url);
            sessionStorage.setItem("URL", pxyUrl);
            createNewTab();
            return null;
          };
        }
      } catch (e) {}
      Load();
    });

    const currentTabIndex = tabCounter;
    iframeContainer.appendChild(newIframe);
    tabCounter += 1;

    const rawUrl = sessionStorage.getItem("GoRawUrl");
    const goUrl = sessionStorage.getItem("GoUrl");
    const popupUrl = sessionStorage.getItem("URL");

    (async () => {
      await ensureTransportReady();
      if (window.__isSj?.controller?.createFrame) {
        try {
          window.__isSj.controller.createFrame(newIframe);
        } catch (e) {}
      }
      if (currentTabIndex > 1 && popupUrl !== null) {
        sessionStorage.removeItem("URL");
        newIframe.src = normalizeProxyPath(popupUrl);
        return;
      }
      if (currentTabIndex === 1 && rawUrl) {
        sessionStorage.removeItem("GoRawUrl");
        const freshUrl = await getPxyUrl(rawUrl);
        sessionStorage.setItem("GoUrl", freshUrl);
        newIframe.src = normalizeProxyPath(freshUrl);
        if (input) input.value = rawUrl;
        return;
      }
      if (currentTabIndex === 1 && goUrl !== null) {
        newIframe.src = normalizeProxyPath(goUrl);
        return;
      }
      newIframe.src = "/interstellar/";
    })();
  }

  function closeTab(event) {
    event.stopPropagation();
    const tabId = event.target.closest("li").dataset.tabId;
    const tabToRemove = tabList.querySelector(`[data-tab-id='${tabId}']`);
    const iframeToRemove = iframeContainer.querySelector(`[data-tab-id='${tabId}']`);
    if (tabToRemove && iframeToRemove) {
      tabToRemove.remove();
      iframeToRemove.remove();
      const remainingTabs = Array.from(tabList.querySelectorAll("li"));
      if (remainingTabs.length === 0) {
        tabCounter = 0;
        if (input) input.value = "";
      } else {
        const nextTabIndex = remainingTabs.findIndex(tab => tab.dataset.tabId !== tabId);
        if (nextTabIndex > -1) {
          const nextTabToActivate = remainingTabs[nextTabIndex];
          const nextIframeToActivate = iframeContainer.querySelector(
            `[data-tab-id='${nextTabToActivate.dataset.tabId}']`,
          );
          for (const tab of remainingTabs) tab.classList.remove("active");
          nextTabToActivate.classList.add("active");
          for (const iframe of Array.from(iframeContainer.querySelectorAll("iframe"))) {
            iframe.classList.remove("active");
          }
          if (nextIframeToActivate) nextIframeToActivate.classList.add("active");
        }
      }
    }
  }

  function switchTab(event) {
    const tabId = event.target.closest("li").dataset.tabId;
    for (const tab of Array.from(tabList.querySelectorAll("li"))) {
      tab.classList.remove("active");
    }
    for (const iframe of Array.from(iframeContainer.querySelectorAll("iframe"))) {
      iframe.classList.remove("active");
    }
    const selectedTab = tabList.querySelector(`[data-tab-id='${tabId}']`);
    if (selectedTab) {
      selectedTab.classList.add("active");
      Load();
    }
    const selectedIframe = iframeContainer.querySelector(`[data-tab-id='${tabId}']`);
    if (selectedIframe) {
      selectedIframe.classList.add("active");
    }
  }

  let dragTab = null;
  tabList.addEventListener("dragstart", event => {
    dragTab = event.target;
  });
  tabList.addEventListener("dragover", event => {
    event.preventDefault();
    const targetTab = event.target;
    if (targetTab.tagName === "LI" && targetTab !== dragTab) {
      const targetIndex = Array.from(tabList.children).indexOf(targetTab);
      const dragIndex = Array.from(tabList.children).indexOf(dragTab);
      if (targetIndex < dragIndex) {
        tabList.insertBefore(dragTab, targetTab);
      } else {
        tabList.insertBefore(dragTab, targetTab.nextSibling);
      }
    }
  });
  tabList.addEventListener("dragend", () => {
    dragTab = null;
  });

  const tb = document.getElementById("tabs-button");
  const nb = document.getElementById("right-side-nav");
  if (tb && nb) {
    tb.addEventListener("click", () => {
      const activeIframe = document.querySelector("#frame-container iframe.active");
      if (nb.style.display === "none") {
        nb.style.display = "";
        if (activeIframe) {
          activeIframe.style.top = "10%";
          activeIframe.style.height = "90%";
        }
        tb.querySelector("i")?.classList.remove("fa-magnifying-glass-plus");
        tb.querySelector("i")?.classList.add("fa-magnifying-glass-minus");
      } else {
        nb.style.display = "none";
        if (activeIframe) {
          activeIframe.style.top = "5%";
          activeIframe.style.height = "95%";
        }
        tb.querySelector("i")?.classList.remove("fa-magnifying-glass-minus");
        tb.querySelector("i")?.classList.add("fa-magnifying-glass-plus");
      }
    });
  }

  createNewTab();
});

function reload() {
  const activeIframe = document.querySelector("#frame-container iframe.active");
  if (activeIframe) {
    activeIframe.src = activeIframe.src;
    Load();
  }
}

function popout() {
  const activeIframe = document.querySelector("#frame-container iframe.active");
  if (activeIframe) {
    const newWindow = window.open("about:blank", "_blank");
    if (newWindow) {
      const name = localStorage.getItem("name") || "Google";
      const icon =
        localStorage.getItem("icon") ||
        "/favicon.ico";
      newWindow.document.title = name;
      const link = newWindow.document.createElement("link");
      link.rel = "icon";
      link.href = encodeURI(icon);
      newWindow.document.head.appendChild(link);

      const newIframe = newWindow.document.createElement("iframe");
      const style = newIframe.style;
      style.position = "fixed";
      style.top = style.bottom = style.left = style.right = 0;
      style.border = style.outline = "none";
      style.width = style.height = "100%";
      newIframe.src = activeIframe.src;
      newWindow.document.body.appendChild(newIframe);
    }
  }
}

function eToggle() {
  const activeIframe = document.querySelector("#frame-container iframe.active");
  if (!activeIframe) return;
  const erudaWindow = activeIframe.contentWindow;
  if (!erudaWindow) return;
  if (erudaWindow.eruda) {
    if (erudaWindow.eruda._isInit) erudaWindow.eruda.destroy();
  } else {
    const erudaDocument = activeIframe.contentDocument;
    if (!erudaDocument) return;
    const script = erudaDocument.createElement("script");
    script.src = "https://cdn.jsdelivr.net/npm/eruda";
    script.onload = () => {
      if (erudaWindow.eruda) {
        erudaWindow.eruda.init();
        erudaWindow.eruda.show();
      }
    };
    erudaDocument.head.appendChild(script);
  }
}

function FS() {
  const activeIframe = document.querySelector("#frame-container iframe.active");
  if (activeIframe && activeIframe.contentDocument) {
    if (activeIframe.contentDocument.fullscreenElement) {
      activeIframe.contentDocument.exitFullscreen();
    } else {
      activeIframe.contentDocument.documentElement.requestFullscreen();
    }
  }
}

const fullscreenButton = document.getElementById("fullscreen-button");
if (fullscreenButton) fullscreenButton.addEventListener("click", FS);

function Home() {
  window.location.href = "/interstellar/";
}

const homeButton = document.getElementById("home-page");
if (homeButton) homeButton.addEventListener("click", Home);

function goBack() {
  const activeIframe = document.querySelector("#frame-container iframe.active");
  if (activeIframe && activeIframe.contentWindow) {
    activeIframe.contentWindow.history.back();
    Load();
  }
}

function goForward() {
  const activeIframe = document.querySelector("#frame-container iframe.active");
  if (activeIframe && activeIframe.contentWindow) {
    activeIframe.contentWindow.history.forward();
    Load();
  }
}

if (navigator.userAgent.includes("Chrome") && navigator.keyboard?.lock) {
  window.addEventListener("resize", () => {
    try {
      navigator.keyboard.lock(["Escape"]);
    } catch (e) {}
  });
}

function Load() {
  const activeIframe = document.querySelector("#frame-container iframe.active");
  try {
    if (
      activeIframe &&
      activeIframe.contentWindow &&
      activeIframe.contentWindow.document.readyState === "complete"
    ) {
      const website = activeIframe.contentWindow.document.location.href;
      const inputEl = document.getElementById("input");
      if (!inputEl) return;
      if (website.includes("/a/sj/")) {
        if (window.__isSj?.decodeUrl) {
          const decodedValue = window.__isSj.decodeUrl(website);
          localStorage.setItem("decoded", decodedValue);
          inputEl.value = decodedValue;
        } else {
          const raw = website.split("/a/sj/")[1] || "";
          inputEl.value = decodeURIComponent(raw);
        }
      } else if (website.includes("/a/q/")) {
        const websitePath = website.split("/a/q/")[1] || "";
        const decodedValue = decodeXor(websitePath);
        localStorage.setItem("decoded", websitePath);
        inputEl.value = decodedValue;
      } else if (website.includes("/a/")) {
        const websitePath = website.split("/a/")[1] || "";
        localStorage.setItem("decoded", websitePath);
        const decodedValue = decodeXor(websitePath);
        inputEl.value = decodedValue;
      } else {
        const websitePath = website.replace(window.location.origin, "");
        inputEl.value = websitePath === "/interstellar/" ? "" : websitePath;
        localStorage.setItem("decoded", websitePath);
      }
    }
  } catch (e) {}
}
"""

_UV_CONFIG_JS = r"""self.__uv$config = {
  prefix: "/interstellar/a/",
  bare: "/interstellar/ca/",
  encodeUrl: Ultraviolet.codec.xor.encode,
  decodeUrl: Ultraviolet.codec.xor.decode,
  handler: "/interstellar/assets/mathematics/handler.js?v=9-30-2024",
  bundle: "/interstellar/assets/mathematics/bundle.js?v=9-30-2024",
  config: "/interstellar/assets/mathematics/config.js?v=9-30-2024",
  sw: "/interstellar/assets/mathematics/sw.js?v=9-30-2024",
};
"""

_DYNAMIC_CONFIG_JS = r"""self.__dynamic$config = {
  prefix: "/interstellar/a/q/",
  encoding: "xor",
  mode: "production",
  logLevel: 0,
  bare: {
    version: 2,
    path: "/interstellar/ca/",
  },
  tab: {
    title: null,
    icon: null,
    ua: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.3",
  },
  assets: {
    prefix: "/interstellar/assets/history/",
    files: {
      handler: "handler.js?v=2025-04-15",
      client: "client.js?v=12",
      worker: "worker.js?v=12",
      config: "config.js?v=2025-04-15",
      inject: "",
    },
  },
  block: [],
};
"""


async def _sw(request: Request):
    return Response(
        _SW_JS.encode("utf-8"),
        media_type="application/javascript",
        headers={
            **_NO_STORE_JS_HEADERS,
            "Service-Worker-Allowed": "/interstellar/",
            "X-Interstellar-Service-Worker": "1",
        },
    )


async def _sj_tp(request: Request):
    return Response(
        _SJ_TP_JS.encode("utf-8"),
        media_type="application/javascript",
        headers=_NO_STORE_JS_HEADERS,
    )


async def _index3(request: Request):
    return Response(
        _INDEX3_JS.encode("utf-8"),
        media_type="application/javascript",
        headers=_NO_STORE_JS_HEADERS,
    )


async def _t3(request: Request):
    return Response(
        _T3_JS.encode("utf-8"),
        media_type="application/javascript",
        headers=_NO_STORE_JS_HEADERS,
    )


async def _uv_config(request: Request):
    return Response(
        _UV_CONFIG_JS.encode("utf-8"),
        media_type="application/javascript",
        headers=_NO_STORE_JS_HEADERS,
    )


async def _uv_sw(request: Request):
    await _interstellar.ensure_native()
    source = _interstellar.SOURCE / "static" / "assets" / "mathematics" / "sw.js"
    if not source.is_file():
        return await _interstellar.proxy_http(request, "assets/mathematics/sw.js")
    text = source.read_text(encoding="utf-8", errors="replace")
    text = text.replace('"/assets/mathematics/', '"/interstellar/assets/mathematics/')
    text = text.replace("'/assets/mathematics/", "'/interstellar/assets/mathematics/")
    return Response(
        text.encode("utf-8"),
        media_type="application/javascript",
        headers=_NO_STORE_JS_HEADERS,
    )


async def _dynamic_config(request: Request):
    return Response(
        _DYNAMIC_CONFIG_JS.encode("utf-8"),
        media_type="application/javascript",
        headers=_NO_STORE_JS_HEADERS,
    )


async def _c1(request: Request):
    await _interstellar.ensure_native()
    source = _interstellar.SOURCE / "static" / "assets" / "js" / "c1.js"
    if not source.is_file():
        return await _interstellar.proxy_http(request, "assets/js/c1.js")
    text = source.read_text(encoding="utf-8", errors="replace")
    text = text.replace(
        'const g = window.location.pathname === "/a";',
        'const g = window.location.pathname === "/a" || window.location.pathname.endsWith("/interstellar/a");',
    )
    text = text.replace(
        'const a = window.location.pathname === "/b";',
        'const a = window.location.pathname === "/b" || window.location.pathname.endsWith("/interstellar/b");',
    )
    text = text.replace(
        'const c = window.location.pathname === "/gt";',
        'const c = window.location.pathname === "/gt" || window.location.pathname.endsWith("/interstellar/gt");',
    )
    text = re.sub(
        r"let\s+t\s*;\s*try\s*\{.*?\}\s*catch\s*\{\s*(?:try\s*\{.*?\}\s*catch\s*\{.*?\}\s*|[^}]*)\}",
        (
            "let t = false;\n"
            "try { if (window.parent && window.parent !== window && (window.parent.location.pathname === '/d' || window.parent.location.pathname === '/interstellar/d')) t = true; } catch {}\n"
            "try { if (!t && window.top && window.top !== window && (window.top.location.pathname === '/d' || window.top.location.pathname === '/interstellar/d')) t = true; } catch {}"
        ),
        text,
        count=1,
        flags=re.DOTALL,
    )
    text = text.replace(
        'saveToLocal(Selected);\n    window.location.href = "rx";\n    if (t) {\n      window.location.href = Selected;\n    }',
        'const localPath = (Selected && Selected.startsWith("/") && !Selected.startsWith("/interstellar/")) ? ("/interstellar" + Selected) : Selected;\n    saveToLocal(localPath);\n    if (t) {\n      window.location.href = localPath;\n    } else {\n      window.location.href = "/interstellar/d";\n    }',
    )
    text = text.replace(
        'go(Selected);\n    if (t) {\n      blank(Selected);\n    }',
        'if (t) {\n      blank(Selected);\n    } else {\n      go(Selected);\n    }',
    )
    text = text.replace(
        "image.src = app.image;",
        'image.src = (app.image && app.image.startsWith("/") && !app.image.startsWith("/interstellar/")) ? ("/interstellar" + app.image) : app.image;',
    )
    text = text.replace(
        'const appsContainer = document.getElementById("apps-container");\n    appsContainer.appendChild(pinnedApps);\n    appsContainer.appendChild(nonPinnedApps);',
        'const appsContainer = document.getElementById("apps-container");\n    if (appsContainer) {\n      appsContainer.appendChild(pinnedApps);\n      appsContainer.appendChild(nonPinnedApps);\n    }',
    )
    text = text.replace('"/assets/', '"/interstellar/assets/')
    text = text.replace('window.location.href = "rx";', 'window.location.href = "/interstellar/d";')
    return Response(
        text.encode("utf-8"),
        media_type="application/javascript",
        headers=_NO_STORE_JS_HEADERS,
    )


async def _h1(request: Request):
    await _interstellar.ensure_native()
    source = _interstellar.SOURCE / "static" / "assets" / "js" / "h1.js"
    if not source.is_file():
        return await _interstellar.proxy_http(request, "assets/js/h1.js")
    text = source.read_text(encoding="utf-8", errors="replace")
    text = text.replace(
        'if (!localStorage.getItem("ab")) localStorage.setItem("ab", true);',
        'if (!localStorage.getItem("ab")) localStorage.setItem("ab", "false");',
    )
    return Response(
        text.encode("utf-8"),
        media_type="application/javascript",
        headers=_NO_STORE_JS_HEADERS,
    )


async def _m1(request: Request):
    await _interstellar.ensure_native()
    source = _interstellar.SOURCE / "static" / "assets" / "js" / "m1.js"
    if not source.is_file():
        return await _interstellar.proxy_http(request, "assets/js/m1.js")
    text = source.read_text(encoding="utf-8", errors="replace")
    text = re.sub(
        r"\s*if\s*\(!blockedHostnames\.includes\(window\.location\.hostname\)\)\s*\{.*?document\.body\.appendChild\(script\);\s*\}",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(
        r"let\s+qp\s*;\s*try\s*\{.*?\}\s*catch\s*\{\s*(?:try\s*\{.*?\}\s*catch\s*\{.*?\}\s*|[^}]*)\}",
        (
            "let qp = false;\n"
            "try { if (window.parent && window.parent !== window && (window.parent.location.pathname === '/d' || window.parent.location.pathname === '/interstellar/d')) qp = true; } catch {}\n"
            "try { if (!qp && window.top && window.top !== window && (window.top.location.pathname === '/d' || window.top.location.pathname === '/interstellar/d')) qp = true; } catch {}"
        ),
        text,
        count=1,
        flags=re.DOTALL,
    )
    text = text.replace("icon.setAttribute(", "icon?.setAttribute(")
    text = text.replace("name.textContent = FinalNameValue;", "if (name) name.textContent = FinalNameValue;")
    text = text.replace('href="/./"', 'href="/interstellar/"')
    text = text.replace('href="/./a"', 'href="/interstellar/a"')
    text = text.replace('href="/./b"', 'href="/interstellar/b"')
    text = text.replace('href="/./c"', 'href="/interstellar/c"')
    text = text.replace('href="/./d"', 'href="/interstellar/d"')
    text = text.replace('"/assets/', '"/interstellar/assets/')
    return Response(
        text.encode("utf-8"),
        media_type="application/javascript",
        headers={**_NO_STORE_JS_HEADERS, "X-Interstellar-Ad-Redirect-Removed": "1"},
    )


async def _bm_worker(request: Request):
    await _interstellar.ensure_native()
    candidates = [
        _interstellar.SOURCE / "node_modules" / "@mercuryworkshop" / "bare-mux" / "dist" / "worker.js",
        _interstellar.SOURCE / "node_modules" / "@mercuryworkshop" / "bare-mux" / "worker.js",
    ]
    body = None
    for candidate in candidates:
        if candidate.is_file():
            body = candidate.read_bytes()
            break
    if body is None:
        resp = await _interstellar._native_proxy_http(request, "bm/worker.js") if hasattr(_interstellar, "_native_proxy_http") else Response(b"", status_code=404)
        raw = getattr(resp, "body", None)
        if isinstance(raw, bytes):
            body = raw
        else:
            iterator = getattr(resp, "body_iterator", None)
            if iterator is None:
                return resp
            chunks = []
            async for chunk in iterator:
                chunks.append(chunk.encode("utf-8") if isinstance(chunk, str) else chunk)
            body = b"".join(chunks)
    patch = b"""
;(function(){
  self.addEventListener("message", function(ev) {
    if (ev && ev.data && ev.data.__sh_connect && ev.data.port && typeof self.onconnect === "function") {
      self.onconnect({ ports: [ev.data.port] });
    }
  });
})();
"""
    return Response(
        body + patch,
        media_type="application/javascript",
        headers=_NO_STORE_JS_HEADERS,
    )


async def _scramjet_alias(request: Request, name: str):
    await _interstellar.ensure_native()
    rel = {
        "scramjet.all.js": "static/assets/languagearts/sj.all.js",
        "scramjet.sync.js": "static/assets/languagearts/sj.sync.js",
        "scramjet.wasm.wasm": "static/assets/languagearts/sj.wasm.wasm",
    }[name]
    source = _interstellar.SOURCE / rel
    if not source.is_file():
        return Response(b"Interstellar Scramjet asset missing", status_code=404, media_type="text/plain")
    media = "application/wasm" if name.endswith(".wasm.wasm") else "application/javascript"
    return Response(
        source.read_bytes(),
        media_type=media,
        headers={"Cache-Control": "public, max-age=3600", "X-Interstellar-Asset": "native-scramjet-5.3.0"},
    )


@router.get("/interstellar/sw.js", include_in_schema=False)
@router.head("/interstellar/sw.js", include_in_schema=False)
async def interstellar_sw(request: Request):
    return await _sw(request)


@router.get("/interstellar/assets/js/sj-tp.js", include_in_schema=False)
@router.head("/interstellar/assets/js/sj-tp.js", include_in_schema=False)
async def interstellar_sj_tp(request: Request):
    return await _sj_tp(request)


@router.get("/interstellar/assets/js/index-3.js", include_in_schema=False)
@router.head("/interstellar/assets/js/index-3.js", include_in_schema=False)
async def interstellar_index3(request: Request):
    return await _index3(request)


@router.get("/interstellar/assets/js/t3.js", include_in_schema=False)
@router.head("/interstellar/assets/js/t3.js", include_in_schema=False)
async def interstellar_t3(request: Request):
    return await _t3(request)


@router.get("/interstellar/assets/js/c1.js", include_in_schema=False)
@router.head("/interstellar/assets/js/c1.js", include_in_schema=False)
async def interstellar_c1(request: Request):
    return await _c1(request)


@router.get("/interstellar/assets/js/h1.js", include_in_schema=False)
@router.head("/interstellar/assets/js/h1.js", include_in_schema=False)
async def interstellar_h1(request: Request):
    return await _h1(request)


@router.get("/interstellar/assets/js/m1.js", include_in_schema=False)
@router.head("/interstellar/assets/js/m1.js", include_in_schema=False)
async def interstellar_m1(request: Request):
    return await _m1(request)


@router.get("/interstellar/assets/mathematics/config.js", include_in_schema=False)
@router.head("/interstellar/assets/mathematics/config.js", include_in_schema=False)
async def interstellar_uv_config(request: Request):
    return await _uv_config(request)


@router.get("/interstellar/assets/mathematics/sw.js", include_in_schema=False)
@router.head("/interstellar/assets/mathematics/sw.js", include_in_schema=False)
async def interstellar_uv_sw(request: Request):
    return await _uv_sw(request)


@router.get("/interstellar/assets/history/config.js", include_in_schema=False)
@router.head("/interstellar/assets/history/config.js", include_in_schema=False)
async def interstellar_dynamic_config(request: Request):
    return await _dynamic_config(request)


@router.get("/interstellar/bm/worker.js", include_in_schema=False)
@router.head("/interstellar/bm/worker.js", include_in_schema=False)
async def interstellar_bm_worker(request: Request):
    return await _bm_worker(request)


@router.get("/interstellar/scramjet.all.js", include_in_schema=False)
async def scramjet_all(request: Request):
    return await _scramjet_alias(request, "scramjet.all.js")


@router.get("/interstellar/scramjet.sync.js", include_in_schema=False)
async def scramjet_sync(request: Request):
    return await _scramjet_alias(request, "scramjet.sync.js")


@router.get("/interstellar/scramjet.wasm.wasm", include_in_schema=False)
async def scramjet_wasm(request: Request):
    return await _scramjet_alias(request, "scramjet.wasm.wasm")

