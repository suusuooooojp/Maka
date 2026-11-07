from __future__ import annotations

import atexit
import asyncio
import fcntl
import hashlib
import http.client
import json
import mimetypes
import os
import platform
import re
import shutil
import signal
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from collections import deque
from typing import Any
from pathlib import Path

import httpx
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from starlette.requests import ClientDisconnect
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse

SERVICE_NAME = "Interstellar"
SERVICE_ICON = "🚀"
SERVICE_PATH = "/interstellar/"
PREFIX = "/interstellar"
VERSION = "5.3.0"
ROOT = Path("/tmp/interstellar-native-5.3.0")
SOURCE = ROOT / "source"
LOGS = ROOT / "logs"
NODE_ROOT = ROOT / "node"
HOST, PORT = "127.0.0.1", 18153
NODE_VERSION = "22.23.2"
NODE_SHA256 = {
    "x64": "d60acfe00a2932254bb0ad20e01b0d74397a0875595de719654b214f4b03f307",
    "arm64": "fff4078c5def658577f92c88db7db3bc0072924bfb93fe52c1e744a54e94abb8",
}
MAX_SOURCE = 150 * 1024 * 1024
MAX_NODE = 120 * 1024 * 1024
MAX_EXTRACT = 600 * 1024 * 1024
MAX_MEMBERS = 20000
MAX_BODY = 64 * 1024 * 1024
MAX_REWRITE_BODY = 8 * 1024 * 1024
MAX_LOG = 5 * 1024 * 1024
MAX_LOG_TAIL = 256 * 1024
MAX_WS_MESSAGE = 64 * 1024 * 1024
MAX_STATUS_PATH = 512
ARCHIVES = (
    f"https://github.com/UseInterstellar/Interstellar/archive/refs/tags/v{VERSION}.tar.gz",
    f"https://codeload.github.com/UseInterstellar/Interstellar/tar.gz/refs/tags/v{VERSION}",
)
router = APIRouter(prefix=PREFIX)
lock = asyncio.Lock()
thread_lock = ROOT / ".runtime.lock"
proc = None
client: httpx.AsyncClient | None = None
starting: asyncio.Task | None = None
phase, detail, error = "idle", "", None
last_retry_at = 0.0
native_external_ready = False
stopping = False
asset_cache: dict[str, tuple[tuple[Any, ...], Path | None]] = {}
_CACHE_MISS = object()


def _redact_log_text(text: str) -> str:
    text = re.sub(r"(?i)(authorization\s*[:=]\s*)([^\r\n]+)", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(cookie\s*[:=]\s*)([^\r\n]+)", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)((?:token|secret|password|passwd|api[-_]?key)\s*[:=]\s*)([^\s,;]+)", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)([?&](?:access[_-]?token|auth(?:orization)?|token|secret|password|passwd|api[_-]?key|sig|signature|x-amz-signature|x-amz-credential)=)[^&#\s]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(https?://)([^/@\s:]+):([^/@\s]+)@", r"\1[REDACTED]:[REDACTED]@", text)
    return text


def log(msg: str):
    LOGS.mkdir(parents=True, exist_ok=True)
    msg = _redact_log_text(str(msg).replace("\x00", "\\0"))[:4000]
    line = f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] [INTERSTELLAR] {msg}"
    print(line, flush=True)
    try:
        p = LOGS / "runtime.log"
        with p.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        if p.stat().st_size > MAX_LOG:
            p.write_bytes(p.read_bytes()[-MAX_LOG // 2 :])
    except Exception:
        pass


def set_phase(name: str, msg: str = ""):
    global phase, detail
    phase, detail = str(name)[:100], str(msg)[:1000]
    log(f"PHASE {phase}: {detail}" if detail else f"PHASE {phase}")
    try:
        ROOT.mkdir(parents=True, exist_ok=True)
        (ROOT / "runtime.json").write_text(
            json.dumps({"phase": phase, "detail": detail, "time": time.time()}),
            encoding="utf-8",
        )
    except Exception:
        pass


def tail_lines(path: Path, limit: int):
    """Read only a bounded tail of a log file."""
    if limit <= 0 or not path.exists():
        return []
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - MAX_LOG_TAIL))
            data = f.read(MAX_LOG_TAIL)
        text = data.decode("utf-8", "replace")
        lines = text.splitlines()
        if size > MAX_LOG_TAIL and lines:
            lines = lines[1:]
        return lines[-limit:]
    except OSError:
        return []


def running():
    return bool(proc and proc.poll() is None)


def native_available():
    """Whether this worker can use the native runtime, including a runtime started by another worker."""
    return running() or native_external_ready


def terminate(p):
    if not p:
        return
    try:
        if p.poll() is None:
            os.killpg(p.pid, signal.SIGTERM)
            end = time.time() + 8
            while p.poll() is None and time.time() < end:
                time.sleep(0.1)
        if p.poll() is None:
            os.killpg(p.pid, signal.SIGKILL)
    except Exception:
        pass
    try:
        p.wait(timeout=2)
    except Exception:
        pass


