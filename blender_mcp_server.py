#!/usr/bin/env -S uv run --script
# Code created by Daniel Galleguillos: https://github.com/dnlgalleguillos
# Copyright (C) 2026 Daniel Galleguillos Cruz
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify it under the terms of the
# GNU General Public License as published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version. It is distributed WITHOUT ANY WARRANTY; see the
# GNU General Public License (file LICENSE) for details.
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.10,<2", "httpx"]
# ///
"""
Blender MCP server 2.0, designed for Claude Code and Daniel Galleguillos Cruz.

Talks to the Blender MCP Bridge add-on (blender_mcp.py 2.x, in this repository) over a local socket and exposes
Blender as MCP tools. The add-on only runs code and takes screenshots; everything else, including all network
access and API keys, lives here. What this server provides:

  * get_scene_info: compact text facts about the scene, with mesh/rig "health" checks and ground contact
  * get_node_tree: read a shader, Geometry Nodes, world or compositor node tree (nodes, settings, links)
  * look: the live viewport, the scene camera, auto-framed multi-angle views and animation strips as images,
    so the model can *see* its work (Workbench renders through temporary cameras)
  * search_assets / import_asset: Poly Haven, Sketchfab and Poly Pizza behind one pair of tools, with
    real-world scaling of imported models
  * AI text/image-to-3D: Hyper3D Rodin (hyper3d.ai, fal.ai) and Hunyuan3D (Tencent cloud, local server)
  * run_blender_script: execute a .py file from your repo with real file names in tracebacks
  * render, save and export helpers that write into your project
  * opt-in safe mode: static check of the Python the model writes (guards against prompt injection)
  * a persistent connection with stale-socket detection, deadlines and no unsafe retries
  * optional shared-token auth and a read-only mode
  * no telemetry, no analytics, no promotional text in tool replies

Configuration (environment variables):
  BLENDER_HOST            default 127.0.0.1
  BLENDER_PORT            default 9876
  BLENDER_MCP_TOKEN       shared secret, must match "Auth Token" in the add-on preferences
  BLENDER_TIMEOUT         default per-command timeout in seconds (default 30)
  BLENDER_MCP_READONLY    1 = do not register tools that modify the scene or write files
  BLENDER_MCP_SAFE_MODE   1 = validate model-written Python before it reaches Blender (see "safe mode")
  POLYPIZZA_API_KEY       free key from poly.pizza/settings/api, to search and import Poly Pizza models
  SKETCHFAB_API_KEY       Sketchfab API token (search and import; Poly Haven needs no key)
  HYPER3D_API_KEY         Hyper3D Rodin key; HYPER3D_PLATFORM=fal to use fal.ai instead of hyper3d.ai
  HUNYUAN3D_SECRET_ID / HUNYUAN3D_SECRET_KEY   Tencent Cloud credentials for the official Hunyuan3D API
  HUNYUAN3D_API_URL       your own Hunyuan3D server, e.g. http://localhost:8081 (HUNYUAN3D_MODE=official|local to force)
  BLENDER_MCP_LOG         logging level (default INFO); logs go to stderr, never stdout

Diagnostics:  uv run blender_mcp_server.py --check
"""

import argparse
import ast
import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import re
import select
import shutil
import socket
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal, Optional, Union
from urllib.parse import quote, urlparse

import anyio
import httpx
from mcp.server.fastmcp import Context, FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

# --------------------------------------------------------------------------- config

HOST = os.environ.get("BLENDER_HOST", "127.0.0.1")
PORT = int(os.environ.get("BLENDER_PORT", "9876"))
TOKEN = os.environ.get("BLENDER_MCP_TOKEN", "")
TIMEOUT = float(os.environ.get("BLENDER_TIMEOUT", "30"))
READ_ONLY = os.environ.get("BLENDER_MCP_READONLY", "").lower() in ("1", "true", "yes", "on")
SAFE_MODE = os.environ.get("BLENDER_MCP_SAFE_MODE", "").lower() in ("1", "true", "yes", "on")
POLYPIZZA_API_KEY = os.environ.get("POLYPIZZA_API_KEY", "")
SKETCHFAB_API_KEY = os.environ.get("SKETCHFAB_API_KEY", "")
HYPER3D_API_KEY = os.environ.get("HYPER3D_API_KEY", "")
HYPER3D_PLATFORM = os.environ.get("HYPER3D_PLATFORM", "hyper3d").strip().lower()  # hyper3d | fal
HUNYUAN3D_SECRET_ID = os.environ.get("HUNYUAN3D_SECRET_ID", "")
HUNYUAN3D_SECRET_KEY = os.environ.get("HUNYUAN3D_SECRET_KEY", "")
HUNYUAN3D_API_URL = os.environ.get("HUNYUAN3D_API_URL", "")
HUNYUAN3D_MODE = os.environ.get("HUNYUAN3D_MODE", "").strip().lower()  # official | local | (auto)

MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_CHARS = 20_000
MAX_IMAGE_BYTES = 1_500_000

log = logging.getLogger("blender-mcp")


# --------------------------------------------------------------------------- connection


class BlenderError(Exception):
    """The command reached Blender (or may have) but failed."""


class BlenderUnavailable(BlenderError):
    """Could not reach Blender at all; nothing was executed."""


class BlenderConnection:
    """One persistent, thread-safe socket to the add-on.

    Retry policy: a command is only re-sent if it could not be *sent* (stale or dead socket).
    Once the bytes are out we never retry, because Blender may already be executing it and
    re-sending would run e.g. a "create object" script twice.
    """

    def __init__(self, host: str, port: int, token: str, timeout: float):
        self.host, self.port, self.token, self.timeout = host, port, token, timeout
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()

    # -- socket management
    def _connect(self) -> None:
        try:
            sock = socket.create_connection((self.host, self.port), timeout=5)
        except OSError as e:
            raise BlenderUnavailable(
                f"Cannot connect to Blender at {self.host}:{self.port} ({e}). In Blender: press N in the "
                "3D viewport, open the 'BlenderMCP' tab and click 'Connect to MCP server'."
            ) from None
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = sock
        log.info("Connected to Blender at %s:%s", self.host, self.port)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _drop_if_stale(self) -> None:
        """If the idle socket is readable it was closed by Blender (or holds a late reply): drop it."""
        if self._sock is None:
            return
        try:
            readable, _, _ = select.select([self._sock], [], [], 0)
            if readable:
                self.close()
        except (OSError, ValueError):
            self.close()

    # -- protocol
    def _read_response(self, deadline: float) -> dict:
        assert self._sock is not None
        import codecs

        decoder = json.JSONDecoder()
        utf8 = codecs.getincrementaldecoder("utf-8")()
        buf = ""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout()
            self._sock.settimeout(remaining)
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("Blender closed the connection")
            buf += utf8.decode(chunk)
            if len(buf) > MAX_RESPONSE_BYTES:
                raise BlenderError("Response from Blender is too large")
            try:
                obj, _ = decoder.raw_decode(buf.lstrip())
            except json.JSONDecodeError:
                continue  # incomplete, keep reading
            if not isinstance(obj, dict):
                raise BlenderError(f"Unexpected response from Blender: {str(obj)[:200]}")
            return obj

    def send(self, command: str, params: Optional[dict] = None, timeout: Optional[float] = None) -> dict:
        timeout = timeout or self.timeout
        payload: dict[str, Any] = {"type": command, "params": params or {}}
        if self.token:
            payload["token"] = self.token
        data = json.dumps(payload).encode("utf-8")

        with self._lock:
            self._drop_if_stale()
            for attempt in (1, 2):
                if self._sock is None:
                    self._connect()
                try:
                    self._sock.sendall(data)  # type: ignore[union-attr]
                    break
                except OSError:
                    self.close()
                    if attempt == 2:
                        raise BlenderUnavailable("Lost the connection to Blender while sending the command") from None

            try:
                return self._read_response(time.monotonic() + timeout)
            except socket.timeout:
                self.close()  # a late reply must not pollute the next call
                raise BlenderError(
                    f"Timed out after {timeout:g}s waiting for Blender. Blender runs commands on its UI thread, so "
                    "it may still be busy with this command; pass a larger timeout_s for long operations."
                ) from None
            except (OSError, ValueError, UnicodeDecodeError) as e:
                self.close()
                raise BlenderError(f"Connection to Blender failed while waiting for the reply: {e}") from None


_conn = BlenderConnection(HOST, PORT, TOKEN, TIMEOUT)


def _unwrap(resp: dict) -> Any:
    """Turn an add-on reply into a result, or raise ToolError."""
    if resp.get("status") == "error":
        raise ToolError(str(resp.get("message", "Unknown error")))
    return resp.get("result")


async def call(command: str, params: Optional[dict] = None, *, timeout: Optional[float] = None) -> Any:
    try:
        resp = await anyio.to_thread.run_sync(lambda: _conn.send(command, params, timeout))
    except BlenderError as e:
        raise ToolError(str(e)) from None
    return _unwrap(resp)


# --------------------------------------------------------------------------- helpers


