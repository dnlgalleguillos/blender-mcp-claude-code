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

Speaks the TCP/JSON protocol of the BlenderMCP add-on (blender_mcp.py 1.3.1 or the stock add-on) and exposes
it as MCP tools. What it adds over a thin "execute code" bridge:

  * get_scene_info: compact text facts about the scene, with mesh/rig "health" checks and ground contact
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
  BLENDER_MCP_LOG         logging level (default INFO); logs go to stderr, never stdout

Diagnostics:  uv run blender_mcp_server.py --check
"""

import argparse
import ast
import base64
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
from pathlib import Path
from typing import Annotated, Any, Literal, Optional, Union
from urllib.parse import quote

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


def _unwrap(resp: dict, integration: Optional[str] = None) -> Any:
    """Turn an add-on reply into a result, or raise ToolError.

    The add-on reports failures in two ways: {"status": "error", "message": ...} and, for most
    integration handlers, {"status": "success", "result": {"error": ...}}. Both are errors here.
    """
    if resp.get("status") == "error":
        message = str(resp.get("message", "Unknown error"))
        if integration and message.startswith("Unknown command type"):
            message = (
                f"The {integration} integration is disabled in Blender. Tick it in the BlenderMCP panel "
                "(N sidebar > BlenderMCP) and try again."
            )
        raise ToolError(message)
    result = resp.get("result")
    if isinstance(result, dict) and result.get("error"):
        raise ToolError(str(result["error"]))
    return result


async def call(command: str, params: Optional[dict] = None, *, timeout: Optional[float] = None,
               integration: Optional[str] = None) -> Any:
    try:
        resp = await anyio.to_thread.run_sync(lambda: _conn.send(command, params, timeout))
    except BlenderError as e:
        raise ToolError(str(e)) from None
    return _unwrap(resp, integration)


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


def _is_code_disabled(e: ToolError) -> bool:
    return "disabled" in str(e).lower() and "code" in str(e).lower()


def _path(p: str) -> Path:
    """Resolve a path. Relative paths resolve against this server's working directory
    (Claude Code starts MCP servers in the project directory)."""
    return Path(p).expanduser().resolve()


# --------------------------------------------------------------------------- server

INSTRUCTIONS = """\
Controls a running Blender through the BlenderMCP add-on (the user must have clicked 'Connect' in Blender).

Working method:
1. Look before you edit: get_scene_info for the overview (add fields, or query / root, for detail) and
   get_object_details for what you will touch.
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

