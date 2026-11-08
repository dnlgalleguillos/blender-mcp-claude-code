# Code created by Daniel Galleguillos: https://github.com/dnlgalleguillos
# Copyright (C) 2026 Daniel Galleguillos Cruz
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify it under the terms of the
# GNU General Public License as published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version. It is distributed WITHOUT ANY WARRANTY; see the
# GNU General Public License (file LICENSE) for details.
"""
Blender MCP Bridge 2.0 - a small, local-only door into Blender for an MCP server such as Claude Code.

The add-on does three things and nothing else:

  * listens on 127.0.0.1 (optionally requiring a shared token) for JSON commands,
  * runs them on Blender's main thread, one at a time, in the order they arrived,
  * answers with JSON.

Commands:  ping | execute_code | get_viewport_screenshot

Everything else (inspecting the scene, rendering views, importing assets, talking to asset libraries and AI
generators) is done by the MCP server, which sends Python through execute_code. API keys and network traffic
therefore never live inside Blender, and nothing is stored in your .blend files.

This add-on collects and sends nothing.
"""

import codecs
import hmac
import io
import json
import logging
import math
import queue
import secrets
import socket
import threading
import traceback
from contextlib import redirect_stdout

import bmesh
import bpy
import mathutils
from bpy.props import BoolProperty, IntProperty, StringProperty

bl_info = {
    "name": "Blender MCP Bridge",
    "author": "Daniel Galleguillos",
    "version": (2, 0, 0),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar > BlenderMCP",
    "description": "Local bridge that lets an MCP server (for example Claude Code) inspect and drive Blender",
    "doc_url": "https://github.com/dnlgalleguillos/blender-mcp-claude-code",
    "category": "Interface",
}

PROTOCOL = 2
HOST = "127.0.0.1"
DEFAULT_PORT = 9876
MAX_MESSAGE_CHARS = 64 * 1024 * 1024  # one command may carry a long script

log = logging.getLogger("BlenderMCP")


def _setup_logging():
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("[BlenderMCP] %(levelname)s: %(message)s"))
        log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False


def _code_allowed() -> bool:
    return bool(getattr(bpy.context.window_manager, "blendermcp_allow_code", True))


# --------------------------------------------------------------------------------------------- commands
# Handlers run on the main thread. They return a JSON-serialisable result or raise; the bridge turns a
# raised exception into {"status": "error", "message": ...}.


def cmd_ping():
    scene = bpy.context.scene
    return {
        "addon": "blender-mcp-bridge",
        "addon_version": ".".join(str(n) for n in bl_info["version"]),
        "protocol": PROTOCOL,
        "blender_version": bpy.app.version_string,
        "file": bpy.data.filepath or None,
        "unsaved_changes": bool(bpy.data.is_dirty),
        "scene": scene.name,
        "objects": len(scene.objects),
        "render_engine": scene.render.engine,
        "code_execution": _code_allowed(),
    }


_SCRIPT_NAME = "<blender-mcp>"


def cmd_execute_code(code):
    """Run Python with bpy, bmesh, mathutils and math preloaded; return what it printed."""
    if not _code_allowed():
        raise PermissionError(
            "Code execution is disabled. Turn on 'Allow code execution' in the BlenderMCP panel "
            "(3D Viewport > N sidebar > BlenderMCP)."
        )
    if not isinstance(code, str):
        raise TypeError("'code' must be a string")
    try:  # a Ctrl+Z restore point before arbitrary code runs
        bpy.ops.ed.undo_push(message="BlenderMCP: execute_code")
    except Exception:
        pass
    namespace = {"__name__": "__main__", "bpy": bpy, "bmesh": bmesh, "mathutils": mathutils, "math": math}
    captured = io.StringIO()
    try:
        compiled = compile(code, _SCRIPT_NAME, "exec")
        with redirect_stdout(captured):
            exec(compiled, namespace)
    except Exception as e:  # noqa: BLE001 - everything the script raises goes back to the caller
        lines = code.splitlines()
        where = []
        for frame in traceback.extract_tb(e.__traceback__):
            if frame.filename == _SCRIPT_NAME and frame.lineno and frame.lineno <= len(lines):
                where.append(f"  line {frame.lineno}: {lines[frame.lineno - 1].strip()}")
        if isinstance(e, SyntaxError) and e.filename == _SCRIPT_NAME and e.lineno:
            where.append(f"  line {e.lineno}")
        raise RuntimeError(f"Code execution error: {type(e).__name__}: {e}" + ("\n" + "\n".join(where[-3:]) if where else ""))
    return {"executed": True, "result": captured.getvalue()}


