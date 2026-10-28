#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.10,<2"]
# ///
"""
Blender MCP server, designed for Claude Code.

Speaks the TCP/JSON protocol of the BlenderMCP add-on (blender_mcp.py, v1.3 or the stock add-on)
and exposes it as MCP tools. Compared with a thin "execute code" bridge it adds:

  * structured, token-cheap inspection tools (scene, objects, materials, modifiers, mesh stats)
  * viewport screenshots and renders returned as images, so the model can *see* its work
  * run_blender_script: execute a .py file from your repo with real file names in tracebacks
  * save / export helpers that write into your project
  * Poly Haven and Sketchfab asset tools (results trimmed to what matters)
  * AI text/image-to-3D: Hyper3D Rodin (hyper3d.ai, fal.ai) and Hunyuan3D (Tencent cloud, local server)
  * a persistent connection with stale-socket detection, deadlines and no unsafe retries
  * optional shared-token auth and a read-only mode

Configuration (environment variables):
  BLENDER_HOST            default 127.0.0.1
  BLENDER_PORT            default 9876
  BLENDER_MCP_TOKEN       shared secret, must match "Auth Token" in the add-on preferences
  BLENDER_TIMEOUT         default per-command timeout in seconds (default 30)
  BLENDER_MCP_READONLY    1 = do not register tools that modify the scene or write files
  BLENDER_MCP_LOG         logging level (default INFO); logs go to stderr, never stdout

Diagnostics:  uv run blender_mcp_server.py --check
"""

import argparse
import base64
import json
import logging
import os
import re
import select
import socket
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Annotated, Any, Literal, Optional

import anyio
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


async def _query(body: str, params: Optional[dict] = None, timeout: Optional[float] = None) -> Any:
    """Run an inspection snippet that prints a single JSON document."""
    out = await _run_py(_header(params) + body, timeout)
    try:
        data = json.loads(out)
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
1. Look before you edit: call scene_overview, then list_objects / get_object_details for what you will touch.
2. Work in small steps and verify visually: after each meaningful change call viewport_screenshot and
   check the result against the goal. Use render_image only for final-quality checks (it blocks Blender).
3. For anything beyond a few lines, write the code to a .py file in the project and run it with
   run_blender_script instead of pasting it into execute_blender_code. Make scripts idempotent (remove or
   reuse objects by name before creating) so they can be re-run after edits.
4. Prefer the bpy data API (bpy.data.objects.new, bmesh, mesh.from_pydata) over bpy.ops: operators depend on
   UI context that is often missing when called from the add-on.
5. Conventions: metres, Z-up, name every object and material meaningfully, put generated objects in a
   dedicated collection, apply a smooth/flat shading choice deliberately.
6. Blender executes commands on its UI thread: long operations (renders, bakes, simulations, big imports)
   freeze it until done. Pass a generous timeout_s for them.
7. execute_blender_code and run_blender_script can do anything Python can. Never delete user data, quit
   Blender, or overwrite files you were not asked to touch. Call save_blend only when the user wants it.
8. File paths are resolved on the machine running Blender; relative paths resolve against the project.
9. rodin_generate and hunyuan3d_generate spend the user's API credits and can take minutes: confirm with the
   user before the first call. Generated meshes are raw: check scale and orientation with get_object_details
   (generators rarely produce real-world sizes) and clean up before building on them.
"""

mcp = FastMCP("blender", instructions=INSTRUCTIONS)


def _tool(*, read_only: bool, destructive: bool = True, open_world: bool = False):
    """Register a tool. In BLENDER_MCP_READONLY mode, tools that change things are not registered."""

    def deco(fn):
        if READ_ONLY and not read_only:
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

SNIP_OVERVIEW = """
scene = bpy.context.scene

def tree(coll):
    return {"name": coll.name, "objects": len(coll.objects), "children": [tree(c) for c in coll.children]}

counts = {}
for o in scene.objects:
    counts[o.type] = counts.get(o.type, 0) + 1
active = bpy.context.view_layer.objects.active
us = scene.unit_settings
try:
    mode = bpy.context.mode
except Exception:
    mode = None
