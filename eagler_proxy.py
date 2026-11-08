"""Eaglercraft 1.5.2 Japanese Edition integration for School Hub.

Includes:
- Japanese localization by U5KUN
- Render Dedicated Eaglercraft 1.5.2 Server (FastAPI WebSocket at /eagler/ws)
- Render Dedicated WebRTC Relay Server (/eagler/relay)
- Client CPU Headroom Detection & Server Offloading (navigator.hardwareConcurrency)
- Google Tab title and Google Favicon cloaking
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import mimetypes
import os
import random
import string
import struct
import time
import zlib
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

logger = logging.getLogger("SchoolHub.Minecraft")

SERVICE_NAME = "マインクラフト(日本語版)"
SERVICE_ICON = "⛏️"
SERVICE_PATH = "/minecraft/"

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent
MINECRAFT_SRC = BASE_DIR / "minecraft_src"

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/octet-stream", ".epk")
mimetypes.add_type("application/wasm", ".wasm")
mimetypes.add_type("image/x-icon", ".ico")
mimetypes.add_type("image/png", ".png")


# ==============================================================================
# Helper Functions for Minecraft 1.5.2 Packet Protocol
# ==============================================================================

def write_string16(s: str) -> bytes:
    encoded = s.encode("utf-16be")
    return struct.pack(">h", len(s)) + encoded


def read_string16(data: bytes, offset: int) -> Tuple[str, int]:
    if offset + 2 > len(data):
        return "", offset
    length = struct.unpack_from(">h", data, offset)[0]
    offset += 2
    byte_len = length * 2
    if offset + byte_len > len(data):
        return "", offset
    raw = data[offset : offset + byte_len]
    return raw.decode("utf-16be", errors="ignore"), offset + byte_len


# ==============================================================================
# Procedural Survival World Generator (Minecraft 1.5.2)
# ==============================================================================

WORLD_SEED = 84729104
NUM_SECTIONS = 6  # Y = 0..95 (plenty of room for sea level 62, hills up to 78, trees up to 85)
SECTION_MASK = (1 << NUM_SECTIONS) - 1

def hash2d(x: int, z: int, seed: int = WORLD_SEED) -> float:
    n = (x * 374761393 + z * 668265263 + seed * 1013904223) & 0xFFFFFFFF
    n = (n ^ (n >> 13)) * 1274126177 & 0xFFFFFFFF
    return ((n ^ (n >> 16)) & 0xFFFFFF) / 16777215.0

def smooth_noise(x: float, z: float, seed: int = WORLD_SEED) -> float:
    ix = int(math.floor(x))
    iz = int(math.floor(z))
    fx = x - ix
    fz = z - iz
    sx = fx * fx * (3 - 2 * fx)
    sz = fz * fz * (3 - 2 * fz)
    v00 = hash2d(ix, iz, seed)
    v10 = hash2d(ix + 1, iz, seed)
    v01 = hash2d(ix, iz + 1, seed)
    v11 = hash2d(ix + 1, iz + 1, seed)
    vx0 = v00 + sx * (v10 - v00)
    vx1 = v01 + sx * (v11 - v01)
    return vx0 + sz * (vx1 - vx0)

def get_height(wx: int, wz: int) -> int:
    h1 = (smooth_noise(wx * 0.015, wz * 0.015, WORLD_SEED) - 0.5) * 22.0
    h2 = (smooth_noise(wx * 0.045, wz * 0.045, WORLD_SEED + 101) - 0.5) * 8.0
    h3 = (smooth_noise(wx * 0.1, wz * 0.1, WORLD_SEED + 202) - 0.5) * 3.0
    h = int(round(64.0 + h1 + h2 + h3))
    return max(50, min(85, h))

def is_tree_at(wx: int, wz: int) -> bool:
    return hash2d(wx, wz, WORLD_SEED + 999) < 0.02

# Chunk Cache
_CHUNK_CACHE: Dict[Tuple[int, int], bytes] = {}

def get_or_generate_chunk(cx: int, cz: int) -> bytes:
    key = (cx, cz)
    if key in _CHUNK_CACHE:
        return _CHUNK_CACHE[key]

    block_ids = bytearray(NUM_SECTIONS * 4096)
    meta = bytearray(NUM_SECTIONS * 2048)
    block_light = bytearray(NUM_SECTIONS * 2048)
    sky_light = bytearray([0xFF] * (NUM_SECTIONS * 2048))
    biomes = bytearray([1] * 256)

    def set_block(lx: int, y: int, lz: int, bid: int, bmeta: int = 0):
        if not (0 <= lx < 16 and 0 <= lz < 16 and 0 <= y < NUM_SECTIONS * 16):
            return
        sec = y >> 4
        sec_y = y & 15
        idx = (sec * 4096) + (sec_y * 256) + (lz * 16) + lx
        block_ids[idx] = bid
        if bmeta:
            m_idx = (sec * 2048) + ((sec_y * 256 + lz * 16 + lx) >> 1)
            if lx & 1:
                meta[m_idx] = (meta[m_idx] & 0x0F) | ((bmeta & 0x0F) << 4)
            else:
                meta[m_idx] = (meta[m_idx] & 0xF0) | (bmeta & 0x0F)

    # 1. Base terrain columns
    for lz in range(16):
        wz = (cz << 4) + lz
        for lx in range(16):
            wx = (cx << 4) + lx
            gh = get_height(wx, wz)

            # Bedrock at 0
            set_block(lx, 0, lz, 7)

            # Stone from 1 to gh - 4
            for y in range(1, gh - 3):
                ore_rnd = hash2d(wx * 31 + y * 7, wz * 31, WORLD_SEED + 555)
                if ore_rnd < 0.02:
                    set_block(lx, y, lz, 15)  # Iron Ore
                elif ore_rnd < 0.05:
                    set_block(lx, y, lz, 16)  # Coal Ore
                else:
                    set_block(lx, y, lz, 1)   # Stone

            # Dirt
            for y in range(max(1, gh - 3), gh):
                set_block(lx, y, lz, 3)

            # Top block
            if gh <= 62:
                set_block(lx, gh, lz, 12)  # Sand
                for wy in range(gh + 1, 63):
                    set_block(lx, wy, lz, 9)  # Still Water
            else:
                set_block(lx, gh, lz, 2)  # Grass Block
                veg_rnd = hash2d(wx, wz, WORLD_SEED + 888)
                if veg_rnd < 0.12:
                    set_block(lx, gh + 1, lz, 31, 1)  # Tall Grass
                elif veg_rnd < 0.14:
                    set_block(lx, gh + 1, lz, 37)      # Dandelion
                elif veg_rnd < 0.16:
                    set_block(lx, gh + 1, lz, 38)      # Rose

    # 2. Trees (check range [-2..17] so foliage crosses chunk boundaries)
    for t_wz in range((cz << 4) - 2, (cz << 4) + 18):
        for t_wx in range((cx << 4) - 2, (cx << 4) + 18):
            t_gh = get_height(t_wx, t_wz)
            if t_gh >= 63 and is_tree_at(t_wx, t_wz):
                # Trunk
                for ty in range(t_gh + 1, t_gh + 6):
                    set_block(t_wx - (cx << 4), ty, t_wz - (cz << 4), 17)
                # Leaves canopy
                for dy in range(3, 7):
                    ly = t_gh + dy
                    radius = 2 if dy in (3, 4) else (1 if dy == 5 else 0)
                    for ddx in range(-radius, radius + 1):
                        for ddz in range(-radius, radius + 1):
                            if dy in (3, 4) and abs(ddx) == 2 and abs(ddz) == 2:
                                continue
                            leaf_x = t_wx + ddx - (cx << 4)
                            leaf_z = t_wz + ddz - (cz << 4)
                            sec = ly >> 4
                            sec_y = ly & 15
                            if 0 <= leaf_x < 16 and 0 <= leaf_z < 16 and 0 <= ly < NUM_SECTIONS * 16:
                                b_idx = (sec * 4096) + (sec_y * 256) + (leaf_z * 16) + leaf_x
                                if block_ids[b_idx] == 0:
                                    set_block(leaf_x, ly, leaf_z, 18)

    raw = bytes(block_ids) + bytes(meta) + bytes(block_light) + bytes(sky_light) + bytes(biomes)
    comp = zlib.compress(raw)
    pkt = struct.pack(">Bii?HHi", 0x33, cx, cz, True, SECTION_MASK, 0, len(comp)) + comp
    _CHUNK_CACHE[key] = pkt
    return pkt


def make_spawn_player_packet(
    eid: int, name: str, x: float, y: float, z: float, yaw: float, pitch: float, item: int = 0
) -> bytes:
    ix = int(x * 32)
    iy = int(y * 32)
    iz = int(z * 32)
    byaw = int((yaw % 360) * 256 / 360) & 0xFF
    bpitch = int((pitch % 360) * 256 / 360) & 0xFF
    metadata = b"\x7f"  # End metadata marker
    return (
        struct.pack(">B", 0x14)
        + struct.pack(">i", eid)
        + write_string16(name)
        + struct.pack(">iiiBBh", ix, iy, iz, byaw, bpitch, item)
        + metadata
    )


def make_entity_look_packet(eid: int, yaw: float, pitch: float) -> bytes:
    byaw = int((yaw % 360) * 256 / 360) & 0xFF
    bpitch = int((pitch % 360) * 256 / 360) & 0xFF
    return struct.pack(">BiBB", 0x20, eid, byaw, bpitch) + struct.pack(">BiB", 0x23, eid, byaw)


def make_teleport_player_packet(eid: int, x: float, y: float, z: float, yaw: float, pitch: float) -> bytes:
    ix = int(x * 32)
    iy = int(y * 32)
    iz = int(z * 32)
    byaw = int((yaw % 360) * 256 / 360) & 0xFF
    bpitch = int((pitch % 360) * 256 / 360) & 0xFF
    return struct.pack(">BiiiiBB", 0x22, eid, ix, iy, iz, byaw, bpitch) + struct.pack(">BiB", 0x23, eid, byaw)


def make_entity_metadata_packet(eid: int, crouching: bool = False, sprinting: bool = False) -> bytes:
    flags = 0
    if crouching:
        flags |= 0x02
    if sprinting:
        flags |= 0x08
    return struct.pack(">BiBBb", 0x28, eid, 0, 0, flags) + b"\x7f"


def make_block_change_packet(x: int, y: int, z: int, block_id: int, meta: int = 0) -> bytes:
    cy = max(0, min(255, int(y)))
    return struct.pack(">B", 0x35) + struct.pack(">iBiHb", int(x), cy, int(z), int(block_id), int(meta))


def make_chat_packet(message: str) -> bytes:
    return struct.pack(">B", 0x03) + write_string16(message)


def make_destroy_entity_packet(eid: int) -> bytes:
    return struct.pack(">BB", 0x1D, 1) + struct.pack(">i", eid)


def make_item_slot_packet(window_id: int, slot: int, item_id: int, count: int = 1, damage: int = 0) -> bytes:
    if item_id < 0:
        return struct.pack(">Bbh", 0x67, window_id, slot) + struct.pack(">h", -1)
    return struct.pack(">Bbh", 0x67, window_id, slot) + struct.pack(">hbh", item_id, count, damage) + struct.pack(">h", -1)


# Pre-cache spawn chunks (7x7 area)
for _cx in range(-3, 4):
    for _cz in range(-3, 4):
        get_or_generate_chunk(_cx, _cz)


# ==============================================================================
# Dedicated Render Server State & Logic
# ==============================================================================

class ConnectedPlayer:
    def __init__(self, ws: WebSocket, eid: int, name: str, spawn_x: float = 0.5, spawn_y: float = 67.0, spawn_z: float = 0.5):
        self.ws = ws
        self.eid = eid
        self.name = name
        self.x = spawn_x
        self.y = spawn_y
        self.z = spawn_z
        self.yaw = 0.0
        self.pitch = 0.0
        self.crouching = False
        self.sprinting = False
        self.held_item = 0
        self.last_alive = time.time()
        self.loaded_chunks: Set[Tuple[int, int]] = set()


class DedicatedServer:
    def __init__(self):
        self.players: Dict[int, ConnectedPlayer] = {}
        self.next_eid = 100
        self.lock = asyncio.Lock()
        self.block_changes: Dict[Tuple[int, int, int], Tuple[int, int]] = {}
        self.world_ticks = 6000
        self.last_tick_time = time.time()

    def get_world_ticks(self) -> int:
        now = time.time()
        elapsed = now - self.last_tick_time
        ticks = int(self.world_ticks + elapsed * 20) % 24000
        return ticks

    def get_online_count(self) -> int:
        return len(self.players)

    def get_player_names(self) -> List[str]:
        return [p.name for p in self.players.values()]

    async def broadcast(self, data: bytes, exclude_eid: Optional[int] = None):
        dead_eids = []
        for eid, p in list(self.players.items()):
            if exclude_eid is not None and eid == exclude_eid:
                continue
            try:
                await p.ws.send_bytes(data)
            except Exception:
                dead_eids.append(eid)
        for eid in dead_eids:
            self.players.pop(eid, None)


dedicated_server = DedicatedServer()


# ==============================================================================
# WebRTC Relay Server Protocol & Logic
# ==============================================================================

class RelayPacket:
    @staticmethod
    def read_ascii8(stream: BytesIO) -> str:
        length = int.from_bytes(stream.read(1), "big")
        return stream.read(length).decode("ascii", errors="ignore")

    @staticmethod
    def write_ascii8(stream: BytesIO, text: str):
        data = (text or "").encode("ascii")[:255]
        stream.write(len(data).to_bytes(1, "big"))
        stream.write(data)

    @staticmethod
    def read_ascii16(stream: BytesIO) -> str:
        length = int.from_bytes(stream.read(2), "big")
        return stream.read(length).decode("ascii", errors="ignore")

    @staticmethod
    def write_ascii16(stream: BytesIO, text: str):
        data = (text or "").encode("ascii")[:65535]
        stream.write(len(data).to_bytes(2, "big"))
        stream.write(data)


class RelayServerNode:
    def __init__(self, ws: WebSocket, code: str, name: str, addr: str):
        self.ws = ws
        self.code = code
        self.name = name
        self.addr = addr
        self.clients: Dict[str, "RelayClientNode"] = {}


class RelayClientNode:
    def __init__(self, ws: WebSocket, server: RelayServerNode, client_id: str, addr: str):
        self.ws = ws
        self.server = server
        self.id = client_id
        self.addr = addr


class EaglerWebRTCRelay:
    def __init__(self):
        self.servers_by_code: Dict[str, RelayServerNode] = {}
        self.clients_by_id: Dict[str, RelayClientNode] = {}
        self.lock = asyncio.Lock()

    def generate_code(self) -> str:
        chars = string.ascii_lowercase + string.digits
        return "".join(random.choice(chars) for _ in range(5))

    def generate_client_id(self) -> str:
        chars = string.ascii_letters + string.digits
        return "".join(random.choice(chars) for _ in range(16))


webrtc_relay = EaglerWebRTCRelay()


# ==============================================================================
# Frontend HTML Template with CPU Offloading & Cloaking
# ==============================================================================

def get_minecraft_html() -> str:
    return """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