def _capture_process_log(stream, path: Path):
    """Drain child output without allowing its log file to grow without bound."""
    try:
        while True:
            chunk = stream.readline()
            if not chunk:
                break
            try:
                with path.open("ab") as out:
                    out.write(chunk)
                    out.flush()
                    size = out.tell()
                if size > MAX_LOG:
                    with path.open("rb") as src:
                        src.seek(max(0, size - MAX_LOG // 2))
                        tail = src.read(MAX_LOG // 2)
                    path.write_bytes(tail)
            except OSError:
                continue
    except Exception as exc:
        log(f"PROCESS LOG CAPTURE STOPPED {type(exc).__name__}: {exc}")


atexit.register(lambda: terminate(proc))


def download(url, dst, limit, timeout=900):
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_name(dst.name + ".part")
    try:
        part.unlink()
    except OSError:
        pass
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"SchoolHub-Interstellar/{VERSION}",
            "Accept": "application/gzip, application/octet-stream;q=0.9, */*;q=0.1",
        },
    )
    log(f"DOWNLOAD START {url}")
    deadline = time.monotonic() + timeout
    with urllib.request.urlopen(req, timeout=timeout) as src, part.open("wb") as out:
        status = getattr(src, "status", None) or src.getcode()
        if status != 200:
            raise RuntimeError(f"HTTP {status}")
        raw = src.headers.get("Content-Length")
        total = int(raw) if raw and raw.isdigit() else 0
        if total > limit:
            raise RuntimeError("download exceeds limit")
        copied = next_report = 0
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            copied += len(chunk)
            if time.monotonic() >= deadline:
                raise TimeoutError(f"download timed out after {timeout} seconds")
            if copied > limit:
                raise RuntimeError("download exceeds configured limit")
            out.write(chunk)
            if total and copied * 100 // total >= next_report:
                log(f"DOWNLOAD PROGRESS {copied}/{total} ({copied * 100 // total}%)")
                next_report += 10
        out.flush()
        os.fsync(out.fileno())
    if total and copied != total:
        raise RuntimeError(f"incomplete download: expected {total} bytes, received {copied}")
    if copied < 100000:
        raise RuntimeError("downloaded file is unexpectedly small")
    os.replace(part, dst)
    log(f"DOWNLOAD COMPLETE bytes={copied}")


def sha256(p):
    h = hashlib.sha256()
    with p.open("rb") as f:
        for c in iter(lambda: f.read(1024 * 1024), b""):
            h.update(c)
    return h.hexdigest()


def verify_sha256(p: Path, expected: str):
    actual = sha256(p)
    if not expected or actual.lower() != expected.lower():
        raise RuntimeError(f"sha256 mismatch for {p.name}")
    return actual


def safe_extract(tf, dest):
    members = tf.getmembers()
    if len(members) > MAX_MEMBERS:
        raise RuntimeError("archive has too many members")
    root = dest.resolve()
    total = 0
    seen_targets = set()
    for m in members:
        if m.name.startswith("/") or "\x00" in m.name:
            raise RuntimeError(f"unsafe archive member: {m.name!r}")
        target = (root / m.name).resolve()
        if os.path.commonpath((str(root), str(target))) != str(root):
            raise RuntimeError(f"archive traversal: {m.name}")
        target_key = str(target)
        if target_key in seen_targets:
            raise RuntimeError(f"duplicate archive member: {m.name}")
        seen_targets.add(target_key)
        if m.isfile():
            total += max(0, m.size)
            if total > MAX_EXTRACT:
                raise RuntimeError("archive expansion limit exceeded")
        if m.isdev():
            raise RuntimeError("device entry rejected")
        if m.issym() or m.islnk():
            raise RuntimeError(f"archive link rejected: {m.name}")
    try:
        tf.extractall(dest, filter="data")
    except TypeError:
        tf.extractall(dest)


def source_ready():
    try:
        pkg = json.loads((SOURCE / "package.json").read_text(encoding="utf-8"))
        stamp = json.loads((ROOT / "source.json").read_text(encoding="utf-8"))
        if not (
            (SOURCE / "index.js").is_file()
            and pkg.get("name") == "interstellar"
            and str(stamp.get("version")) == VERSION
        ):
            return False
        pkg_sha = hashlib.sha256((SOURCE / "package.json").read_bytes()).hexdigest()
        stored_sha = stamp.get("pkg_sha256")
        if stored_sha and pkg_sha == stored_sha:
            return True
        fp = str(stamp.get("package_fingerprint", ""))
        curr_fp = fingerprint(SOURCE)
        if fp == curr_fp:
            return True
        h = hashlib.sha256()
        h.update(b"package.json")
        h.update((SOURCE / "package.json").read_bytes())
        return fp == h.hexdigest()
    except Exception:
        return False


def prepare_source():
    ROOT.mkdir(parents=True, exist_ok=True)
    with thread_lock.open("a+") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            if source_ready():
                log("SOURCE CACHE HIT")
                return SOURCE
            archive = ROOT / f"Interstellar-v{VERSION}.tar.gz"
            last = None
            for url in ARCHIVES:
                try:
                    set_phase("downloading", url)
                    if not archive.is_file() or archive.stat().st_size < 100000:
                        download(url, archive, MAX_SOURCE)
                    set_phase("extracting", archive.name)
                    tmp = Path(tempfile.mkdtemp(prefix="is-extract-", dir=ROOT))
                    staging = ROOT / "source.staging"
                    try:
                        with tarfile.open(archive, "r:gz") as tf:
                            safe_extract(tf, tmp)
                        dirs = [p for p in tmp.iterdir() if p.is_dir()]
                        if len(dirs) != 1:
                            raise RuntimeError("unexpected archive layout")
                        shutil.rmtree(staging, ignore_errors=True)
                        shutil.copytree(dirs[0], staging, symlinks=False)
                        if not (staging / "package.json").is_file() or not (staging / "index.js").is_file():
                            raise RuntimeError("official source structure invalid")
                        package_fp = fingerprint(staging)
                        shutil.rmtree(SOURCE, ignore_errors=True)
                        os.replace(staging, SOURCE)
                        (ROOT / "source.json").write_text(
                            json.dumps({
                                "version": VERSION,
                                "sha256": sha256(archive),
                                "package_fingerprint": package_fp,
                                "pkg_sha256": hashlib.sha256((SOURCE / "package.json").read_bytes()).hexdigest(),
                                "time": time.time(),
                            }),
                            encoding="utf-8",
                        )
                    finally:
                        shutil.rmtree(staging, ignore_errors=True)
                        shutil.rmtree(tmp, ignore_errors=True)
                    log("SOURCE READY")
                    return SOURCE
                except Exception as exc:
                    last = exc
                    log(f"SOURCE FAILED {type(exc).__name__}: {exc}")
                    try:
                        archive.unlink()
                    except OSError:
                        pass
            raise RuntimeError(f"unable to download Interstellar {VERSION}: {last}")
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def node_ok(n):
    try:
        out = subprocess.check_output([n, "--version"], stderr=subprocess.STDOUT, text=True, timeout=10).strip()
        m = re.fullmatch(r"v(\d+)(?:\.\d+){0,2}", out)
        return bool(m and int(m.group(1)) >= 18)
    except Exception:
        return False


def ensure_node():
    n, npm = shutil.which("node"), shutil.which("npm")
    if n and npm and node_ok(n):
        return n, npm
    n, npm = NODE_ROOT / "bin/node", NODE_ROOT / "bin/npm"
    if n.is_file() and npm.is_file() and node_ok(str(n)):
        return str(n), str(npm)
    if platform.system().lower() != "linux":
        raise RuntimeError("unsupported runtime OS")
    m = platform.machine().lower()
    arch = "x64" if m in {"x86_64", "amd64"} else "arm64" if m in {"aarch64", "arm64"} else None
    if not arch:
        raise RuntimeError(f"unsupported Linux architecture: {m}")
    name = f"node-v{NODE_VERSION}-linux-{arch}.tar.xz"
    arc = ROOT / name
    set_phase("downloading-node", f"Node.js {NODE_VERSION} {arch}")
    expected_sha = NODE_SHA256[arch]
    if arc.is_file() and arc.stat().st_size >= 10000000:
        try:
            verify_sha256(arc, expected_sha)
        except RuntimeError:
            arc.unlink(missing_ok=True)
    if not arc.is_file() or arc.stat().st_size < 10000000:
        download(f"https://nodejs.org/dist/v{NODE_VERSION}/{name}", arc, MAX_NODE)
    verify_sha256(arc, expected_sha)
    tmp = Path(tempfile.mkdtemp(prefix="node-extract-", dir=ROOT))
    staging = ROOT / "node.staging"
    try:
        set_phase("extracting-node", name)
        with tarfile.open(arc, "r:xz") as tf:
            safe_extract(tf, tmp)
        dirs = [p for p in tmp.iterdir() if p.is_dir()]
        if len(dirs) != 1:
            raise RuntimeError("unexpected Node archive layout")
        shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(dirs[0], staging, symlinks=False)
        shutil.rmtree(NODE_ROOT, ignore_errors=True)
        os.replace(staging, NODE_ROOT)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)
    if not n.is_file() or not npm.is_file() or not node_ok(str(n)):
        raise RuntimeError("Node fallback is unusable")
    return str(n), str(npm)


def fingerprint(source):
    h = hashlib.sha256()
    for name in ("package.json", "package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml"):
        p = source / name
        if p.is_file():
            h.update(name.encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def install_deps(npm, source, env):
    modules, stamp = source / "node_modules", source / ".schoolhub-npm-installed"
    with (ROOT / ".npm-install.lock").open("a+") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            fp, old = fingerprint(source), None
            try:
                stamp_data = json.loads(stamp.read_text(encoding="utf-8"))
                old = stamp_data.get("fingerprint")
            except Exception:
                pass
            if modules.is_dir() and old == fp:
                log("NPM CACHE HIT")
                return
            shutil.rmtree(modules, ignore_errors=True)
            try:
                stamp.unlink()
            except OSError:
                pass
            set_phase("installing", "installing official Interstellar dependencies")
            cmd = [npm, "install", "--omit=dev", "--no-audit", "--no-fund"]
            pth = LOGS / "npm-install.log"
            recent = deque(maxlen=3000)
            deadline = time.monotonic() + 1800
            last_report = 0.0
            with pth.open("w", encoding="utf-8") as out:
                p = subprocess.Popen(cmd, cwd=source, env=env, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    while True:
                        rc = p.poll()
                        try:
                            tail = tail_lines(pth, 40)
                            recent.extend(tail[-40:])
                        except OSError:
                            tail = []
                        now = time.monotonic()
                        if tail and now - last_report >= 5:
                            log(f"NPM INSTALL: {tail[-1].strip()[:900]}")
                            last_report = now
                        if rc is not None:
                            break
                        if now >= deadline:
                            terminate(p)
                            raise RuntimeError("npm install timed out after 1800 seconds")
                        time.sleep(0.5)
                finally:
                    if p.poll() is None:
                        terminate(p)
            if rc:
                try:
                    stamp.unlink()
                except OSError:
                    pass
                raise RuntimeError(f"npm install failed exit={rc}: {list(recent)[-8:]}")
            post_fp = fingerprint(source)
            stamp.write_text(json.dumps({"version": VERSION, "fingerprint": post_fp, "time": time.time()}), encoding="utf-8")
            try:
                sj = json.loads((ROOT / "source.json").read_text(encoding="utf-8"))
                sj["package_fingerprint"] = post_fp
                (ROOT / "source.json").write_text(json.dumps(sj), encoding="utf-8")
            except Exception:
                pass
            log("NPM INSTALL COMPLETE")
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def prepare():
    source = prepare_source()
    node, npm = ensure_node()
    pkg = json.loads((source / "package.json").read_text(encoding="utf-8"))
    if pkg.get("name") != "interstellar":
        raise RuntimeError(f"unexpected package: {pkg.get('name')!r}")
    env = os.environ.copy()
    env.update({"CI": "true", "NODE_ENV": "production", "npm_config_audit": "false", "npm_config_fund": "false", "npm_config_update_notifier": "false", "NPM_CONFIG_COLOR": "false", "PATH": str(Path(node).parent) + os.pathsep + env.get("PATH", "")})
    install_deps(npm, source, env)
    return node, npm, source, pkg, env


def health():
    c = http.client.HTTPConnection(HOST, PORT, timeout=2)
    try:
        c.request("GET", "/")
        r = c.getresponse()
        b = r.read(512)
        return r.status, b.decode("utf-8", "replace")
    except Exception as exc:
        return 0, str(exc)
    finally:
        try:
            c.close()
        except Exception:
            pass


def start_sync():
    global proc, client, asset_cache, error, native_external_ready, stopping
    ROOT.mkdir(parents=True, exist_ok=True)
    start_lock = ROOT / ".runtime-start.lock"
    with start_lock.open("a+") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            if running():
                return
            existing_status, _ = health()
            if 100 <= existing_status < 500:
                asset_cache = {}
                error = None
                native_external_ready = True
                set_phase("ready", f"native runtime already listening status={existing_status}")
                return
            node, npm, source, pkg, env = prepare()
            if stopping:
                raise RuntimeError("Interstellar startup cancelled by application shutdown")
            cmd = [npm, "run", "start"] if (pkg.get("scripts") or {}).get("start") else [node, "index.js"]
            env.update({"PORT": str(PORT), "HOST": HOST, "BIND": HOST})
            outp = LOGS / "interstellar-node.log"
            new = None
            try:
                set_phase("starting", f"{' '.join(cmd)} on {HOST}:{PORT}")
                new = subprocess.Popen(cmd, cwd=source, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
                proc = new
                if new.stdout is not None:
                    threading.Thread(target=_capture_process_log, args=(new.stdout, outp), name="interstellar-node-log", daemon=True).start()
                native_external_ready = False
                set_phase("checking-runtime", f"waiting for port {PORT}")
                deadline = time.time() + 120
                while time.time() < deadline:
                    if proc.poll() is not None:
                        raise RuntimeError(f"native exited {proc.returncode}: {outp.read_text(encoding='utf-8', errors='replace')[-16000:]}")
                    s, b = health()
                    if 100 <= s < 500:
                        asset_cache = {}
                        error = None
                        set_phase("ready", f"native ready status={s}")
                        return
                    time.sleep(0.25)
                raise RuntimeError(f"native runtime did not become healthy: {b}")
            except Exception:
                terminate(new)
                proc = None
                client = None
                raise
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


async def ensure_native():
    global client, error, phase, native_external_ready
    if native_external_ready and not running():
        status_code, _ = await asyncio.to_thread(health)
        if not (100 <= status_code < 500):
            native_external_ready = False
        else:
            phase = "ready"
            error = None
            if client is None:
                client = httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=httpx.Timeout(connect=10, read=180, write=180, pool=20), limits=httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=30))
            return
    if running() and phase != "ready":
        status_code, _ = await asyncio.to_thread(health)
        if 100 <= status_code < 500:
            phase = "ready"
            error = None
            log(f"PHASE ready: detected existing native runtime status={status_code}")
    if running() and phase == "ready":
        if client is None:
            client = httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=httpx.Timeout(connect=10, read=180, write=180, pool=20), limits=httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=30))
        return
    async with lock:
        was_running = running()
        if not was_running and client is not None:
            try:
                await client.aclose()
            except Exception:
                pass
            client = None
        if not running():
            try:
                await asyncio.to_thread(start_sync)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                set_phase("error", error)
                log("START FAILED " + error)
                raise
        if client is None:
            client = httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=httpx.Timeout(connect=10, read=180, write=180, pool=20), limits=httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=30))