SNIP_STATUS = """
scene = bpy.context.scene
print(json.dumps({
    "blender_version": bpy.app.version_string,
    "file": bpy.data.filepath or None,
    "unsaved_changes": bpy.data.is_dirty,
    "scene": scene.name,
    "objects": len(scene.objects),
    "render_engine": scene.render.engine,
}))
"""

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
    anything fails: it tells you whether Blender is reachable, whether the token is accepted, and
    whether code execution is enabled."""
    try:
        data = await _query(SNIP_STATUS)
        data["code_execution"] = "enabled"
    except ToolError as e:
        if not _is_code_disabled(e):
            raise
        info = await call("get_scene_info")
        data = {"scene": info.get("name"), "objects": info.get("object_count"), "code_execution": "disabled"}
    data["endpoint"] = f"{HOST}:{PORT}"
    data["read_only_mode"] = READ_ONLY
    data["safe_mode"] = SAFE_MODE
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
    params = _scene_params(query, root, fields, limit)
    try:
        return _format_scene(await _query(SNIP_SCENE, params))
    except ToolError as e:
        if not _is_code_disabled(e):
            raise
        info = await call("get_scene_info")
        info["note"] = "Code execution is disabled in the add-on panel; showing the reduced built-in summary."
        return _compact(info)


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
USER_AGENT = "blender-mcp-claude-code"
ASSET_SOURCES = ("polyhaven", "sketchfab", "polypizza")
POLYHAVEN_TYPES = {0: "hdris", 1: "textures", 2: "models"}
POLYHAVEN_TTL = 600.0
THUMBNAIL_HOSTS = ("polyhaven.com", "sketchfab.com", "poly.pizza")
_polyhaven_cache: dict[str, tuple[float, dict]] = {}

# Category ids are the Poly Pizza API's; the names and aliases follow the table published by mcp-for-blender
# (MIT, (c) 2025 Siddharth Ahuja, see LICENSE-blender-mcp.txt).
POLYPIZZA_CATEGORIES = {
    "Food & Drink": 0, "Clutter": 1, "Weapons": 2, "Transport": 3, "Furniture & Decor": 4, "Objects": 5,
    "Nature": 6, "Animals": 7, "Buildings": 8, "People & Characters": 9, "Scenes & Levels": 10, "Other": 11,
}
POLYPIZZA_ALIASES = {
    "food": 0, "drink": 0, "drinks": 0, "weapon": 2, "vehicle": 3, "vehicles": 3, "transportation": 3,
    "furniture": 4, "decor": 4, "object": 5, "prop": 5, "props": 5, "plant": 6, "plants": 6, "animal": 7,
    "building": 8, "architecture": 8, "person": 9, "character": 9, "characters": 9, "people": 9,
    "scene": 10, "scenes": 10, "level": 10, "levels": 10,
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

SNIP_IMPORT_GLB = """
before = set(bpy.data.objects.keys())
bpy.ops.import_scene.gltf(filepath=_P["path"])
new = [o for o in bpy.data.objects if o.name not in before]
names = [o.name for o in new]
roots = [o for o in new if o.parent is None or o.parent.name not in names]
for o in roots:
    for key, value in _P["props"].items():
        o[key] = value
print(json.dumps({"names": names, "roots": [o.name for o in roots]}))
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


async def _http_get(url: str, *, params: Optional[dict] = None, headers: Optional[dict] = None,
                    timeout: float = 30, redirects: bool = True) -> httpx.Response:
    try:
        async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT, **(headers or {})}, timeout=timeout,
                                     follow_redirects=redirects) as client:
            return await client.get(url, params=params)
    except httpx.HTTPError as e:
        raise ToolError(f"Network error contacting {httpx.URL(url).host}: {e}") from None


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


async def _search_sketchfab(query: str, category: Optional[str], limit: int) -> tuple[str, list[str]]:
    data = await call("search_sketchfab_models",
                      {"query": query, "categories": category, "count": min(limit, 24), "downloadable": True},
                      integration="Sketchfab")
    lines, thumbs = [], []
    for r in (data or {}).get("results", []):
        lic = r.get("license")
        images = (r.get("thumbnails") or {}).get("images") or []
        pick = next((i for i in sorted(images, key=lambda i: i.get("width", 0)) if i.get("width", 0) >= 200),
                    images[0] if images else None)
        thumbs.append((pick or {}).get("url", ""))
        lines.append(" | ".join([
            str(r.get("uid")), str(r.get("name")), "by " + str((r.get("user") or {}).get("username")),
            f"{r.get('faceCount')} faces", "licence: " + str(lic.get("label") if isinstance(lic, dict) else lic)]))
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
      knows real-world sizes. Needs 'Use assets from Poly Haven' enabled in the Blender panel to import.
    - sketchfab: a large catalogue of user-made models, including very specific ones; licences and face
      counts vary per model. Needs the Sketchfab API key set in the Blender panel.
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