def _clip(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n... [truncated, {len(text) - MAX_OUTPUT_CHARS} more characters]"


def _compact(data: Any) -> str:
    return json.dumps(data, separators=(",", ":"), default=str)


def _header(params: Optional[dict] = None) -> str:
    """Prelude for generated code: imports, plus parameters passed as safely-escaped JSON."""
    payload = json.dumps(json.dumps(params or {}))
    return f"import json, math, mathutils, bmesh\n_P = json.loads({payload})\n"


async def _run_py(code: str, timeout: Optional[float] = None) -> str:
    result = await call("execute_code", {"code": code}, timeout=timeout)
    return result.get("result", "") if isinstance(result, dict) else ""


def _last_json_line(output: str) -> str:
    """The JSON document a snippet printed. Blender operators (the glTF importer, for one) also log to stdout, which
    the add-on captures, so the document is the last line that looks like one."""
    for line in reversed(output.strip().splitlines()):
        if line[:1] in "{[":
            return line
    return output


async def _query(body: str, params: Optional[dict] = None, timeout: Optional[float] = None) -> Any:
    """Run an inspection snippet that prints a single JSON document."""
    out = await _run_py(_header(params) + body, timeout)
    try:
        data = json.loads(_last_json_line(out))
    except json.JSONDecodeError:
        raise ToolError(f"Unexpected output from Blender: {out[:500]!r}") from None
    if isinstance(data, dict) and "error" in data:
        similar = data.get("similar")
        raise ToolError(data["error"] + (f" Similar names: {similar}" if similar else ""))
    return data


def _path(p: str) -> Path:
    """Resolve a path. Relative paths resolve against this server's working directory
    (Claude Code starts MCP servers in the project directory)."""
    return Path(p).expanduser().resolve()


# --------------------------------------------------------------------------- network
#
# Everything this server sends over the internet goes through the helpers below, so it can be reviewed in one
# place and swapped for a mock transport in the tests. The add-on in Blender does no networking at all.

_client_factory = None  # the tests replace this with a factory that builds clients on an httpx.MockTransport

USER_AGENT = "blender-mcp-claude-code"
_MAX_DOWNLOAD_BYTES = 600 * 1024 * 1024
_MAX_EXTRACT_BYTES = 2 * 1024 * 1024 * 1024
_MAX_EXTRACT_FILES = 5000


def _new_client(timeout: float = 30, redirects: bool = True, headers: Optional[dict] = None) -> httpx.AsyncClient:
    kwargs = dict(headers={"User-Agent": USER_AGENT, **(headers or {})}, timeout=timeout, follow_redirects=redirects)
    return (_client_factory or httpx.AsyncClient)(**kwargs)


def _check_url(url: Any, *, allow_http: bool = False, allow_local: bool = False) -> httpx.URL:
    """Refuse URLs that would make this server talk to the user's own machine or network (the data in these
    URLs comes from third-party APIs). `allow_local` is for the one case where the user configured such a
    server on purpose: a local Hunyuan3D."""
    try:
        parsed = httpx.URL(str(url))
    except Exception:
        raise ToolError(f"Invalid URL: {str(url)[:120]}") from None
    if parsed.scheme not in (("https", "http") if allow_http else ("https",)):
        raise ToolError(f"Refusing to use a non-HTTPS URL ({parsed.scheme}://{parsed.host})")
    host = (parsed.host or "").lower()
    if not host:
        raise ToolError(f"Invalid URL: {str(url)[:120]}")
    if not allow_local:
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if host == "localhost" or host.endswith((".local", ".localhost", ".internal")) or (
                ip is not None and (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                                    or ip.is_multicast or ip.is_unspecified)):
            raise ToolError(f"Refusing to contact a local or private address ({host})")
    return parsed


async def _http_get(url: str, *, params: Optional[dict] = None, headers: Optional[dict] = None, timeout: float = 30,
                    redirects: bool = True, allow_http: bool = False, allow_local: bool = False) -> httpx.Response:
    parsed = _check_url(url, allow_http=allow_http, allow_local=allow_local)
    try:
        async with _new_client(timeout, redirects, headers) as client:
            return await client.get(url, params=params)
    except httpx.HTTPError as e:
        raise ToolError(f"Network error contacting {parsed.host}: {e}") from None


async def _http_post(url: str, *, json_body: Any = None, content: Optional[bytes] = None, files: Optional[list] = None,
                     headers: Optional[dict] = None, timeout: float = 60, allow_http: bool = False,
                     allow_local: bool = False) -> httpx.Response:
    parsed = _check_url(url, allow_http=allow_http, allow_local=allow_local)
    try:
        async with _new_client(timeout, True, headers) as client:
            return await client.post(url, json=json_body, content=content, files=files)
    except httpx.HTTPError as e:
        raise ToolError(f"Network error contacting {parsed.host}: {e}") from None


async def _download(url: str, dest: Path, *, headers: Optional[dict] = None, timeout: float = 300,
                    max_bytes: int = _MAX_DOWNLOAD_BYTES) -> int:
    """Stream a file to disk, refusing anything larger than max_bytes. Returns the size."""
    parsed = _check_url(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    try:
        async with _new_client(timeout, True, headers) as client:
            async with client.stream("GET", url) as response:
                if response.status_code != 200:
                    raise ToolError(f"Download failed: HTTP {response.status_code} from {parsed.host}")
                with dest.open("wb") as out:
                    async for chunk in response.aiter_bytes(65536):
                        total += len(chunk)
                        if total > max_bytes:
                            raise ToolError(f"A download from {parsed.host} is larger than {max_bytes // 2**20} MB; refusing it")
                        out.write(chunk)
    except httpx.HTTPError as e:
        dest.unlink(missing_ok=True)
        raise ToolError(f"Network error downloading from {parsed.host}: {e}") from None
    except ToolError:
        dest.unlink(missing_ok=True)
        raise
    return total


async def _download_all(items: list[tuple[str, Path]], concurrency: int = 4) -> None:
    """Download several files at once. Every download finishes (or fails) before this returns."""
    gate = asyncio.Semaphore(concurrency)

    async def one(url: str, dest: Path):
        async with gate:
            await _download(url, dest)

    results = await asyncio.gather(*(one(u, d) for u, d in items), return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result


def _safe_join(base: Path, relative: str) -> Path:
    """base/relative, refusing anything that would land outside base (absolute paths, '..', drive letters)."""
    rel = PurePosixPath(str(relative).replace("\\", "/"))
    if rel.is_absolute() or not rel.parts or ".." in rel.parts or rel.parts[0].endswith(":"):
        raise ToolError(f"Unsafe path in downloaded data: {str(relative)[:100]!r}")
    root = base.resolve()
    target = (root / Path(*rel.parts)).resolve()
    if root not in target.parents:
        raise ToolError(f"Unsafe path in downloaded data: {str(relative)[:100]!r}")
    return target


def _safe_extract(archive: Path, dest: Path) -> list[Path]:
    """Unpack a zip into dest, writing only regular files strictly inside it, with limits on size and count."""
    written: list[Path] = []
    total = 0
    try:
        with zipfile.ZipFile(archive) as z:
            infos = z.infolist()
            if len(infos) > _MAX_EXTRACT_FILES:
                raise ToolError(f"The archive has too many files ({len(infos)})")
            for info in infos:
                if info.is_dir():
                    continue
                total += info.file_size
                if total > _MAX_EXTRACT_BYTES:
                    raise ToolError("The archive is too large once unpacked; refusing it")
                target = _safe_join(dest, info.filename)
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, target.open("wb") as out:
                    shutil.copyfileobj(src, out)
                written.append(target)
    except zipfile.BadZipFile:
        raise ToolError("The downloaded file is not a valid zip archive") from None
    return written


def _new_tempdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="blender_mcp_dl_"))


# --------------------------------------------------------------------------- server

INSTRUCTIONS = """\
Controls a running Blender through the BlenderMCP add-on (the user must have clicked 'Connect' in Blender).

Working method:
1. Look before you edit: get_scene_info for the overview (add fields, or query / root, for detail) and
   get_object_details for what you will touch. Before changing a material or Geometry Nodes setup you did not
   make yourself, read it with get_node_tree.
2. Work in small steps and verify visually after each meaningful change with look: mode 'angles' with
   shading 'clay' to judge form, 'viewport' to see what the user sees, 'camera' for the framed shot. Check
   mesh quality with get_scene_info fields=['health'] (n-gons, holes, unweighted vertices). Use render_image
   only for final-quality checks (it blocks Blender).
3. For anything beyond a few lines, write the code to a .py file in the project and run it with
   run_blender_script instead of pasting it into execute_blender_code. Make scripts idempotent (remove or
   reuse objects by name before creating) so they can be re-run after edits.
4. Prefer the bpy data API (bpy.data.objects.new, bmesh, mesh.from_pydata) over bpy.ops: operators depend on
   UI context that is often missing when called from the add-on.
5. Conventions: metres, Z-up, name every object and material meaningfully, put generated objects in a
   dedicated collection, apply a smooth/flat shading choice deliberately.
6. Library and generated models arrive at arbitrary scale and position. Always give import_asset a
   target_size, read the reported dimensions, rest the object on the ground (get_scene_info
   fields=['ground']) and look at it. Credit the creator of CC-BY models.
7. Blender executes commands on its UI thread: long operations (renders, bakes, simulations, big imports)
   freeze it until done. Pass a generous timeout_s for them.
8. execute_blender_code and run_blender_script can do anything Python can. Never delete user data, quit
   Blender, or overwrite files you were not asked to touch. Call save_blend only when the user wants it.
   If safe mode is on (blender_status shows it), your code is checked first: stay with bpy, bmesh, mathutils.
9. File paths are resolved on the machine running Blender; relative paths resolve against the project.
10. rodin_generate and hunyuan3d_generate spend the user's API credits and can take minutes: confirm with the
    user before the first call. Generated meshes are raw: check scale and orientation and clean up before
    building on them.
"""

mcp = FastMCP("blender", instructions=INSTRUCTIONS)


def _tool(*, read_only: bool, destructive: bool = True, open_world: bool = False, readonly_mode_ok: bool = False):
    """Register a tool. In BLENDER_MCP_READONLY mode, tools that change things are not registered,
    except those flagged readonly_mode_ok (they only use temporary state that is restored afterwards)."""

    def deco(fn):
        if READ_ONLY and not read_only and not readonly_mode_ok:
            return fn
        annotations = ToolAnnotations(
            readOnlyHint=read_only,
            destructiveHint=None if read_only else destructive,
            idempotentHint=read_only or None,
            openWorldHint=open_world,
        )
        # structured_output=False: return plain content blocks (text/images) without a duplicated JSON schema
        return mcp.tool(annotations=annotations, structured_output=False)(fn)

    return deco


# --------------------------------------------------------------------------- safe mode
#
# Opt-in (BLENDER_MCP_SAFE_MODE=1) static check of the Python that the MODEL writes, applied before it
# reaches Blender through execute_blender_code / run_blender_script.
#
# Threat: prompt injection. Third-party text (asset names and descriptions from Poly Haven, Sketchfab,
# Poly Pizza, generators) enters the model's context and could steer it into writing hostile code that a
# user approves without reading. This is a guard on what the model can be talked into, NOT a sandbox:
# the add-on's socket still accepts raw execute_code from any local process, files can still be written
# through bpy export operators, and a static check can never be complete. The real boundary is the
# "Allow code execution" switch in the Blender panel.


class SafeModeViolation(ValueError):
    """The code uses something safe mode does not allow."""


SAFE_MAX_CHARS = 200_000

# Importable modules (root name). Deliberately small: no os/sys/io/pathlib/subprocess/socket/importlib,
# and none of the modules that offer string-based attribute access (operator, string, functools.partial
# is fine because getattr is checked by name) or re-export dangerous modules (typing, dataclasses, enum).
SAFE_MODULES = frozenset({
    "bpy", "bmesh", "mathutils", "bpy_extras", "math", "random", "json", "re", "itertools",
    "functools", "collections", "colorsys", "datetime", "statistics", "heapq", "bisect", "textwrap",
})

# Names that must not even be referenced.
SAFE_BAD_NAMES = frozenset({
    "eval", "exec", "compile", "__import__", "open", "input", "breakpoint", "help", "globals", "locals",
    "vars", "memoryview", "exit", "quit",
})

# getattr-style builtins: only allowed with a literal, harmless attribute name.
SAFE_ATTR_FUNCS = frozenset({"getattr", "setattr", "delattr", "hasattr"})

# Attribute names refused on any object. They either reach the interpreter, the filesystem, the network or
# the process (modules re-exported by allowed modules), or install persistent behaviour in Blender.
SAFE_BAD_ATTRS = frozenset({
    # process / filesystem / network / interpreter, reachable as attributes of allowed modules
    "sys", "os", "subprocess", "socket", "shutil", "importlib", "builtins", "ctypes", "inspect", "pickle",
    "marshal", "io", "pathlib", "tempfile", "urllib", "http", "requests", "threading", "multiprocessing",
    "signal", "platform", "codecs", "operator", "attrgetter", "methodcaller", "execfile",
    # frames and code objects
    "gi_frame", "gi_code", "cr_frame", "cr_code", "ag_frame", "f_globals", "f_locals", "f_back",
    "f_builtins", "tb_frame", "co_code", "func_globals",
    # persistence inside Blender
    "register_class", "unregister_class", "register_submodule_factory", "load_scripts", "handlers",
    "timers", "driver_namespace", "driver_add", "driver_remove", "drivers",
    # preferences (script directories, auto-run of scripts, add-on installation)
    "preferences", "user_preferences", "script_directories", "script_directory", "use_scripts_auto_execute",
})

# Dotted paths refused after resolving import aliases (prefix match on whole components).
SAFE_BAD_PATHS = (
    "bpy.app.handlers", "bpy.app.timers", "bpy.app.driver_namespace",
    "bpy.props", "bpy.data.libraries", "bpy.data.texts",
    "bpy.ops.script", "bpy.ops.text", "bpy.ops.preferences", "bpy.ops.extensions", "bpy.ops.console",
    "bpy.ops.wm.append", "bpy.ops.wm.link", "bpy.ops.wm.lib_relocate", "bpy.ops.wm.lib_reload",
    "bpy.ops.wm.url_open", "bpy.ops.wm.url_open_preset", "bpy.ops.wm.path_open",
    "bpy.ops.wm.quit_blender", "bpy.ops.wm.console_toggle", "bpy.ops.wm.app_template_install",
    "bpy.ops.wm.save_userpref", "bpy.ops.wm.read_userpref", "bpy.ops.wm.read_factory_settings",
    "bpy.ops.wm.open_mainfile", "bpy.ops.wm.revert_mainfile", "bpy.ops.wm.recover_last_session",
    "bpy.ops.wm.recover_auto_save", "bpy.ops.wm.save_mainfile", "bpy.ops.wm.save_as_mainfile",
)

_FORMAT_DUNDER = re.compile(r"\{[^{}]*__")


def _blocked_path(path: Optional[str]) -> Optional[str]:
    """The refused path that `path` equals or lives under, if any."""
    if path:
        for bad in SAFE_BAD_PATHS:
            if path == bad or path.startswith(bad + "."):
                return bad
    return None


def _chain(node: ast.AST) -> Optional[list[str]]:
    """['a', 'b', 'c'] for the pure attribute chain a.b.c, else None."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return parts[::-1]
    return None


def _count_bindings(tree: ast.AST) -> dict[str, int]:
    """How many times each name is bound anywhere in the code (assignments, loops, imports, defs, args...)."""
    counts: dict[str, int] = {}

    def bump(name: Optional[str]) -> None:
        if name:
            counts[name] = counts.get(name, 0) + 1

    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            bump(n.id)
        elif isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
            bump(n.name)
        elif isinstance(n, ast.arg):
            bump(n.arg)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                bump(a.asname or a.name.split(".")[0])
        elif isinstance(n, ast.ExceptHandler):
            bump(n.name)
        elif isinstance(n, (ast.MatchAs, ast.MatchStar)):
            bump(n.name)
        elif isinstance(n, ast.MatchMapping):
            bump(n.rest)
    return counts


def _build_aliases(tree: ast.AST) -> dict[str, str]:
    """Names that stand for a dotted path: imports and simple `x = bpy.ops` style assignments."""
    aliases: dict[str, str] = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                aliases[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
        elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
            for a in n.names:
                if a.name != "*":
                    aliases[a.asname or a.name] = f"{n.module}.{a.name}"
    changed = True
    while changed:  # follow `a = bpy; b = a.ops` chains until nothing new resolves
        changed = False
        for n in ast.walk(tree):
            if (isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
                    and (chain := _chain(n.value)) and chain[0] in aliases):
                resolved = ".".join([aliases[chain[0]], *chain[1:]])
                if aliases.get(n.targets[0].id) != resolved:
                    aliases[n.targets[0].id] = resolved
                    changed = True
    return aliases


class _SafeValidator(ast.NodeVisitor):
    def __init__(self, aliases: dict[str, str]):
        self.aliases = aliases
        self._attr_func_names: set[int] = set()

    def fail(self, node: ast.AST, reason: str) -> None:
        raise SafeModeViolation(f"line {getattr(node, 'lineno', '?')}: {reason}")

    def resolve(self, node: ast.AST) -> Optional[str]:
        chain = _chain(node)
        if not chain:
            return None
        return ".".join([self.aliases.get(chain[0], chain[0]), *chain[1:]])

    @staticmethod
    def _literal_attr_ok(name: str) -> bool:
        return not (name.startswith("_") or name in SAFE_BAD_ATTRS)

    # -- structure
    def visit_AsyncFunctionDef(self, node):
        self.fail(node, "async code is not allowed")

    visit_Await = visit_AsyncFor = visit_AsyncWith = visit_AsyncFunctionDef

    def visit_Import(self, node):
        for a in node.names:
            if a.name.split(".")[0] not in SAFE_MODULES:
                self.fail(node, f"import of '{a.name}' is not allowed (allowed: {', '.join(sorted(SAFE_MODULES))})")
            if bad := _blocked_path(a.name):
                self.fail(node, f"'{bad}' is not allowed")
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if node.level or not node.module or node.module.split(".")[0] not in SAFE_MODULES:
            self.fail(node, f"import from '{'.' * node.level}{node.module or ''}' is not allowed")
        for a in node.names:
            if bad := _blocked_path(f"{node.module}.{a.name}"):
                self.fail(node, f"'{bad}' is not allowed")
        self.generic_visit(node)

    # -- names and attributes
    def visit_Name(self, node):
        if node.id.startswith("__") and node.id.endswith("__") and node.id != "__name__":
            self.fail(node, f"access to '{node.id}' is not allowed")
        if node.id in SAFE_BAD_NAMES:
            self.fail(node, f"'{node.id}' is not allowed")
        if node.id in SAFE_ATTR_FUNCS and id(node) not in self._attr_func_names:
            self.fail(node, f"'{node.id}' may only be called with a literal attribute name")
        if bad := _blocked_path(self.aliases.get(node.id)):
            self.fail(node, f"'{bad}' is not allowed")

    def visit_Attribute(self, node):
        attr = node.attr
        if attr.startswith("_"):
            self.fail(node, f"access to private/dunder attribute '{attr}' is not allowed")
        if attr in SAFE_BAD_ATTRS:
            self.fail(node, f"attribute '{attr}' is not allowed")
        if bad := _blocked_path(self.resolve(node)):
            self.fail(node, f"'{bad}' is not allowed")
        self.generic_visit(node)

    def visit_Call(self, node):
        if isinstance(node.func, ast.Name) and node.func.id in SAFE_ATTR_FUNCS:
            args = node.args
            ok = (len(args) >= 2 and isinstance(args[1], ast.Constant) and isinstance(args[1].value, str)
                  and self._literal_attr_ok(args[1].value))
            if not ok:
                self.fail(node, f"{node.func.id}() needs a literal, public attribute name as its 2nd argument")
            self._attr_func_names.add(id(node.func))
        self.generic_visit(node)

    def visit_Constant(self, node):
        if isinstance(node.value, str) and _FORMAT_DUNDER.search(node.value):
            self.fail(node, "format strings that reach dunder attributes are not allowed")

    # -- assignments into Blender's type system (persistent properties on bpy.types.*)
    def _check_target(self, target):
        if isinstance(target, (ast.Tuple, ast.List)):
            for t in target.elts:
                self._check_target(t)
        elif isinstance(target, ast.Attribute):
            path = self.resolve(target)
            if path and (path == "bpy.types" or path.startswith("bpy.types.")):
                self.fail(target, "assigning to bpy.types is not allowed")

    def visit_Assign(self, node):
        for t in node.targets:
            self._check_target(t)
        self.generic_visit(node)

    def visit_AugAssign(self, node):
        self._check_target(node.target)
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        self._check_target(node.target)
        self.generic_visit(node)


def validate_code(code: str) -> None:
    """Raise SafeModeViolation unless `code` stays inside the safe-mode allowlist."""
    if len(code) > SAFE_MAX_CHARS:
        raise SafeModeViolation(f"code is larger than {SAFE_MAX_CHARS} characters")
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise SafeModeViolation(f"line {e.lineno}: syntax error: {e.msg}") from None
    except (RecursionError, MemoryError, ValueError):
        raise SafeModeViolation("code is too deeply nested or complex to check") from None

    aliases = _build_aliases(tree)
    bindings = _count_bindings(tree)
    for name in aliases:
        if bindings.get(name, 0) > 1:  # an alias that is re-bound could hide what it points to
            raise SafeModeViolation(f"the name '{name}' is bound more than once; use distinct names for modules and aliases")
    try:
        _SafeValidator(aliases).visit(tree)
    except RecursionError:
        raise SafeModeViolation("code is too deeply nested or complex to check") from None


_PRELUDE = "import math, mathutils, bmesh; "  # the stock add-on only preloads bpy


def _guard_code(code: str) -> None:
    if not SAFE_MODE:
        return
    try:
        validate_code(code)
    except SafeModeViolation as e:
        raise ToolError(
            f"Safe mode (BLENDER_MCP_SAFE_MODE=1) blocked this code: {e}. Rewrite it using only bpy, bmesh, "
            "mathutils and the basic standard modules, or ask the user to turn safe mode off."
        ) from None


# --------------------------------------------------------------------------- inspection snippets

SNIP_DETAILS = """
def r(v, n=4):
    return [round(float(x), n) for x in v]

def mat_info(m):
    info = {"name": m.name}
    try:
        if m.use_nodes and m.node_tree:
            for n in m.node_tree.nodes:
                if n.type == "BSDF_PRINCIPLED":
                    ins = {}
                    for key in ("Base Color", "Metallic", "Roughness", "Alpha"):
                        inp = n.inputs.get(key)
                        if inp is None:
                            continue
                        if inp.is_linked:
                            ins[key] = "linked:" + inp.links[0].from_node.name
                        else:
                            v = inp.default_value
                            ins[key] = r(v) if hasattr(v, "__len__") else round(float(v), 3)
                    info["principled"] = ins
                    break
    except Exception:
        pass
    return info

o = bpy.data.objects.get(_P["name"])
if o is None:
    q = _P["name"].lower()
    print(json.dumps({"error": "Object not found: " + _P["name"],
                      "similar": [n for n in bpy.data.objects.keys() if q in n.lower()][:10]}))
else:
    d = {
        "name": o.name, "type": o.type, "data": o.data.name if o.data else None,
        "location": r(o.location),
        "rotation_euler_deg": [round(math.degrees(a), 2) for a in o.rotation_euler],
        "rotation_mode": o.rotation_mode,
        "scale": r(o.scale), "dimensions": r(o.dimensions),
        "parent": o.parent.name if o.parent else None,
        "children": [c.name for c in o.children],
        "collections": [c.name for c in o.users_collection],
        "visible": o.visible_get(), "hide_viewport": o.hide_viewport, "hide_render": o.hide_render,
        "modifiers": [{"name": m.name, "type": m.type, "viewport": m.show_viewport, "render": m.show_render}
                      for m in o.modifiers],
        "constraints": [{"name": c.name, "type": c.type} for c in o.constraints],
        "materials": [mat_info(s.material) for s in o.material_slots if s.material],
    }
    try:
        corners = [o.matrix_world @ mathutils.Vector(c) for c in o.bound_box]
        d["world_bbox"] = {"min": r([min(c[i] for c in corners) for i in range(3)]),
                           "max": r([max(c[i] for c in corners) for i in range(3)])}
    except Exception:
        d["world_bbox"] = None
    props = {k: o[k] for k in o.keys() if not k.startswith("_")}
    if props:
        d["custom_properties"] = props
    ad = o.animation_data
    if ad and ad.action:
        d["animation"] = {"action": ad.action.name, "frame_range": [round(x, 2) for x in ad.action.frame_range]}

    if o.type == "MESH":
        me = o.data
        d["mesh"] = {
            "vertices": len(me.vertices), "edges": len(me.edges), "faces": len(me.polygons),
            "uv_layers": [u.name for u in me.uv_layers],
            "vertex_groups": [g.name for g in o.vertex_groups],
            "shape_keys": [k.name for k in me.shape_keys.key_blocks] if me.shape_keys else [],
        }
        try:
            em = o.evaluated_get(bpy.context.evaluated_depsgraph_get()).to_mesh()
            d["mesh"]["evaluated"] = {"vertices": len(em.vertices), "faces": len(em.polygons),
                                      "triangles": sum(len(p.vertices) - 2 for p in em.polygons)}
            o.evaluated_get(bpy.context.evaluated_depsgraph_get()).to_mesh_clear()
        except Exception:
            pass
    elif o.type == "CAMERA":
        c = o.data
        d["camera"] = {"type": c.type, "lens_mm": c.lens, "sensor_width_mm": c.sensor_width,
                       "clip": [c.clip_start, c.clip_end], "ortho_scale": c.ortho_scale}
    elif o.type == "LIGHT":
        l = o.data
        d["light"] = {"type": l.type, "energy": l.energy, "color": r(l.color)}
        for attr in ("size", "spot_size", "angle"):
            if hasattr(l, attr):
                d["light"][attr] = round(float(getattr(l, attr)), 4)
    print(json.dumps(d, default=str))
"""

SNIP_RENDER = """
import os
scene = bpy.context.scene
if scene.camera is None and not _P.get("camera"):
    raise Exception("The scene has no active camera. Create one, or pass camera=<object name>.")

# Candidate identifiers per alias (EEVEE was called BLENDER_EEVEE_NEXT in Blender 4.2-4.5). The engine list that
# bl_rna reports is incomplete outside the UI, so each candidate is simply tried.
aliases = {
    "CYCLES": ["CYCLES"],
    "WORKBENCH": ["BLENDER_WORKBENCH"],
    "EEVEE": ["BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"],
}

def set_engine(alias):
    for candidate in aliases.get(alias.upper(), []):
        try:
            rs.engine = candidate
            return candidate
        except TypeError:
            continue
    raise Exception("Render engine %r is not available in this Blender" % alias)
formats = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".exr": "OPEN_EXR", ".tif": "TIFF", ".tiff": "TIFF"}
ext = os.path.splitext(_P["path"])[1].lower()
if ext not in formats:
    raise Exception("Unsupported output extension %r; use one of %s" % (ext, sorted(formats)))

rs = scene.render
try:
    view_prefs = bpy.context.preferences.view
except Exception:
    view_prefs = None
saved_display = view_prefs.render_display_type if view_prefs is not None else None
saved = {
    "engine": rs.engine, "pct": rs.resolution_percentage, "path": rs.filepath,
    "fmt": rs.image_settings.file_format, "camera": scene.camera,
}
cycles_samples = scene.cycles.samples if hasattr(scene, "cycles") else None
eevee_samples = scene.eevee.taa_render_samples if hasattr(scene, "eevee") else None
try:
    if _P.get("engine"):
        set_engine(_P["engine"])
    if _P.get("camera"):
        cam = bpy.data.objects.get(_P["camera"])
        if cam is None or cam.type != "CAMERA":
            raise Exception("Camera object not found: %s" % _P["camera"])
        scene.camera = cam
    rs.resolution_percentage = _P["percentage"]
    if _P.get("samples"):
        if rs.engine == "CYCLES":
            scene.cycles.samples = _P["samples"]
        elif "EEVEE" in rs.engine:
            scene.eevee.taa_render_samples = _P["samples"]
    rs.image_settings.file_format = formats[ext]
    os.makedirs(os.path.dirname(_P["path"]), exist_ok=True)
    rs.filepath = _P["path"]
    if view_prefs is not None:
        view_prefs.render_display_type = "NONE"
    bpy.ops.render.render(write_still=True)
    out = {"path": _P["path"], "engine": rs.engine, "samples": scene.cycles.samples if rs.engine == "CYCLES" else None,
           "resolution": [rs.resolution_x * rs.resolution_percentage // 100,
                          rs.resolution_y * rs.resolution_percentage // 100],
           "size_bytes": os.path.getsize(_P["path"])}
finally:
    if view_prefs is not None:
        view_prefs.render_display_type = saved_display
    rs.engine = saved["engine"]
    rs.resolution_percentage = saved["pct"]
    rs.filepath = saved["path"]
    rs.image_settings.file_format = saved["fmt"]
    scene.camera = saved["camera"]
    if cycles_samples is not None:
        scene.cycles.samples = cycles_samples
    if eevee_samples is not None:
        scene.eevee.taa_render_samples = eevee_samples
print(json.dumps(out))
"""

SNIP_SAVE = """
import os
path = _P.get("path")
if path:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=path, copy=_P["copy"])
elif bpy.data.filepath:
    bpy.ops.wm.save_mainfile()
    path = bpy.data.filepath
else:
    raise Exception("The .blend file has never been saved: pass an explicit filepath.")
print(json.dumps({"saved": path, "size_bytes": os.path.getsize(path), "copy": bool(path and _P["copy"])}))
"""

SNIP_EXPORT = """
import os
path = _P["path"]
ext = os.path.splitext(path)[1].lower()
sel = _P["selected_only"]
os.makedirs(os.path.dirname(path), exist_ok=True)
ops = bpy.ops
if ext in (".glb", ".gltf"):
    ops.export_scene.gltf(filepath=path, export_format="GLB" if ext == ".glb" else "GLTF_SEPARATE", use_selection=sel)