def kick():
    global starting
    if stopping:
        return
    if not running() and (starting is None or starting.done()):
        starting = asyncio.create_task(_background())


async def _background():
    global starting
    try:
        await ensure_native()
    except Exception:
        pass
    finally:
        starting = None


def target(path, q=""):
    raw_path = str(path).lstrip("/")
    encoded = urllib.parse.quote(raw_path, safe="/~!$&'()*+,;=:@-._")
    p = "/" + encoded
    return f"http://{HOST}:{PORT}{p}" + (f"?{q}" if q else "")


INTERNAL_PREFIXES = ("/scramjet/", "/scram/", "/bare/", "/baremod/", "/wisp/", "/service-worker/", "/sw.js", "/controller/", "/utils/", "/libcurl/", "/epoxy/", "/clients/")
PACKAGE_SPECS = {
    "scramjet": "@mercuryworkshop/scramjet",
    "controller": "@mercuryworkshop/scramjet-controller",
    "utils": "@mercuryworkshop/scramjet-utils",
    "libcurl": "@mercuryworkshop/libcurl-transport",
    "epoxy": "@mercuryworkshop/epoxy-transport",
    "bare": "@mercuryworkshop/bare-transport",
}


def is_internal_path(path):
    p = "/" + str(path).lstrip("/")
    return p in {"/scramjet", "/scram", "/bare", "/baremod", "/wisp", "/epoxy"} or p.startswith(INTERNAL_PREFIXES)


def package_roots():
    nm = SOURCE / "node_modules"
    roots = {}
    nm_resolved = nm.resolve()
    for key, spec in PACKAGE_SPECS.items():
        direct = nm.joinpath(*spec.split("/"))
        try:
            direct_resolved = direct.resolve()
            direct_inside = os.path.commonpath((str(nm_resolved), str(direct_resolved))) == str(nm_resolved)
        except (OSError, ValueError):
            direct_inside = False
        if direct.is_dir() and direct_inside:
            roots[key] = direct
            continue
        candidates = []
        scope, name = spec.split("/", 1) if spec.startswith("@") else (None, spec)
        encoded_prefix = spec.replace("/", "+") + "@"
        pnpm = nm / ".pnpm"
        if pnpm.is_dir():
            try:
                for entry in pnpm.iterdir():
                    if not entry.name.startswith(encoded_prefix):
                        continue
                    candidate = entry / "node_modules" / spec
                    if candidate.is_dir():
                        candidates.append(candidate)
            except OSError:
                pass
        safe_candidates = []
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
                if os.path.commonpath((str(nm_resolved), str(resolved))) == str(nm_resolved):
                    safe_candidates.append(candidate)
            except (OSError, ValueError):
                continue
        def _candidate_version(path: Path):
            try:
                meta = json.loads((path / "package.json").read_text(encoding="utf-8"))
                raw = str(meta.get("version", "0"))
                m = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?", raw)
                return tuple(int(x or 0) for x in m.groups()) if m else (0, 0, 0)
            except Exception:
                return (0, 0, 0)
        roots[key] = max(safe_candidates, key=lambda x: (_candidate_version(x), x.as_posix())) if safe_candidates else (nm / ".missing" / key)
    return roots


def _safe_asset(root: Path, rel: str):
    if not root.is_dir():
        return None
    rel = urllib.parse.unquote(str(rel)).replace("\\", "/").lstrip("/")
    if not rel or "\x00" in rel:
        return None
    candidate = (root / rel).resolve()
    root_resolved = root.resolve()
    try:
        if os.path.commonpath((str(root_resolved), str(candidate))) != str(root_resolved):
            return None
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _package_meta(root: Path):
    try:
        p = root / "package.json"
        if not p.is_file():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _iter_export_targets(value: Any, *, conditions: tuple[str, ...] = ("browser", "import", "require", "node", "default"), depth: int = 0):
    if depth > 32:
        return
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, list):
        for item in value:
            yield from _iter_export_targets(item, conditions=conditions, depth=depth + 1)
        return
    if not isinstance(value, dict):
        return
    for cond in conditions:
        if cond in value:
            yield from _iter_export_targets(value[cond], conditions=conditions, depth=depth + 1)


def _export_pairs(exports):
    if isinstance(exports, (str, list)):
        return [(".", target) for target in _iter_export_targets(exports) if isinstance(target, str)]
    if not isinstance(exports, dict):
        return []
    pairs = []
    saw_subpath = any(isinstance(k, str) and (k == "." or k.startswith("./")) for k in exports)
    if saw_subpath:
        for key, value in exports.items():
            if not isinstance(key, str) or not (key == "." or key.startswith("./")):
                continue
            for target in _iter_export_targets(value):
                if isinstance(target, str):
                    pairs.append((key, target))
    else:
        for target in _iter_export_targets(exports):
            if isinstance(target, str):
                pairs.append((".", target))
    return pairs


def _metadata_candidates(root: Path, logical: str | None = None):
    pkg = _package_meta(root)
    values: list[tuple[int, str]] = []
    logical = logical or ""
    exact_subpaths = {
        "scramjet_js": {"./scramjet.js", "./dist/scramjet.js"},
        "scramjet_wasm": {"./scramjet.wasm", "./dist/scramjet.wasm"},
        "controller_api": {"./controller.api.js", "./dist/controller.api.js"},
        "controller_inject": {"./controller.inject.js", "./dist/controller.inject.js"},
        "controller_sw": {"./controller.sw.js", "./dist/controller.sw.js"},
        "utils": {"./scramjet-utils.js", "./dist/scramjet-utils.js"},
    }.get(logical, set())
    if isinstance(pkg.get("exports"), (dict, list, str)):
        for subpath, target in _export_pairs(pkg.get("exports")):
            sub = str(subpath)
            target = str(target)
            if sub in exact_subpaths:
                score = 1000
            elif logical == "utils" and "utils" in (sub + target).lower():
                score = 500
            elif logical in {"libcurl", "epoxy", "bare"} and sub == ".":
                score = 700
            else:
                score = -1
            if score >= 0:
                values.append((score, target))
    if logical in {"libcurl", "epoxy", "bare", "utils"}:
        for key, score in (("browser", 420), ("module", 410), ("main", 400), ("unpkg", 380), ("jsdelivr", 370)):
            v = pkg.get(key)
            if isinstance(v, str):
                values.append((score, v))
    seen: set[Path] = set()
    candidates: list[tuple[int, str, Path]] = []
    logical_tokens = {
        "utils": ("scramjet-utils", "utils"),
        "libcurl": ("libcurl", "client", "index"),
        "epoxy": ("epoxy", "client", "index"),
        "bare": ("bare", "client", "index"),
    }.get(logical, ())
    for base_score, raw in values:
        value = raw.strip()
        if not value or value.startswith("#") or value.startswith(("http://", "https://")) or "*" in value:
            continue
        if value.startswith("./"):
            value = value[2:]
        value = value.lstrip("/")
        candidate = _safe_asset(root, value)
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        low = (candidate.name + " " + candidate.as_posix()).lower()
        score = base_score + sum(12 for token in logical_tokens if token in low)
        suffix = candidate.suffix.lower()
        if suffix in {".js", ".mjs", ".wasm"}:
            score += 2
        if logical in {"libcurl", "epoxy", "bare"} and suffix == ".mjs":
            score += 8
        candidates.append((score, candidate.as_posix(), candidate))
    for _, _, candidate in sorted(candidates, key=lambda x: (-x[0], x[1])):
        yield candidate


