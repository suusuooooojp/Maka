"""Priority shim for isolated Interstellar compatibility routes and buffered Wisp."""

import asyncio
import struct

from fastapi import WebSocket, WebSocketDisconnect
from fastapi.routing import APIRoute
from starlette.routing import WebSocketRoute

from app import (
    WISP_BUFFER,
    WISP_IDLE_TIMEOUT,
    WISP_MAX_STREAMS,
    WISP_REPLENISH,
    app,
    public_tcp,
    wisp_close,
    wisp_continue,
    wisp_extensions,
    wisp_info,
    wisp_packet,
)
from interstellar_compat_proxy import (
    interstellar_bm_worker,
    interstellar_c1,
    interstellar_dynamic_config,
    interstellar_h1,
    interstellar_index3,
    interstellar_m1,
    interstellar_sj_tp,
    interstellar_sw,
    interstellar_t3,
    interstellar_uv_config,
    interstellar_uv_sw,
    scramjet_all,
    scramjet_sync,
    scramjet_wasm,
)


async def buffered_wisp_endpoint(websocket: WebSocket):
    """Wisp v1/v2 endpoint that buffers 0x02 DATA packets sent before TCP connect completes."""
    requested = list(websocket.scope.get("subprotocols", []) or [])
    v2 = bool(requested)
    try:
        await websocket.accept(subprotocol=requested[0] if requested else None)
    except Exception:
        return
    streams: dict[int, asyncio.StreamWriter] = {}
    pending_buffers: dict[int, list[bytes]] = {}
    tasks: dict[int, asyncio.Task] = {}
    extensions = set()
    alive = True

    async def send_packet(data: bytes):
        nonlocal alive
        if not alive:
            return False
        try:
            await websocket.send_bytes(data)
            return True
        except Exception:
            alive = False
            return False

    async def close_stream(stream_id: int, reason=2, notify=True):
        writer = streams.pop(stream_id, None)
        pending_buffers.pop(stream_id, None)
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
            await send_packet(wisp_close(stream_id, reason))

    async def pump(stream_id: int, reader: asyncio.StreamReader):
        try:
            while alive:
                data = await asyncio.wait_for(reader.read(64 * 1024), WISP_IDLE_TIMEOUT)
                if not data:
                    await send_packet(wisp_close(stream_id, 2))
                    break
                if not await send_packet(wisp_packet(0x02, stream_id, data)):
                    break
        except Exception:
            await send_packet(wisp_close(stream_id, 0x03))
        finally:
            writer = streams.pop(stream_id, None)
            pending_buffers.pop(stream_id, None)
            tasks.pop(stream_id, None)
            if writer:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

    try:
        if v2:
            await send_packet(wisp_info())
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
                    await send_packet(wisp_close(stream_id, 0x41))
                    continue
                if kind == 0x05:
                    if len(payload) < 2 or payload[0] != 2:
                        await send_packet(wisp_close(0, 0x04))
                        return
                    extensions = wisp_extensions(payload)
                    break
                if kind == 0x03:
                    v2 = False
                    break
                if kind == 0x04:
                    return
                await send_packet(wisp_close(0, 0x04))
                return
        await send_packet(wisp_continue(0, WISP_BUFFER))
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
                    or stream_id in pending_buffers
                    or (len(streams) + len(pending_buffers)) >= WISP_MAX_STREAMS
                    or len(payload) < 3
                ):
                    await send_packet(wisp_close(stream_id, 0x41))
                    continue
                stream_type = payload[0]
                port = struct.unpack_from("<H", payload, 1)[0]
                try:
                    host = payload[3:].decode("utf-8", "strict").strip().rstrip(".").lower()
                except UnicodeDecodeError:
                    await send_packet(wisp_close(stream_id, 0x41))
                    continue
                if (
                    stream_type != 1
                    or not host
                    or len(host) > 253
                    or not (1 <= port <= 65535)
                    or any(c.isspace() for c in host)
                ):
                    await send_packet(wisp_close(stream_id, 0x41))
                    continue

                pending_buffers[stream_id] = []

                async def open_one(sid=stream_id, hostname=host, dst_port=port):
                    try:
                        reader, writer = await public_tcp(hostname, dst_port)
                        buffered = pending_buffers.pop(sid, None)
                        if buffered is None:
                            writer.close()
                            await writer.wait_closed()
                            return
                        streams[sid] = writer
                        if v2 and 0x05 in extensions:
                            await send_packet(wisp_continue(sid, WISP_BUFFER))
                        for chunk in buffered:
                            if chunk:
                                writer.write(chunk)
                        if buffered:
                            await writer.drain()
                            await send_packet(wisp_continue(sid, WISP_REPLENISH))
                        tasks[sid] = asyncio.create_task(pump(sid, reader))
                    except Exception:
                        pending_buffers.pop(sid, None)
                        await send_packet(wisp_close(sid, 0x03))

                tasks[stream_id] = asyncio.create_task(open_one())
            elif kind == 0x02:
                writer = streams.get(stream_id)
                if writer is None:
                    buf = pending_buffers.get(stream_id)
                    if buf is not None and payload:
                        if sum(len(x) for x in buf) + len(payload) <= 512 * 1024:
                            buf.append(payload)
                    continue
                try:
                    if payload:
                        writer.write(payload)
                        await writer.drain()
                        await send_packet(wisp_continue(stream_id, WISP_REPLENISH))
                except Exception:
                    await close_stream(stream_id, 0x03)
            elif kind == 0x04:
                await close_stream(stream_id, 0x02)
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        pending_buffers.clear()
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