elif ext == ".fbx":
    ops.export_scene.fbx(filepath=path, use_selection=sel)
elif ext == ".obj":
    if hasattr(ops.wm, "obj_export"):
        ops.wm.obj_export(filepath=path, export_selected_objects=sel)
    else:
        ops.export_scene.obj(filepath=path, use_selection=sel)
elif ext == ".stl":
    if hasattr(ops.wm, "stl_export"):
        ops.wm.stl_export(filepath=path, export_selected_objects=sel)
    else:
        ops.export_mesh.stl(filepath=path, use_selection=sel)
elif ext == ".ply":
    if hasattr(ops.wm, "ply_export"):
        ops.wm.ply_export(filepath=path, export_selected_objects=sel)
    else:
        ops.export_mesh.ply(filepath=path, use_selection=sel)
elif ext in (".usd", ".usda", ".usdc"):
    ops.wm.usd_export(filepath=path, selected_objects_only=sel)
else:
    raise Exception("Unsupported export extension %r. Use .glb .gltf .fbx .obj .stl .ply .usd .usda .usdc" % ext)
print(json.dumps({"exported": path, "size_bytes": os.path.getsize(path)}))
"""

SNIP_RUN_SCRIPT = """
import sys, os, traceback
_dir = os.path.dirname(_P["path"])
if _dir not in sys.path:
    sys.path.insert(0, _dir)
_ns = {"__name__": "__main__", "__file__": _P["path"], "bpy": bpy, "bmesh": bmesh, "math": math,
       "mathutils": mathutils, "json": json, "SCRIPT_ARGS": _P["args"]}
try:
    exec(compile(_P["source"], _P["path"], "exec"), _ns)
except Exception as _e:
    # Report where in *the script* it failed, whatever the add-on version does with tracebacks
    _frames = [f for f in traceback.extract_tb(_e.__traceback__) if f.filename == _P["path"]]
    _where = "\\n".join('  File "%s", line %d, in %s\\n    %s' % (f.filename, f.lineno, f.name, f.line or "") for f in _frames[-3:])
    raise Exception("%s: %s\\n%s" % (type(_e).__name__, _e, _where))
"""


# --------------------------------------------------------------------------- tools: inspection


@_tool(read_only=True)
async def blender_status() -> str:
    """Check the connection to Blender and report version, open file and scene. Call this first when
    anything fails: it tells you whether Blender is reachable, whether the token is accepted, whether code
    execution is enabled, and which asset libraries and AI generators this server has keys for."""
    try:
        data = await call("ping")
    except ToolError as e:
        if "Unknown command type" in str(e):
            raise ToolError(
                "The Blender add-on is too old for this server. Reinstall blender_mcp.py (version 2.x) from this "
                "repository: Preferences > Add-ons, remove the old one, restart Blender, install the new file."
            ) from None
        raise
    data["endpoint"] = f"{HOST}:{PORT}"
    data["read_only_mode"] = READ_ONLY
    data["safe_mode"] = SAFE_MODE
    data["libraries"] = {"polyhaven": "ready (no key needed)", "sketchfab": bool(SKETCHFAB_API_KEY),
                         "polypizza": bool(POLYPIZZA_API_KEY)}
    data["generators"] = {"hyper3d_rodin": (_rodin_platform() if HYPER3D_API_KEY else False),
                          "hunyuan3d": (HUNYUAN3D_MODE if HUNYUAN3D_MODE in ("official", "local") else
                                        "official" if (HUNYUAN3D_SECRET_ID and HUNYUAN3D_SECRET_KEY) else
                                        "local" if HUNYUAN3D_API_URL else False)}
    return _compact(data)


SNIP_SCENE = """
scene = bpy.context.scene
dg = bpy.context.evaluated_depsgraph_get()
F = set(_P["fields"])
GEO = ("MESH", "CURVE", "SURFACE", "FONT", "META", "LATTICE", "ARMATURE", "VOLUME", "POINTCLOUD", "CURVES", "GREASEPENCIL")
BIG = 200000

def r(v, n=3):
    return [round(float(x), n) for x in v]

def corners(o):
    try:
        e = o.evaluated_get(dg)
        if o.type in GEO:
            return [e.matrix_world @ mathutils.Vector(c) for c in e.bound_box]
        return [e.matrix_world.translation.copy()]
    except Exception:
        return [o.matrix_world.translation.copy()]

def hier_corners(o):
    pts = list(corners(o))
    for c in o.children:
        pts += hier_corners(c)
    return pts

def topology(o):
    me = o.data
    if len(me.polygons) > BIG:
        return {"skipped": "mesh too large (%d faces)" % len(me.polygons)}
    bm = bmesh.new()
    try:
        bm.from_mesh(me)
        tris = quads = ngons = 0
        for f in bm.faces:
            n = len(f.verts)
            if n == 3:
                tris += 1
            elif n == 4:
                quads += 1
            else:
                ngons += 1
        boundary = sum(1 for e in bm.edges if e.is_boundary)
        nonmanifold = sum(1 for e in bm.edges if not e.is_manifold and not e.is_boundary)
        loose = sum(1 for v in bm.verts if not v.link_edges)
        poles3 = sum(1 for v in bm.verts if len(v.link_edges) == 3)
        poles5 = sum(1 for v in bm.verts if len(v.link_edges) >= 5)
        return {"tris": tris, "quads": quads, "ngons": ngons, "nonmanifold_edges": nonmanifold,
                "boundary_edges": boundary, "loose_verts": loose, "poles3": poles3, "poles5plus": poles5}
    finally:
        bm.free()

def weights(o):
    arm = None
    for m in o.modifiers:
        if m.type == "ARMATURE" and m.object:
            arm = m.object
            break
    if arm is None:
        return None
    me = o.data
    if len(me.vertices) > BIG:
        return {"armature": arm.name, "skipped": "mesh too large (%d vertices)" % len(me.vertices)}
    deform = set(b.name for b in arm.data.bones if b.use_deform)
    names = [g.name for g in o.vertex_groups]
    moving = set(i for i, g in enumerate(o.vertex_groups) if g.name in deform)
    unweighted = sum(1 for v in me.vertices if not any(g.group in moving and g.weight > 0 for g in v.groups))
    no_group = sorted(b for b in deform if b not in names)
    return {"armature": arm.name, "unweighted_vertices": unweighted, "deform_bones_without_group": no_group[:20],
            "deform_bones_without_group_count": len(no_group)}

def row(o, depth):
    d = {"name": o.name, "type": o.type, "depth": depth}
    if F & {"size", "ground"}:
        pts = hier_corners(o)
        lo = [min(p[i] for p in pts) for i in range(3)]
        hi = [max(p[i] for p in pts) for i in range(3)]
        if "size" in F:
            d["size"] = r([hi[i] - lo[i] for i in range(3)])
        if "ground" in F:
            d["ground"] = round(lo[2], 3)
    if "location" in F:
        d["location"] = r(o.matrix_world.translation)
    if "rotation" in F:
        d["rotation"] = [round(math.degrees(a), 1) for a in o.matrix_world.to_euler()]
    if "scale" in F:
        d["scale"] = r(o.scale)
    if "parent" in F:
        d["parent"] = o.parent.name if o.parent else None
    if "children" in F:
        d["children"] = len(o.children)
    if "hidden" in F:
        try:
            d["hidden_viewport"] = not o.visible_get()
        except Exception:
            d["hidden_viewport"] = bool(o.hide_viewport)
        d["hidden_render"] = bool(o.hide_render)
    if "details" in F:
        if o.type == "MESH":
            d["details"] = {"faces": len(o.data.polygons), "verts": len(o.data.vertices)}
        elif o.type == "ARMATURE":
            d["details"] = {"bones": len(o.data.bones)}
        elif o.type == "LIGHT":
            d["details"] = {"light": o.data.type, "watts": round(o.data.energy, 1)}
        elif o.type == "CAMERA":
            d["details"] = {"lens_mm": round(o.data.lens, 1)}
    if "materials" in F:
        d["materials"] = [s.material.name for s in o.material_slots if s.material]
    if "modifiers" in F:
        d["modifiers"] = ["%s:%s" % (m.name, m.type) for m in o.modifiers]
    if "animation" in F:
        ad = o.animation_data
        if ad and ad.action:
            fr = ad.action.frame_range
            d["animation"] = {"action": ad.action.name, "frames": [round(fr[0]), round(fr[1])]}
    if "topology" in F and o.type == "MESH":
        d["topology"] = topology(o)
    if "weights" in F and o.type == "MESH":
        d["weights"] = weights(o)
    return d

def build():
    if _P.get("root"):
        ro = bpy.data.objects.get(_P["root"])
        if ro is None:
            q = _P["root"].lower()
            return {"error": "Object not found: " + _P["root"],
                    "similar": [n for n in bpy.data.objects.keys() if q in n.lower()][:10]}
        order = []
        def walk(o, depth):
            order.append((o, depth))
            for c in sorted(o.children, key=lambda x: x.name):
                walk(c, depth + 1)
        walk(ro, 0)
    elif _P.get("query"):
        q = _P["query"].lower()
        order = [(o, 0) for o in sorted(scene.objects, key=lambda x: x.name) if q in o.name.lower()]
    else:
        order = [(o, 0) for o in sorted(scene.objects, key=lambda x: x.name) if o.parent is None]
    total = len(order)
    rows = [row(o, depth) for o, depth in order[:_P["limit"]]]

    counts = {}
    for o in scene.objects:
        counts[o.type] = counts.get(o.type, 0) + 1
    active = bpy.context.view_layer.objects.active
    selected = [o.name for o in scene.objects if o.select_get()]
    try:
        mode = bpy.context.mode
    except Exception:
        mode = None
    out = {
        "scene": scene.name, "total_objects": len(scene.objects), "counts": counts,
        "active": active.name if active else None,
        "selected": selected[:10], "selected_count": len(selected), "mode": mode,
        "file": bpy.data.filepath or None, "unsaved": bpy.data.is_dirty,
        "matched": total, "rows": rows,
    }
    if "settings" in F:
        hdri = None
        w = scene.world
        if w and w.node_tree:
            for n in w.node_tree.nodes:
                if n.type == "TEX_ENVIRONMENT" and n.image:
                    hdri = n.image.name
                    break
        us = scene.unit_settings
        out["settings"] = {
            "blender": bpy.app.version_string, "engine": scene.render.engine,
            "resolution": [scene.render.resolution_x, scene.render.resolution_y],
            "percentage": scene.render.resolution_percentage,
            "frames": [scene.frame_start, scene.frame_end], "current_frame": scene.frame_current,
            "fps": scene.render.fps,
            "camera": scene.camera.name if scene.camera else None, "hdri": hdri,
            "unit_system": us.system, "unit_scale": us.scale_length,
        }
    if "collections" in F:
        def tree(c):
            return {"name": c.name, "objects": len(c.objects), "children": [tree(x) for x in c.children]}
        out["collections"] = tree(scene.collection)
    return out

print(json.dumps(build()))
"""

SCENE_FIELD_GROUPS = {
    "placement": ["location", "size", "ground", "rotation", "scale", "parent"],
    "contents": ["children", "hidden", "details", "materials", "modifiers", "animation"],
    "health": ["topology", "weights"],
    "extras": ["settings", "collections"],
}
SCENE_FIELDS = tuple(f for group in SCENE_FIELD_GROUPS.values() for f in group)
SCENE_DEFAULT_FIELDS = ["location", "size", "children", "hidden"]


def _num(x: Any) -> str:
    return f"{x + 0.0:g}" if isinstance(x, (int, float)) else str(x)  # + 0.0 turns -0.0 into 0


def _vec(v: list) -> str:
    return "(" + ", ".join(_num(x) for x in v) + ")"


def _ground_text(min_z: float) -> str:
    if abs(min_z) < 0.005:
        return "on ground"
    return f"floating {_num(round(min_z, 2))} m" if min_z > 0 else f"below ground by {_num(round(-min_z, 2))} m"


def _format_row(d: dict) -> str:
    parts = [f"{'  ' * d.get('depth', 0)}{d['name']} [{d['type']}]"]
    if "location" in d:
        parts.append(f"loc={_vec(d['location'])}")
    if "size" in d:
        parts.append(f"size={_vec(d['size'])}")
    if "ground" in d:
        parts.append(f"ground={_ground_text(d['ground'])}")
    if "rotation" in d:
        parts.append(f"rot={_vec(d['rotation'])}°")
    if "scale" in d:
        parts.append(f"scale={_vec(d['scale'])}")
    if "parent" in d:
        parts.append(f"parent={d['parent'] or '-'}")
    if "children" in d:
        parts.append(f"children={d['children']}")
    if "hidden_viewport" in d:
        hidden = [n for n, k in (("viewport", "hidden_viewport"), ("render", "hidden_render")) if d.get(k)]
        parts.append("hidden=" + ("+".join(hidden) if hidden else "no"))
    if d.get("details"):
        parts.append(" ".join(f"{k}={_num(v)}" for k, v in d["details"].items()))
    if "materials" in d:
        parts.append("materials=" + (",".join(d["materials"]) or "-"))
    if "modifiers" in d:
        parts.append("modifiers=" + (",".join(d["modifiers"]) or "-"))
    if "animation" in d:
        a = d["animation"]
        parts.append(f"anim={a['action']} {a['frames'][0]}-{a['frames'][1]}" if a else "anim=-")
    t = d.get("topology")
    if t:
        if "skipped" in t:
            parts.append(f"topology=skipped ({t['skipped']})")
        else:
            parts.append(
                f"topology[tris {t['tris']}, quads {t['quads']}, ngons {t['ngons']}, non-manifold edges "
                f"{t['nonmanifold_edges']}, boundary edges {t['boundary_edges']}, loose verts {t['loose_verts']}, "
                f"poles 3-valence {t['poles3']} / 5+ {t['poles5plus']}]")
    w = d.get("weights")
    if w:
        if "skipped" in w:
            parts.append(f"weights[{w['armature']}: skipped ({w['skipped']})]")
        else:
            missing = w["deform_bones_without_group"]
            more = w["deform_bones_without_group_count"] - len(missing)
            parts.append(
                f"weights[{w['armature']}: {w['unweighted_vertices']} vertices no deform bone moves; "
                f"deform bones without vertex group: "
                + (", ".join(missing) + (f" (+{more} more)" if more > 0 else "") if missing else "none") + "]")
    return " ".join(parts)


def _format_collections(c: dict) -> str:
    inner = ", ".join(_format_collections(x) for x in c["children"])
    return f"{c['name']}[{c['objects']}]" + (f" > ({inner})" if inner else "")


def _format_scene(data: dict) -> str:
    counts = ", ".join(f"{k} {v}" for k, v in sorted(data["counts"].items())) or "empty"
    sel = ", ".join(data["selected"]) + (f" (+{data['selected_count'] - len(data['selected'])} more)"
                                         if data["selected_count"] > len(data["selected"]) else "")
    lines = [
        f"Scene \"{data['scene']}\" | {data['total_objects']} objects ({counts}) | active: {data['active'] or '-'} | "
        f"selected: {sel or '-'} | mode: {data.get('mode') or '-'} | file: {data['file'] or 'unsaved'}"
        + (" (unsaved changes)" if data["file"] and data.get("unsaved") else "")
    ]
    s = data.get("settings")
    if s:
        lines.append(
            f"settings: Blender {s['blender']} | engine {s['engine']} | {s['resolution'][0]}x{s['resolution'][1]} @ "
            f"{s['percentage']}% | frames {s['frames'][0]}-{s['frames'][1]} (now {s['current_frame']}) @ {s['fps']} fps | "
            f"camera {s['camera'] or '-'} | hdri {s['hdri'] or '-'} | units {s['unit_system']} x{_num(s['unit_scale'])}")
    if data.get("collections"):
        lines.append("collections: " + _format_collections(data["collections"]))
    lines.extend(_format_row(r) for r in data["rows"])
    if data["matched"] > len(data["rows"]):
        lines.append(f"(showing {len(data['rows'])} of {data['matched']}; raise limit, or narrow with query / root)")
    elif not data["rows"]:
        lines.append("(no matching objects)")
    return "\n".join(lines)


def _scene_params(query, root, fields, limit) -> dict:
    chosen = list(fields) if fields else list(SCENE_DEFAULT_FIELDS)
    expanded: list[str] = []
    for f in chosen:  # a group name (placement, contents, health, extras) stands for all of its fields
        expanded.extend(SCENE_FIELD_GROUPS.get(f, [f]))
    unknown = [f for f in expanded if f not in SCENE_FIELDS]
    if unknown:
        raise ToolError(f"Unknown field(s) {unknown}. Valid fields: {', '.join(SCENE_FIELDS)} "
                        f"(or the groups {', '.join(SCENE_FIELD_GROUPS)})")
    return {"query": query, "root": root, "fields": sorted(set(expanded)), "limit": limit}


@_tool(read_only=True)
async def get_scene_info(
    query: Annotated[Optional[str], Field(description="List every object whose name contains this text (case-insensitive)")] = None,
    root: Annotated[Optional[str], Field(description="List this object and its whole hierarchy, indented")] = None,
    fields: Annotated[Optional[list[str]], Field(description="What to show per object; names or groups, see the description")] = None,
    limit: Annotated[int, Field(ge=1, le=300, description="Maximum object lines")] = 20,
) -> str:
    """Facts about the scene as compact text: a header (counts by type, active, selection, mode, file) and
    one line per object with the fields you ask for. No image: use look to see it. Top-level objects by
    default; root="Name" lists that object's hierarchy; query="chair" lists every object whose name has it.

    fields (default: location, size, children, hidden), either single names or whole groups:
    - placement: location (world), size (world bounding box incl. children), ground (on ground / floating /
      below, with the distance), rotation (degrees), scale, parent
    - contents: children (count), hidden, details (faces, bones, light power, lens), materials, modifiers, animation
    - health: topology (tris/quads/ngons, non-manifold and boundary edges, loose verts, poles; meshes up to
      200k faces), weights (vertices no deform bone moves, deform bones with no vertex group)
    - extras: settings (engine, resolution, frames, camera, HDRI, units) and collections (hierarchy)
    Use get_object_details for one object's full state."""
    return _format_scene(await _query(SNIP_SCENE, _scene_params(query, root, fields, limit)))


@mcp.resource("blender://scene")
async def scene_resource() -> str:
    """Live scene summary (same data as get_scene_info with settings and collections)."""
    return _format_scene(await _query(SNIP_SCENE, _scene_params(None, None, ["extras", "location", "size"], 40)))


@_tool(read_only=True)
async def get_object_details(
    name: Annotated[str, Field(description="Exact object name (case-sensitive)")],
) -> str:
    """Full state of one object: transform (rotation in degrees), world bounding box, hierarchy,
    visibility, modifiers, constraints, materials (Principled BSDF values), custom properties,
    animation, and type-specific data (mesh stats incl. evaluated triangle count, camera lens, light
    settings). If the name is wrong, similar names are suggested."""
    return _compact(await _query(SNIP_DETAILS, {"name": name}))


# --------------------------------------------------------------------------- node trees
#
# Reading shader, geometry, world and compositor node trees. Creating and editing nodes is done with
# execute_blender_code; this tool only reads. To report only what a user changed, it compares every node with a
# freshly made node of the same type in a scratch node group, which is removed again before the snippet ends.