def _find_viewport():
    """The largest 3D viewport of any window: (window, area, region), or None."""
    best = None
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type != "VIEW_3D":
                continue
            region = next((r for r in area.regions if r.type == "WINDOW"), None)
            if region and (best is None or area.width * area.height > best[1].width * best[1].height):
                best = (window, area, region)
    return best


def cmd_get_viewport_screenshot(max_size=800, filepath=None, format="png"):
    """Save a picture of the live 3D viewport to `filepath`, shrunk so its longest side is at most max_size."""
    if not filepath:
        raise ValueError("'filepath' is required")
    found = _find_viewport()
    if found is None:
        raise RuntimeError("No 3D viewport found: open a 3D Viewport editor in Blender")
    window, area, region = found
    try:
        with bpy.context.temp_override(window=window, area=area, region=region):
            bpy.ops.screen.screenshot_area(filepath=filepath)
    except RuntimeError as e:
        raise RuntimeError(
            f"Could not capture the 3D viewport ({str(e).strip()}). The Blender window must be open and visible."
        ) from None
    image = bpy.data.images.load(filepath)
    try:
        width, height = image.size
        if max(width, height) > max_size:
            scale = max_size / max(width, height)
            image.scale(max(1, int(width * scale)), max(1, int(height * scale)))
            image.file_format = {"JPG": "JPEG", "JPEG": "JPEG"}.get(str(format).upper(), "PNG")
            image.save()
            width, height = image.size
    finally:
        bpy.data.images.remove(image)
    return {"success": True, "width": width, "height": height, "filepath": filepath}


COMMANDS = {
    "ping": cmd_ping,
    "execute_code": cmd_execute_code,
    "get_viewport_screenshot": cmd_get_viewport_screenshot,
}


# --------------------------------------------------------------------------------------------- the bridge