print(json.dumps({
    "blender_version": bpy.app.version_string,
    "file": bpy.data.filepath or None,
    "unsaved_changes": bpy.data.is_dirty,
    "scene": scene.name,
    "scenes": [s.name for s in bpy.data.scenes],
    "mode": mode,
    "frame": {"current": scene.frame_current, "start": scene.frame_start, "end": scene.frame_end, "fps": scene.render.fps},
    "render": {
        "engine": scene.render.engine,
        "resolution": [scene.render.resolution_x, scene.render.resolution_y],
        "percentage": scene.render.resolution_percentage,
        "camera": scene.camera.name if scene.camera else None,
    },
    "units": {"system": us.system, "scale_length": us.scale_length, "length_unit": us.length_unit},
    "total_objects": len(scene.objects),
    "object_counts": counts,
    "active_object": active.name if active else None,
    "selected": [o.name for o in scene.objects if o.select_get()][:50],
    "collections": tree(scene.collection),
    "data_blocks": {
        "materials": len(bpy.data.materials), "meshes": len(bpy.data.meshes),
        "images": len(bpy.data.images), "actions": len(bpy.data.actions),
    },
}))
"""

SNIP_LIST = """
def r3(v):
    return [round(float(x), 3) for x in v]

scene = bpy.context.scene
objs = list(scene.objects)
if _P.get("type"):
    objs = [o for o in objs if o.type == _P["type"].upper()]
if _P.get("name_contains"):
    needle = _P["name_contains"].lower()
    objs = [o for o in objs if needle in o.name.lower()]
if _P.get("collection"):
    coll = bpy.data.collections.get(_P["collection"])
    names = set(o.name for o in coll.all_objects) if coll else set()
    objs = [o for o in objs if o.name in names]
if _P.get("selected_only"):
    objs = [o for o in objs if o.select_get()]
objs.sort(key=lambda o: o.name)
total = len(objs)
off, lim = _P["offset"], _P["limit"]
rows = []
for o in objs[off:off + lim]:
    row = {
        "name": o.name, "type": o.type,
        "location": r3(o.location), "dimensions": r3(o.dimensions),
        "parent": o.parent.name if o.parent else None,
        "collections": [c.name for c in o.users_collection],
        "visible": o.visible_get(),
        "materials": [s.material.name for s in o.material_slots if s.material],
    }
    if o.type == "MESH":
        row["vertices"] = len(o.data.vertices)
        row["faces"] = len(o.data.polygons)
    rows.append(row)
print(json.dumps({"total": total, "offset": off, "returned": len(rows), "objects": rows}))
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

engine_ids = [e.identifier for e in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items]
aliases = {
    "CYCLES": "CYCLES",
    "WORKBENCH": "BLENDER_WORKBENCH",
    "EEVEE": "BLENDER_EEVEE_NEXT" if "BLENDER_EEVEE_NEXT" in engine_ids else "BLENDER_EEVEE",
}
formats = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".exr": "OPEN_EXR", ".tif": "TIFF", ".tiff": "TIFF"}
ext = os.path.splitext(_P["path"])[1].lower()
if ext not in formats:
    raise Exception("Unsupported output extension %r; use one of %s" % (ext, sorted(formats)))