SNIP_NODE_TREE = """
KIND, NAME, OBJ = _P["kind"], _P["name"], _P["object"]
MAX_NODES, RECURSE, MAX_TREES = int(_P["max_nodes"]), bool(_P["recurse"]), 8


class Fail(Exception):
    def __init__(self, msg, names=(), query=""):
        super().__init__(msg)
        self.names, self.query = list(names), (query or "").lower()


def fmt(v):
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return round(v, 4)
    if isinstance(v, bpy.types.ID):
        return {"ref": v.name}
    if isinstance(v, (set, frozenset)):
        return sorted(v)
    try:
        return [fmt(x) for x in v]
    except TypeError:
        return str(v)


BASE_PROPS = set(bpy.types.Node.bl_rna.properties.keys()) | {"inputs", "outputs", "internal_links", "dimensions"}
PLAIN = {"FRAME", "REROUTE", "GROUP_INPUT", "GROUP_OUTPUT"}
scratch, cache = {}, {}


def settings(n):
    out = {}
    for p in n.bl_rna.properties:
        pid = p.identifier
        if pid in BASE_PROPS or pid.startswith("bl_") or p.is_readonly or p.type == "COLLECTION":
            continue
        try:
            v = getattr(n, pid)
        except Exception:
            continue
        if p.type == "POINTER":
            if isinstance(v, bpy.types.ID):
                out[pid] = {"ref": v.name}
            continue
        out[pid] = fmt(v)
    cr = getattr(n, "color_ramp", None)
    if cr is not None:
        out["color_ramp"] = {"interpolation": cr.interpolation, "stops": [[round(e.position, 4), fmt(e.color)] for e in cr.elements]}
    return out


def defaults_for(tree, n):
    key = (tree.bl_idname, n.bl_idname)
    if key not in cache:
        res = None
        try:
            sg = scratch.get(tree.bl_idname)
            if sg is None:
                sg = scratch[tree.bl_idname] = bpy.data.node_groups.new("_mcp_scratch", tree.bl_idname)
            tmp = sg.nodes.new(n.bl_idname)
            res = ({s.identifier: fmt(s.default_value) for s in tmp.inputs if hasattr(s, "default_value")}, settings(tmp))
            sg.nodes.remove(tmp)
        except Exception:
            res = None
        cache[key] = res
    return cache[key]


def group_defaults(n):
    gt = getattr(n, "node_tree", None)
    if gt is None:
        return {}
    return {i.identifier: fmt(i.default_value) for i in gt.interface.items_tree
            if i.item_type == "SOCKET" and i.in_out == "INPUT" and hasattr(i, "default_value")}


def sname(s):
    side = s.node.outputs if s.is_output else s.node.inputs
    same = [x for x in side if x.name == s.name and (x.enabled or x is s)]
    return f"{s.name}[{same.index(s)}]" if len(same) > 1 else s.name


def node_entry(tree, n):
    e = {"name": n.name, "type": n.bl_label, "kind": n.type}
    if n.label and n.label != n.name:
        e["label"] = n.label
    if n.mute:
        e["muted"] = True
    if n.parent:
        e["parent"] = n.parent.name
    if n.type in PLAIN:
        return e
    is_group = n.type == "GROUP"
    if is_group and n.node_tree:
        e["group"] = n.node_tree.name
    dflt = defaults_for(tree, n)
    base_inputs, base_settings = dflt if dflt else ({}, {})
    if is_group:
        base_inputs = group_defaults(n)
    st = {}
    for k, v in settings(n).items():
        if is_group and k == "node_tree":
            continue
        if k == "color_ramp" or (dflt and base_settings.get(k) != v) or (not dflt and v not in (None, False, 0, "")):
            st[k] = v
    if st:
        e["settings"] = st
    vals = {}
    for s in n.inputs:
        if s.is_linked or not s.enabled or s.type in ("GEOMETRY", "SHADER", "CUSTOM") or not hasattr(s, "default_value"):
            continue
        v = fmt(s.default_value)
        if s.identifier in base_inputs and base_inputs[s.identifier] == v:
            continue
        if not base_inputs and v in (None, False, 0, 0.0, [0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0]):
            continue
        vals[sname(s)] = v
    if vals:
        e["inputs"] = vals
    return e


def ordered(nodes, links):
    indeg = {n.name: 0 for n in nodes}
    out = {n.name: [] for n in nodes}
    for l in links:
        a, b = l.from_node.name, l.to_node.name
        if a in indeg and b in indeg and a != b:
            indeg[b] += 1
            out[a].append(b)
    by = {n.name: n for n in nodes}
    key = lambda name: (round(by[name].location.x), -round(by[name].location.y), name)
    ready = sorted((k for k, v in indeg.items() if v == 0), key=key)
    result = []
    while ready:
        cur = ready.pop(0)
        result.append(cur)
        for nxt in out[cur]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                ready.append(nxt)
        ready.sort(key=key)
    rest = sorted((k for k in indeg if k not in result), key=key)
    return [by[k] for k in result + rest]


def interface_of(tree):
    items = []
    for i in tree.interface.items_tree:
        if i.item_type != "SOCKET":
            continue
        d = {"name": i.name, "id": i.identifier, "dir": i.in_out, "type": i.socket_type}
        if hasattr(i, "default_value"):
            d["default"] = fmt(i.default_value)
        for a in ("min_value", "max_value"):
            if hasattr(i, a):
                d[a] = fmt(getattr(i, a))
        items.append(d)
    return items


def modifier_values(mod, tree):
    vals = []
    for i in tree.interface.items_tree:
        if i.item_type != "SOCKET" or i.in_out != "INPUT" or i.socket_type in ("NodeSocketGeometry", "NodeSocketShader"):
            continue
        found, val = False, fmt(getattr(i, "default_value", None))
        try:
            g = mod.properties.inputs[i.identifier]
            if "value" in g.keys():
                found, val = True, fmt(g["value"])
        except Exception:
            try:
                found, val = True, fmt(mod[i.identifier])
            except Exception:
                pass
        vals.append({"name": i.name, "value": val, "changed": found})
    return vals


def describe(tree, title, extra=None):
    if tree is None:
        return {"title": title, "empty": True}
    nodes = list(tree.nodes)
    shown = ordered(nodes, tree.links)[:MAX_NODES]
    names = {n.name for n in shown}
    d = {"title": title, "tree_type": tree.bl_idname, "total_nodes": len(nodes), "total_links": len(tree.links),
         "nodes": [node_entry(tree, n) for n in shown], "links": []}
    for l in tree.links:
        if l.from_node.name in names and l.to_node.name in names:
            flag = "invalid" if not l.is_valid else ("muted" if l.is_muted else "")
            d["links"].append([l.from_node.name, sname(l.from_socket), l.to_node.name, sname(l.to_socket), flag])
    iface = interface_of(tree)
    if iface:
        d["interface"] = iface
    d["groups"] = sorted({n.node_tree.name for n in nodes if n.type == "GROUP" and n.node_tree})
    if extra:
        d.update(extra)
    return d


def listing():
    if KIND == "material":
        items = [{"name": m.name, "nodes": len(m.node_tree.nodes), "users": m.users} for m in bpy.data.materials if m.node_tree]
    elif KIND == "node_group":
        items = [{"name": g.name, "type": g.bl_idname, "nodes": len(g.nodes), "users": g.users} for g in bpy.data.node_groups]
    else:
        items = [{"object": o.name, "modifiers": [{"name": m.name, "group": m.node_group.name if m.node_group else None}
                                                  for m in o.modifiers if m.type == "NODES"]}
                 for o in bpy.data.objects if any(m.type == "NODES" for m in o.modifiers)]
    return {"list": KIND, "items": items[:100], "more": max(0, len(items) - 100)}


def collect():
    sc = bpy.context.scene
    todo = []  # (tree or None, title, extra)
    if KIND == "material":
        if OBJ:
            ob = bpy.data.objects.get(OBJ)
            if ob is None:
                raise Fail("Object not found: " + OBJ, bpy.data.objects.keys(), OBJ)
            mats = []
            for s in ob.material_slots:
                if s.material and s.material not in mats:
                    mats.append(s.material)
            if NAME:
                mats = [m for m in mats if m.name == NAME]
                if not mats:
                    raise Fail(f"Object {OBJ} has no material named {NAME}", [s.material.name for s in ob.material_slots if s.material], NAME)
            if not mats:
                raise Fail(f"Object {OBJ} has no materials")
        else:
            m = bpy.data.materials.get(NAME)
            if m is None:
                raise Fail("Material not found: " + NAME, bpy.data.materials.keys(), NAME)
            mats = [m]
        todo = [(m.node_tree, f'Material "{m.name}"', None) for m in mats[:4]]
    elif KIND == "node_group":
        if not NAME:
            raise Fail("kind node_group needs name (leave it empty to list the node groups)")
        g = bpy.data.node_groups.get(NAME)
        if g is None:
            raise Fail("Node group not found: " + NAME, bpy.data.node_groups.keys(), NAME)
        todo = [(g, f'Node group "{g.name}"', None)]
    elif KIND == "geometry_nodes":
        ob = bpy.data.objects.get(OBJ)
        if ob is None:
            raise Fail("Object not found: " + OBJ, bpy.data.objects.keys(), OBJ)
        mods = [m for m in ob.modifiers if m.type == "NODES"]
        if NAME:
            mods = [m for m in mods if m.name == NAME or (m.node_group and m.node_group.name == NAME)]
        if not mods:
            raise Fail(f"Object {OBJ} has no Geometry Nodes modifier" + (f" matching {NAME}" if NAME else ""),
                       [m.name for m in ob.modifiers if m.type == "NODES"], NAME)
        for m in mods[:4]:
            todo.append((m.node_group, f'Geometry Nodes modifier "{m.name}" on {ob.name}',
                         {"modifier_values": modifier_values(m, m.node_group)} if m.node_group else None))
    elif KIND == "world":
        w = bpy.data.worlds.get(NAME) if NAME else sc.world
        if w is None:
            raise Fail(("World not found: " + NAME) if NAME else "The scene has no world", bpy.data.worlds.keys(), NAME)
        todo = [(w.node_tree, f'World "{w.name}"', None)]
    elif KIND == "compositor":
        s2 = bpy.data.scenes.get(NAME) if NAME else sc
        if s2 is None:
            raise Fail("Scene not found: " + NAME, bpy.data.scenes.keys(), NAME)
        tree = getattr(s2, "compositing_node_group", None)
        if tree is None and getattr(s2, "use_nodes", False):
            tree = getattr(s2, "node_tree", None)
        todo = [(tree, f'Compositor of scene "{s2.name}"', None)]
    results, seen = [], set()
    for tree, title, extra in todo:
        results.append(describe(tree, title, extra))
        if tree is not None:
            seen.add(tree.name)
    skipped = 0
    if RECURSE:
        queue = [g for r in results for g in r.get("groups", [])]
        while queue:
            g = queue.pop(0)
            if g in seen:
                continue
            seen.add(g)
            if len(results) >= MAX_TREES:
                skipped += 1
                continue
            gt = bpy.data.node_groups.get(g)
            if gt is not None:
                r = describe(gt, f'Node group "{g}" (used inside)')
                results.append(r)
                queue.extend(r["groups"])
    return {"trees": results, "groups_not_expanded": skipped}


def main():
    try:
        if not NAME and not OBJ and KIND in ("material", "node_group"):
            res = listing()
        elif KIND == "geometry_nodes" and not OBJ:
            res = listing()
        else:
            res = collect()
        print(json.dumps(res, default=str))
    except Fail as f:
        print(json.dumps({"error": str(f), "similar": [n for n in f.names if f.query and f.query in n.lower()][:10] or f.names[:10]}))
    finally:
        for g in scratch.values():
            try:
                bpy.data.node_groups.remove(g)
            except Exception:
                pass


main()
"""


def _nv(v: Any) -> str:
    """One value from a node tree as text."""
    if isinstance(v, dict) and "ref" in v:
        return f"<{v['ref']}>"
    if isinstance(v, bool):
        return "on" if v else "off"
    if isinstance(v, (int, float)):
        return _num(v)
    if isinstance(v, list):
        return "(" + ", ".join(_nv(x) for x in v) + ")"
    return str(v)


def _format_node(n: dict) -> str:
    name = n["name"]
    base = re.sub(r"\.\d{3}$", "", name)
    head = name if base == n["type"] or n["kind"] in ("FRAME", "REROUTE") else f"{name} ({n['type']})"
    if n.get("label"):
        head += f' "{n["label"]}"'
    bits = [head]
    if n.get("group"):
        bits.append(f"group=<{n['group']}>")
    if n.get("muted"):
        bits.append("MUTED")
    if n.get("parent"):
        bits.append(f"in {n['parent']}")
    st = dict(n.get("settings") or {})
    ramp = st.pop("color_ramp", None)
    if ramp:
        st["interpolation"] = ramp["interpolation"]
    if st:
        bits.append("[" + ", ".join(f"{k}={_nv(v)}" for k, v in st.items()) + "]")
    if ramp:
        bits.append("stops " + " ".join(f"{_nv(p)}:{_nv(c)}" for p, c in ramp["stops"]))
    if n.get("inputs"):
        bits.append(" ".join(f"{k}={_nv(v)}" for k, v in n["inputs"].items()))
    return "  ".join(bits)


def _format_node_trees(data: dict) -> str:
    if "list" in data:
        kind, items = data["list"], data["items"]
        if not items:
            return f"No {kind.replace('_', ' ')} trees in this file."
        lines = []
        for it in items:
            if "object" in it:
                lines.append(f"{it['object']}: " + ", ".join(f"{m['name']} -> {m['group']}" for m in it["modifiers"]))
            elif "type" in it:
                lines.append(f"{it['name']} [{it['type']}] {it['nodes']} nodes, {it['users']} users")
            else:
                lines.append(f"{it['name']} {it['nodes']} nodes, {it['users']} users")
        more = f"\n... and {data['more']} more" if data.get("more") else ""
        return f"Available ({kind.replace('_', ' ')}); pass one as name (or object_name):\n" + "\n".join(lines) + more
    blocks = []
    for t in data["trees"]:
        if t.get("empty"):
            blocks.append(f"{t['title']}: no node tree (it does not use nodes).")
            continue
        head = f"{t['title']} - {t['total_nodes']} nodes, {t['total_links']} links ({t['tree_type']})"
        lines = [head]
        iface = t.get("interface")
        if iface:
            for direction, label in (("INPUT", "Inputs"), ("OUTPUT", "Outputs")):
                parts = []
                for i in (x for x in iface if x["dir"] == direction):
                    detail = [i["type"].replace("NodeSocket", "")]
                    if "default" in i and i["default"] is not None and direction == "INPUT":
                        detail.append(f"default {_nv(i['default'])}")
                    lo, hi = i.get("min_value"), i.get("max_value")
                    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and abs(lo) < 1e6 and abs(hi) < 1e6:
                        detail.append(f"range {_nv(lo)}..{_nv(hi)}")
                    parts.append(f"{i['name']} ({', '.join(detail)})")
                if parts:
                    lines.append(f"{label}: " + "; ".join(parts))
        if t.get("modifier_values"):
            lines.append("Modifier values: " + ", ".join(f"{v['name']}={_nv(v['value'])}" for v in t["modifier_values"]))
        lines.append("Nodes (inputs first; [..] = node settings; the rest are unconnected input values that differ from the node's defaults):")
        lines += ["  " + _format_node(n) for n in t["nodes"]]
        if t["total_nodes"] > len(t["nodes"]):
            lines.append(f"  ... {t['total_nodes'] - len(t['nodes'])} more nodes not shown (raise max_nodes)")
        if t["links"]:
            lines.append("Links:")
            for fn, fs, tn, ts, flag in t["links"]:
                lines.append(f"  {fn}.{fs} -> {tn}.{ts}" + (f"  ({flag})" if flag else ""))
        if t.get("groups"):
            lines.append("Uses node groups: " + ", ".join(t["groups"]))
        blocks.append("\n".join(lines))
    if data.get("groups_not_expanded"):
        blocks.append(f"({data['groups_not_expanded']} more node groups not expanded)")
    return "\n\n".join(blocks)


@_tool(read_only=True)
async def get_node_tree(
    kind: Annotated[Literal["material", "geometry_nodes", "node_group", "world", "compositor"],
                    Field(description="Which node tree to read")],
    name: Annotated[Optional[str], Field(description="material / node_group: its name. geometry_nodes: the modifier or group name (default: all Nodes modifiers of the object). world / compositor: world or scene name (default: the active scene's). Leave empty with kind material, node_group or geometry_nodes to list what exists")] = None,
    object_name: Annotated[Optional[str], Field(description="material: read the materials of this object. geometry_nodes: the object that has the modifier (required)")] = None,
    recurse_groups: Annotated[bool, Field(description="Also read the node groups used inside (up to 8 trees in total)")] = False,
    max_nodes: Annotated[int, Field(ge=1, le=500, description="Maximum nodes shown per tree")] = 80,
) -> str:
    """Read a node tree (read-only): what nodes it has, their settings, and how they are connected. Use it
    before changing a material or Geometry Nodes setup you did not build in this conversation, to explain one,
    or to find what is disconnected. Create and edit nodes with execute_blender_code.

    Output: nodes in data-flow order (inputs first), then the links as `Node.Socket -> Node.Socket`. Only what
    differs from a new node of that type is listed: settings in [brackets] and unconnected input values.
    Sockets with the same name on one node are written Name[0], Name[1]. For a node group (and a Geometry
    Nodes modifier) the interface is listed with defaults and ranges; a modifier also shows the values set
    on it. Reading briefly makes a scratch node group to learn each node's defaults and removes it at once."""
    if kind == "geometry_nodes" and not object_name and name:
        raise ToolError("kind geometry_nodes needs object_name (leave both empty to list the objects that have one)")
    return _clip(_format_node_trees(await _query(
        SNIP_NODE_TREE, dict(kind=kind, name=name, object=object_name, recurse=recurse_groups, max_nodes=max_nodes))))


# --------------------------------------------------------------------------- look
#
# Several ways of seeing the scene. "viewport" is the live 3D viewport (what the user sees). The other modes
# render with the Workbench engine through temporary cameras, which is fast, needs no lights and works the same
# on every machine. All Blender settings and temporary objects are restored/removed afterwards.

LOOK_VIEWS = {  # direction from the target towards the eye (Blender: Z up, front view looks along +Y)
    "front": (0.0, -1.0, 0.0), "back": (0.0, 1.0, 0.0),
    "left": (-1.0, 0.0, 0.0), "right": (1.0, 0.0, 0.0),
    "top": (0.0, -0.0001, 1.0), "bottom": (0.0, -0.0001, -1.0),  # tiny offset keeps "up" well defined
    "three_quarter": (1.0, -1.0, 0.8),
}
LOOK_DEFAULT_ANGLES = ["front", "right", "top", "three_quarter"]
LOOK_SHADINGS = ("solid", "textured", "clay", "flat", "xray")
LOOK_FOCAL_MM = 50.0
LOOK_SENSOR_MM = 36.0
LOOK_FIT_MARGIN = 1.15