def _all_files(root: Path):
    if not root.is_dir():
        return
    root_resolved = root.resolve()
    try:
        for p in root.rglob("*"):
            if not p.is_file() or ".cache" in p.parts or "node_modules" in p.parts or ".pnpm" in p.parts:
                continue
            resolved = p.resolve()
            try:
                if os.path.commonpath((str(root_resolved), str(resolved))) != str(root_resolved):
                    continue
            except ValueError:
                continue
            yield resolved
    except OSError:
        return


def _asset_signature(root: Path):
    try:
        root_resolved = root.resolve()
        pkg = root / "package.json"
        pstat = pkg.stat() if pkg.is_file() else root.stat()
        try:
            root_mtime = root.stat().st_mtime_ns
        except OSError:
            root_mtime = 0
        stamp = SOURCE / ".schoolhub-npm-installed"
        try:
            sstat = stamp.stat()
            stamp_sig = (sstat.st_mtime_ns, sstat.st_size)
        except OSError:
            stamp_sig = (0, 0)
        return (str(root_resolved), pstat.st_mtime_ns, pstat.st_size, root_mtime, stamp_sig)
    except OSError:
        return (str(root), 0, 0, 0, (0, 0))


def _cache_get(key, root):
    entry = asset_cache.get(key)
    if isinstance(entry, tuple) and len(entry) == 2 and entry[0] == _asset_signature(root):
        return entry[1]
    return _CACHE_MISS


def _cache_put(key, root, value):
    asset_cache[key] = (_asset_signature(root), value)
    return value


def _find_named(root: Path, logical: str, hints: tuple[str, ...] = ()):
    if not root.is_dir():
        return None
    key = "named:" + str(root.resolve()) + ":" + logical
    cached = _cache_get(key, root)
    if cached is not _CACHE_MISS:
        return cached
    metadata = list(_metadata_candidates(root, logical))
    for p in metadata:
        return _cache_put(key, root, p)
    exact = {
        "scramjet_js": ("dist/scramjet.js", "scramjet.js"),
        "scramjet_wasm": ("dist/scramjet.wasm", "scramjet.wasm"),
        "controller_api": ("dist/controller.api.js", "controller.api.js"),
        "controller_inject": ("dist/controller.inject.js", "controller.inject.js"),
        "controller_sw": ("dist/controller.sw.js", "controller.sw.js"),
        "utils": ("dist/scramjet-utils.js", "scramjet-utils.js"),
        "libcurl": ("index.mjs", "dist/index.mjs", "index.js", "dist/index.js"),
        "bare": ("index.mjs", "dist/index.mjs", "index.js", "dist/index.js"),
    }.get(logical, ())
    for rel in (*exact, *hints):
        p = _safe_asset(root, rel)
        if p:
            return _cache_put(key, root, p)
    files = list(_all_files(root))
    by_name = {p.name.lower(): p for p in files}
    for hint in hints:
        base = Path(hint).name.lower()
        if base in by_name:
            return _cache_put(key, root, by_name[base])
    keyword_map = {
        "scramjet_js": ("scramjet", ".js"),
        "scramjet_wasm": ("scramjet", ".wasm"),
        "controller_api": ("controller", "api", ".js"),
        "controller_inject": ("controller", "inject", ".js"),
        "controller_sw": ("controller", "sw", ".js"),
        "utils": ("scramjet-utils", ".js"),
        "libcurl": ("index",),
        "bare": ("index",),
    }
    keys = keyword_map.get(logical, ())
    for p in files:
        low = p.name.lower()
        if all(k in low for k in keys):
            if logical in {"libcurl", "bare"} and p.suffix.lower() not in {".js", ".mjs"}:
                continue
            return _cache_put(key, root, p)
    return _cache_put(key, root, None)


def _find_exact(root: Path, names: tuple[str, ...]):
    key = "exact:" + str(root) + "|" + "|".join(names)
    cached = _cache_get(key, root)
    if cached is not _CACHE_MISS:
        return cached
    if not root.is_dir():
        return _cache_put(key, root, None)
    for name in names:
        direct = _safe_asset(root, name)
        if direct:
            return _cache_put(key, root, direct)
    for p in _metadata_candidates(root):
        if p.name in names or any(p.as_posix().endswith("/" + n) for n in names):
            return _cache_put(key, root, p)
    files = list(_all_files(root))
    for name in names:
        base = Path(name).name.lower()
        for p in files:
            if p.name.lower() == base:
                return _cache_put(key, root, p)
    return _cache_put(key, root, None)


def resolve_scramjet_asset(virtual: str):
    roots = package_roots()
    v = "/" + urllib.parse.unquote(str(virtual)).lstrip("/")
    if v in {"/scramjet.js", "/scramjet/scramjet.js"}:
        return _find_named(roots["scramjet"], "scramjet_js", ("dist/scramjet.js", "scramjet.js"))
    if v in {"/scramjet.wasm", "/scramjet/scramjet.wasm"}:
        return _find_named(roots["scramjet"], "scramjet_wasm", ("dist/scramjet.wasm", "scramjet.wasm"))
    if v in {"/scramjet-utils.js", "/scram/scramjet-utils.js", "/scramjet/scramjet-utils.js", "/utils/scramjet-utils.js"}:
        return _find_named(roots["utils"], "utils", ("dist/scramjet-utils.js", "scramjet-utils.js"))
    if v.startswith("/controller/"):
        rel = v[len("/controller/"):].lstrip("/")
        known = {"controller.api.js": "controller_api", "controller.inject.js": "controller_inject", "controller.sw.js": "controller_sw"}
        logical = known.get(Path(rel).name)
        if logical:
            return _find_named(roots["controller"], logical, (f"dist/{rel}", rel))
        return _safe_asset(roots["controller"], rel)
    if v.startswith("/utils/"):
        rel = v[len("/utils/"):].lstrip("/")
        return _safe_asset(roots["utils"], rel)
    if v.startswith("/libcurl/"):
        rel = v[len("/libcurl/"):].lstrip("/")
        if Path(rel).name in {"index.js", "index.mjs", "libcurl-client.js"}:
            return _find_named(roots["libcurl"], "libcurl", (rel, "index.mjs", "dist/index.mjs", "index.js", "dist/index.js"))
        return _safe_asset(roots["libcurl"], rel)
    if v.startswith("/epoxy/"):
        rel = v[len("/epoxy/"):].lstrip("/")
        if Path(rel).name in {"index.js", "index.mjs", "epoxy-client.js"}:
            return _find_named(roots["epoxy"], "epoxy", (rel, "index.mjs", "dist/index.mjs", "index.js", "dist/index.js"))
        return _safe_asset(roots["epoxy"], rel)
    if v.startswith("/clients/"):
        name = Path(v[len("/clients/"):]).name
        if name == "libcurl-client.js":
            return _find_named(roots["libcurl"], "libcurl", ("index.mjs", "dist/index.mjs", "index.js", "dist/index.js"))
        if name == "epoxy-client.js":
            return _find_named(roots["epoxy"], "epoxy", ("index.mjs", "dist/index.mjs", "index.js", "dist/index.js"))
        if name == "bare-client.js":
            return _find_named(roots["bare"], "bare", ("index.mjs", "dist/index.mjs", "index.js", "dist/index.js"))
    if v.startswith("/bare/"):
        return _safe_asset(roots["bare"], v[len("/bare/"):].lstrip("/"))
    if v.startswith("/baremod/"):
        return _safe_asset(roots["bare"], v[len("/baremod/"):].lstrip("/"))
    return None


def asset_for(path: str):
    p = "/" + urllib.parse.unquote(path).lstrip("/")
    if p.startswith("/scramjet/"):
        return resolve_scramjet_asset(p[len("/scramjet/"):])
    if p == "/scramjet":
        return resolve_scramjet_asset("scramjet.js")
    if p.startswith("/scram/"):
        return resolve_scramjet_asset(p[len("/scram/"):])
    if p == "/scram":
        return resolve_scramjet_asset("scramjet.js")
    return resolve_scramjet_asset(p.lstrip("/"))


def asset_response(path: str):
    f = asset_for(path)
    if not f:
        return None
    media, _ = mimetypes.guess_type(str(f))
    if str(f).endswith(".wasm"):
        media = "application/wasm"
    elif str(f).endswith(".mjs"):
        media = "text/javascript"
    elif str(f).endswith(".js"):
        media = "application/javascript"
    log(f"ASSET {path} -> {f}")
    headers = {"Cache-Control": "public, max-age=3600", "X-Interstellar-Asset": "native-scramjet-5.3.0"}
    if str(path).strip("/").lower().endswith("controller.sw.js"):
        headers.update({"Service-Worker-Allowed": PREFIX + "/", "Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"})
    return FileResponse(f, media_type=media or "application/octet-stream", headers=headers)


def normalize_internal_url(u):
    if not isinstance(u, str):
        return u
    s = u.strip()
    if not s or s.startswith("#") or s.lower().startswith(("about:", "data:", "blob:", "javascript:", "mailto:", "tel:")):
        return u
    if s.startswith(PREFIX + "/"):
        return s
    return u


def text_type(ct):
    return any(x in (ct or "").lower() for x in ("text/html", "application/xhtml+xml", "text/css", "text/javascript", "application/javascript", "application/x-javascript", "application/json", "application/xml", "text/xml", "image/svg+xml"))