_PRIORITY_ROUTES = (
    ("/interstellar/sw.js", interstellar_sw, ("GET", "HEAD"), "interstellar_sw_priority"),
    ("/interstellar/assets/js/sj-tp.js", interstellar_sj_tp, ("GET", "HEAD"), "interstellar_sj_tp_priority"),
    ("/interstellar/assets/js/index-3.js", interstellar_index3, ("GET", "HEAD"), "interstellar_index3_priority"),
    ("/interstellar/assets/js/t3.js", interstellar_t3, ("GET", "HEAD"), "interstellar_t3_priority"),
    ("/interstellar/assets/js/c1.js", interstellar_c1, ("GET", "HEAD"), "interstellar_c1_priority"),
    ("/interstellar/assets/js/h1.js", interstellar_h1, ("GET", "HEAD"), "interstellar_h1_priority"),
    ("/interstellar/assets/js/m1.js", interstellar_m1, ("GET", "HEAD"), "interstellar_m1_priority"),
    ("/interstellar/assets/mathematics/config.js", interstellar_uv_config, ("GET", "HEAD"), "interstellar_uv_cfg_priority"),
    ("/interstellar/assets/mathematics/sw.js", interstellar_uv_sw, ("GET", "HEAD"), "interstellar_uv_sw_priority"),
    ("/interstellar/assets/history/config.js", interstellar_dynamic_config, ("GET", "HEAD"), "interstellar_dyn_cfg_priority"),
    ("/interstellar/bm/worker.js", interstellar_bm_worker, ("GET", "HEAD"), "interstellar_bm_worker_priority"),
    ("/interstellar/scramjet.all.js", scramjet_all, ("GET",), "scramjet_all_priority"),
    ("/interstellar/scramjet.sync.js", scramjet_sync, ("GET",), "scramjet_sync_priority"),
    ("/interstellar/scramjet.wasm.wasm", scramjet_wasm, ("GET",), "scramjet_wasm_priority"),
)

_generic_index = next(
    (
        index
        for index, route in enumerate(app.router.routes)
        if isinstance(route, APIRoute)
        and route.path in {"/{service}/sw.js", "/{service}/{path:path}", "/{service}", "/interstellar/{path:path}"}
    ),
    None,
)
if _generic_index is not None:
    for path, endpoint, methods, name in reversed(_PRIORITY_ROUTES):
        app.router.routes.insert(
            _generic_index,
            APIRoute(path=path, endpoint=endpoint, methods=list(methods), name=name, include_in_schema=False),
        )

wisp_index = next(
    (
        index
        for index, route in enumerate(app.router.routes)
        if isinstance(route, WebSocketRoute) and route.path == "/wisp/"
    ),
    None,
)
if wisp_index is not None:
    app.router.routes.insert(wisp_index, WebSocketRoute("/wisp/", buffered_wisp_endpoint))
    app.router.routes.insert(0, WebSocketRoute("/interstellar/wisp/", buffered_wisp_endpoint))
    app.router.routes.insert(0, WebSocketRoute("/interstellar/wisp", buffered_wisp_endpoint))
    print("[Interstellar] Buffered Wisp priority route installed", flush=True)