SNIP_LOOK_TARGET = """
scene = bpy.context.scene
dg = bpy.context.evaluated_depsgraph_get()
GEO = ("MESH", "CURVE", "SURFACE", "FONT", "META", "LATTICE", "ARMATURE", "VOLUME", "POINTCLOUD", "CURVES", "GREASEPENCIL")

def corners(o):
    try:
        e = o.evaluated_get(dg)
        if o.type in GEO:
            return [e.matrix_world @ mathutils.Vector(c) for c in e.bound_box]
        return [e.matrix_world.translation.copy()]
    except Exception:
        return [o.matrix_world.translation.copy()]

def build():
    out = {
        "frame_start": scene.frame_start, "frame_end": scene.frame_end, "frame_current": scene.frame_current,
        "scene_camera": scene.camera.name if scene.camera else None,
        "resolution": [scene.render.resolution_x, scene.render.resolution_y],
    }
    if not _P["need_bbox"]:
        return out
    objs, seen = [], set()
    def add(o):
        if o.name in seen:
            return
        seen.add(o.name)
        objs.append(o)
        for c in o.children:
            add(c)
    if _P.get("targets"):
        for n in _P["targets"]:
            o = bpy.data.objects.get(n)
            if o is None:
                q = n.lower()
                return {"error": "Object not found: " + n,
                        "similar": [k for k in bpy.data.objects.keys() if q in k.lower()][:10]}
            add(o)
    else:
        geo = [o for o in scene.objects if o.type in GEO and o.visible_get()]
        for o in (geo or [o for o in scene.objects if o.visible_get()]):
            add(o)
    if not objs:
        return {"error": "Nothing to look at: the scene has no visible objects"}
    pts = []
    for o in objs:
        pts += corners(o)
    lo = [min(p[i] for p in pts) for i in range(3)]
    hi = [max(p[i] for p in pts) for i in range(3)]
    out["center"] = [round((lo[i] + hi[i]) / 2, 4) for i in range(3)]
    out["size"] = [round(hi[i] - lo[i], 3) for i in range(3)]
    out["radius"] = round(max(0.5 * math.sqrt(sum((hi[i] - lo[i]) ** 2 for i in range(3))), 0.01), 4)
    out["objects"] = [o.name for o in objs][:20]
    out["object_count"] = len(objs)
    return out

print(json.dumps(build()))
"""

SNIP_LOOK_RENDER = """
import os
scene = bpy.context.scene
rs = scene.render
sh = scene.display.shading
try:
    view_prefs = bpy.context.preferences.view
except Exception:
    view_prefs = None

SHADINGS = {
    "solid": {"light": "STUDIO", "color_type": "MATERIAL", "show_xray": False},
    "textured": {"light": "STUDIO", "color_type": "TEXTURE", "show_xray": False},
    "clay": {"light": "STUDIO", "color_type": "SINGLE", "show_xray": False},
    "flat": {"light": "FLAT", "color_type": "MATERIAL", "show_xray": False},
    "xray": {"light": "STUDIO", "color_type": "MATERIAL", "show_xray": True},
}

def sync_materials(saved_list):
    # Workbench colours surfaces with a material's *viewport display* colour, not its node tree, so a red
    # Principled BSDF would render grey. Mirror the base colour (and, for 'textured', the image texture) while
    # rendering; the originals are put back afterwards.
    for mat in bpy.data.materials:
        try:
            tree = mat.node_tree
            if tree is None:
                continue
            bsdf = next((n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
            inp = bsdf.inputs.get("Base Color") if bsdf else None
            if inp is None:
                continue
            saved_list.append((mat, tuple(mat.diffuse_color), tree.nodes.active))
            if not inp.is_linked:
                mat.diffuse_color = tuple(inp.default_value)
            elif _P["shading"] == "textured" and inp.links[0].from_node.type == "TEX_IMAGE":
                tree.nodes.active = inp.links[0].from_node
        except Exception:
            pass

def build():
    results, temps, saved_mats = [], [], []
    orig_camera, orig_frame = scene.camera, scene.frame_current
    saved = {"engine": rs.engine, "pct": rs.resolution_percentage, "rx": rs.resolution_x, "ry": rs.resolution_y,
             "path": rs.filepath, "fmt": rs.image_settings.file_format, "mode": rs.image_settings.color_mode}
    saved_shading = {k: getattr(sh, k) for k in ("light", "color_type", "show_xray")}
    saved_display = view_prefs.render_display_type if view_prefs is not None else None
    try:
        rs.engine = "BLENDER_WORKBENCH"
        for k, v in SHADINGS[_P["shading"]].items():
            setattr(sh, k, v)
        sync_materials(saved_mats)
        rs.resolution_percentage = 100
        rs.resolution_x, rs.resolution_y = _P["resolution"]
        rs.image_settings.file_format = "PNG"
        rs.image_settings.color_mode = "RGB"
        if view_prefs is not None:
            view_prefs.render_display_type = "NONE"
        cams = []
        for spec in _P["cameras"]:
            if spec.get("scene_camera"):
                if orig_camera is None:
                    return {"error": "The scene has no active camera. Create one, or use mode 'angles'."}
                cams.append((spec["label"], orig_camera))
                continue
            cd = bpy.data.cameras.new("_mcp_look")
            cd.lens, cd.sensor_width = spec["lens"], spec["sensor"]
            cd.clip_start, cd.clip_end = spec["clip_start"], spec["clip_end"]
            co = bpy.data.objects.new("_mcp_look", cd)
            scene.collection.objects.link(co)
            temps.append((co, cd))
            co.location = spec["location"]
            direction = mathutils.Vector(spec["target"]) - mathutils.Vector(spec["location"])
            co.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
            cams.append((spec["label"], co))
        n = 0
        for f in (_P.get("frames") or [None]):
            if f is not None:
                scene.frame_set(int(f))
            for label, cam in cams:
                scene.camera = cam
                n += 1
                path = os.path.join(_P["outdir"], "look_%02d.png" % n)
                rs.filepath = path
                bpy.ops.render.render(write_still=True)
                results.append({"label": label if f is None else "%s @ frame %d" % (label, f), "path": path})
        return {"images": results}
    finally:
        for co, cd in temps:
            try:
                bpy.data.objects.remove(co, do_unlink=True)
            except Exception:
                pass
            try:
                bpy.data.cameras.remove(cd)
            except Exception:
                pass
        for mat, diffuse, active in saved_mats:
            try:
                mat.diffuse_color = diffuse
                mat.node_tree.nodes.active = active
            except Exception:
                pass
        scene.camera = orig_camera
        scene.frame_set(orig_frame)
        rs.engine = saved["engine"]
        rs.resolution_percentage = saved["pct"]
        rs.resolution_x, rs.resolution_y = saved["rx"], saved["ry"]
        rs.filepath = saved["path"]
        rs.image_settings.file_format = saved["fmt"]
        rs.image_settings.color_mode = saved["mode"]
        for k, v in saved_shading.items():
            setattr(sh, k, v)
        if view_prefs is not None:
            view_prefs.render_display_type = saved_display

print(json.dumps(build()))
"""


def _look_direction(view: Any) -> tuple[str, tuple[float, float, float]]:
    """(label, unit vector from the target towards the eye) for a view name or an [x, y, z] direction."""
    if isinstance(view, str):
        key = view.lower().replace("-", "_").replace(" ", "_")
        if key not in LOOK_VIEWS:
            raise ToolError(f"Unknown view '{view}'. Use one of {', '.join(LOOK_VIEWS)} or an [x, y, z] direction.")
        v, label = LOOK_VIEWS[key], key
    elif isinstance(view, (list, tuple)) and len(view) == 3 and all(isinstance(x, (int, float)) for x in view):
        v, label = tuple(float(x) for x in view), f"dir({','.join(_num(x) for x in view)})"
    else:
        raise ToolError(f"Invalid view {view!r}: expected a name or an [x, y, z] direction")
    length = math.sqrt(sum(x * x for x in v))
    if length < 1e-9:
        raise ToolError("A view direction cannot be [0, 0, 0]")
    return label, (v[0] / length, v[1] / length, v[2] / length)


def _look_fit_distance(radius: float) -> float:
    """Eye distance from the centre so a sphere of this radius fits a square frame with some margin."""
    half_fov = math.atan(LOOK_SENSOR_MM / 2 / LOOK_FOCAL_MM)
    return max(radius, 0.01) / math.sin(half_fov) * LOOK_FIT_MARGIN


def _look_cameras(center: list, radius: float, views: list, distance: Optional[float]) -> list[dict]:
    dist = distance if distance else _look_fit_distance(radius)
    cams = []
    for view in views:
        label, d = _look_direction(view)
        cams.append({
            "label": label,
            "location": [round(center[i] + d[i] * dist, 4) for i in range(3)],
            "target": [round(c, 4) for c in center],
            "lens": LOOK_FOCAL_MM, "sensor": LOOK_SENSOR_MM,
            "clip_start": round(max(0.001, min(0.1, dist * 0.02)), 4),
            "clip_end": round(dist + radius * 4 + 10, 2),
        })
    return cams


def _look_resolution(size: int, scene_resolution: Optional[list]) -> list[int]:
    """Square frame, or the scene's aspect ratio when a scene resolution is given (longest side = size)."""
    if not scene_resolution or not scene_resolution[0] or not scene_resolution[1]:
        return [size, size]
    rx, ry = scene_resolution
    if rx >= ry:
        return [size, max(1, round(size * ry / rx))]
    return [max(1, round(size * rx / ry)), size]


def _look_frames(explicit: Optional[list[int]], count: int, start: int, end: int) -> list[int]:
    if explicit:
        return list(dict.fromkeys(int(f) for f in explicit))[:12]
    if end <= start:
        return [start]
    count = max(2, min(12, count))
    return sorted({round(start + i * (end - start) / (count - 1)) for i in range(count)})


async def _look_viewport(max_size: int) -> tuple[str, list]:
    tmp = Path(tempfile.gettempdir()) / f"blender_mcp_{uuid.uuid4().hex}.png"
    try:
        await call("get_viewport_screenshot", {"max_size": max_size, "filepath": str(tmp), "format": "png"})
        try:
            data = tmp.read_bytes()
        except OSError as e:
            raise ToolError(
                f"Blender reported success but the screenshot at {tmp} cannot be read ({e}). "
                "Blender and this server must share a filesystem."
            ) from None
    finally:
        tmp.unlink(missing_ok=True)
    return "Live 3D viewport, as the user sees it.", [("viewport", data)]


async def _look_render(shading: str, resolution: list[int], cameras: list[dict], frames: Optional[list[int]],
                       timeout: float) -> list:
    outdir = Path(tempfile.mkdtemp(prefix="blender_mcp_look_"))
    try:
        info = await _query(SNIP_LOOK_RENDER, {
            "shading": shading, "resolution": resolution, "cameras": cameras, "frames": frames,
            "outdir": str(outdir),
        }, timeout=timeout)
        images = []
        for item in info["images"]:
            try:
                images.append((item["label"], Path(item["path"]).read_bytes()))
            except OSError as e:
                raise ToolError(
                    f"Blender rendered {item['path']} but it cannot be read ({e}). "
                    "Blender and this server must share a filesystem."
                ) from None
        return images
    finally:
        shutil.rmtree(outdir, ignore_errors=True)


@_tool(read_only=False, destructive=False, readonly_mode_ok=True)
async def look(
    mode: Annotated[Literal["viewport", "camera", "angles", "frames"], Field(description="What to show; see the description")] = "viewport",
    target: Annotated[Optional[list[str]], Field(description="Object names to frame (children included). Default: every visible object")] = None,
    views: Annotated[Optional[list[Union[str, list[float]]]], Field(description="angles: up to 6 of front, back, left, right, top, bottom, three_quarter, or [x, y, z] directions from the target towards the eye")] = None,
    distance: Annotated[Optional[float], Field(gt=0, description="Metres from the target's centre to the eye (default: far enough to fit it)")] = None,
    shading: Annotated[Optional[Literal["solid", "textured", "clay", "flat", "xray"]], Field(description="How surfaces are drawn (not for mode viewport). Default solid")] = None,
    frames: Annotated[Optional[list[int]], Field(description="frames: explicit frame numbers (max 12)")] = None,
    frame_count: Annotated[int, Field(ge=2, le=12, description="frames: how many frames to sample across the animation")] = 6,
    view: Annotated[Optional[Union[str, list[float]]], Field(description="frames: 'camera', an angle name or an [x, y, z] direction. Default: the scene camera, else three_quarter")] = None,
    max_size: Annotated[int, Field(ge=128, le=2048, description="Longest side of each image in pixels. Images stay in the conversation, so go small for quick checks")] = 768,
    save_dir: Annotated[Optional[str], Field(description="Also save the images (look_01.png, ...) into this directory")] = None,
    timeout_s: Annotated[float, Field(ge=5, le=600)] = 120,
) -> list:
    """See the scene as images. For counts, sizes and positions use get_scene_info instead.

    mode:
    - viewport (default): the live 3D viewport exactly as the user sees it (needs a 3D Viewport open).
    - camera: through the scene camera, at the render aspect ratio.
    - angles: the target from several sides, auto-framed (default front, right, top, three_quarter).
    - frames: the animation sampled over time, seen through `view`.
    The last three render with the Workbench engine through temporary cameras (removed afterwards, your
    settings are restored), which is quick and needs no lighting setup. shading: solid (default, your materials'
    base colours), textured, clay (one neutral colour, best to judge form), flat (unlit), xray (see-through).
    For a fully shaded, lit result use render_image. Blender is busy while it renders."""
    if mode == "viewport":
        if shading or target or views or frames or view or distance:
            raise ToolError("mode 'viewport' shows the live viewport as it is; target / views / shading / frames / "
                            "view / distance only apply to the modes camera, angles and frames")
        note, images = await _look_viewport(max_size)
    else:
        shading = shading or "solid"
        need_bbox = mode in ("angles", "frames")
        info = await _query(SNIP_LOOK_TARGET, {"targets": target, "need_bbox": need_bbox})
        scene_res, cams, frame_list = info["resolution"], [], None
        subject = ", ".join(info.get("objects", [])[:4]) + ("…" if info.get("object_count", 0) > 4 else "")
        if mode == "camera":
            if not info["scene_camera"]:
                raise ToolError("The scene has no active camera. Create one, or use mode 'angles'.")
            cams = [{"label": f"camera ({info['scene_camera']})", "scene_camera": True}]
            resolution, note = _look_resolution(max_size, scene_res), "Through the scene camera."
        elif mode == "angles":
            chosen = views or LOOK_DEFAULT_ANGLES
            if len(chosen) > 6:
                raise ToolError("At most 6 views per call")
            cams = _look_cameras(info["center"], info["radius"], chosen, distance)
            resolution = [max_size, max_size]
            note = (f"{', '.join(c['label'] for c in cams)} views of {subject or 'the scene'} "
                    f"(size {_vec(info['size'])} m). Images are in that order.")
        else:  # frames
            chosen_view = view if view is not None else ("camera" if info["scene_camera"] else "three_quarter")
            frame_list = _look_frames(frames, frame_count, info["frame_start"], info["frame_end"])
            if isinstance(chosen_view, str) and chosen_view.lower() == "camera":
                if not info["scene_camera"]:
                    raise ToolError("The scene has no active camera; pass view='three_quarter' (or another angle)")
                cams = [{"label": f"camera ({info['scene_camera']})", "scene_camera": True}]
                resolution = _look_resolution(max_size, scene_res)
            else:
                cams = _look_cameras(info["center"], info["radius"], [chosen_view], distance)
                resolution = [max_size, max_size]
            note = f"Frames {', '.join(str(f) for f in frame_list)} seen from {cams[0]['label']}. Images are in that order."
        images = await _look_render(shading, resolution, cams, frame_list, timeout_s)
    if save_dir:
        dest = _path(save_dir)
        dest.mkdir(parents=True, exist_ok=True)
        for i, (_, data) in enumerate(images, 1):
            (dest / f"look_{i:02d}.png").write_bytes(data)
    return [note, *(Image(data=data, format="png") for _, data in images)]


@_tool(read_only=False, destructive=False)
async def render_image(
    output_path: Annotated[str, Field(description="Where to write the image (.png .jpg .exr .tif). Relative paths resolve against the project.")],
    resolution_percentage: Annotated[int, Field(ge=1, le=200, description="Percent of the scene's render resolution")] = 50,
    samples: Annotated[Optional[int], Field(ge=1, le=4096, description="Override sample count (Cycles / EEVEE)")] = None,
    engine: Annotated[Optional[Literal["CYCLES", "EEVEE", "WORKBENCH"]], Field(description="Override the render engine for this render only")] = None,
    camera: Annotated[Optional[str], Field(description="Camera object to use instead of the scene camera")] = None,
    return_image: bool = True,
    timeout_s: Annotated[float, Field(ge=5, le=3600)] = 300,
) -> list:
    """Render the scene from the active camera to a file. Blender is frozen while it renders, so start
    with a low percentage/samples, or use WORKBENCH / EEVEE for previews. Scene render settings are
    restored afterwards. For a quick look at the work in progress prefer look."""
    dest = _path(output_path)
    params = dict(path=str(dest), percentage=resolution_percentage, samples=samples, engine=engine, camera=camera)
    info = await _query(SNIP_RENDER, params, timeout=timeout_s)
    out: list = [_compact(info)]
    if return_image:
        try:
            data = dest.read_bytes()
        except OSError:
            data = b""
        if dest.suffix.lower() in (".png", ".jpg", ".jpeg") and 0 < len(data) <= MAX_IMAGE_BYTES:
            out.append(Image(data=data, format="png" if dest.suffix.lower() == ".png" else "jpeg"))
        else:
            out[0] = _compact({**info, "note": "Image not inlined (unsupported format or too large); open the file."})
    return out


# --------------------------------------------------------------------------- tools: code


@_tool(read_only=False)
async def execute_blender_code(
    code: Annotated[str, Field(description="Python to run inside Blender. print() what you want returned.")],
    timeout_s: Annotated[float, Field(ge=1, le=3600)] = 30,
) -> str:
    """Run Python inside Blender (bpy, bmesh, math and mathutils are preloaded) and return its stdout.
    Creates an undo step first. Good for short experiments; for longer code use run_blender_script.
    Anything Python can do is possible, so be careful with file and data deletion. Fails if 'Allow
    code execution' is switched off in the add-on panel. With BLENDER_MCP_SAFE_MODE=1 the code is
    checked first and anything outside bpy / bmesh / mathutils and basic modules is refused."""
    _guard_code(code)
    out = await _run_py(_PRELUDE + code, timeout_s)  # same line: tracebacks keep the user's line numbers
    return _clip(out) if out.strip() else "(executed without output; use print() to return values)"


@_tool(read_only=False)
async def run_blender_script(
    path: Annotated[str, Field(description="Path to a .py file. Relative paths resolve against the project.")],
    args: Annotated[Optional[dict], Field(description="JSON-serialisable dict exposed to the script as SCRIPT_ARGS")] = None,
    timeout_s: Annotated[float, Field(ge=1, le=3600)] = 120,
) -> str:
    """Run a Python file from disk inside Blender and return its stdout. The file is compiled with its real
    path, so tracebacks show your file names and line numbers; __file__ is set, its directory is added to
    sys.path (imported helper modules are cached by Python: restart or reload them after editing), and
    SCRIPT_ARGS carries args. Preferred over execute_blender_code for anything non-trivial because the code
    lives in the repo and can be edited and re-run."""
    script = _path(path)
    try:
        source = script.read_text(encoding="utf-8")
    except OSError as e:
        raise ToolError(f"Cannot read script {script}: {e}") from None
    _guard_code(source)
    params = {"path": str(script), "source": source, "args": args or {}}
    out = await _run_py(_header(params) + SNIP_RUN_SCRIPT, timeout_s)
    return _clip(out) if out.strip() else f"(ran {script.name} without output)"


@_tool(read_only=False)
async def save_blend(
    filepath: Annotated[Optional[str], Field(description="Target .blend path. Omit to save over the current file.")] = None,
    copy: Annotated[bool, Field(description="Save a copy without switching the open session to the new file")] = False,
) -> str:
    """Save the .blend file. Without a path it overwrites the currently open file (errors if the scene was
    never saved). Only call it when the user wants the work saved; use copy=True for snapshots."""
    params = {"path": str(_path(filepath)) if filepath else None, "copy": copy}
    return _compact(await _query(SNIP_SAVE, params, timeout=120))