def req_headers(request, rewrite=False):
    connection_tokens = {token.strip().lower() for token in request.headers.get("connection", "").split(",") if token.strip()}
    skip = {"host", "content-length", "connection", "transfer-encoding", "accept-encoding", "proxy-authorization", "proxy-authenticate", "proxy-connection", "te", "trailer", "upgrade", "forwarded", "x-forwarded-host", "x-forwarded-proto", "x-forwarded-for", "x-forwarded-port", "x-forwarded-server", "x-real-ip", "true-client-ip", "cf-connecting-ip", "x-client-ip"}
    skip |= connection_tokens
    if rewrite:
        skip |= {"if-none-match", "if-modified-since"}
    h = {k: v for k, v in request.headers.items() if k.lower() not in skip}
    h.update({"accept-encoding": "identity", "host": f"{HOST}:{PORT}", "x-forwarded-host": request.headers.get("host", "")[:255], "x-forwarded-proto": request.url.scheme})
    return h

ROOT_RE = re.compile(r'''(?P<lead>["'`(=:\s>])(?P<url>/(?!/|interstellar(?:[/?:#"'`(),<>\s]|$)|(?:scramjet|scram|bare|baremod|wisp|controller|utils|libcurl|epoxy)(?:[/?:#"'`(),<>\s]|$))[^"'`\s<>()]+)''', re.I)
EMPTY_ROOT_ATTR_RE = re.compile(r'''(?P<lead>\b(?:href|src|action|poster|data|manifest|serviceworker|scope)\s*=\s*[\"\'])/(?P<end>[\"\'])''', re.I)
SPECIAL_RE = re.compile(r'''(?P<lead>["'`(=:\s>])(?P<url>/(?:scramjet|scram|bare|baremod|wisp|controller|utils|libcurl|epoxy)(?:[/?:#"'`(),<>\s]|$)[^"'`\s<>()]*)''', re.I)
SRCSET_ATTR_RE = re.compile(r'''(?P<prefix>\b(?:srcset|imagesrcset)\s*=\s*[\"'])(?P<value>[^\"']*)(?P<suffix>[\"'])''', re.I)


def _rewrite_srcset(value):
    return re.sub(r"(^|,\s*)(/(?!/)[^\s,]+)", lambda m: m.group(1) + root_url(m.group(2)), value)


def root_url(u):
    if not isinstance(u, str) or not u.startswith("/") or u.startswith("//"):
        return u
    if u == PREFIX or u.startswith((PREFIX + "/", PREFIX + "?", PREFIX + "#")):
        return u
    return PREFIX + u


def url_like(v):
    if not isinstance(v, str):
        return v
    s = v.strip()
    if not s or s.lower().startswith(("about:", "data:", "blob:", "javascript:", "mailto:", "tel:", "#")):
        return v
    if s.startswith(("/scramjet/", "/scram/", "/bare/", "/baremod/", "/wisp/", "/controller/", "/utils/", "/libcurl/", "/epoxy/")):
        return PREFIX + s
    if s.startswith("//"):
        return s
    return root_url(v) if s.startswith("/") else v


def rewrite_json_known(v, key=None):
    url_keys = {"url", "uri", "href", "src", "source", "location", "target", "endpoint", "ws", "websocket", "wisp", "bare", "scramjet", "icon", "manifest", "start_url", "scope", "serviceworker", "serviceWorker"}
    if isinstance(v, list):
        return [rewrite_json_known(x, key) for x in v]
    if isinstance(v, dict):
        return {k: rewrite_json_known(x, k) for k, x in v.items()}
    if isinstance(v, str) and key in url_keys:
        return url_like(v)
    return v


def inject(html):
    if "data-schoolhub-interstellar-isolation" in html:
        return html
    tag = '<script src="/interstellar/__shim.js" data-schoolhub-interstellar-isolation="1"></script>'
    if "sj.all.js" not in html and ("c1.js" in html or "index-3.js" in html):
        tag += (
            '<script src="/interstellar/assets/languagearts/sj.all.js?v=2025-04-15"></script>'
            '<script src="/interstellar/assets/js/sj-tp.js?v=2025-04-15"></script>'
        )
    if re.search(r"<head[^>]*>", html, re.I):
        return re.sub(r"(<head[^>]*>)", r"\1" + tag, html, count=1, flags=re.I)
    if re.search(r"</head\s*>", html, re.I):
        return re.sub(r"</head\s*>", tag + "</head>", html, count=1, flags=re.I)
    if re.search(r"<body[^>]*>", html, re.I):
        return re.sub(r"(<body[^>]*>)", r"\1" + tag, html, count=1, flags=re.I)
    return tag + html