<title>Google</title>
<link rel="icon" type="image/x-icon" href="/favicon.ico">
<link rel="stylesheet" href="/design-system/m3.css">
<script src="/design-system/m3.js"></script>
<script>
// Google Title & Favicon Cloak Protection
(function(){
  var T = "Google";
  var F = "/favicon.ico";
  function cloak() {
    try { if (document.title !== T) document.title = T; } catch(_) {}
    try {
      var links = document.querySelectorAll("link[rel*='icon']");
      var hasFav = false;
      for (var i = 0; i < links.length; i++) {
        if (links[i].getAttribute("href") !== F) {
          links[i].rel = "icon";
          links[i].type = "image/x-icon";
          links[i].href = F;
        }
        hasFav = true;
      }
      if (!hasFav && document.head) {
        var l = document.createElement("link");
        l.rel = "icon";
        l.type = "image/x-icon";
        l.href = F;
        document.head.appendChild(l);
      }
    } catch(_) {}
  }
  cloak();
  try {
    Object.defineProperty(document, "title", {
      get: function() { return T; },
      set: function() { return T; },
      configurable: true
    });
  } catch(_) {}
  if (window.MutationObserver && document.head) {
    new MutationObserver(cloak).observe(document.head, { childList: true, subtree: true, characterData: true });
  }
  window.addEventListener("load", cloak);
})();
</script>
<style>
  body, html {
    margin: 0;
    padding: 0;
    width: 100vw;
    height: 100vh;
    overflow: hidden;
    background-color: var(--md-sys-color-surface-dim, #000);
    font-family: var(--md-sys-typescale-font-plain, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif);
  }
  #game_frame {
    width: 100vw;
    height: 100vh;
    position: absolute;
    top: 0;
    left: 0;
    z-index: 1;
  }