@_tool(read_only=False, destructive=False)
async def export_scene(
    filepath: Annotated[str, Field(description="Output file; the extension picks the format: .glb .gltf .fbx .obj .stl .ply .usd .usda .usdc")],
    selected_only: bool = False,
    timeout_s: Annotated[float, Field(ge=5, le=3600)] = 120,
) -> str:
    """Export the scene (or only the selection) to a file in your project, e.g. a .glb for a web app or
    game engine. Relative paths resolve against the project."""
    params = {"path": str(_path(filepath)), "selected_only": selected_only}
    return _compact(await _query(SNIP_EXPORT, params, timeout=timeout_s))


# --------------------------------------------------------------------------- assets
#
# One search tool and one import tool for three libraries. Poly Haven and Poly Pizza are queried from this
# server (no work on Blender's UI thread, richer filtering); Sketchfab goes through the add-on because that is
# where its API key lives. Only the final import runs inside Blender.

POLYHAVEN_API = "https://api.polyhaven.com"
POLYPIZZA_API = "https://api.poly.pizza/v1.1"
ASSET_SOURCES = ("polyhaven", "sketchfab", "polypizza")
POLYHAVEN_TYPES = {0: "hdris", 1: "textures", 2: "models"}
POLYHAVEN_TTL = 600.0
THUMBNAIL_HOSTS = ("polyhaven.com", "sketchfab.com", "poly.pizza")
_polyhaven_cache: dict[str, tuple[float, dict]] = {}

# Category ids and names are the Poly Pizza API's; the aliases are everyday words people use for them.
POLYPIZZA_CATEGORIES = {
    "Food & Drink": 0, "Clutter": 1, "Weapons": 2, "Transport": 3, "Furniture & Decor": 4, "Objects": 5,
    "Nature": 6, "Animals": 7, "Buildings": 8, "People & Characters": 9, "Scenes & Levels": 10, "Other": 11,
}
POLYPIZZA_ALIASES = {
    "food": 0, "drink": 0, "drinks": 0, "meal": 0, "snack": 0,
    "junk": 1, "mess": 1,
    "weapon": 2, "gun": 2, "sword": 2,
    "vehicle": 3, "vehicles": 3, "car": 3, "cars": 3, "transportation": 3,
    "furniture": 4, "decor": 4, "decoration": 4, "interior": 4,
    "object": 5, "prop": 5, "props": 5, "tool": 5,
    "plant": 6, "plants": 6, "tree": 6, "trees": 6, "terrain": 6,
    "animal": 7, "pet": 7,
    "building": 8, "architecture": 8, "house": 8,
    "person": 9, "character": 9, "characters": 9, "people": 9, "human": 9,
    "scene": 10, "scenes": 10, "level": 10, "levels": 10, "environment": 10,
    "misc": 11, "miscellaneous": 11,
}
_SYNONYMS = {
    "couch": ("sofa",), "sofa": ("couch",), "tv": ("television",), "television": ("tv",),
    "bin": ("trash", "garbage"), "trash": ("bin", "garbage"), "garbage": ("bin", "trash"),
    "bike": ("bicycle",), "bicycle": ("bike",), "rock": ("stone",), "stone": ("rock",),
    "cupboard": ("cabinet", "wardrobe"), "cabinet": ("cupboard",), "carpet": ("rug",), "rug": ("carpet",),
    "sea": ("ocean",), "ocean": ("sea",), "lorry": ("truck",), "truck": ("lorry",),
}
_STOPWORDS = frozenset({"a", "an", "the", "of", "for", "with", "and", "in", "on"})

SNIP_FIT = """
objs = [bpy.data.objects[n] for n in _P["names"] if n in bpy.data.objects]
dg = bpy.context.evaluated_depsgraph_get()
GEO = ("MESH", "CURVE", "SURFACE", "FONT", "META", "LATTICE", "ARMATURE", "VOLUME", "POINTCLOUD", "CURVES", "GREASEPENCIL")

def bbox():
    bpy.context.view_layer.update()
    pts = []
    for o in objs:
        if o.type in GEO:
            try:
                e = o.evaluated_get(bpy.context.evaluated_depsgraph_get())
                pts += [e.matrix_world @ mathutils.Vector(c) for c in e.bound_box]
            except Exception:
                pts += [o.matrix_world @ mathutils.Vector(c) for c in o.bound_box]
    if not pts:
        return None
    lo = [min(p[i] for p in pts) for i in range(3)]
    hi = [max(p[i] for p in pts) for i in range(3)]
    return lo, hi

def build():
    if not objs:
        return {"error": "None of the imported objects were found in the scene"}
    box = bbox()
    if box is None:
        return {"objects": [o.name for o in objs], "note": "no geometry to measure"}
    lo, hi = box
    dims = [hi[i] - lo[i] for i in range(3)]
    applied = None
    target = _P.get("target_size")
    if target and max(dims) > 1e-9:
        factor = target / max(dims)
        names = set(o.name for o in objs)
        for root in [o for o in objs if o.parent is None or o.parent.name not in names]:
            root.scale = root.scale * factor
        applied = factor
        lo, hi = bbox()
        dims = [hi[i] - lo[i] for i in range(3)]
    return {"objects": [o.name for o in objs][:30], "object_count": len(objs),
            "dimensions_m": [round(d, 3) for d in dims], "bbox_min": [round(x, 3) for x in lo],
            "bbox_max": [round(x, 3) for x in hi],
            "scale_applied": round(applied, 6) if applied else None}

print(json.dumps(build()))
"""

SNIP_MATERIAL = """
mat = bpy.data.materials.get(_P["material"])
if mat is None:
    print(json.dumps({"error": "Material not found: " + _P["material"]}))
else:
    done, skipped = [], []
    for n in _P["apply_to"]:
        o = bpy.data.objects.get(n)
        if o is None or not hasattr(o.data, "materials"):
            skipped.append(n)
            continue
        o.data.materials.clear()
        o.data.materials.append(mat)
        done.append(n)
    if not _P["apply_to"]:
        mat.use_fake_user = True  # otherwise an unused material is dropped when the file is saved
    print(json.dumps({"material": mat.name, "assigned": done, "skipped": skipped, "kept_with_fake_user": bool(mat.use_fake_user)}))
"""

SNIP_IMPORT_FILE = """
import os
before = set(bpy.data.objects.keys())
path, kind = _P["path"], _P["kind"]
if kind == "gltf":
    bpy.ops.import_scene.gltf(filepath=path)
elif kind == "fbx":
    bpy.ops.import_scene.fbx(filepath=path)
elif kind == "obj":
    if hasattr(bpy.ops.wm, "obj_import"):
        bpy.ops.wm.obj_import(filepath=path)
    else:
        bpy.ops.import_scene.obj(filepath=path)
else:
    raise Exception("Unknown file kind: " + kind)
new = [o for o in bpy.data.objects if o.name not in before]
# Images that still point into the download folder would break when it is deleted: embed them in the file
root_dir = os.path.normcase(os.path.realpath(_P["pack_dir"]))
for img in bpy.data.images:
    try:
        if img.packed_file is None and img.filepath:
            if os.path.normcase(os.path.realpath(bpy.path.abspath(img.filepath))).startswith(root_dir):
                img.pack()
    except Exception:
        pass
names = [o.name for o in new]
roots = [o for o in new if o.parent is None or o.parent.name not in names]
for o in roots:
    for key, value in _P["props"].items():
        o[key] = value
if _P.get("name") and len(roots) == 1:
    roots[0].name = _P["name"]
print(json.dumps({"names": [o.name for o in new], "roots": [o.name for o in roots]}))
"""

SNIP_HDRI = """
scene = bpy.context.scene
img = bpy.data.images.load(_P["path"])
img.name = _P["name"]
try:
    img.pack()
except Exception:
    pass
world = scene.world
if world is None:
    world = bpy.data.worlds.new("World")
    scene.world = world
try:
    world.use_nodes = True
except Exception:
    pass
nt = world.node_tree
for n in list(nt.nodes):
    nt.nodes.remove(n)
coord = nt.nodes.new("ShaderNodeTexCoord")
mapping = nt.nodes.new("ShaderNodeMapping")
env = nt.nodes.new("ShaderNodeTexEnvironment")
bg = nt.nodes.new("ShaderNodeBackground")
out = nt.nodes.new("ShaderNodeOutputWorld")
env.image = img
coord.location, mapping.location, env.location, bg.location, out.location = (-800, 0), (-600, 0), (-380, 0), (-120, 0), (120, 0)
nt.links.new(coord.outputs["Generated"], mapping.inputs["Vector"])
nt.links.new(mapping.outputs["Vector"], env.inputs["Vector"])
nt.links.new(env.outputs["Color"], bg.inputs["Color"])
nt.links.new(bg.outputs["Background"], out.inputs["Surface"])
print(json.dumps({"world": world.name, "image": img.name, "size": list(img.size)}))
"""

SNIP_TEXTURE_MATERIAL = """
mat = bpy.data.materials.new(_P["name"])
try:
    mat.use_nodes = True
except Exception:
    pass
nt = mat.node_tree
for n in list(nt.nodes):
    nt.nodes.remove(n)
out = nt.nodes.new("ShaderNodeOutputMaterial")
out.location = (600, 0)
bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
bsdf.location = (300, 0)
nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
coord = nt.nodes.new("ShaderNodeTexCoord")
coord.location = (-1000, 0)
mapping = nt.nodes.new("ShaderNodeMapping")
mapping.location = (-800, 0)
nt.links.new(coord.outputs["UV"], mapping.inputs["Vector"])

def tex(path, label, colorspace, y):
    img = bpy.data.images.load(path)
    img.colorspace_settings.name = colorspace
    try:
        img.pack()
    except Exception:
        pass
    node = nt.nodes.new("ShaderNodeTexImage")
    node.image = img
    node.label = label
    node.location = (-500, y)
    nt.links.new(mapping.outputs["Vector"], node.inputs["Vector"])
    return node

maps, used = _P["maps"], []
if "diff" in maps:
    n = tex(maps["diff"], "Base Color", "sRGB", 300)
    nt.links.new(n.outputs["Color"], bsdf.inputs["Base Color"])
    used.append("diff")
if "arm" in maps:
    n = tex(maps["arm"], "ARM (occlusion / roughness / metallic)", "Non-Color", 0)
    sep = nt.nodes.new("ShaderNodeSeparateColor")
    sep.location = (-200, 0)
    nt.links.new(n.outputs["Color"], sep.inputs["Color"])
    nt.links.new(sep.outputs["Green"], bsdf.inputs["Roughness"])
    nt.links.new(sep.outputs["Blue"], bsdf.inputs["Metallic"])
    used.append("arm")
else:
    if "rough" in maps:
        n = tex(maps["rough"], "Roughness", "Non-Color", 0)
        nt.links.new(n.outputs["Color"], bsdf.inputs["Roughness"])
        used.append("rough")
    if "metal" in maps:
        n = tex(maps["metal"], "Metallic", "Non-Color", -300)
        nt.links.new(n.outputs["Color"], bsdf.inputs["Metallic"])
        used.append("metal")
if "nor_gl" in maps:
    n = tex(maps["nor_gl"], "Normal (OpenGL)", "Non-Color", -600)
    nm = nt.nodes.new("ShaderNodeNormalMap")
    nm.location = (0, -600)
    nt.links.new(n.outputs["Color"], nm.inputs["Color"])
    nt.links.new(nm.outputs["Normal"], bsdf.inputs["Normal"])
    used.append("nor_gl")
print(json.dumps({"material": mat.name, "maps": used}))
"""


def _query_groups(query: str) -> list[set[str]]:
    """Each search word becomes a set of acceptable alternatives (synonyms, singular form)."""
    groups = []
    for word in re.findall(r"[a-z0-9]+", (query or "").lower()):
        if word in _STOPWORDS:
            continue
        alts = {word, *_SYNONYMS.get(word, ())}
        if len(word) > 3 and word.endswith("s"):
            alts.add(word[:-1])
        groups.append(alts)
    return groups


def _polyhaven_score(entry: dict, groups: list[set[str]]) -> int:
    """0 = not a match. Every word must be found; matches in the name count most, then tags/categories."""
    name = str(entry.get("name", "")).lower()
    tags = [str(t).lower() for t in entry.get("tags", [])]
    cats = [str(c).lower() for c in entry.get("categories", [])] + [str(entry.get("category", "")).lower()]
    desc = str(entry.get("description", "")).lower()
    total = 0
    for alts in groups:
        best = 0
        for a in alts:
            if a in name:
                best = max(best, 3)
            elif any(a in t for t in tags) or any(a in c for c in cats):
                best = max(best, 2)
            elif a in desc:
                best = max(best, 1)
        if not best:
            return 0
        total += best
    return total


def _polypizza_id(table_value: Optional[str]) -> Optional[int]:
    if not table_value:
        return None
    key = "".join(ch for ch in str(table_value).lower() if ch.isalnum())
    for name, value in POLYPIZZA_CATEGORIES.items():
        if "".join(ch for ch in name.lower() if ch.isalnum()) == key:
            return value
    if key in POLYPIZZA_ALIASES:
        return POLYPIZZA_ALIASES[key]
    raise ToolError(f"Unknown Poly Pizza category '{table_value}'. Valid categories: {', '.join(POLYPIZZA_CATEGORIES)}")





def _thumbnail_allowed(url: Any) -> bool:
    try:
        parsed = httpx.URL(str(url))
    except Exception:
        return False
    host = parsed.host or ""
    return parsed.scheme == "https" and any(host == h or host.endswith("." + h) for h in THUMBNAIL_HOSTS)


async def _fetch_images(urls: list[str]) -> list:
    """Thumbnails as Image objects. Only https URLs on the libraries' own hosts, no redirects, size-capped."""
    images = []
    for url in urls:
        if not _thumbnail_allowed(url):
            continue
        try:
            r = await _http_get(url, timeout=15, redirects=False)
        except ToolError:
            continue
        ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
        if r.status_code == 200 and ctype in ("image/png", "image/jpeg", "image/webp", "image/gif") \
                and len(r.content) <= MAX_IMAGE_BYTES:
            images.append(Image(data=r.content, format=ctype.split("/")[1]))
    return images


async def _search_polyhaven(query: str, asset_type: str, category: Optional[str], min_size_m: Optional[float],
                            limit: int) -> tuple[str, list[str]]:
    key = f"{asset_type}|{category or ''}"
    cached = _polyhaven_cache.get(key)
    if cached and time.monotonic() - cached[0] < POLYHAVEN_TTL:
        data = cached[1]
    else:
        params = {}
        if asset_type != "all":
            params["t"] = asset_type
        if category:
            params["c"] = category
        r = await _http_get(f"{POLYHAVEN_API}/assets", params=params)
        if r.status_code != 200:
            raise ToolError(f"Poly Haven API returned HTTP {r.status_code}")
        data = r.json()
        _polyhaven_cache[key] = (time.monotonic(), data)

    groups = _query_groups(query)
    scored = []
    for asset_id, e in data.items():
        if not isinstance(e, dict):
            continue
        dims = e.get("dimensions")
        if min_size_m and not (dims and max(dims) / 1000 >= min_size_m):
            continue
        score = _polyhaven_score(e, groups) if groups else 1
        if score:
            scored.append((score, e.get("download_count", 0), asset_id, e))
    scored.sort(key=lambda t: (-t[0], -t[1], t[2]))

    lines, thumbs = [], []
    for _, _, asset_id, e in scored[:limit]:
        kind = POLYHAVEN_TYPES.get(e.get("type"), "?")
        bits = [asset_id, str(e.get("name", "")), kind, str(e.get("category") or ",".join(e.get("categories", [])))]
        if e.get("polycount"):
            bits.append(f"{e['polycount']} polys")
        if e.get("dimensions"):
            size = " x ".join(_num(round(d / 1000, 2)) for d in e["dimensions"])
            bits.append(("size " if kind == "models" else "tile ") + size + " m")
        bits.append("tags: " + ", ".join(e.get("tags", [])[:6]))
        lines.append(" | ".join(bits))
        thumbs.append(f"https://cdn.polyhaven.com/asset_img/thumbs/{asset_id}.png?width=256&height=256")
    head = f"Poly Haven (CC0): {len(scored)} match(es)" + (f", showing {len(lines)}" if len(scored) > len(lines) else "")
    tail = "import_asset(source='polyhaven', id=<first column>, asset_type=<hdris|textures|models>)"
    return "\n".join([head, *lines, tail]) if lines else head + ". Try other words or a broader category.", thumbs


SKETCHFAB_API = "https://api.sketchfab.com/v3"


def _sketchfab_headers() -> dict:
    key = _need(SKETCHFAB_API_KEY, "Sketchfab", "SKETCHFAB_API_KEY",
                "Create an API token in your Sketchfab account settings (Password & API).")
    return {"Authorization": f"Token {key}"}


def _sketchfab_check(r: httpx.Response, what: str) -> None:
    if r.status_code == 401:
        raise ToolError(f"Sketchfab authentication failed for {what}. Check SKETCHFAB_API_KEY.")
    if r.status_code == 403:
        raise ToolError(f"Sketchfab refused {what}: the model is probably not downloadable with your account")
    if r.status_code == 404:
        raise ToolError(f"Sketchfab: {what} not found")
    if r.status_code == 429:
        raise ToolError("Sketchfab rate limit reached; try again in a moment")
    if r.status_code != 200:
        raise ToolError(f"Sketchfab returned HTTP {r.status_code} for {what}")


async def _search_sketchfab(query: str, category: Optional[str], limit: int) -> tuple[str, list[str]]:
    headers = _sketchfab_headers()
    params = {"type": "models", "q": query, "count": max(1, min(limit, 24)), "downloadable": "true",
              "archives_flavours": "false"}
    if category:
        params["categories"] = category
    r = await _http_get(f"{SKETCHFAB_API}/search", params=params, headers=headers)
    _sketchfab_check(r, "search")
    lines, thumbs = [], []
    for m in (r.json() or {}).get("results", []):
        lic = m.get("license")
        images = (m.get("thumbnails") or {}).get("images") or []
        pick = next((i for i in sorted(images, key=lambda i: i.get("width", 0)) if i.get("width", 0) >= 200),
                    images[0] if images else None)
        thumbs.append((pick or {}).get("url", ""))
        lines.append(" | ".join([
            str(m.get("uid")), str(m.get("name")), "by " + str((m.get("user") or {}).get("username")),
            f"{m.get('faceCount')} faces", "licence: " + str(lic.get("label") if isinstance(lic, dict) else lic)]))
    if not lines:
        return "Sketchfab: no downloadable results. Try other words.", []
    return "\n".join([f"Sketchfab: {len(lines)} result(s). Check each licence before using a model.", *lines,
                      "import_asset(source='sketchfab', id=<uid>, target_size=<metres>)"]), thumbs


def _polypizza_headers() -> dict:
    if not POLYPIZZA_API_KEY:
        raise ToolError(
            "Poly Pizza needs a free API key: create one at https://poly.pizza/settings/api and give it to this "
            "server as POLYPIZZA_API_KEY (e.g. claude mcp add blender -e POLYPIZZA_API_KEY=... -- uv run ...)")
    return {"x-auth-token": POLYPIZZA_API_KEY}


def _polypizza_check(r: httpx.Response, what: str) -> None:
    if r.status_code in (401, 403):
        raise ToolError(f"Poly Pizza authentication failed ({r.status_code}) for {what}. Check POLYPIZZA_API_KEY.")
    if r.status_code == 400:
        raise ToolError(f"Poly Pizza rejected the {what} request (400): check the category and licence values.")
    if r.status_code == 404:
        raise ToolError(f"Poly Pizza: {what} not found")
    if r.status_code == 429:
        raise ToolError("Poly Pizza rate limit exceeded; try again in a moment")
    if r.status_code != 200:
        raise ToolError(f"Poly Pizza API returned HTTP {r.status_code} for {what}")