def rewrite_body(body, ct):
    c = (ct or "").lower()
    if "application/json" in c:
        try:
            return json.dumps(rewrite_json_known(json.loads(body.decode("utf-8"))), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except Exception:
            return body
    if not text_type(c):
        return body
    try:
        t = body.decode("utf-8")
    except UnicodeDecodeError:
        return body
    if "text/html" in c or "application/xhtml+xml" in c or "image/svg+xml" in c:
        t = EMPTY_ROOT_ATTR_RE.sub(lambda m: m.group("lead") + PREFIX + "/" + m.group("end"), t)
        t = ROOT_RE.sub(lambda m: m.group("lead") + root_url(m.group("url")), t)
        t = SPECIAL_RE.sub(lambda m: m.group("lead") + root_url(m.group("url")), t)
        t = SRCSET_ATTR_RE.sub(lambda m: m.group("prefix") + _rewrite_srcset(m.group("value")) + m.group("suffix"), t)
        t = re.sub(r'''(\b(?:src|href)\s*=\s*["'])(?:\./)?(assets/|favicon\.(?:png|ico))''', lambda m: m.group(1) + PREFIX + "/" + m.group(2), t, flags=re.I)
        t = re.sub(r'''(["'`(=:\s])(?:\.?\./)+sw\.js(?=["'`),\s<]|$)''', lambda m: m.group(1) + PREFIX + "/sw.js", t)
        if "text/html" in c or "application/xhtml+xml" in c:
            t = inject(t)
        return t.encode("utf-8")
    if "text/css" in c:
        t = ROOT_RE.sub(lambda m: m.group("lead") + root_url(m.group("url")), t)
        t = SPECIAL_RE.sub(lambda m: m.group("lead") + root_url(m.group("url")), t)
        return t.encode("utf-8")
    return body


def rewrite_location(v, current="/"):
    if not v:
        return v
    v = v.strip()
    if v.lower().startswith("about:blank"):
        return v
    p = urllib.parse.urlsplit(v)
    try:
        local = p.scheme in ("http", "https") and p.hostname in {HOST, "localhost"} and (p.port in {None, PORT})
    except ValueError:
        local = False
    if local:
        return PREFIX + urllib.parse.urlunsplit(("", "", p.path or "/", p.query, p.fragment))
    if p.scheme or v.startswith("//"):
        return v
    joined = urllib.parse.urljoin("/" + current.lstrip("/"), v)
    if joined.startswith("/about:blank"):
        return v
    return root_url(joined)


def _rewrite_link_header(value: str, current: str):
    def repl(match):
        raw = match.group(1)
        return "<" + rewrite_location(raw, current) + ">"
    return re.sub(r"<([^<>]+)>", repl, value)


def out_headers(h, current, rid, rewrite=True, drop_content_length=False):
    connection_tokens = {token.strip().lower() for token in h.get("connection", "").split(",") if token.strip()}
    skip = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection", "te", "trailer", "transfer-encoding", "upgrade", "set-cookie", "x-request-id"}
    skip |= connection_tokens
    if rewrite or drop_content_length:
        skip.add("content-length")
    if rewrite:
        skip |= {"content-encoding", "etag", "content-md5"}
    out = []
    for k, v in h.multi_items():
        lo = k.lower()
        if lo in skip:
            continue
        if lo == "location":
            v = rewrite_location(v, current)
        if lo == "refresh":
            v = re.sub(r"(?i)(^\s*\d+\s*;\s*url\s*=\s*)(.+)$", lambda m: m.group(1) + rewrite_location(m.group(2), current), v)
        if lo == "content-location" and rewrite:
            v = rewrite_location(v, current)
        if lo == "link" and rewrite:
            v = _rewrite_link_header(v, current)
        if lo == "content-security-policy" and rewrite:
            directives = []
            for d in v.split(";"):
                d = d.strip()
                if not d:
                    continue
                parts = d.split()
                name = parts[0].lower()
                if name in {"script-src", "connect-src", "worker-src"} and "'self'" not in {x.lower() for x in parts[1:]}:
                    parts.append("'self'")
                directives.append(" ".join(parts))
            names = {d.split()[0].lower() for d in directives if d.split()}
            if "default-src" in names:
                for name in ("script-src", "connect-src", "worker-src"):
                    if name not in names:
                        for i, d in enumerate(directives):
                            parts = d.split()
                            if parts and parts[0].lower() == "default-src" and "'self'" not in {x.lower() for x in parts[1:]}:
                                parts.append("'self'")
                                directives[i] = " ".join(parts)
                                break
            else:
                if "script-src" not in names:
                    directives.append("script-src 'self'")
                if "connect-src" not in names:
                    directives.append("connect-src 'self'")
                if "worker-src" not in names:
                    directives.append("worker-src 'self'")
            v = "; ".join(directives)
        out.append((k, v))
    for cookie in h.get_list("set-cookie"):
        parts = []
        has = False
        for item in cookie.split(";"):
            x = item.strip()
            if x.lower().startswith("domain="):
                continue
            if x.lower().startswith("path="):
                raw_path = x.split("=", 1)[1].strip() or "/"
                if raw_path == PREFIX or raw_path.startswith(PREFIX + "/"):
                    mapped_path = raw_path
                elif raw_path.startswith("/"):
                    mapped_path = PREFIX + raw_path
                else:
                    mapped_path = PREFIX + "/" + raw_path
                parts.append(f"Path={mapped_path}")
                has = True
            else:
                parts.append(x)
        if not has:
            parts.append(f"Path={PREFIX}/")
        out.append(("set-cookie", "; ".join(parts)))
    out.append(("x-request-id", rid))
    return out

async def proxy_http(request, path):
    rid = uuid.uuid4().hex
    if path == "__shim.js":
        return Response(SHIM_JS.encode(), media_type="application/javascript", headers={"Cache-Control": "no-store", "X-Interstellar-Isolated": "1", "X-Request-ID": rid})
    if path == "__about-blank.js":
        return Response(b"window.__SCHOOLHUB_INTERSTELLAR_ABOUT_BLANK__=true;", media_type="application/javascript", headers={"Cache-Control": "no-store", "X-Request-ID": rid})
    if is_internal_path(path):
        asset = asset_response(path)
        if asset is not None:
            return asset
    accept = request.headers.get("accept", "").lower()
    clean_p = path.strip("/").lower()
    is_bare = clean_p == "ca" or clean_p.startswith("ca/")
    is_subresource = is_bare or clean_p.startswith(("assets/", "bm/", "ep/", "e/")) or clean_p.endswith((".js", ".mjs", ".wasm", ".css", ".json", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff", ".woff2"))
    if (not native_available() or phase != "ready") and not is_subresource:
        kick()
        return loading()
    await ensure_native()
    if client is None:
        return Response(b"Interstellar client unavailable", status_code=503, headers={"Cache-Control": "no-store", "X-Request-ID": rid})
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_BODY:
        return Response(b"Request body too large", status_code=413, headers={"Cache-Control": "no-store", "X-Request-ID": rid})
    rewrite_expected = (not is_bare) and request.method != "HEAD" and ("text/html" in accept or "application/xhtml+xml" in accept or "text/css" in accept or "application/json" in accept or clean_p in {"", "a", "b", "c", "d", "play.html"} or clean_p.endswith((".html", ".htm", ".xhtml", ".svg", ".css", ".json")))
    sent = 0
    async def request_body():
        nonlocal sent
        async for chunk in request.stream():
            if not chunk:
                continue
            sent += len(chunk)
            if sent > MAX_BODY:
                raise ValueError("request body too large")
            yield chunk
    try:
        upstream_ctx = client.stream(request.method, target(path, request.url.query), headers=req_headers(request, rewrite_expected), content=request_body())
        upstream = await upstream_ctx.__aenter__()
    except ClientDisconnect:
        log(f"HTTP CLIENT DISCONNECT before upstream headers request_id={rid}")
        return Response(status_code=499, headers={"X-Request-ID": rid})
    except ValueError:
        return Response(b"Request body too large", status_code=413, headers={"Cache-Control": "no-store", "X-Request-ID": rid})
    except httpx.TimeoutException:
        return HTMLResponse("<h1>Interstellar upstream timeout</h1><p>Retry shortly.</p>", status_code=504, headers={"Cache-Control": "no-store", "X-Request-ID": rid})
    except httpx.HTTPError:
        return HTMLResponse("<h1>Interstellar upstream error</h1><p>Retry shortly.</p>", status_code=502, headers={"Cache-Control": "no-store", "X-Request-ID": rid})
    ct = upstream.headers.get("content-type", "")
    cl = upstream.headers.get("content-length")
    range_request = "range" in request.headers or upstream.status_code in {206, 416}
    content_encoding = (upstream.headers.get("content-encoding") or "").strip().lower()
    can_rewrite = (not is_bare) and ((rewrite_expected or "text/html" in ct.lower()) and text_type(ct) and upstream.status_code not in {204, 304} and request.method != "HEAD" and not range_request and content_encoding in {"", "identity"})
    if can_rewrite and cl and cl.isdigit() and int(cl) > MAX_REWRITE_BODY:
        can_rewrite = False
    response_headers = out_headers(upstream.headers, path, rid, rewrite=can_rewrite)
    if sum(len(k) + len(v) + 4 for k, v in response_headers) > 256 * 1024:
        await upstream_ctx.__aexit__(None, None, None)
        return HTMLResponse("<h1>Interstellar upstream headers too large</h1>", status_code=502, headers={"Cache-Control": "no-store", "X-Request-ID": rid})
    status_code = upstream.status_code
    upstream_closed = False
    async def close_upstream():
        nonlocal upstream_closed
        if not upstream_closed:
            upstream_closed = True
            await upstream_ctx.__aexit__(None, None, None)
    try:
        if request.method == "HEAD":
            await close_upstream()
            r = Response(status_code=status_code)
            for k, v in response_headers:
                r.headers.append(k, v)
        elif can_rewrite:
            iterator = upstream.aiter_raw(chunk_size=64 * 1024)
            buffered = bytearray()
            overflow = None
            async for chunk in iterator:
                if not chunk:
                    continue
                buffered.extend(chunk)
                if len(buffered) > MAX_REWRITE_BODY:
                    overflow = bytes(buffered)
                    break
            if overflow is None:
                data = rewrite_body(bytes(buffered), ct)
                await close_upstream()
                response_headers = out_headers(upstream.headers, path, rid, rewrite=True)
                r = Response(data, status_code=status_code)
                for k, v in response_headers:
                    r.headers.append(k, v)
            else:
                can_rewrite = False
                response_headers = out_headers(upstream.headers, path, rid, rewrite=False, drop_content_length=True)
                if sum(len(k) + len(v) + 4 for k, v in response_headers) > 256 * 1024:
                    await close_upstream()
                    return HTMLResponse("<h1>Interstellar upstream headers too large</h1>", status_code=502, headers={"Cache-Control": "no-store", "X-Request-ID": rid})
                async def stream_body_with_buffer():
                    try:
                        yield overflow
                        async for chunk in iterator:
                            if chunk:
                                yield chunk
                    except asyncio.CancelledError:
                        log(f"HTTP STREAM CANCELLED request_id={rid}")
                        raise
                    except Exception as exc:
                        log(f"HTTP STREAM ERROR request_id={rid}: {type(exc).__name__}: {exc}")
                        raise
                    finally:
                        await close_upstream()
                r = StreamingResponse(stream_body_with_buffer(), status_code=status_code, media_type=None)
                r.raw_headers = [(str(k).lower().encode("latin-1"), str(v).encode("latin-1")) for k, v in response_headers]
        else:
            async def stream_body():
                try:
                    async for chunk in upstream.aiter_raw():
                        if chunk:
                            yield chunk
                except asyncio.CancelledError:
                    log(f"HTTP STREAM CANCELLED request_id={rid}")
                    raise
                except Exception as exc:
                    log(f"HTTP STREAM ERROR request_id={rid}: {type(exc).__name__}: {exc}")
                    raise
                finally:
                    await close_upstream()
            r = StreamingResponse(stream_body(), status_code=status_code, media_type=None)
            r.raw_headers = [(str(k).lower().encode("latin-1"), str(v).encode("latin-1")) for k, v in response_headers]
    except asyncio.CancelledError:
        await close_upstream()
        raise
    except Exception:
        await close_upstream()
        raise
    if path.strip("/").lower() == "sw.js" or path.strip("/").lower().endswith("/sw.js"):
        r.headers.update({"Service-Worker-Allowed": PREFIX + "/", "Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"})
    r.headers.update({"X-Interstellar-Native": VERSION, "X-Interstellar-Isolated": "1", "X-Interstellar-Scramjet": PREFIX + "/scramjet/", "X-Interstellar-Controller": PREFIX + "/controller/", "X-Interstellar-Libcurl": PREFIX + "/libcurl/", "X-Interstellar-Epoxy": PREFIX + "/epoxy/", "X-Interstellar-Wisp": PREFIX + "/wisp/"})
    log(f"HTTP {'REWRITE' if can_rewrite else 'STREAM'} {request.method} {request.url.path} -> {status_code} request_id={rid}")
    return r


@router.api_route("/b", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def b_root(r: Request):
    return await proxy_http(r, "b")


@router.api_route("/b/{rest:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def b_path(r: Request, rest: str):
    return await proxy_http(r, "b/" + rest)


@router.api_route("/", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def root(r: Request):
    return await proxy_http(r, "")


@router.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def all_http(r: Request, path: str):
    return await proxy_http(r, path)


async def relay(ws, up):
    async def c2u():
        while True:
            m = await ws.receive()
            if m.get("type") == "websocket.disconnect":
                return
            if m.get("text") is not None:
                payload = m["text"]
                payload_size = len(payload.encode("utf-8"))
                if payload_size > MAX_WS_MESSAGE:
                    log(f"WS CLIENT MESSAGE TOO LARGE bytes={payload_size} limit={MAX_WS_MESSAGE}")
                    try:
                        await ws.close(code=1009, reason="WebSocket message too large")
                    except Exception:
                        pass
                    return
                await up.send(payload)
            elif m.get("bytes") is not None:
                payload = m["bytes"]
                if len(payload) > MAX_WS_MESSAGE:
                    log(f"WS CLIENT MESSAGE TOO LARGE bytes={len(payload)} limit={MAX_WS_MESSAGE}")
                    try:
                        await ws.close(code=1009, reason="WebSocket message too large")
                    except Exception:
                        pass
                    return
                await up.send(payload)
    async def u2c():
        async for m in up:
            if isinstance(m, bytes):
                await ws.send_bytes(m)
            else:
                await ws.send_text(m)
    tasks = {asyncio.create_task(c2u()), asyncio.create_task(u2c())}
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    await asyncio.gather(*pending, *done, return_exceptions=True)


async def ws_proxy(ws, native_path):
    try:
        import websockets
    except ImportError:
        log("WS ERROR websockets package unavailable")
        await ws.close(code=1011, reason="Interstellar WebSocket support unavailable")
        return
    try:
        await ensure_native()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log(f"WS START ERROR {native_path}: {type(exc).__name__}: {exc}")
        try:
            await ws.close(code=1013, reason="Interstellar runtime unavailable")
        except Exception:
            pass
        return
    if phase != "ready":
        await ws.close(code=1013, reason="Interstellar runtime is still starting")
        return
    query = ws.scope.get("query_string", b"").decode("latin-1")
    dest = target("/" + native_path.lstrip("/"), query).replace("http://", "ws://", 1)
    protocols = [x.strip() for x in ws.headers.get("sec-websocket-protocol", "").split(",") if x.strip()]
    skip = {"host", "connection", "upgrade", "sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions", "sec-websocket-protocol"}
    hdr = [(k, v) for k, v in ws.headers.items() if k.lower() not in skip]
    try:
        try:
            ctx = websockets.connect(dest, additional_headers=hdr, subprotocols=protocols or None, max_size=MAX_WS_MESSAGE, ping_interval=20, ping_timeout=20, close_timeout=5)
        except TypeError:
            ctx = websockets.connect(dest, extra_headers=hdr, subprotocols=protocols or None, max_size=MAX_WS_MESSAGE, ping_interval=20, ping_timeout=20, close_timeout=5)
        async with ctx as up:
            negotiated = up.subprotocol
            if protocols and negotiated not in protocols:
                log(f"WS PROTOCOL MISMATCH {native_path} requested={protocols!r} negotiated={negotiated!r}")
                await ws.close(code=1002, reason="WebSocket subprotocol negotiation failed")
                return
            await ws.accept(subprotocol=negotiated)
            log(f"WS CONNECTED {native_path} negotiated={negotiated!r}")
            try:
                await relay(ws, up)
            except Exception as exc:
                if "message too big" in str(exc).lower() or "max size" in str(exc).lower():
                    log(f"WS MESSAGE TOO LARGE {native_path} limit={MAX_WS_MESSAGE} request_id=unknown")
                    try:
                        await ws.close(code=1009, reason="WebSocket message too large")
                    except Exception:
                        pass
                    return
                raise
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log(f"WS ERROR {native_path}: {type(exc).__name__}: {exc}")
        try:
            await ws.close(code=1011, reason="Interstellar WebSocket upstream failed")
        except Exception:
            pass
    finally:
        try:
            await ws.close()
        except Exception:
            pass


@router.websocket("/wisp")
@router.websocket("/wisp/")
async def interstellar_wisp(ws: WebSocket):
    await ws_proxy(ws, "wisp/")


@router.websocket("/{path:path}")
async def websocket_all(ws: WebSocket, path: str):
    await ws_proxy(ws, path)


def _public_asset_path(path: Path | None, root: Path | None = None):
    if not path:
        return None
    try:
        base = root.resolve() if root else SOURCE.resolve()
        rel = path.resolve().relative_to(base)
        text = "/".join(rel.parts)
        return text[:MAX_STATUS_PATH]
    except (OSError, ValueError):
        return None


@router.get("/__status")
async def status():
    roots = package_roots()
    resolved = {
        "scramjet_js": _find_named(roots["scramjet"], "scramjet_js", ("dist/scramjet.js", "scramjet.js")),
        "scramjet_wasm": _find_named(roots["scramjet"], "scramjet_wasm", ("dist/scramjet.wasm", "scramjet.wasm")),
        "controller_api": _find_named(roots["controller"], "controller_api", ("dist/controller.api.js", "controller.api.js")),
        "controller_inject": _find_named(roots["controller"], "controller_inject", ("dist/controller.inject.js", "controller.inject.js")),
        "controller_sw": _find_named(roots["controller"], "controller_sw", ("dist/controller.sw.js", "controller.sw.js")),
        "utils": _find_named(roots["utils"], "utils", ("dist/scramjet-utils.js", "scramjet-utils.js")),
        "libcurl": _find_named(roots["libcurl"], "libcurl", ("index.mjs", "dist/index.mjs", "index.js", "dist/index.js")),
        "epoxy": _find_named(roots["epoxy"], "epoxy", ("index.mjs", "dist/index.mjs", "index.js", "dist/index.js")),
    }
    assets = {k: bool(v) for k, v in resolved.items()}
    assets["bare_transport"] = roots["bare"].is_dir()
    resolved_public = {k: _public_asset_path(v, roots.get(k)) for k, v in resolved.items()}
    safe_error = _redact_log_text(str(error))[:2000] if error else None
    safe_detail = _redact_log_text(str(detail))[:2000]
    return {"service": SERVICE_NAME, "version": VERSION,"running": native_available(), "starting": bool(starting and not starting.done()), "pid": proc.pid if running() else None, "port": PORT, "phase": phase, "detail": safe_detail, "error": safe_error, "node_fallback": (NODE_ROOT / "bin/node").is_file(), "native_wisp": "/wisp/", "wisp": PREFIX + "/wisp/", "scramjet": PREFIX + "/scramjet/", "scramjet_legacy": PREFIX + "/scram/", "controller": PREFIX + "/controller/", "utils": PREFIX + "/utils/", "libcurl": PREFIX + "/libcurl/", "epoxy": PREFIX + "/epoxy/", "bare": PREFIX + "/bare/", "b": PREFIX + "/b", "about_blank": True, "shim": PREFIX + "/__shim.js", "assets": assets, "resolved_assets": resolved_public, "package_roots": {k: (PACKAGE_SPECS[k] if v.is_dir() else None) for k, v in roots.items()}}


@router.get("/__logs")
async def logs():
    rows = []
    for n, pfx, c in (("runtime.log", "", 110), ("interstellar-node.log", "[node] ", 110), ("npm-install.log", "[npm] ", 70)):
        rows += [pfx + x for x in tail_lines(LOGS / n, c)]
    return {"lines": [_redact_log_text(x) for x in rows[-260:]]}


@router.post("/__retry")
async def retry():
    global error, last_retry_at
    if stopping:
        return {"phase": "stopped", "retry_after": None}
    if running():
        return {"phase": phase, "running": True}
    now = time.monotonic()
    if now - last_retry_at < 5.0:
        return {"phase": phase, "retry_after": round(5.0 - (now - last_retry_at), 2)}
    retry_file = ROOT / ".last-retry"
    try:
        with retry_file.open("a+") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            lf.seek(0)
            raw = lf.read().strip()
            prior = float(raw) if raw else 0.0
            if time.time() - prior < 5.0:
                return {"phase": phase, "retry_after": round(5.0 - (time.time() - prior), 2)}
            lf.seek(0)
            lf.truncate()
            lf.write(str(time.time()))
            lf.flush()
            os.fsync(lf.fileno())
            last_retry_at = now
    except (OSError, ValueError):
        return {"phase": phase, "retry_after": 5.0}
    error = None
    asset_cache.clear()
    set_phase("starting", "retry requested")
    kick()
    return {"phase": phase}


@router.get("/__loading")
async def loading_status():
    if not running() and not error:
        kick()
    rows = []
    for n in ("runtime.log", "npm-install.log", "interstellar-node.log"):
        prefix = "[npm] " if n == "npm-install.log" else "[node] " if n == "interstellar-node.log" else ""
        rows += [prefix + x for x in tail_lines(LOGS / n, 42)]
    ready = running() and phase == "ready"
    return {"phase": "ready" if ready else "error" if error else phase, "running": ready, "starting": bool(starting and not starting.done()), "error": _redact_log_text(str(error))[:2000] if error else None, "detail": _redact_log_text(str(detail))[:2000], "lines": [_redact_log_text(x) for x in rows[-120:]]}


@router.on_event("shutdown")
async def shutdown():
    global client, proc, starting, native_external_ready, stopping
    stopping = True
    if starting and not starting.done():
        starting.cancel()
        await asyncio.gather(starting, return_exceptions=True)
    starting = None
    if client:
        try:
            await client.aclose()
        except Exception:
            pass
        client = None
    def _stop_runtime_sync():
        global proc, native_external_ready
        ROOT.mkdir(parents=True, exist_ok=True)
        start_lock = ROOT / ".runtime-start.lock"
        with start_lock.open("a+") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                local_proc = proc
                proc = None
                native_external_ready = False
                terminate(local_proc)
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
    await asyncio.to_thread(_stop_runtime_sync)
    set_phase("stopped", "application shutdown")


SHIM_JS = r'''(()=>{"use strict";const P="/interstellar",sp=/^(about:|data:|blob:|javascript:|mailto:|tel:)/i;const blank=u=>typeof u==="string"&&/^about:blank(?:$|[?#])/i.test(u.trim());const internal=s=>typeof s==="string"&&(/^\/interstellar(?:\/|[?#]|$)/i.test(s)||/^\/(?:openchat|activity|scramjet|scram|bare|baremod|wisp|controller|utils|libcurl|epoxy|bm|ep|ca|a|b|c|d|e|assets)(?:\/|[?#]|$)/i.test(s));const f=u=>{if(typeof u!=="string")return u;const s=u.trim();if(!s||blank(s)||sp.test(s)||s.startsWith("#"))return u;if(internal(s))return s.startsWith(P)?s:P+s;if(s.startsWith("//")){try{const x=new URL(location.protocol+s);if(x.origin===location.origin)return f(x.pathname+(x.search||"")+(x.hash||""))}catch(e){}return u}return s.startsWith("/")?P+s:u};const fu=u=>{try{if(u instanceof URL){if(sp.test(u.href)||u.protocol==="about:")return u;if(u.origin===location.origin){if(u.pathname.startsWith(P+"/"))return u;if(/^\/(?:scramjet|scram|bare|baremod|wisp|controller|utils|libcurl|epoxy|bm|ep|ca|a|b|c|d|e|assets)(?:\/|$)/i.test(u.pathname)){u.pathname=P+u.pathname;return u}if(u.pathname.startsWith("/")){u.pathname=P+u.pathname;return u}}return u}}catch(e){}return f(u)};const fa=u=>{try{if(typeof Request!=="undefined"&&u instanceof Request)return new Request(fu(u.url),u)}catch(e){}return fu(u)};const safeBlank=u=>blank(u)||u==="";try{if(typeof indexedDB!=="undefined"&&indexedDB.open){const _open=indexedDB.open.bind(indexedDB);indexedDB.open=function(name,ver){const isSj=name==="$scramjet";const req=_open(name,isSj?1:ver);if(isSj){req.addEventListener("upgradeneeded",()=>{try{const db=req.result;for(const s of["config","cookies","redirectTrackers","referrerPolicies","publicSuffixList"]){if(!db.objectStoreNames.contains(s))db.createObjectStore(s)}try{const tx=req.transaction;if(tx){tx.objectStore("config").put({prefix:"/interstellar/a/sj/",globals:{wrapfn:"$scramjet$wrap",wrappropertybase:"$scramjet__",wrappropertyfn:"$scramjet$prop",cleanrestfn:"$scramjet$clean",importfn:"$scramjet$import",rewritefn:"$scramjet$rewrite",metafn:"$scramjet$meta",setrealmfn:"$scramjet$setrealm",pushsourcemapfn:"$scramjet$pushsourcemap",trysetfn:"$scramjet$tryset",templocid:"$scramjet$temploc",tempunusedid:"$scramjet$tempunused"},files:{wasm:"/interstellar/assets/languagearts/sj.wasm.wasm",all:"/interstellar/assets/languagearts/sj.all.js",sync:"/interstellar/assets/languagearts/sj.sync.js"},flags:{serviceworkers:false,syncxhr:false,strictRewrites:true,rewriterLogs:false,captureErrors:true,cleanErrors:true,scramitize:false,sourcemaps:true,destructureRewrites:false,interceptDownloads:false,allowInvalidJs:true,allowFailedIntercepts:true},siteFlags:{},codec:{encode:"e=>e?encodeURIComponent(e):e",decode:"e=>e?decodeURIComponent(e):e"}},"config")}}catch(e){}}catch(e){}})}return req}}}catch(e){}try{const ps=history.pushState.bind(history),rs=history.replaceState.bind(history);history.pushState=(s,t,u)=>ps(s,t,u==null||safeBlank(u)?u:fu(u));history.replaceState=(s,t,u)=>rs(s,t,u==null||safeBlank(u)?u:fu(u));const ow=window.open.bind(window);window.open=(u,...a)=>safeBlank(u)?ow(u,...a):ow(fu(u),...a);const of=window.fetch.bind(window);window.fetch=(u,...a)=>of(fa(u),...a);const xo=XMLHttpRequest.prototype.open;XMLHttpRequest.prototype.open=function(m,u,...a){return xo.call(this,m,safeBlank(u)?u:fu(u),...a)};const NWS=window.WebSocket;window.WebSocket=function(u,...a){try{if(typeof u==="string"&&!sp.test(u)){const x=new URL(u,location.href);if(x.origin===location.origin&&!x.pathname.startsWith(P+"/")){if(/^\/(?:wisp)(?:\/|$)/i.test(x.pathname))x.pathname=P+x.pathname;u=x.toString()}}else if(u instanceof URL&&u.origin===location.origin&&!u.pathname.startsWith(P+"/")&&/^\/(?:wisp)(?:\/|$)/i.test(u.pathname)){u.pathname=P+u.pathname}}catch(e){}return new NWS(u,...a)};window.WebSocket.prototype=NWS.prototype;Object.defineProperties(window.WebSocket,{CONNECTING:{value:NWS.CONNECTING},OPEN:{value:NWS.OPEN},CLOSING:{value:NWS.CLOSING},CLOSED:{value:NWS.CLOSED}});const NR=window.Request;if(NR){window.Request=function(u,...a){return new NR(safeBlank(u)?u:fu(u),...a)};window.Request.prototype=NR.prototype}if(window.EventSource){const N=window.EventSource;window.EventSource=function(u,...a){return new N(safeBlank(u)?u:fu(u),...a)};window.EventSource.prototype=N.prototype;Object.defineProperties(window.EventSource,{CONNECTING:{value:N.CONNECTING},OPEN:{value:N.OPEN},CLOSED:{value:N.CLOSED}})}if(window.Worker){const N=window.Worker;window.Worker=function(u,...a){return new N(safeBlank(u)?u:fu(u),...a)};window.Worker.prototype=N.prototype}if(window.SharedWorker){const N=window.SharedWorker;window.SharedWorker=function(u,...a){return new N(safeBlank(u)?u:fu(u),...a)};window.SharedWorker.prototype=N.prototype}else if(window.Worker){const pool=new Map();window.SharedWorker=function(u,o){const k=String(safeBlank(u)?u:fu(u));let w=pool.get(k);if(!w){w=new window.Worker(k,typeof o==="object"?o:undefined);pool.set(k,w)}const mc=new MessageChannel();w.postMessage({__sh_connect:true,port:mc.port2},[mc.port2]);mc.port1.start();this.port=mc.port1}}if(typeof HTMLIFrameElement!=="undefined"){const d=Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype,"src");if(d&&d.set){Object.defineProperty(HTMLIFrameElement.prototype,"src",{configurable:true,enumerable:d.enumerable,get:d.get,set:function(v){if(typeof v==="string"){const s=v.trim();if(s==="/"||s==="./")v=P+"/";else if(!safeBlank(s))v=fu(s)}return d.set.call(this,v)}})}}window.__SCHOOLHUB_INTERSTELLAR__={prefix:P,scramjet:P+"/scramjet/",scramjetLegacy:P+"/scram/",controller:P+"/controller/",utils:P+"/utils/",libcurl:P+"/libcurl/",bare:P+"/bare/",wisp:P+"/wisp/",nativeWisp:"/wisp/",aboutBlank:true,version:"5.3.0"}}catch(e){console.debug("Interstellar isolation shim",e)}})();'''

LOADING_HTML = '''<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Google</title><link rel="icon" type="image/x-icon" href="/favicon.ico"><style>body{font:14px system-ui;margin:0;min-height:100vh;display:grid;place-items:center;background:#f7f7f5}.x{width:min(800px,92%);padding:22px;background:#fff;border:1px solid #ddd;border-radius:12px}.d{color:#666;font-size:12px;word-break:break-word;margin-top:6px}.p{height:7px;background:#eee;border-radius:8px;overflow:hidden;margin:16px 0}.bar{height:100%;width:10%;background:#333;transition:width .3s}pre{max-height:46vh;overflow:auto;background:#f5f5f5;padding:12px;font:11px ui-monospace,monospace;white-space:pre-wrap}</style><main class=x><b id=p>Starting Interstellar…</b><div class=p><div id=b class=bar></div></div><div id=d class=d></div><pre id=l>Connecting…</pre></main><script>const p=document.getElementById('p'),b=document.getElementById('b'),d=document.getElementById('d'),l=document.getElementById('l'),names={downloading:'Downloading official Interstellar source…',extracting:'Extracting official Interstellar source…','downloading-node':'Downloading Node.js runtime…','extracting-node':'Preparing Node.js runtime…',installing:'Installing official dependencies…',starting:'Starting native Interstellar runtime…','checking-runtime':'Waiting for native HTTP server…',ready:'Opening official Interstellar…',error:'Interstellar startup failed'},pct={downloading:15,extracting:28,'downloading-node':45,'extracting-node':57,installing:73,starting:85,'checking-runtime':95,ready:100,error:100};async function f(){try{const j=await(await fetch('./__loading?'+Date.now(),{cache:'no-store'})).json();p.textContent=j.error?names.error:(names[j.phase]||'Preparing Interstellar…');d.textContent=j.error||j.detail||'';l.textContent=(j.lines||[]).join('\\n');b.style.width=(pct[j.phase]||10)+'%';if(j.running)location.replace(location.pathname+location.search+location.hash);else setTimeout(f,700)}catch(e){l.textContent=String(e);setTimeout(f,1800)}}f()</script>'''


def loading():
    return HTMLResponse(LOADING_HTML, headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"})