async def _import_polyhaven(id: str, asset_type: Optional[str], target_size: Optional[float],
                            apply_to: Optional[list[str]], resolution: str, file_format: Optional[str],
                            timeout: float) -> dict:
    if not asset_type:
        r = await _http_get(f"{POLYHAVEN_API}/info/{quote(id, safe='')}")
        if r.status_code != 200:
            raise ToolError(f"Poly Haven has no asset '{id}' (HTTP {r.status_code}); search again for a valid id")
        asset_type = POLYHAVEN_TYPES.get(r.json().get("type"))
        if not asset_type:
            raise ToolError(f"Could not tell the type of Poly Haven asset '{id}'; pass asset_type")
    result = await call("download_polyhaven_asset",
                        {"asset_id": id, "asset_type": asset_type, "resolution": resolution, "file_format": file_format},
                        timeout=timeout, integration="Poly Haven")
    out: dict[str, Any] = {"source": "polyhaven", "asset_type": asset_type, "licence": "CC0 (no credit required)",
                           "message": (result or {}).get("message")}
    if asset_type == "textures" and (result or {}).get("material"):
        out["material"] = await _query(SNIP_MATERIAL, {"material": result["material"], "apply_to": apply_to or []})
        if not apply_to:
            out["note"] = "Material created but not assigned to anything; pass apply_to=[object names] to use it."
    elif asset_type == "models":
        out["placed"] = await _fit((result or {}).get("imported_objects", []), target_size)
    elif asset_type == "hdris":
        out["note"] = "The HDRI is now the world lighting."
    return out


async def _import_sketchfab(id: str, target_size: Optional[float], timeout: float) -> dict:
    if not target_size:
        raise ToolError("target_size (metres, largest dimension) is required for sketchfab: library models come "
                        "at arbitrary scale. Examples: chair 1.0, car 4.5, cup 0.12")
    result = await call("download_sketchfab_model", {"uid": id, "normalize_size": True, "target_size": target_size},
                        timeout=timeout, integration="Sketchfab")
    return {"source": "sketchfab", "licence": "see the model's licence from search_assets", **(result or {})}


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
    tmpdir = Path(tempfile.mkdtemp(prefix="blender_mcp_pp_"))
    try:
        glb = tmpdir / ((re.sub(r"[^A-Za-z0-9_-]", "_", id) or "model") + ".glb")
        glb.write_bytes(f.content)
        props = {"polypizza_id": str(model.get("ID") or id), "polypizza_licence": str(model.get("Licence") or ""),
                 "polypizza_attribution": str(model.get("Attribution") or "")}
        imported = await _query(SNIP_IMPORT_GLB, {"path": str(glb), "props": props}, timeout=timeout)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
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
# (hyper3d.ai / fal.ai) and Hunyuan3D (Tencent cloud / local server) behind an opaque job handle:
#   rodin|main|<subscription_key>|<uuid>     rodin|fal|<request_id>     hunyuan|<job_id>

RODIN_POLL_SECONDS = 8
HUNYUAN_POLL_SECONDS = 10

SNIP_OBJECT_NAMES = """
print(json.dumps(list(bpy.data.objects.keys())))
"""

SNIP_RENAME_ROOT = """
names = _P["names"]
objs = [bpy.data.objects[n] for n in names if n in bpy.data.objects]
roots = [o for o in objs if o.parent is None or o.parent.name not in names]
root = None
if len(roots) == 1:
    roots[0].name = _P["name"]
    root = roots[0].name
print(json.dumps({"root": root, "new_objects": [o.name for o in objs]}))
"""


def _msg(text: Any) -> str:
    return " ".join(str(text).split())


async def _require_ready(status_command: str, label: str) -> dict:
    """The add-on reports whether an integration is enabled and configured (API key, URL...)."""
    status = await call(status_command)
    if not isinstance(status, dict) or not status.get("enabled"):
        detail = _msg(status.get("message", "")) if isinstance(status, dict) else ""
        raise ToolError(f"{label} is not ready: {detail}")
    return status


def _is_url(text: str) -> bool:
    return re.match(r"^https?://", text, re.IGNORECASE) is not None