async def _search_polypizza(query: str, category: Optional[str], licence: Optional[str], animated: bool,
                            limit: int) -> tuple[str, list[str]]:
    headers = _polypizza_headers()
    params: dict[str, Any] = {"Limit": max(1, min(limit, 32))}  # the API ignores lowercase parameter names
    cat = _polypizza_id(category)
    if cat is not None:
        params["Category"] = cat
    if licence:
        params["License"] = 1 if licence.upper() == "CC0" else 0
    if animated:
        params["Animated"] = 1
    keyword = (query or "").strip()
    if not keyword and len(params) == 1:
        raise ToolError("Poly Pizza needs a search word or at least one filter (category, licence, animated)")
    url = f"{POLYPIZZA_API}/search/{quote(keyword, safe='')}" if keyword else f"{POLYPIZZA_API}/search"
    r = await _http_get(url, params=params, headers=headers)
    _polypizza_check(r, "search")
    results = (r.json() or {}).get("results", [])
    lines, thumbs = [], []
    for m in results:
        if not isinstance(m, dict):
            continue
        creator = m.get("Creator")
        lines.append(" | ".join([
            str(m.get("ID")), str(m.get("Title")),
            "by " + str(creator.get("Username") if isinstance(creator, dict) else creator),
            str(m.get("Licence")), f"{m.get('Tri Count')} tris",
            "animated" if m.get("Animated") else "static", ",".join(m.get("Tags") or [])[:60]]))
        thumbs.append(m.get("Thumbnail") or "")
    if not lines:
        return "Poly Pizza: no results.", []
    return "\n".join([f"Poly Pizza (low-poly; CC-BY models need credit to the creator): {len(lines)} result(s)", *lines,
                      "import_asset(source='polypizza', id=<first column>, target_size=<metres>)"]), thumbs


@_tool(read_only=True, open_world=True)
async def search_assets(
    source: Annotated[Literal["polyhaven", "sketchfab", "polypizza"], Field(description="Which library to search")],
    query: Annotated[str, Field(description="What you are looking for, in plain words ('worn leather armchair')")] = "",
    asset_type: Annotated[Literal["all", "hdris", "textures", "models"], Field(description="polyhaven only")] = "all",
    category: Annotated[Optional[str], Field(description="polyhaven: category slug(s), e.g. 'furniture' or 'wood'. sketchfab: category slug(s). polypizza: e.g. 'Furniture & Decor', 'Nature', 'Animals'")] = None,
    min_size_m: Annotated[Optional[float], Field(gt=0, description="polyhaven only: minimum real-world size in metres. Use 2+ for floors, walls and ground so textures do not visibly repeat")] = None,
    licence: Annotated[Optional[Literal["CC0", "CC-BY"]], Field(description="polypizza only")] = None,
    animated: Annotated[bool, Field(description="polypizza only: animated models only")] = False,
    limit: Annotated[int, Field(ge=1, le=40)] = 15,
    previews: Annotated[int, Field(ge=0, le=6, description="Attach thumbnails of the first N results as images")] = 0,
) -> Union[str, list]:
    """Search a library of existing assets and list the best matches (one line each, with the id that
    import_asset needs).
    - polyhaven: HDRIs, PBR textures and realistic models, all CC0 (free, no key). Matches words against
      names, tags, categories and descriptions, understands simple synonyms (couch finds sofas), and
      knows real-world sizes.
    - sketchfab: a large catalogue of user-made models, including very specific ones; licences and face
      counts vary per model. Needs SKETCHFAB_API_KEY in this server's environment.
    - polypizza: stylised low-poly models, CC0 or CC-BY (credit the creator). Needs POLYPIZZA_API_KEY in
      this server's environment (free key at poly.pizza/settings/api).
    Ask for previews=3 when you need to judge how something looks before importing."""
    if source == "polyhaven":
        text, thumbs = await _search_polyhaven(query, asset_type, category, min_size_m, limit)
    elif source == "sketchfab":
        text, thumbs = await _search_sketchfab(query, category, limit)
    else:
        text, thumbs = await _search_polypizza(query, category, licence, animated, limit)
    images = await _fetch_images(thumbs[:previews]) if previews else []
    return [text, *images] if images else text


async def _fit(names: list[str], target_size: Optional[float]) -> dict:
    return await _query(SNIP_FIT, {"names": names, "target_size": target_size})


def _res_key(resolution: Any) -> int:
    try:
        return int(str(resolution).lower().rstrip("k"))
    except ValueError:
        return 0


def _available(by_res: dict) -> str:
    return ", ".join(sorted(by_res, key=_res_key)) or "none"


async def _import_polyhaven(id: str, asset_type: Optional[str], target_size: Optional[float],
                            apply_to: Optional[list[str]], resolution: str, file_format: Optional[str],
                            timeout: float) -> dict:
    quoted = quote(id, safe="")
    if not asset_type:
        r = await _http_get(f"{POLYHAVEN_API}/info/{quoted}")
        if r.status_code != 200:
            raise ToolError(f"Poly Haven has no asset '{id}' (HTTP {r.status_code}); search again for a valid id")
        asset_type = POLYHAVEN_TYPES.get(r.json().get("type"))
        if not asset_type:
            raise ToolError(f"Could not tell the type of Poly Haven asset '{id}'; pass asset_type")
    r = await _http_get(f"{POLYHAVEN_API}/files/{quoted}")
    if r.status_code == 404:
        raise ToolError(f"Poly Haven has no asset '{id}'; search again for a valid id")
    if r.status_code != 200:
        raise ToolError(f"Poly Haven API returned HTTP {r.status_code}")
    files = r.json()
    resolution = resolution.lower()
    out: dict[str, Any] = {"source": "polyhaven", "asset_type": asset_type, "licence": "CC0 (no credit required)"}
    workdir = _new_tempdir()
    try:
        if asset_type == "hdris":
            by_res = files.get("hdri") or {}
            if resolution not in by_res:
                raise ToolError(f"Resolution {resolution} is not available for this HDRI. Available: {_available(by_res)}")
            fmt = (file_format or "hdr").lower()
            entry = by_res[resolution].get(fmt)
            if not entry:
                raise ToolError(f"Format '{fmt}' is not available for this HDRI at {resolution}: {', '.join(by_res[resolution])}")
            dest = workdir / f"{id}_{resolution}.{fmt}"
            await _download(entry["url"], dest)
            out["world"] = await _query(SNIP_HDRI, {"path": str(dest), "name": id}, timeout=timeout)
            out["note"] = "The HDRI is now the world lighting."
        elif asset_type == "textures":
            wanted = [("diff", "Diffuse")] + ([("arm", "arm")] if files.get("arm") else [("rough", "Rough"), ("metal", "Metal")]) \
                + [("nor_gl", "nor_gl")]
            maps: dict[str, str] = {}
            items: list[tuple[str, Path]] = []
            for key, api_key in wanted:
                by_res = files.get(api_key)
                if not by_res:
                    if key == "diff":
                        raise ToolError("This texture has no colour (Diffuse) map")
                    continue
                if resolution not in by_res:
                    if key == "diff":
                        raise ToolError(f"Resolution {resolution} is not available for this texture. Available: {_available(by_res)}")
                    continue
                formats = ([file_format.lower()] if file_format else []) + ["jpg", "png"]
                fmt = next((f for f in formats if f in by_res[resolution]), None)
                if fmt is None:
                    if key == "diff":
                        raise ToolError(f"Format not available for this texture at {resolution}: {', '.join(by_res[resolution])}")
                    continue
                dest = workdir / f"{key}_{resolution}.{fmt}"
                items.append((by_res[resolution][fmt]["url"], dest))
                maps[key] = str(dest)
            await _download_all(items)
            created = await _query(SNIP_TEXTURE_MATERIAL, {"name": id, "maps": maps}, timeout=timeout)
            assigned = await _query(SNIP_MATERIAL, {"material": created["material"], "apply_to": apply_to or []})
            out["material"] = {**assigned, "maps": created["maps"]}
            if not apply_to:
                out["note"] = "Material created but not assigned to anything; pass apply_to=[object names] to use it."
        elif asset_type == "models":
            fmt = (file_format or "gltf").lower()
            if fmt not in ("gltf", "fbx"):
                raise ToolError("Poly Haven models: file_format must be 'gltf' (default) or 'fbx'")
            by_res = files.get(fmt) or {}
            entry = (by_res.get(resolution) or {}).get(fmt)
            if not entry:
                raise ToolError(f"No {fmt} version of this model at {resolution}. Available resolutions: {_available(by_res)}")
            main = _safe_join(workdir, PurePosixPath(urlparse(entry["url"]).path).name)
            items = [(entry["url"], main)]
            for relative, extra in (entry.get("include") or {}).items():
                items.append((extra["url"], _safe_join(workdir, relative)))
            await _download_all(items)
            imported = await _query(SNIP_IMPORT_FILE, {"path": str(main), "kind": fmt, "pack_dir": str(workdir),
                                                       "props": {"polyhaven_id": id, "licence": "CC0"}, "name": None},
                                    timeout=timeout)
            out["placed"] = await _fit(imported["names"], target_size)
            out["roots"] = imported["roots"]
        else:
            raise ToolError(f"Unknown Poly Haven asset type '{asset_type}'")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return out


async def _import_sketchfab(id: str, target_size: Optional[float], timeout: float) -> dict:
    if not target_size:
        raise ToolError("target_size (metres, largest dimension) is required for sketchfab: library models come "
                        "at arbitrary scale. Examples: chair 1.0, car 4.5, cup 0.12")
    headers = _sketchfab_headers()
    quoted = quote(id, safe="")
    r = await _http_get(f"{SKETCHFAB_API}/models/{quoted}/download", headers=headers)
    _sketchfab_check(r, f"download of model '{id}'")
    url = ((r.json() or {}).get("gltf") or {}).get("url")
    if not url:
        raise ToolError("Sketchfab offers no glTF download for this model (it may not be downloadable with your account)")
    meta: dict = {}
    try:  # best effort: name, author and licence, kept on the object because most models need credit
        info = await _http_get(f"{SKETCHFAB_API}/models/{quoted}", headers=headers)
        if info.status_code == 200:
            meta = info.json() or {}
    except ToolError:
        pass
    workdir = _new_tempdir()
    try:
        archive = workdir / "model.zip"
        await _download(url, archive, timeout=max(timeout, 120))  # a signed URL: it takes no API token
        unpacked = _safe_extract(archive, workdir / "model")
        model = _first_with_suffix(unpacked, ".gltf", ".glb")
        licence = (meta.get("license") or {}).get("label") if isinstance(meta.get("license"), dict) else meta.get("license")
        author = (meta.get("user") or {}).get("username")
        props = {"sketchfab_uid": id, "sketchfab_name": str(meta.get("name") or ""), "sketchfab_author": str(author or ""),
                 "sketchfab_licence": str(licence or ""), "sketchfab_url": str(meta.get("viewerUrl") or "")}
        imported = await _query(SNIP_IMPORT_FILE, {"path": str(model), "kind": "gltf", "pack_dir": str(workdir),
                                                   "props": props, "name": None}, timeout=timeout)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    out = {"source": "sketchfab", "name": meta.get("name"), "author": author, "licence": licence,
           "placed": await _fit(imported["names"], target_size), "roots": imported["roots"]}
    if licence and "attribution" in str(licence).lower():
        out["credit_required"] = f"{meta.get('name')} by {author} ({meta.get('viewerUrl')}) - {licence}"
    return out


async def _import_polypizza(id: str, target_size: Optional[float], timeout: float) -> dict:
    headers = _polypizza_headers()
    r = await _http_get(f"{POLYPIZZA_API}/model/{quote(id, safe='')}", headers=headers)
    _polypizza_check(r, f"model '{id}'")
    model = r.json()
    url = model.get("Download") if isinstance(model, dict) else None
    if not url:
        raise ToolError(f"Poly Pizza model '{id}' has no downloadable file")
    f = await _http_get(url, timeout=60)  # the CDN is a different host and must not receive the API key
    if f.status_code != 200 or f.content[:4] != b"glTF":
        if f.status_code in (403, 503) or b"<html" in f.content[:300].lower():
            raise ToolError("Poly Pizza's CDN answered with a bot-protection page instead of the model. It blocks "
                            "datacenter/VPN IPs: retry from a normal connection, or download the .glb by hand from "
                            "https://poly.pizza and import it with File > Import > glTF 2.0.")
        raise ToolError(f"Poly Pizza returned something that is not a GLB file (HTTP {f.status_code})")
    workdir = _new_tempdir()
    try:
        glb = workdir / ((re.sub(r"[^A-Za-z0-9_-]", "_", id) or "model") + ".glb")
        glb.write_bytes(f.content)
        props = {"polypizza_id": str(model.get("ID") or id), "polypizza_licence": str(model.get("Licence") or ""),
                 "polypizza_attribution": str(model.get("Attribution") or "")}
        imported = await _query(SNIP_IMPORT_FILE, {"path": str(glb), "kind": "gltf", "pack_dir": str(workdir),
                                                   "props": props, "name": None}, timeout=timeout)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    out = {"source": "polypizza", "licence": model.get("Licence"), "creator": (model.get("Creator") or {}).get("Username"),
           "placed": await _fit(imported["names"], target_size), "roots": imported["roots"]}
    if model.get("Attribution"):
        out["credit_required"] = model["Attribution"]
        out["note"] = "Attribution is also stored on the root object as the custom property 'polypizza_attribution'."
    return out


@_tool(read_only=False, destructive=False, open_world=True)
async def import_asset(
    source: Annotated[Literal["polyhaven", "sketchfab", "polypizza"], Field(description="Library the id came from")],
    id: Annotated[str, Field(description="Asset id from search_assets (the uid for sketchfab)")],
    asset_type: Annotated[Optional[Literal["hdris", "textures", "models"]], Field(description="polyhaven only; looked up automatically if omitted")] = None,
    target_size: Annotated[Optional[float], Field(gt=0, description="Size in metres of the model's largest dimension (chair 1.0, car 4.5, cup 0.12). Required for sketchfab, strongly recommended for polypizza and polyhaven models: library models come at arbitrary scale")] = None,
    apply_to: Annotated[Optional[list[str]], Field(description="polyhaven textures: object names to put the material on (replaces their materials)")] = None,
    resolution: Annotated[str, Field(description="polyhaven: 1k, 2k, 4k, 8k. 1k-2k for background, 4k for close-ups")] = "1k",
    file_format: Annotated[Optional[str], Field(description="polyhaven, optional: hdr/exr for HDRIs, jpg/png/exr for textures")] = None,
    timeout_s: Annotated[float, Field(ge=10, le=1800)] = 240,
) -> str:
    """Download an asset found with search_assets and bring it into the scene. HDRIs become the world
    lighting, textures become a PBR material (assign it with apply_to), models are imported and scaled to
    target_size. Reports the final dimensions and bounding box. Afterwards check that the object sits on
    the ground, and look at it. Imports can freeze Blender for a few seconds."""
    if source == "polyhaven":
        out = await _import_polyhaven(id, asset_type, target_size, apply_to, resolution, file_format, timeout_s)
    elif source == "sketchfab":
        out = await _import_sketchfab(id, target_size, timeout_s)
    else:
        out = await _import_polypizza(id, target_size, timeout_s)
    return _compact(out)


# --------------------------------------------------------------------------- tools: AI 3D generation
#
# Generation is asynchronous on the provider side. The tools hide the differences between Hyper3D Rodin
# (hyper3d.ai / fal.ai) and Hunyuan3D (Tencent cloud / your own server) behind an opaque job handle:
#   rodin|main|<subscription_key>|<uuid>     rodin|fal|<request_id>     hunyuan|<job_id>
# The server talks to the providers itself (keys come from environment variables) and only the finished
# model is imported into Blender, so Blender is never frozen while waiting for a provider.

RODIN_POLL_SECONDS = 8
HUNYUAN_POLL_SECONDS = 10
HYPER3D_API = "https://hyperhuman.deemos.com/api/v2"
FAL_RODIN_API = "https://queue.fal.run/fal-ai/hyper3d"
RODIN_TIER = "Sketch"
RODIN_MESH_MODE = "Raw"
TENCENT_SERVICE, TENCENT_VERSION, TENCENT_REGION = "hunyuan", "2023-09-01", "ap-guangzhou"
MAX_REFERENCE_IMAGE_BYTES = 10 * 1024 * 1024


def _tc3_headers(secret_id: str, secret_key: str, action: str, payload: dict, *, timestamp: Optional[int] = None,
                 service: str = TENCENT_SERVICE, version: str = TENCENT_VERSION,
                 region: str = TENCENT_REGION) -> tuple[dict, str, str]:
    """Sign a Tencent Cloud API call (TC3-HMAC-SHA256, https://cloud.tencent.com/document/api/213/30654).
    Returns (headers, body, endpoint); `body` is exactly the text that was signed and must be sent as is."""
    ts = int(time.time() if timestamp is None else timestamp)
    date = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
    host = f"{service}.tencentcloudapi.com"
    body = json.dumps(payload)
    content_type = "application/json; charset=utf-8"
    signed_headers = "content-type;host;x-tc-action"
    canonical_request = "\n".join([
        "POST", "/", "",
        f"content-type:{content_type}\nhost:{host}\nx-tc-action:{action.lower()}\n",
        signed_headers,
        hashlib.sha256(body.encode("utf-8")).hexdigest(),
    ])
    scope = f"{date}/{service}/tc3_request"
    string_to_sign = "\n".join(["TC3-HMAC-SHA256", str(ts), scope, hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()])

    def hmac_sha256(key: bytes, message: str) -> bytes:
        return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()

    signing_key = hmac_sha256(hmac_sha256(hmac_sha256(("TC3" + secret_key).encode("utf-8"), date), service), "tc3_request")
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    headers = {
        "Authorization": f"TC3-HMAC-SHA256 Credential={secret_id}/{scope}, SignedHeaders={signed_headers}, Signature={signature}",
        "Content-Type": content_type,
        "Host": host,
        "X-TC-Action": action,
        "X-TC-Timestamp": str(ts),
        "X-TC-Version": version,
        "X-TC-Region": region,
    }
    return headers, body, f"https://{host}"


def _need(value: str, provider: str, variable: str, how: str) -> str:
    if not value:
        raise ToolError(f"{provider} is not configured: {how} Give it to this server as {variable} "
                        f"(for example: claude mcp add blender -e {variable}=... -- uv run ...).")
    return value


def _json_or_error(r: httpx.Response, what: str) -> Any:
    if r.status_code in (401, 403):
        raise ToolError(f"{what}: the API key or credentials were rejected (HTTP {r.status_code})")
    if r.status_code == 429:
        raise ToolError(f"{what}: rate limit reached or out of credits (HTTP 429)")
    try:
        data = r.json()
    except ValueError:
        raise ToolError(f"{what}: unexpected non-JSON reply (HTTP {r.status_code})") from None
    if r.status_code >= 400:
        raise ToolError(f"{what}: HTTP {r.status_code}: {_compact(data)[:300]}")
    return data


def _read_local_images(paths: Optional[list[str]]) -> list[tuple[str, bytes]]:
    out = []
    for p in paths or []:
        f = _path(p)
        try:
            data = f.read_bytes()
        except OSError as e:
            raise ToolError(f"Cannot read image {f}: {e}") from None
        if len(data) > MAX_REFERENCE_IMAGE_BYTES:
            raise ToolError(f"Image {f.name} is larger than 10 MB; downscale it first")
        out.append((f.suffix.lower() or ".png", data))
    return out