class Bridge:
    """Socket threads feed a queue; one timer on the main thread drains it.

    Only the main thread ever touches bpy and only the main thread ever writes to a client socket, so replies
    keep the order of the commands and no locking around sockets is needed.
    """

    def __init__(self, port: int = DEFAULT_PORT, token: str = "", host: str = HOST):
        self.host, self.port, self.token = host, port, token or ""
        self.running = False
        self.max_message_chars = MAX_MESSAGE_CHARS
        self._sock = None
        self._clients = set()
        self._clients_lock = threading.Lock()
        self._jobs: "queue.Queue" = queue.Queue()
        self._pump_fn = self._pump  # one object, so register/unregister refer to the same callable

    # -- lifecycle (main thread)
    def start(self) -> bool:
        if self.running:
            return True
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.host, self.port))
            sock.listen(8)
        except OSError as e:
            log.error("Could not listen on %s:%s: %s", self.host, self.port, e)
            return False
        self._sock = sock
        self.running = True
        threading.Thread(target=self._accept_loop, args=(sock,), daemon=True).start()
        if not bpy.app.timers.is_registered(self._pump_fn):
            bpy.app.timers.register(self._pump_fn, first_interval=0.0, persistent=True)
        log.info("Listening on %s:%s%s", self.host, self.port, " (token required)" if self.token else "")
        return True

    def stop(self) -> None:
        if not self.running:
            return
        self.running = False
        try:
            self._sock.close()
        except OSError:
            pass
        with self._clients_lock:
            clients, _ = list(self._clients), self._clients.clear()
        for client in clients:
            for action in (lambda c: c.shutdown(socket.SHUT_RDWR), lambda c: c.close()):
                try:
                    action(client)
                except OSError:
                    pass
        while not self._jobs.empty():
            try:
                self._jobs.get_nowait()
            except queue.Empty:
                break
        log.info("Stopped")

    # -- socket threads
    def _accept_loop(self, sock) -> None:
        sock.settimeout(0.5)
        while self.running:
            try:
                client, _ = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._clients_lock:
                self._clients.add(client)
            threading.Thread(target=self._read_loop, args=(client,), daemon=True).start()

    def _read_loop(self, client) -> None:
        """Split the byte stream into JSON documents; tolerate partial, concatenated and multi-byte-split input."""
        decoder, utf8, buffer = json.JSONDecoder(), codecs.getincrementaldecoder("utf-8")(), ""
        try:
            while self.running:
                data = client.recv(65536)
                if not data:
                    break
                buffer += utf8.decode(data)
                if len(buffer) > self.max_message_chars:
                    self._jobs.put(("reply", client, {"status": "error", "message": "Message too large"}))
                    break
                while True:
                    buffer = buffer.lstrip()
                    if not buffer:
                        break
                    try:
                        command, end = decoder.raw_decode(buffer)
                    except json.JSONDecodeError:
                        break  # incomplete: wait for more bytes
                    buffer = buffer[end:]
                    self._accept(client, command)
        except (OSError, UnicodeDecodeError):
            pass
        finally:
            with self._clients_lock:
                self._clients.discard(client)
            # the socket is closed from the main thread, after any queued reply has been sent
            self._jobs.put(("close", client, None))

    def _accept(self, client, command) -> None:
        if not isinstance(command, dict):
            self._jobs.put(("reply", client, {"status": "error", "message": "Invalid command: expected a JSON object"}))
            return
        if self.token:
            supplied = command.get("token")
            if not (isinstance(supplied, str) and hmac.compare_digest(supplied.encode(), self.token.encode())):
                log.warning("Rejected a command: missing or invalid token")
                self._jobs.put(("reply", client, {"status": "error", "message": "Unauthorized: missing or invalid token"}))
                return
        self._jobs.put(("run", client, command))

    # -- main thread
    def _pump(self):
        """Timer callback: handle one queued job. Returns the delay until the next call (None = unregister)."""
        if not self.running:
            return None
        try:
            kind, client, payload = self._jobs.get_nowait()
        except queue.Empty:
            return 0.02
        if kind == "run":
            self._send(client, self._execute(payload))
        elif kind == "reply":
            self._send(client, payload)
        elif kind == "close":
            try:
                client.close()
            except OSError:
                pass
        return 0.0  # more jobs may be waiting

    def _execute(self, command: dict) -> dict:
        name = command.get("type")
        handler = COMMANDS.get(name)
        if handler is None:
            return {"status": "error", "message": f"Unknown command type: {name}"}
        params = command.get("params") or {}
        try:
            return {"status": "success", "result": handler(**params)}
        except Exception as e:  # noqa: BLE001
            log.error("%s failed: %s", name, e)
            return {"status": "error", "message": str(e) or type(e).__name__}

    @staticmethod
    def _send(client, response: dict) -> None:
        try:
            client.settimeout(30)
            client.sendall(json.dumps(response).encode("utf-8"))
        except OSError:
            pass  # the client went away


_bridge = None


def bridge_running() -> bool:
    return _bridge is not None and _bridge.running


# --------------------------------------------------------------------------------------------- UI


class BLENDERMCP_Preferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    auth_token: StringProperty(
        name="Auth Token",
        description="Optional shared secret. When set, every command must include it as a \"token\" field",
        subtype="PASSWORD",
        default="",
    )

    def draw(self, context):
        layout = self.layout
        layout.label(text="Security:", icon="LOCKED")
        box = layout.box()
        row = box.row()
        row.prop(self, "auth_token")
        row.operator("blendermcp.generate_token", text="Generate", icon="FILE_REFRESH")
        box.label(text="Empty = no authentication. Only programs on this computer can connect either way.", icon="INFO")
        box.label(text="If set, give the same value to the MCP server as BLENDER_MCP_TOKEN.", icon="BLANK1")
        box.label(text="Takes effect the next time you press Connect.", icon="BLANK1")
        layout.label(text="Privacy:", icon="PREFERENCES")
        box = layout.box()
        box.label(text="This add-on collects and sends nothing, and stores nothing in your .blend files.", icon="CHECKMARK")
        box.label(text="It only listens on 127.0.0.1. Network access (asset libraries, AI generators)", icon="INFO")
        box.label(text="belongs to the MCP server, with API keys given to it as environment variables.", icon="BLANK1")