def _encode_images(paths: Optional[list[str]], urls: Optional[list[str]]) -> list:
    """Local files become [suffix, base64]; URLs stay strings. The add-on adapts them to each platform."""
    items: list = []
    for p in paths or []:
        f = _path(p)
        try:
            data = f.read_bytes()
        except OSError as e:
            raise ToolError(f"Cannot read image {f}: {e}") from None
        if len(data) > 10 * 1024 * 1024:
            raise ToolError(f"Image {f.name} is larger than 10 MB; downscale it first")
        items.append([f.suffix.lower() or ".png", base64.b64encode(data).decode("ascii")])
    for u in urls or []:
        if not _is_url(u):
            raise ToolError(f"Not an http(s) URL: {u}")
        items.append(u)
    return items


def _rodin_job(created: Any) -> str:
    if isinstance(created, dict):
        if created.get("request_id"):
            return f"rodin|fal|{created['request_id']}"
        jobs = created.get("jobs") or {}
        if created.get("uuid") and isinstance(jobs, dict) and jobs.get("subscription_key"):
            return f"rodin|main|{jobs['subscription_key']}|{created['uuid']}"
    raise ToolError("Unexpected response from Rodin when creating the job (check the API key and credits): "
                    + _compact(created)[:500])


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


async def _poll_job(kind: str, parts: list[str]) -> tuple[str, Any]:
    """Returns (state, detail) with state in running / done / failed."""
    valid = ((kind == "rodin" and parts[:1] == ["main"] and len(parts) == 3)
             or (kind == "rodin" and parts[:1] == ["fal"] and len(parts) == 2)
             or (kind == "hunyuan" and len(parts) == 1))
    if not valid:
        raise ToolError("Invalid job handle. Pass the 'job' value returned by rodin_generate / hunyuan3d_generate unchanged.")
    try:
        if kind == "rodin" and parts[0] == "main":
            data = await call("poll_rodin_job_status", {"subscription_key": parts[1]}, integration="Hyper3D Rodin")
            statuses = data.get("status_list") if isinstance(data, dict) else None
            if not statuses:
                raise ToolError(f"unexpected status reply: {_compact(data)[:300]}")
            if any(st == "Failed" for st in statuses):
                return "failed", statuses
            return ("done" if all(st == "Done" for st in statuses) else "running"), statuses
        if kind == "rodin":
            data = await call("poll_rodin_job_status", {"request_id": parts[1]}, integration="Hyper3D Rodin")
            status = data.get("status") if isinstance(data, dict) else None
            if status == "COMPLETED":
                return "done", data
            if status in ("IN_QUEUE", "IN_PROGRESS"):
                return "running", {k: data[k] for k in ("status", "queue_position") if k in data}
            return "failed", data
        data = await call("poll_hunyuan_job_status", {"job_id": parts[0]}, integration="Hunyuan3D")
        resp = data.get("Response") if isinstance(data, dict) else None
        if not isinstance(resp, dict):
            raise ToolError(f"unexpected status reply: {_compact(data)[:300]}")
        if (err := _tencent_error(resp)) is not None:
            raise err
        status = resp.get("Status")
        if status == "DONE":
            return "done", resp
        if status == "FAIL":
            return "failed", resp.get("ErrorMessage") or resp
        return "running", {"status": status}
    except ToolError as e:
        raise ToolError(f"Status check failed: {e}. Check the job handle, API key and credits.") from None