</style>
<script type="text/javascript">
(function() {
  var CDN_BASE = "https://cdn.jsdelivr.net/gh/U5KUN/Eaglercraft-Japanese@main/";
  var origFetch = window.fetch;
  if (typeof origFetch === "function") {
    window.fetch = function(input, init) {
      var url = typeof input === "string" ? input : (input && input.url ? input.url : "");
      if (url && url.indexOf(CDN_BASE) === 0) {
        var assetName = url.slice(CDN_BASE.length);
        return origFetch.call(this, input, init).then(function(resp) {
          if (!resp || !resp.ok) {
            return origFetch.call(window, "/minecraft/" + assetName, init);
          }
          return resp;
        }).catch(function() {
          return origFetch.call(window, "/minecraft/" + assetName, init);
        });
      }
      return origFetch.call(this, input, init);
    };
  }
  var origOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(method, url) {
    this.__mcUrl = url;
    return origOpen.apply(this, arguments);
  };
})();
</script>
<script type="text/javascript" src="https://cdn.jsdelivr.net/gh/U5KUN/Eaglercraft-Japanese@main/classes.js" crossorigin="anonymous" onerror="this.onerror=null;this.src='/minecraft/classes.js';"></script>
<script type="text/javascript" src="https://cdn.jsdelivr.net/gh/U5KUN/Eaglercraft-Japanese@main/eagswebrtc.js" crossorigin="anonymous" onerror="this.onerror=null;this.src='/minecraft/eagswebrtc.js';"></script>
<script type="text/javascript">
(function(){
  function startMinecraft() {
    if (typeof main !== "function") {
      setTimeout(startMinecraft, 100);
      return;
    }
    const host = location.host;
    const wsProto = location.protocol === "https:" ? "wss://" : "ws://";
    const dedicatedWs = wsProto + host + "/eagler/ws";
    const relayWs = wsProto + host + "/eagler/relay";
    const cdnBase = "https://cdn.jsdelivr.net/gh/U5KUN/Eaglercraft-Japanese@main";

    // Configure Eaglercraft 1.5.2 Options (Standard UI + jsDelivr CDN assets)
    window.eaglercraftOpts = {
      container: "game_frame",
      assetsURI: cdnBase + "/assets.epk",
      serverWorkerURI: "/minecraft/worker_bootstrap.js",
      worldsFolder: "MAIN",
      servers: [
        {
          serverName: "School Hub 独自サーバー (Render)",
          serverAddress: dedicatedWs,
          hideAddress: false
        }
      ],
      relays: [
        {
          addr: relayWs,
          name: "School Hub 独自リレー (Render)",
          primary: true
        },
        {
          addr: "wss://relay.deev.is/",
          name: "lax1dude relay #1",
          primary: false
        },
        {
          addr: "wss://relay.lax1dude.net/",
          name: "lax1dude relay #2",
          primary: false
        },
        {
          addr: "wss://relay.shhnowisnottheti.me/",
          name: "ayunami relay #1",
          primary: false
        }
      ],
      mainMenu: {
        splashes: [
          "Translated by U5KUN!!",
          "U5KUNが翻訳!!",
          "School Hub 独自サーバー稼働中!!",
          "jsDelivr 高速CDN配信対応!!"
        ],
        eaglerLogo: true
      }
    };

    // If auto-connect parameter is present
    var q = window.location.search;
    if (typeof q === 'string' && q.startsWith("?")) {
      q = new URLSearchParams(q);
      var s = q.get("server");
      if (s) {
        window.minecraftOpts = [s];
      }
    }

    // Launch Eaglercraft 1.5.2
    main();
  }
  window.addEventListener("load", startMinecraft);
})();
</script>
</head>
<body id="game_frame">
</body>
</html>
"""


# ==============================================================================
# Routes & Endpoints
# ==============================================================================

@router.api_route("/minecraft", methods=["GET", "HEAD"], response_class=HTMLResponse)
@router.api_route("/minecraft/", methods=["GET", "HEAD"], response_class=HTMLResponse)
@router.api_route("/minecraft/index.html", methods=["GET", "HEAD"], response_class=HTMLResponse)
@router.api_route("/eagler", methods=["GET", "HEAD"], response_class=HTMLResponse)
@router.api_route("/eagler/", methods=["GET", "HEAD"], response_class=HTMLResponse)
@router.api_route("/eagler/index.html", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def serve_minecraft_client():
    return HTMLResponse(content=get_minecraft_html(), media_type="text/html")


@router.api_route("/minecraft/{filename:path}", methods=["GET", "HEAD"])
@router.api_route("/eagler/{filename:path}", methods=["GET", "HEAD"])
async def serve_minecraft_assets(filename: str):
    # Normalize filename
    safe_path = Path(filename).name
    file_path = MINECRAFT_SRC / safe_path

    if not file_path.is_file():
        return Response(status_code=404, content=f"Asset not found: {filename}")

    media_type, _ = mimetypes.guess_type(str(file_path))
    if safe_path.endswith(".epk"):
        media_type = "application/octet-stream"
    elif safe_path.endswith(".js"):
        media_type = "application/javascript"

    return FileResponse(
        path=str(file_path),
        media_type=media_type or "application/octet-stream",
        headers={
            "Cache-Control": "public, max-age=86400",
            "Access-Control-Allow-Origin": "*",
        },
    )


# ==============================================================================
# Dedicated Eaglercraft Server WebSocket Endpoint (/eagler/ws and /minecraft/ws)
# ==============================================================================

@router.websocket("/eagler/ws")
@router.websocket("/minecraft/ws")
async def eagler_dedicated_server_endpoint(websocket: WebSocket):
    await websocket.accept()

    player_eid: Optional[int] = None
    player_name = "Player"
    player_gamemode = 0
    keep_alive_task: Optional[asyncio.Task] = None

    try:
        while True:
            msg = await websocket.receive()
            if "text" in msg and msg["text"]:
                text_data = msg["text"]
                # Handle Eaglercraft MOTD query
                if text_data.startswith("Accept:") or "MOTD" in text_data:
                    online = dedicated_server.get_online_count()
                    names = dedicated_server.get_player_names()
                    motd_response = {
                        "type": "motd",
                        "data": {
                            "motd": [
                                "§bSchool Hub 独自サーバー (Render)",
                                "§aマイクラ1.5.2 日本語版 §7[24時間常時稼働]",
                            ],
                            "icon": False,
                            "cache": True,
                            "online": online,
                            "max": 20,
                            "players": names if names else ["SchoolHubPlayer"],
                        },
                        "vers": "1.5.2",
                        "brand": "School Hub Render Server",
                        "name": "School Hub Server",
                        "time": int(time.time() * 1000),
                        "uuid": "00000000-0000-0000-0000-000000000000",
                        "cracked": True,
                    }
                    await websocket.send_text(json.dumps(motd_response, ensure_ascii=False))
                    continue

            if "bytes" in msg and msg["bytes"]:
                raw = msg["bytes"]
                if len(raw) == 0:
                    continue

                packet_id = raw[0]

                async def perform_join(chosen_name: str):
                    nonlocal player_eid, player_name, keep_alive_task
                    if player_eid is not None:
                        return
                    player_name = chosen_name
                    spawn_y = float(get_height(0, 0) + 1)
                    async with dedicated_server.lock:
                        player_eid = dedicated_server.next_eid
                        dedicated_server.next_eid += 1

                        player = ConnectedPlayer(websocket, player_eid, player_name, 0.5, spawn_y, 0.5)
                        dedicated_server.players[player_eid] = player

                    # 1. Packet 0x01 Login Response: gameMode = 0 (Survival), levelType = "default"
                    login_resp = (
                        struct.pack(">B", 0x01)
                        + struct.pack(">i", player_eid)
                        + write_string16("default")
                        + struct.pack(">bbbbb", 0, 0, 1, 0, 20)
                    )
                    await websocket.send_bytes(login_resp)

                    # 2. Packet 0x04 Time Update (Daylight noon = 6000 ticks)
                    time_pkt = struct.pack(">Bqq", 0x04, 0, 6000)
                    await websocket.send_bytes(time_pkt)

                    # 3. Packet 0x06 Spawn Position (x=0, y=spawn_y, z=0)
                    spawn_pos = struct.pack(">Biii", 0x06, 0, int(spawn_y), 0)
                    await websocket.send_bytes(spawn_pos)

                    # 4. Packet 0xCA Player Abilities: Normal Survival (flags = 0x00)
                    abilities_pkt = struct.pack(">BBBB", 0xCA, 0x00, 12, 25)
                    await websocket.send_bytes(abilities_pkt)

                    # 5. Send initial chunks in 7x7 grid around spawn (-3..3, -3..3)
                    for cx in range(-3, 4):
                        for cz in range(-3, 4):
                            chunk_pkt = get_or_generate_chunk(cx, cz)
                            await websocket.send_bytes(chunk_pkt)
                            player.loaded_chunks.add((cx, cz))

                    # 6. Send existing modified blocks
                    for (bx, by, bz), (bid, bmeta) in dedicated_server.block_changes.items():
                        await websocket.send_bytes(make_block_change_packet(bx, by, bz, bid, bmeta))

                    # 7. Packet 0x0D Player Position & Look (standing on top of terrain)
                    pos_look = struct.pack(
                        ">Bddddff?", 0x0D, 0.5, spawn_y, spawn_y + 1.62, 0.5, 0.0, 0.0, True
                    )
                    await websocket.send_bytes(pos_look)

                    # 8. Starter Survival Equipment in Hotbar
                    # 36: Stone Pickaxe, 37: Stone Axe, 38: Stone Sword, 39: Stone Shovel, 40: Bread x16, 41: Torches x32, 42: Oak Wood x32
                    starter_items = [
                        (36, 274, 1),
                        (37, 275, 1),
                        (38, 272, 1),
                        (39, 273, 1),
                        (40, 297, 16),
                        (41, 50, 32),
                        (42, 17, 32),
                    ]
                    for slot, item_id, count in starter_items:
                        await websocket.send_bytes(make_item_slot_packet(0, slot, item_id, count))

                    # 9. Welcome Chat
                    welcome_chat = make_chat_packet(
                        f"§e[School Hub] 独自サバイバルサーバーへようこそ、{player_name}さん！ (§b/gamemode 1§e でクリエイティブ切替)"
                    )
                    await websocket.send_bytes(welcome_chat)

                    # 10. Broadcast Join to other players
                    join_broadcast = make_chat_packet(
                        f"§a[School Hub] §f{player_name} §7が参加しました。"
                    )
                    await dedicated_server.broadcast(join_broadcast, exclude_eid=player_eid)

                    # 11. Spawn other players for this client, and spawn this player for others
                    for other_eid, other_p in list(dedicated_server.players.items()):
                        if other_eid == player_eid:
                            continue
                        other_spawn = make_spawn_player_packet(
                            other_eid, other_p.name, other_p.x, other_p.y, other_p.z, other_p.yaw, other_p.pitch
                        )
                        await websocket.send_bytes(other_spawn)

                    new_player_spawn = make_spawn_player_packet(
                        player_eid, player_name, 0.5, spawn_y, 0.5, 0.0, 0.0
                    )
                    await dedicated_server.broadcast(new_player_spawn, exclude_eid=player_eid)

                    # Register session in current_activity & touch openchat
                    try:
                        import current_activity
                        current_activity.touch_session_activity(
                            session=f"mc-{player_eid}",
                            service="マインクラフト(日本語版)",
                            path="/minecraft/",
                            state="active",
                            source="visit",
                            record_access=True,
                        )
                    except Exception:
                        pass
                    try:
                        import openchat_proxy
                        asyncio.create_task(openchat_proxy._broadcast_online_count())
                    except Exception:
                        pass

                    # Start periodic keep-alive and daylight cycle synchronization
                    async def keep_alive_loop(ws: WebSocket, eid: int):
                        while True:
                            await asyncio.sleep(5)
                            try:
                                # Keep Alive
                                await ws.send_bytes(struct.pack(">Bi", 0x00, random.randint(1, 999999)))
                                # Time update (Day/Night cycle sync across server and players)
                                wt = dedicated_server.get_world_ticks()
                                await ws.send_bytes(struct.pack(">Bqq", 0x04, wt, wt))
                                # Touch current activity session
                                import current_activity
                                current_activity.touch_session_activity(
                                    session=f"mc-{eid}",
                                    service="マインクラフト(日本語版)",
                                    path="/minecraft/",
                                    state="active",
                                    source="heartbeat",
                                )
                            except Exception:
                                break

                    keep_alive_task = asyncio.create_task(keep_alive_loop(websocket, player_eid))

                # Packet 0x02: Packet2ClientProtocol (Handshake from client)
                if packet_id == 0x02:
                    pname = player_name
                    try:
                        proto_ver = raw[1]
                        name, offset = read_string16(raw, 2)
                        if name:
                            pname = name
                    except Exception:
                        pass
                    # In Eaglercraft 1.5.2, client expects immediate Packet 0x01 login response
                    await perform_join(pname)

                # Packet 0xCD or Packet 0x01: Client Status / Login fallback
                elif packet_id == 0xCD or (player_eid is None and packet_id == 0x01):
                    await perform_join(player_name)

                # Packet 0xFA: Custom Payload (EAG|MySkin, EAG|MyCape, etc.)
                elif packet_id == 0xFA:
                    pass

                # Packet 0x00: Keep Alive response
                elif packet_id == 0x00:
                    pass

                # Packet 0x03: Chat message
                elif packet_id == 0x03:
                    try:
                        chat_text, _ = read_string16(raw, 1)
                        if chat_text:
                            cmd = chat_text.strip().lower()
                            if cmd in ("/gamemode 1", "/gamemode c", "/gm 1", "/gmc"):
                                player_gamemode = 1
                                await websocket.send_bytes(struct.pack(">BBB", 0x46, 3, 1))
                                await websocket.send_bytes(struct.pack(">BBBB", 0xCA, 0x0D, 12, 25))
                                await websocket.send_bytes(make_chat_packet("§a[School Hub] ゲームモードをクリエイティブに変更しました。"))
                            elif cmd in ("/gamemode 0", "/gamemode s", "/gm 0", "/gms"):
                                player_gamemode = 0
                                await websocket.send_bytes(struct.pack(">BBB", 0x46, 3, 0))
                                await websocket.send_bytes(struct.pack(">BBBB", 0xCA, 0x00, 12, 25))
                                await websocket.send_bytes(make_chat_packet("§a[School Hub] ゲームモードをサバイバルに変更しました。"))
                            elif cmd in ("/time set day", "/day"):
                                dedicated_server.start_epoch = time.time() - (6000.0 / 20.0)
                                wt = dedicated_server.get_world_ticks()
                                await dedicated_server.broadcast(struct.pack(">Bqq", 0x04, wt, wt))
                                await websocket.send_bytes(make_chat_packet("§a[School Hub] 時間を昼にしました。"))
                            elif cmd in ("/time set night", "/night"):
                                dedicated_server.start_epoch = time.time() - (14000.0 / 20.0)
                                wt = dedicated_server.get_world_ticks()
                                await dedicated_server.broadcast(struct.pack(">Bqq", 0x04, wt, wt))
                                await websocket.send_bytes(make_chat_packet("§a[School Hub] 時間を夜にしました。"))
                            elif cmd == "/spawn":
                                spawn_y = float(get_height(0, 0) + 1)
                                await websocket.send_bytes(struct.pack(">Bddddff?", 0x0D, 0.5, spawn_y, spawn_y + 1.62, 0.5, 0.0, 0.0, True))
                                await websocket.send_bytes(make_chat_packet("§a[School Hub] スポーン地点へ移動しました。"))
                            else:
                                broadcast_text = f"<{player_name}> {chat_text}"
                                chat_pkt = make_chat_packet(broadcast_text)
                                await dedicated_server.broadcast(chat_pkt)
                    except Exception:
                        pass

                # Packet 0x0A: Player (onGround only)
                elif packet_id == 0x0A:
                    pass

                # Packet 0x0B: Player Position
                elif packet_id == 0x0B:
                    if len(raw) >= 33 and player_eid in dedicated_server.players:
                        try:
                            x, y, stance, z, on_ground = struct.unpack_from(">dddd?", raw, 1)
                            p = dedicated_server.players[player_eid]
                            p.x, p.y, p.z = x, y, z
                            tp = make_teleport_player_packet(player_eid, x, y, z, p.yaw, p.pitch)
                            await dedicated_server.broadcast(tp, exclude_eid=player_eid)
                        except Exception:
                            pass

                # Packet 0x0C: Player Look
                elif packet_id == 0x0C:
                    if len(raw) >= 9 and player_eid in dedicated_server.players:
                        try:
                            yaw, pitch, on_ground = struct.unpack_from(">ff?", raw, 1)
                            p = dedicated_server.players[player_eid]
                            p.yaw, p.pitch = yaw, pitch
                            look_pkt = make_entity_look_packet(player_eid, yaw, pitch)
                            await dedicated_server.broadcast(look_pkt, exclude_eid=player_eid)
                        except Exception:
                            pass

                # Packet 0x0D: Player Position & Look
                elif packet_id == 0x0D:
                    if len(raw) >= 41 and player_eid in dedicated_server.players:
                        try:
                            x, y, stance, z, yaw, pitch, on_ground = struct.unpack_from(">ddddff?", raw, 1)
                            p = dedicated_server.players[player_eid]
                            p.x, p.y, p.z = x, y, z
                            p.yaw, p.pitch = yaw, pitch
                            tp = make_teleport_player_packet(player_eid, x, y, z, yaw, pitch)
                            await dedicated_server.broadcast(tp, exclude_eid=player_eid)

                            # Stream new chunks in radius 3 as player explores the survival world
                            pcx = int(math.floor(x / 16.0))
                            pcz = int(math.floor(z / 16.0))
                            for dcx in range(-3, 4):
                                for dcz in range(-3, 4):
                                    tgt = (pcx + dcx, pcz + dcz)
                                    if tgt not in p.loaded_chunks:
                                        new_chunk = get_or_generate_chunk(tgt[0], tgt[1])
                                        await websocket.send_bytes(new_chunk)
                                        p.loaded_chunks.add(tgt)
                                        # Also send any block modifications recorded in this chunk
                                        for (bx, by, bz), (bid, bmeta) in dedicated_server.block_changes.items():
                                            if (bx >> 4, bz >> 4) == tgt:
                                                await websocket.send_bytes(make_block_change_packet(bx, by, bz, bid, bmeta))
                        except Exception:
                            pass

                # Packet 0x0E: Player Digging (Block Break)
                elif packet_id == 0x0E:
                    if len(raw) >= 11:
                        try:
                            status, bx, by, bz, face = struct.unpack_from(">biBib", raw, 1)
                            if status == 2 or (status == 0 and player_gamemode == 1):
                                dedicated_server.block_changes[(bx, by, bz)] = (0, 0)
                                bc = make_block_change_packet(bx, by, bz, 0, 0)
                                await dedicated_server.broadcast(bc)
                            elif status == 0:
                                # Instant-break blocks in survival (flowers, tall grass, torches)
                                cur_blk = dedicated_server.block_changes.get((bx, by, bz))
                                if cur_blk and cur_blk[0] in (6, 31, 37, 38, 39, 40, 50):
                                    dedicated_server.block_changes[(bx, by, bz)] = (0, 0)
                                    bc = make_block_change_packet(bx, by, bz, 0, 0)
                                    await dedicated_server.broadcast(bc)
                                elif by == get_height(bx, bz) + 1:
                                    dedicated_server.block_changes[(bx, by, bz)] = (0, 0)
                                    bc = make_block_change_packet(bx, by, bz, 0, 0)
                                    await dedicated_server.broadcast(bc)
                        except Exception:
                            pass

                # Packet 0x0F: Player Block Placement
                elif packet_id == 0x0F:
                    if len(raw) >= 10:
                        try:
                            bx, by, bz, direction = struct.unpack_from(">iBib", raw, 1)
                            if direction in (-1, 255):
                                continue
                            tx, ty, tz = bx, by, bz
                            if direction == 0:
                                ty -= 1
                            elif direction == 1:
                                ty += 1
                            elif direction == 2:
                                tz -= 1
                            elif direction == 3:
                                tz += 1
                            elif direction == 4:
                                tx -= 1
                            elif direction == 5:
                                tx += 1

                            block_id = 1
                            meta = 0
                            if len(raw) >= 13:
                                item_id = struct.unpack_from(">h", raw, 11)[0]
                                if 0 < item_id < 256:
                                    block_id = item_id
                            if len(raw) >= 16:
                                damage = struct.unpack_from(">h", raw, 14)[0]
                                meta = max(0, min(15, damage & 0x0F))

                            dedicated_server.block_changes[(tx, ty, tz)] = (block_id, meta)
                            bc = make_block_change_packet(tx, ty, tz, block_id, meta)
                            await dedicated_server.broadcast(bc)
                        except Exception:
                            pass

                # Packet 0x10: Held Item Change
                elif packet_id == 0x10:
                    if len(raw) >= 3 and player_eid in dedicated_server.players:
                        slot_id = struct.unpack_from(">h", raw, 1)[0]
                        dedicated_server.players[player_eid].held_item = slot_id

                # Packet 0x12: Animation (Arm Swing)
                elif packet_id == 0x12:
                    if len(raw) >= 6 and player_eid in dedicated_server.players:
                        anim = raw[5]
                        anim_pkt = struct.pack(">Bib", 0x12, player_eid, anim)
                        await dedicated_server.broadcast(anim_pkt, exclude_eid=player_eid)

                # Packet 0x13: Entity Action (Crouch / Sprint)
                elif packet_id == 0x13:
                    if len(raw) >= 6 and player_eid in dedicated_server.players:
                        action = raw[5]
                        p = dedicated_server.players[player_eid]
                        if action == 1:
                            p.crouching = True
                        elif action == 2:
                            p.crouching = False
                        elif action == 4:
                            p.sprinting = True
                        elif action == 5:
                            p.sprinting = False
                        meta_pkt = make_entity_metadata_packet(player_eid, crouching=p.crouching, sprinting=p.sprinting)
                        await dedicated_server.broadcast(meta_pkt, exclude_eid=player_eid)

    except (WebSocketDisconnect, Exception) as exc:
        logger.debug(f"Player {player_name} disconnected: {exc}")
    finally:
        if keep_alive_task and not keep_alive_task.done():
            keep_alive_task.cancel()

        if player_eid is not None:
            async with dedicated_server.lock:
                dedicated_server.players.pop(player_eid, None)

            # Broadcast player departure and destroy entity
            leave_chat = make_chat_packet(f"§c[School Hub] §f{player_name} §7が退出しました。")
            destroy_pkt = make_destroy_entity_packet(player_eid)
            await dedicated_server.broadcast(leave_chat)
            await dedicated_server.broadcast(destroy_pkt)

            try:
                import current_activity
                with current_activity._LOCK:
                    current_activity._SESSIONS.pop(f"mc-{player_eid}", None)
            except Exception:
                pass
            try:
                import openchat_proxy
                asyncio.create_task(openchat_proxy._broadcast_online_count())
            except Exception:
                pass


# ==============================================================================
# Dedicated WebRTC Relay WebSocket Endpoint (/eagler/relay and /minecraft/relay)
# ==============================================================================

@router.websocket("/eagler/relay")
@router.websocket("/minecraft/relay")
async def eagler_webrtc_relay_endpoint(websocket: WebSocket):
    await websocket.accept()

    assigned_server: Optional[RelayServerNode] = None
    assigned_client: Optional[RelayClientNode] = None

    try:
        while True:
            raw = await websocket.receive_bytes()
            if not raw:
                continue

            packet_id = raw[0]
            stream = BytesIO(raw[1:])

            # Handshake (0x00)
            if packet_id == 0x00:
                conn_type = int.from_bytes(stream.read(1), "big")
                conn_version = int.from_bytes(stream.read(1), "big")
                code_or_name = RelayPacket.read_ascii8(stream)

                if conn_type == 1:  # LAN Server (Host)
                    async with webrtc_relay.lock:
                        code = webrtc_relay.generate_code()
                        while code in webrtc_relay.servers_by_code:
                            code = webrtc_relay.generate_code()
                        server = RelayServerNode(websocket, code, code_or_name or "LAN World", "render")
                        webrtc_relay.servers_by_code[code] = server
                        assigned_server = server

                    # Reply with assigned code
                    out = BytesIO()
                    out.write(struct.pack(">BBB", 0x00, conn_type, conn_version))
                    RelayPacket.write_ascii8(out, code)
                    await websocket.send_bytes(out.getvalue())

                    # Send ICE servers
                    ice_out = BytesIO()
                    ice_out.write(struct.pack(">BB", 0x01, 1))  # 1 server
                    RelayPacket.write_ascii16(ice_out, "stun:stun.l.google.com:19302")
                    ice_out.write(b"S")
                    RelayPacket.write_ascii16(ice_out, "")
                    RelayPacket.write_ascii16(ice_out, "")
                    await websocket.send_bytes(ice_out.getvalue())

                elif conn_type == 2:  # LAN Client (Joiner)
                    code = code_or_name.lower().strip()
                    server = webrtc_relay.servers_by_code.get(code)
                    if not server:
                        # Error: Code not found
                        err_out = BytesIO()
                        err_out.write(struct.pack(">BB", 0xFF, 5))
                        RelayPacket.write_ascii8(err_out, "Invalid World Code")
                        await websocket.send_bytes(err_out.getvalue())
                        await websocket.close()
                        return

                    client_id = webrtc_relay.generate_client_id()
                    client = RelayClientNode(websocket, server, client_id, "client")
                    assigned_client = client
                    server.clients[client_id] = client

                    # Reply with client ID
                    out = BytesIO()
                    out.write(struct.pack(">BBB", 0x00, conn_type, conn_version))
                    RelayPacket.write_ascii8(out, client_id)
                    await websocket.send_bytes(out.getvalue())

                    # Notify host of new client (0x02)
                    notify = BytesIO()
                    notify.write(struct.pack(">B", 0x02))
                    RelayPacket.write_ascii8(notify, client_id)
                    await server.ws.send_bytes(notify.getvalue())

                    # Send ICE servers to client
                    ice_out = BytesIO()
                    ice_out.write(struct.pack(">BB", 0x01, 1))
                    RelayPacket.write_ascii16(ice_out, "stun:stun.l.google.com:19302")
                    ice_out.write(b"S")
                    RelayPacket.write_ascii16(ice_out, "")
                    RelayPacket.write_ascii16(ice_out, "")
                    await websocket.send_bytes(ice_out.getvalue())

                elif conn_type == 3:  # Ping
                    pong = BytesIO()
                    pong.write(struct.pack(">BB", 0x69, 1))
                    RelayPacket.write_ascii8(pong, "School Hub WebRTC Relay (Render)")
                    RelayPacket.write_ascii8(pong, "Render")
                    await websocket.send_bytes(pong.getvalue())
                    await websocket.close()
                    return

                elif conn_type == 4:  # Poll local worlds
                    poll_out = BytesIO()
                    worlds = list(webrtc_relay.servers_by_code.values())
                    poll_out.write(struct.pack(">BB", 0x07, len(worlds)))
                    for w in worlds:
                        RelayPacket.write_ascii8(poll_out, w.name)
                        RelayPacket.write_ascii8(poll_out, w.code)
                    await websocket.send_bytes(poll_out.getvalue())

            # ICE Candidate (0x03)
            elif packet_id == 0x03:
                peer_id = RelayPacket.read_ascii8(stream)
                candidate_str = RelayPacket.read_ascii16(stream)
                if assigned_server:
                    target_client = assigned_server.clients.get(peer_id)
                    if target_client:
                        fwd = BytesIO()
                        fwd.write(struct.pack(">B", 0x03))
                        RelayPacket.write_ascii8(fwd, peer_id)
                        RelayPacket.write_ascii16(fwd, candidate_str)
                        await target_client.ws.send_bytes(fwd.getvalue())
                elif assigned_client:
                    fwd = BytesIO()
                    fwd.write(struct.pack(">B", 0x03))
                    RelayPacket.write_ascii8(fwd, assigned_client.id)
                    RelayPacket.write_ascii16(fwd, candidate_str)
                    await assigned_client.server.ws.send_bytes(fwd.getvalue())

            # Description (0x04)
            elif packet_id == 0x04:
                peer_id = RelayPacket.read_ascii8(stream)
                desc_str = RelayPacket.read_ascii16(stream)
                if assigned_server:
                    target_client = assigned_server.clients.get(peer_id)
                    if target_client:
                        fwd = BytesIO()
                        fwd.write(struct.pack(">B", 0x04))
                        RelayPacket.write_ascii8(fwd, peer_id)
                        RelayPacket.write_ascii16(fwd, desc_str)
                        await target_client.ws.send_bytes(fwd.getvalue())
                elif assigned_client:
                    fwd = BytesIO()
                    fwd.write(struct.pack(">B", 0x04))
                    RelayPacket.write_ascii8(fwd, assigned_client.id)
                    RelayPacket.write_ascii16(fwd, desc_str)
                    await assigned_client.server.ws.send_bytes(fwd.getvalue())

            # Client Success (0x05)
            elif packet_id == 0x05:
                if assigned_client:
                    fwd = BytesIO()
                    fwd.write(struct.pack(">B", 0x05))
                    RelayPacket.write_ascii8(fwd, assigned_client.id)
                    await assigned_client.server.ws.send_bytes(fwd.getvalue())

            # Client Failure (0x06)
            elif packet_id == 0x06:
                if assigned_client:
                    fwd = BytesIO()
                    fwd.write(struct.pack(">B", 0x06))
                    RelayPacket.write_ascii8(fwd, assigned_client.id)
                    await assigned_client.server.ws.send_bytes(fwd.getvalue())

            # Disconnect Client (0xFE)
            elif packet_id == 0xFE:
                peer_id = RelayPacket.read_ascii8(stream)
                if assigned_server:
                    c = assigned_server.clients.pop(peer_id, None)
                    if c:
                        try:
                            await c.ws.close()
                        except Exception:
                            pass

    except (WebSocketDisconnect, Exception):
        pass
    finally:
        if assigned_server:
            webrtc_relay.servers_by_code.pop(assigned_server.code, None)
            for c in list(assigned_server.clients.values()):
                try:
                    await c.ws.close()
                except Exception:
                    pass
        if assigned_client:
            assigned_client.server.clients.pop(assigned_client.id, None)
            fwd = BytesIO()
            fwd.write(struct.pack(">B", 0xFE))
            RelayPacket.write_ascii8(fwd, assigned_client.id)
            try:
                await assigned_client.server.ws.send_bytes(fwd.getvalue())
            except Exception:
                pass