class BLENDERMCP_OT_generate_token(bpy.types.Operator):
    bl_idname = "blendermcp.generate_token"
    bl_label = "Generate Auth Token"
    bl_description = "Generate a random token and store it in the add-on preferences"

    def execute(self, context):
        addon = context.preferences.addons.get(__name__)
        if addon is None:
            self.report({"ERROR"}, "Add-on preferences are not available")
            return {"CANCELLED"}
        addon.preferences.auth_token = secrets.token_urlsafe(24)
        self.report({"INFO"}, "Token generated. Reconnect for it to take effect.")
        return {"FINISHED"}


class BLENDERMCP_OT_start(bpy.types.Operator):
    bl_idname = "blendermcp.start"
    bl_label = "Connect to MCP server"
    bl_description = "Start listening for the MCP server on this computer"

    def execute(self, context):
        global _bridge
        if bridge_running():
            return {"FINISHED"}
        addon = context.preferences.addons.get(__name__)
        token = addon.preferences.auth_token if addon else ""
        port = context.window_manager.blendermcp_port
        bridge = Bridge(port=port, token=token)
        if not bridge.start():
            self.report({"ERROR"}, f"Could not listen on port {port} (is it already in use?)")
            return {"CANCELLED"}
        _bridge = bridge
        return {"FINISHED"}


class BLENDERMCP_OT_stop(bpy.types.Operator):
    bl_idname = "blendermcp.stop"
    bl_label = "Disconnect"
    bl_description = "Stop listening for the MCP server"

    def execute(self, context):
        global _bridge
        if _bridge is not None:
            _bridge.stop()
            _bridge = None
        return {"FINISHED"}


class BLENDERMCP_PT_panel(bpy.types.Panel):
    bl_label = "Blender MCP"
    bl_idname = "BLENDERMCP_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BlenderMCP"

    def draw(self, context):
        layout, wm = self.layout, context.window_manager
        running = bridge_running()
        row = layout.row()
        row.enabled = not running
        row.prop(wm, "blendermcp_port")
        layout.prop(wm, "blendermcp_allow_code")
        if running:
            layout.operator("blendermcp.stop", icon="PAUSE")
            layout.label(text=f"Connected: listening on port {_bridge.port}", icon="CHECKMARK")
        else:
            layout.operator("blendermcp.start", icon="PLAY")
            layout.label(text="Not connected", icon="X")


_CLASSES = (
    BLENDERMCP_Preferences,
    BLENDERMCP_OT_generate_token,
    BLENDERMCP_OT_start,
    BLENDERMCP_OT_stop,
    BLENDERMCP_PT_panel,
)


def register():
    _setup_logging()
    # Session settings live on the window manager, so nothing is written into .blend files
    bpy.types.WindowManager.blendermcp_port = IntProperty(
        name="Port", description="Port the MCP server connects to", default=DEFAULT_PORT, min=1024, max=65535)
    bpy.types.WindowManager.blendermcp_allow_code = BoolProperty(
        name="Allow code execution",
        description="Let the MCP server run Python in Blender. Almost every tool needs it; turn it off to stop Claude "
                    "from working in this session",
        default=True)
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    log.info("Blender MCP Bridge %s registered", ".".join(str(n) for n in bl_info["version"]))


def unregister():
    global _bridge
    if _bridge is not None:
        _bridge.stop()
        _bridge = None
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
    del bpy.types.WindowManager.blendermcp_port
    del bpy.types.WindowManager.blendermcp_allow_code


if __name__ == "__main__":
    register()