async def _import_job(kind: str, parts: list[str], name: str, detail: Any) -> Any:
    if kind == "rodin":
        params = ({"task_uuid": parts[2], "name": name} if parts[0] == "main"
                  else {"request_id": parts[1], "name": name})
        return await call("import_generated_asset", params, timeout=180, integration="Hyper3D Rodin")
    files = detail.get("ResultFile3Ds") or []
    chosen = next((f for f in files if str(f.get("Type", "")).upper() == "OBJ"), files[0] if files else None)
    if not chosen or not chosen.get("Url"):
        raise ToolError("Hunyuan3D finished but returned no downloadable file: " + _compact(detail)[:400])
    return await call("import_generated_asset_hunyuan", {"name": name, "zip_file_url": chosen["Url"]},
                      timeout=240, integration="Hunyuan3D")


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
    the scene. CONSUMES the user's Rodin credits (the shared free-trial key is heavily rate-limited), so
    confirm before calling. Generation takes a few minutes; if the wait times out you get a job handle to
    pass to collect_generated_model. Needs 'Use Hyper3D Rodin' enabled and a key in the BlenderMCP panel.
    The mesh is raw: check its scale and orientation afterwards."""
    if not prompt and not image_paths and not image_urls:
        raise ToolError("Provide a prompt and/or reference images")
    await _require_ready("get_hyper3d_status", "Hyper3D Rodin")
    images = _encode_images(image_paths, image_urls)
    created = await call("create_rodin_job",
                         {"text_prompt": prompt, "images": images or None, "bbox_condition": bbox_condition},
                         timeout=120, integration="Hyper3D Rodin")
    return await _collect(ctx, _rodin_job(created), name, wait, timeout_s)


@_tool(read_only=False, destructive=False, open_world=True)
async def hunyuan3d_generate(
    ctx: Context,
    prompt: Annotated[Optional[str], Field(description="Text description (official API: max 200 characters)")] = None,
    image: Annotated[Optional[str], Field(description="Reference image: file path (relative to the project) or http(s) URL")] = None,
    name: Annotated[str, Field(description="Name for the imported object")] = "hunyuan_model",
    wait: Annotated[bool, Field(description="Official API only: wait for the result and import it, or return a job handle")] = True,
    timeout_s: Annotated[float, Field(ge=10, le=7200, description="Maximum time to wait for the generation")] = 900,
) -> str:
    """Generate a 3D model with Tencent Hunyuan3D from text or an image and import it into the scene.
    Works with the mode chosen in the BlenderMCP panel. Official API: exactly one of prompt / image; it is
    asynchronous and CONSUMES the user's Tencent credits, so confirm before calling. Local server: runs
    synchronously and Blender is FROZEN until it finishes (can take minutes; shape and texture settings come
    from the panel); the imported root object is renamed to 'name' when it can be identified."""
    status = await _require_ready("get_hunyuan3d_status", "Hunyuan3D")
    if not prompt and not image:
        raise ToolError("Provide a prompt or an image")
    img = (image if _is_url(image) else str(_path(image))) if image else None

    if status.get("mode") == "OFFICIAL_API":
        if prompt and image:
            raise ToolError("The official Hunyuan3D API accepts either a prompt or an image, not both")
        if prompt and len(prompt) > 200:
            raise ToolError("The official Hunyuan3D API limits prompts to 200 characters")
        created = await call("create_hunyuan_job", {"text_prompt": prompt, "image": img},
                             timeout=120, integration="Hunyuan3D")
        resp = created.get("Response") if isinstance(created, dict) else None
        if not isinstance(resp, dict):
            raise ToolError("Unexpected response from Hunyuan3D when creating the job: " + _compact(created)[:500])
        if (err := _tencent_error(resp)) is not None:
            raise err
        if not resp.get("JobId"):
            raise ToolError("Hunyuan3D returned no JobId: " + _compact(resp)[:500])
        return await _collect(ctx, f"hunyuan|{resp['JobId']}", name, wait, timeout_s)

    # Local server: one blocking request; the add-on imports the GLB on Blender's next timer tick.
    before = set(await _query(SNIP_OBJECT_NAMES))
    await call("create_hunyuan_job", {"text_prompt": prompt, "image": img}, timeout=timeout_s, integration="Hunyuan3D")
    new: list[str] = []
    for _ in range(30):  # the import runs asynchronously; wait until the new objects stop appearing
        await anyio.sleep(0.5)
        names = [n for n in await _query(SNIP_OBJECT_NAMES) if n not in before]
        if names and names == new:
            break
        new = names
    if not new:
        raise ToolError("Hunyuan3D reported success but no new objects appeared in the scene")
    info = await _query(SNIP_RENAME_ROOT, {"names": new, "name": name})
    return _compact({"state": "imported", "result": info})


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