rs = scene.render
saved = {
    "engine": rs.engine, "pct": rs.resolution_percentage, "path": rs.filepath,
    "fmt": rs.image_settings.file_format, "camera": scene.camera,
}
cycles_samples = scene.cycles.samples if hasattr(scene, "cycles") else None
eevee_samples = scene.eevee.taa_render_samples if hasattr(scene, "eevee") else None
try:
    if _P.get("engine"):
        want = aliases.get(_P["engine"].upper())
        if want is None or want not in engine_ids:
            raise Exception("Engine %r not available. Available: %s" % (_P["engine"], engine_ids))
        rs.engine = want
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
    bpy.ops.render.render(write_still=True)
    out = {"path": _P["path"], "engine": rs.engine, "samples": scene.cycles.samples if rs.engine == "CYCLES" else None,
           "resolution": [rs.resolution_x * rs.resolution_percentage // 100,
                          rs.resolution_y * rs.resolution_percentage // 100],
           "size_bytes": os.path.getsize(_P["path"])}
finally:
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
import sys, os
_dir = os.path.dirname(_P["path"])
if _dir not in sys.path:
    sys.path.insert(0, _dir)
_ns = {"__name__": "__main__", "__file__": _P["path"], "bpy": bpy, "bmesh": bmesh, "math": math,
       "mathutils": mathutils, "json": json, "SCRIPT_ARGS": _P["args"]}
exec(compile(_P["source"], _P["path"], "exec"), _ns)
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
    return _compact(data)


@_tool(read_only=True)
async def scene_overview() -> str:
    """Summarise the current scene: file, frame range, render settings, units, object counts by type,
    active/selected objects and the collection hierarchy. Use it to orient yourself before editing."""
    try:
        return _compact(await _query(SNIP_OVERVIEW))
    except ToolError as e:
        if not _is_code_disabled(e):
            raise
        info = await call("get_scene_info")
        info["note"] = "Code execution is disabled in the add-on panel; showing the reduced built-in summary."
        return _compact(info)


@mcp.resource("blender://scene")
async def scene_resource() -> str:
    """Live scene overview (same data as the scene_overview tool)."""
    return _compact(await _query(SNIP_OVERVIEW))


@_tool(read_only=True)
async def list_objects(
    type: Annotated[Optional[str], Field(description="Filter by object type: MESH, CAMERA, LIGHT, EMPTY, CURVE, ARMATURE, ...")] = None,
    name_contains: Annotated[Optional[str], Field(description="Case-insensitive substring of the object name")] = None,
    collection: Annotated[Optional[str], Field(description="Only objects inside this collection (including children)")] = None,
    selected_only: bool = False,
    limit: Annotated[int, Field(ge=1, le=500)] = 50,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> str:
    """List objects in the scene with location, dimensions, parent, collections, materials and (for
    meshes) vertex/face counts of the base mesh, sorted by name and paginated. Use get_object_details
    for a single object's full state."""
    params = dict(type=type, name_contains=name_contains, collection=collection,
                  selected_only=selected_only, limit=limit, offset=offset)
    return _compact(await _query(SNIP_LIST, params))


@_tool(read_only=True)
async def get_object_details(
    name: Annotated[str, Field(description="Exact object name (case-sensitive)")],
) -> str:
    """Full state of one object: transform (rotation in degrees), world bounding box, hierarchy,
    visibility, modifiers, constraints, materials (Principled BSDF values), custom properties,
    animation, and type-specific data (mesh stats incl. evaluated triangle count, camera lens, light
    settings). If the name is wrong, similar names are suggested."""
    return _compact(await _query(SNIP_DETAILS, {"name": name}))


@_tool(read_only=True)
async def viewport_screenshot(
    max_size: Annotated[int, Field(ge=128, le=2048, description="Largest image dimension in pixels")] = 800,
    save_to: Annotated[Optional[str], Field(description="Optionally also save the PNG to this path")] = None,
) -> Image:
    """Capture the active 3D viewport as an image. Use it after every meaningful change to check the
    result visually. Shows what the user sees (shading mode, selection, overlays); it requires a 3D
    Viewport to be open in Blender."""
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
    if save_to:
        dest = _path(save_to)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    return Image(data=data, format="png")


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
    restored afterwards. For a quick look at the work in progress prefer viewport_screenshot."""
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
    code execution' is switched off in the add-on panel."""
    out = await _run_py(code, timeout_s)
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


# --------------------------------------------------------------------------- tools: Poly Haven


@_tool(read_only=True, open_world=True)
async def polyhaven_search(
    asset_type: Literal["hdris", "textures", "models", "all"] = "all",
    categories: Annotated[Optional[str], Field(description="Comma-separated Poly Haven categories, e.g. 'wood,floor'")] = None,
) -> str:
    """Search Poly Haven (CC0 HDRIs, PBR textures, models). Returns up to 20 assets plus the total count;
    asset ids are what polyhaven_download needs. Requires 'Use assets from Poly Haven' in the panel."""
    result = await call("search_polyhaven_assets", {"asset_type": asset_type, "categories": categories},
                        integration="Poly Haven")
    return _compact(result)


@_tool(read_only=False, destructive=False, open_world=True)
async def polyhaven_download(
    asset_id: str,
    asset_type: Literal["hdris", "textures", "models"],
    resolution: Annotated[str, Field(description="1k, 2k, 4k, ... (smaller imports faster)")] = "1k",
    file_format: Annotated[Optional[str], Field(description="Optional: hdr/exr for HDRIs, jpg/png for textures, gltf/fbx for models")] = None,
    timeout_s: Annotated[float, Field(ge=5, le=1800)] = 180,
) -> str:
    """Download a Poly Haven asset and import it: HDRIs become the world lighting, textures become a
    material (apply it with polyhaven_apply_texture), models are imported into the scene."""
    result = await call("download_polyhaven_asset",
                        {"asset_id": asset_id, "asset_type": asset_type, "resolution": resolution,
                         "file_format": file_format},
                        timeout=timeout_s, integration="Poly Haven")
    return _compact(result)


@_tool(read_only=False)
async def polyhaven_apply_texture(object_name: str, texture_id: str) -> str:
    """Apply a texture previously fetched with polyhaven_download to an object (creates a new material)."""
    result = await call("set_texture", {"object_name": object_name, "texture_id": texture_id},
                        integration="Poly Haven")
    return _compact(result)


# --------------------------------------------------------------------------- tools: Sketchfab


@_tool(read_only=True, open_world=True)
async def sketchfab_search(
    query: str,
    categories: Annotated[Optional[str], Field(description="Sketchfab category slug(s)")] = None,
    count: Annotated[int, Field(ge=1, le=24)] = 10,
    downloadable: bool = True,
) -> str:
    """Search Sketchfab models. Returns uid, name, author, face/vertex counts and license for each hit;
    check the license before using a model. Needs the Sketchfab API key set in the panel."""
    data = await call("search_sketchfab_models",
                      {"query": query, "categories": categories, "count": count, "downloadable": downloadable},
                      integration="Sketchfab")
    rows = []
    for r in (data or {}).get("results", []):
        lic = r.get("license")
        rows.append({
            "uid": r.get("uid"), "name": r.get("name"),
            "author": (r.get("user") or {}).get("username"),
            "faces": r.get("faceCount"), "vertices": r.get("vertexCount"),
            "license": lic.get("label") if isinstance(lic, dict) else lic,
            "url": r.get("viewerUrl"),
        })
    return _compact({"count": len(rows), "results": rows})


@_tool(read_only=True, open_world=True)
async def sketchfab_preview(uid: str) -> list:
    """Fetch the thumbnail of a Sketchfab model, to judge it before importing."""
    data = await call("get_sketchfab_model_preview", {"uid": uid}, integration="Sketchfab")
    try:
        img = Image(data=base64.b64decode(data["image_data"]), format=data.get("format", "jpeg"))
    except (KeyError, ValueError, TypeError):
        raise ToolError("Sketchfab returned no usable preview image") from None
    meta = {k: data.get(k) for k in ("model_name", "author", "uid", "thumbnail_width", "thumbnail_height")}
    return [_compact(meta), img]


@_tool(read_only=False, destructive=False, open_world=True)
async def sketchfab_download(
    uid: str,
    normalize_size: Annotated[bool, Field(description="Scale so the largest dimension equals target_size")] = False,
    target_size: Annotated[float, Field(gt=0, description="Target size in metres when normalize_size is true")] = 1.0,
    timeout_s: Annotated[float, Field(ge=10, le=1800)] = 240,
) -> str:
    """Download a Sketchfab model (glTF) and import it into the scene. Large models can take a while
    and freeze Blender while importing."""
    result = await call("download_sketchfab_model",
                        {"uid": uid, "normalize_size": normalize_size, "target_size": target_size},
                        timeout=timeout_s, integration="Sketchfab")
    return _compact(result)


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
        "1. Call blender_status, then scene_overview, and inspect anything relevant with list_objects / "
        "get_object_details.\n"
        "2. Plan the steps briefly (units are metres, Z-up).\n"
        "3. Implement in small, idempotent scripts saved in the project and run with run_blender_script.\n"
        "4. After each step call viewport_screenshot and compare with the goal; fix before moving on.\n"
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