async def _fetch_image(url: str) -> tuple[str, bytes]:
    r = await _http_get(url, timeout=60)
    if r.status_code != 200 or not r.content:
        raise ToolError(f"Could not download the reference image (HTTP {r.status_code}): {url[:100]}")
    if len(r.content) > MAX_REFERENCE_IMAGE_BYTES:
        raise ToolError("A reference image is larger than 10 MB; downscale it first")
    suffix = Path(httpx.URL(url).path).suffix.lower()
    return (suffix if suffix in (".png", ".jpg", ".jpeg", ".webp") else ".jpg"), r.content


def _data_uri(suffix: str, data: bytes) -> str:
    mime = suffix.lstrip(".").lower().replace("jpg", "jpeg") or "png"
    return f"data:image/{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _is_url(text: str) -> bool:
    return re.match(r"^https?://", text, re.IGNORECASE) is not None


def _tencent_error(resp: dict) -> Optional[ToolError]:
    err = resp.get("Error")
    if err:
        return ToolError(f"Hunyuan3D API error {err.get('Code')}: {err.get('Message')}")
    return None


async def _progress(ctx: Optional[Context], elapsed: float, total: float) -> None:
    if ctx is None:
        return
    try:
        await ctx.report_progress(min(elapsed, total), total)
    except Exception:  # progress is best-effort, never fail a job because of it
        pass


def _rodin_platform() -> str:
    return "fal" if HYPER3D_PLATFORM == "fal" else "hyper3d"


def _rodin_headers(platform: str) -> dict:
    key = _need(HYPER3D_API_KEY, "Hyper3D Rodin", "HYPER3D_API_KEY",
                "Get an API key at hyper3d.ai (or fal.ai, and set HYPER3D_PLATFORM=fal).")
    return {"Authorization": f"Key {key}" if platform == "fal" else f"Bearer {key}"}


async def _rodin_create(prompt: Optional[str], image_paths: Optional[list[str]], image_urls: Optional[list[str]],
                        bbox_condition: Optional[list[float]]) -> str:
    platform = _rodin_platform()
    headers = _rodin_headers(platform)
    local = _read_local_images(image_paths)
    urls = list(image_urls or [])
    for u in urls:
        if not _is_url(u):
            raise ToolError(f"Not an http(s) URL: {u}")
    if platform == "fal":
        body: dict[str, Any] = {"tier": RODIN_TIER}
        images = [_data_uri(s, d) for s, d in local] + urls
        if images:
            body["input_image_urls"] = images
        if prompt:
            body["prompt"] = prompt
        if bbox_condition:
            body["bbox_condition"] = bbox_condition
        data = _json_or_error(await _http_post(f"{FAL_RODIN_API}/rodin", json_body=body, headers=headers, timeout=120),
                              "Rodin (fal.ai)")
        if not (isinstance(data, dict) and data.get("request_id")):
            raise ToolError("Unexpected response from Rodin when creating the job: " + _compact(data)[:500])
        return f"rodin|fal|{data['request_id']}"
    pictures = local + [await _fetch_image(u) for u in urls]
    files: list = [("images", (f"{i:04d}{suffix}", content)) for i, (suffix, content) in enumerate(pictures)]
    files += [("tier", (None, RODIN_TIER)), ("mesh_mode", (None, RODIN_MESH_MODE))]
    if prompt:
        files.append(("prompt", (None, prompt)))
    if bbox_condition:
        files.append(("bbox_condition", (None, json.dumps(bbox_condition))))
    data = _json_or_error(await _http_post(f"{HYPER3D_API}/rodin", files=files, headers=headers, timeout=120),
                          "Rodin (hyper3d.ai)")
    key = ((data or {}).get("jobs") or {}).get("subscription_key") if isinstance(data, dict) else None
    if not (isinstance(data, dict) and data.get("uuid") and key):
        raise ToolError("Unexpected response from Rodin when creating the job (check the API key and credits): "
                        + _compact(data)[:500])
    return f"rodin|main|{key}|{data['uuid']}"


async def _rodin_glb_url(parts: list[str]) -> str:
    if parts[0] == "main":
        data = _json_or_error(await _http_post(f"{HYPER3D_API}/download", json_body={"task_uuid": parts[2]},
                                               headers=_rodin_headers("hyper3d")), "Rodin download")
        for item in (data or {}).get("list", []):
            if str(item.get("name", "")).lower().endswith(".glb") and item.get("url"):
                return item["url"]
        raise ToolError("Rodin finished but offered no .glb file: " + _compact(data)[:300])
    data = _json_or_error(await _http_get(f"{FAL_RODIN_API}/requests/{quote(parts[1], safe='')}",
                                          headers=_rodin_headers("fal")), "Rodin (fal.ai) result")
    url = ((data or {}).get("model_mesh") or {}).get("url")
    if not url:
        raise ToolError("Rodin finished but returned no model: " + _compact(data)[:300])
    return url


def _hunyuan_mode() -> str:
    choice = HUNYUAN3D_MODE
    if choice in ("official", "local"):
        return choice
    if HUNYUAN3D_SECRET_ID and HUNYUAN3D_SECRET_KEY:
        return "official"
    if HUNYUAN3D_API_URL:
        return "local"
    raise ToolError(
        "Hunyuan3D is not configured. Give this server either HUNYUAN3D_SECRET_ID and HUNYUAN3D_SECRET_KEY "
        "(Tencent Cloud) or HUNYUAN3D_API_URL (your own Hunyuan3D server, e.g. http://localhost:8081), for example: "
        "claude mcp add blender -e HUNYUAN3D_API_URL=http://localhost:8081 -- uv run ...")


async def _hunyuan_api(action: str, payload: dict) -> dict:
    sid = _need(HUNYUAN3D_SECRET_ID, "Hunyuan3D (Tencent Cloud)", "HUNYUAN3D_SECRET_ID", "Create API credentials in the Tencent Cloud console.")
    skey = _need(HUNYUAN3D_SECRET_KEY, "Hunyuan3D (Tencent Cloud)", "HUNYUAN3D_SECRET_KEY", "Create API credentials in the Tencent Cloud console.")
    headers, body, endpoint = _tc3_headers(sid, skey, action, payload)
    r = await _http_post(endpoint, content=body.encode("utf-8"), headers=headers, timeout=60)
    data = _json_or_error(r, f"Hunyuan3D {action}")
    resp = data.get("Response") if isinstance(data, dict) else None
    if not isinstance(resp, dict):
        raise ToolError(f"Hunyuan3D {action}: unexpected response: " + _compact(data)[:300])
    if (err := _tencent_error(resp)) is not None:
        raise err
    return resp


async def _poll_job(kind: str, parts: list[str]) -> tuple[str, Any]:
    """Returns (state, detail) with state in running / done / failed."""
    valid = ((kind == "rodin" and parts[:1] == ["main"] and len(parts) == 3)
             or (kind == "rodin" and parts[:1] == ["fal"] and len(parts) == 2)
             or (kind == "hunyuan" and len(parts) == 1))
    if not valid:
        raise ToolError("Invalid job handle. Pass the 'job' value returned by rodin_generate / hunyuan3d_generate unchanged.")
    try:
        if kind == "rodin" and parts[0] == "main":
            data = _json_or_error(await _http_post(f"{HYPER3D_API}/status", json_body={"subscription_key": parts[1]},
                                                   headers=_rodin_headers("hyper3d")), "Rodin status")
            statuses = [j.get("status") for j in (data or {}).get("jobs", []) if isinstance(j, dict)]
            if not statuses:
                raise ToolError(f"unexpected status reply: {_compact(data)[:300]}")
            if any(st == "Failed" for st in statuses):
                return "failed", statuses
            return ("done" if all(st == "Done" for st in statuses) else "running"), statuses
        if kind == "rodin":
            data = _json_or_error(await _http_get(f"{FAL_RODIN_API}/requests/{quote(parts[1], safe='')}/status",
                                                  headers=_rodin_headers("fal")), "Rodin (fal.ai) status")
            status = data.get("status") if isinstance(data, dict) else None
            if status == "COMPLETED":
                return "done", data
            if status in ("IN_QUEUE", "IN_PROGRESS"):
                return "running", {k: data[k] for k in ("status", "queue_position") if k in data}
            return "failed", data
        resp = await _hunyuan_api("QueryHunyuanTo3DJob", {"JobId": parts[0].removeprefix("job_")})
        status = resp.get("Status")
        if status == "DONE":
            return "done", resp
        if status == "FAIL":
            return "failed", resp.get("ErrorMessage") or resp
        return "running", {"status": status}
    except ToolError as e:
        raise ToolError(f"Status check failed: {e}. Check the job handle, API key and credits.") from None


async def _import_generated(path: Path, kind: str, name: str, workdir: Path, props: dict) -> dict:
    imported = await _query(SNIP_IMPORT_FILE, {"path": str(path), "kind": kind, "pack_dir": str(workdir),
                                               "props": props, "name": name}, timeout=180)
    placed = await _fit(imported["names"], None)
    return {"succeed": True, "name": (imported["roots"] or [None])[0], "objects": imported["names"][:20],
            "dimensions_m": placed.get("dimensions_m"), "bbox_min": placed.get("bbox_min"),
            "bbox_max": placed.get("bbox_max")}


def _first_with_suffix(files: list[Path], *suffixes: str) -> Path:
    for suffix in suffixes:
        matches = sorted((f for f in files if f.suffix.lower() == suffix), key=lambda f: len(f.parts))
        if matches:
            return matches[0]
    raise ToolError(f"The downloaded archive has no {'/'.join(suffixes)} model file")


async def _import_job(kind: str, parts: list[str], name: str, detail: Any) -> Any:
    workdir = _new_tempdir()
    try:
        if kind == "rodin":
            glb = workdir / "model.glb"
            await _download(await _rodin_glb_url(parts), glb)
            if glb.read_bytes()[:4] != b"glTF":
                raise ToolError("Rodin returned a file that is not a GLB model")
            return await _import_generated(glb, "gltf", name, workdir, {"generator": "hyper3d-rodin"})
        files = (detail or {}).get("ResultFile3Ds") or []
        chosen = next((f for f in files if str(f.get("Type", "")).upper() == "OBJ"), files[0] if files else None)
        if not chosen or not chosen.get("Url"):
            raise ToolError("Hunyuan3D finished but returned no downloadable file: " + _compact(detail)[:400])
        archive = workdir / "model.zip"
        await _download(chosen["Url"], archive)
        unpacked = _safe_extract(archive, workdir / "model")
        obj = _first_with_suffix(unpacked, ".obj")
        return await _import_generated(obj, "obj", name, workdir, {"generator": "hunyuan3d"})
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def _collect(ctx: Optional[Context], job: str, name: str, wait: bool, timeout_s: float) -> str:
    kind, *parts = job.split("|")
    interval = RODIN_POLL_SECONDS if kind == "rodin" else HUNYUAN_POLL_SECONDS
    started = time.monotonic()
    deadline = started + (timeout_s if wait else 0)
    while True:
        state, detail = await _poll_job(kind, parts)
        if state == "done":
            break
        if state == "failed":
            raise ToolError(f"Generation failed: {_compact(detail)[:500]}")
        if time.monotonic() >= deadline:
            return _compact({"state": "running", "detail": detail, "job": job,
                             "next": "Call collect_generated_model with this job later (wait=true) to import it."})
        await _progress(ctx, time.monotonic() - started, timeout_s)
        await anyio.sleep(interval)
    imported = await _import_job(kind, parts, name, detail)
    return _compact({"state": "imported", "result": imported})


@_tool(read_only=False, destructive=False, open_world=True)
async def rodin_generate(
    ctx: Context,
    prompt: Annotated[Optional[str], Field(description="Text description of the object (English works best)")] = None,
    image_paths: Annotated[Optional[list[str]], Field(description="Reference image files; relative paths resolve against the project")] = None,
    image_urls: Annotated[Optional[list[str]], Field(description="Reference images as http(s) URLs")] = None,
    bbox_condition: Annotated[Optional[list[float]], Field(description="Optional bounding-box proportions [x, y, z], e.g. [1, 1, 2] for twice as tall as wide")] = None,
    name: Annotated[str, Field(description="Name for the imported object")] = "rodin_model",
    wait: Annotated[bool, Field(description="Wait for the result and import it. If false, returns a job handle immediately")] = True,
    timeout_s: Annotated[float, Field(ge=10, le=3600, description="Maximum time to wait for the generation")] = 600,
) -> str:
    """Generate a 3D model with Hyper3D Rodin from a text prompt and/or reference images and import it into
    the scene. CONSUMES the user's Rodin credits, so confirm before calling. Generation takes a few minutes;
    if the wait times out you get a job handle to pass to collect_generated_model. Needs HYPER3D_API_KEY in
    this server's environment (HYPER3D_PLATFORM=fal to use fal.ai instead of hyper3d.ai). Blender stays
    responsive while waiting. The mesh is raw: check its scale and orientation afterwards."""
    if not prompt and not image_paths and not image_urls:
        raise ToolError("Provide a prompt and/or reference images")
    job = await _rodin_create(prompt, image_paths, image_urls, bbox_condition)
    return await _collect(ctx, job, name, wait, timeout_s)


async def _hunyuan_local(prompt: Optional[str], image: Optional[str], name: str, timeout: float, texture: bool,
                         octree_resolution: int, inference_steps: int, guidance_scale: float) -> str:
    payload: dict[str, Any] = {"octree_resolution": octree_resolution, "num_inference_steps": inference_steps,
                               "guidance_scale": guidance_scale, "texture": texture}
    if prompt:
        payload["text"] = prompt
    if image:
        suffix, data = await _fetch_image(image) if _is_url(image) else _read_local_images([image])[0]
        payload["image"] = base64.b64encode(data).decode("ascii")
    r = await _http_post(f"{HUNYUAN3D_API_URL.rstrip('/')}/generate", json_body=payload, timeout=timeout,
                         allow_http=True, allow_local=True)
    if r.status_code != 200:
        raise ToolError(f"The local Hunyuan3D server failed (HTTP {r.status_code}): {r.text[:300]}")
    if r.content[:4] != b"glTF":
        raise ToolError("The local Hunyuan3D server did not return a GLB model")
    workdir = _new_tempdir()
    try:
        glb = workdir / "model.glb"
        glb.write_bytes(r.content)
        return _compact({"state": "imported",
                         "result": await _import_generated(glb, "gltf", name, workdir, {"generator": "hunyuan3d-local"})})
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@_tool(read_only=False, destructive=False, open_world=True)
async def hunyuan3d_generate(
    ctx: Context,
    prompt: Annotated[Optional[str], Field(description="Text description (official API: max 200 characters)")] = None,
    image: Annotated[Optional[str], Field(description="Reference image: file path (relative to the project) or http(s) URL")] = None,
    name: Annotated[str, Field(description="Name for the imported object")] = "hunyuan_model",
    wait: Annotated[bool, Field(description="Official API only: wait for the result and import it, or return a job handle")] = True,
    timeout_s: Annotated[float, Field(ge=10, le=7200, description="Maximum time to wait for the generation")] = 900,
    texture: Annotated[bool, Field(description="Local server only: also generate a texture")] = False,
    octree_resolution: Annotated[int, Field(ge=128, le=512, description="Local server only: shape resolution")] = 256,
    inference_steps: Annotated[int, Field(ge=1, le=100, description="Local server only")] = 20,
    guidance_scale: Annotated[float, Field(ge=1, le=20, description="Local server only")] = 5.5,
) -> str:
    """Generate a 3D model with Tencent Hunyuan3D from text or an image and import it into the scene.
    Official API (HUNYUAN3D_SECRET_ID + HUNYUAN3D_SECRET_KEY): exactly one of prompt / image; asynchronous and
    CONSUMES the user's Tencent credits, so confirm before calling. Your own server (HUNYUAN3D_API_URL):
    one long request (minutes, and the GPU of that machine is busy); the model options above apply. Blender
    stays responsive in both cases; the imported root object is renamed to 'name'."""
    if not prompt and not image:
        raise ToolError("Provide a prompt or an image")
    mode = _hunyuan_mode()
    if mode == "local":
        _need(HUNYUAN3D_API_URL, "Hunyuan3D (local server)", "HUNYUAN3D_API_URL", "Point it at your server, e.g. http://localhost:8081.")
        return await _hunyuan_local(prompt, image, name, timeout_s, texture, octree_resolution, inference_steps, guidance_scale)
    if prompt and image:
        raise ToolError("The official Hunyuan3D API accepts either a prompt or an image, not both")
    if prompt and len(prompt) > 200:
        raise ToolError("The official Hunyuan3D API limits prompts to 200 characters")
    payload: dict[str, Any] = {"Num": 1}
    if prompt:
        payload["Prompt"] = prompt
    elif _is_url(image):
        payload["ImageUrl"] = image
    else:
        payload["ImageBase64"] = base64.b64encode(_read_local_images([image])[0][1]).decode("ascii")
    resp = await _hunyuan_api("SubmitHunyuanTo3DJob", payload)
    if not resp.get("JobId"):
        raise ToolError("Hunyuan3D returned no JobId: " + _compact(resp)[:500])
    return await _collect(ctx, f"hunyuan|{resp['JobId']}", name, wait, timeout_s)


@_tool(read_only=False, destructive=False, open_world=True)
async def collect_generated_model(
    ctx: Context,
    job: Annotated[str, Field(description="The 'job' handle returned by rodin_generate or hunyuan3d_generate, unchanged")],
    name: Annotated[str, Field(description="Name for the imported object")] = "generated_model",
    wait: Annotated[bool, Field(description="Keep waiting until done (up to timeout_s). If false, check once and return")] = True,
    timeout_s: Annotated[float, Field(ge=10, le=3600)] = 600,
) -> str:
    """Check a generation job started earlier (rodin_generate / hunyuan3d_generate with wait=false, or one
    that timed out) and import the model once it is ready. Safe to call repeatedly while it is running."""
    return await _collect(ctx, job, name, wait, timeout_s)


# --------------------------------------------------------------------------- prompt


@mcp.prompt()
def blender_task(goal: str) -> str:
    """Start a Blender modelling/scene task with the recommended verify-as-you-go workflow."""
    return (
        f"Goal: {goal}\n\n"
        "1. Call blender_status, then get_scene_info, and inspect anything relevant with get_object_details.\n"
        "2. Plan the steps briefly (units are metres, Z-up).\n"
        "3. Implement in small, idempotent scripts saved in the project and run with run_blender_script.\n"
        "4. After each step call look (angles + clay shading to judge form) and compare with the goal; fix "
        "before moving on. Check mesh quality with get_scene_info fields=['health'].\n"
        "5. Finish with a summary of what was created (object/collection names). Do not save or export "
        "unless asked."
    )


# --------------------------------------------------------------------------- entry point


async def _check() -> int:
    try:
        print(await blender_status())
        return 0
    except ToolError as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Blender MCP server for Claude Code")
    parser.add_argument("--check", action="store_true", help="test the connection to Blender and exit")
    args = parser.parse_args()

    logging.basicConfig(
        stream=sys.stderr,  # stdout is reserved for the MCP stdio protocol
        level=getattr(logging, os.environ.get("BLENDER_MCP_LOG", "INFO").upper(), logging.INFO),
        format="[blender-mcp] %(levelname)s: %(message)s",
    )
    if args.check:
        sys.exit(anyio.run(_check))
    log.info("Starting (Blender at %s:%s, read-only=%s, token=%s)", HOST, PORT, READ_ONLY, bool(TOKEN))
    mcp.run()


if __name__ == "__main__":
    main()
