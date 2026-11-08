# Code created by Daniel Galleguillos: https://github.com/dnlgalleguillos
# Copyright (C) 2026 Daniel Galleguillos Cruz
# SPDX-License-Identifier: GPL-3.0-or-later
"""Safe-mode validator tests (pure Python, no Blender needed).

    uv run --no-project --with "mcp>=1.10,<2" --with httpx python -I tests/test_safe_mode.py
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("server", ROOT / "blender_mcp_server.py")
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)

GOOD = {
    "primitive + rename": """
import bpy
bpy.ops.mesh.primitive_cube_add(size=2, location=(0, 0, 1))
o = bpy.context.active_object
o.name = "Box"
""",
    "bmesh / mathutils / math / random": """
import bpy, bmesh, math, random
from mathutils import Vector, Matrix
bm = bmesh.new()
bmesh.ops.create_cube(bm, size=1.0)
me = bpy.data.meshes.new("m")
bm.to_mesh(me)
bm.free()
o = bpy.data.objects.new("o", me)
bpy.context.collection.objects.link(o)
o.location = Vector((math.cos(1), math.sin(1), random.random()))
""",
    "materials and nodes": """
import bpy
mat = bpy.data.materials.new("M")
mat.use_nodes = True
bsdf = mat.node_tree.nodes["Principled BSDF"]
bsdf.inputs["Base Color"].default_value = (1, 0, 0, 1)
""",
    "getattr with a literal name": 'import bpy\nprint(getattr(bpy.context.scene, "frame_end", 250))',
    "render / import / export operators": """
import bpy
bpy.ops.render.render(write_still=True)
bpy.ops.export_scene.gltf(filepath="/tmp/x.glb")
bpy.ops.wm.obj_import(filepath="/tmp/x.obj")
""",
    "defs, classes, comprehensions, f-strings, try, lambda, __name__": """
import bpy
from collections import defaultdict
class Box:
    def __init__(self, n):
        self.n = n
def make(n):
    try:
        return [Box(i) for i in range(n)]
    except Exception as e:
        print(f"failed: {e}")
groups = defaultdict(list)
f = lambda x: x * 2
if __name__ == "__main__":
    make(3)
""",
    "json and re": 'import json, re\nprint(json.dumps({"a": re.sub("x", "y", "xx")}))',
    "aliased bpy": "import bpy as b\nb.ops.mesh.primitive_uv_sphere_add()",
    "from-import of safe names": "from bpy import ops\nfrom mathutils import noise\nops.mesh.primitive_plane_add()",
    "animation keyframes": """
import bpy
o = bpy.context.active_object
o.location = (0, 0, 0)
o.keyframe_insert(data_path="location", frame=1)
""",
}

# (code, substring expected in the reason)
BAD = {
    "import os": ("import os", "import of 'os'"),
    "import subprocess": ("import subprocess", "subprocess"),
    "from os import system": ("from os import system", "not allowed"),
    "import sys": ("import sys", "'sys'"),
    "__import__": ('__import__("os")', "__import__"),
    "eval": ('eval("1+1")', "eval"),
    "exec": ('exec("x=1")', "exec"),
    "compile": ('compile("1", "f", "eval")', "compile"),
    "open": ('open("/etc/passwd")', "open"),
    "frame handler": ("import bpy\nbpy.app.handlers.frame_change_pre.append(print)", "handlers"),
    "timer": ("import bpy\nbpy.app.timers.register(print)", "timers"),
    "aliased timer": ("import bpy as b\nb.app.timers.register(print)", "timers"),
    "alias chain": ("import bpy\nx = bpy.app\ny = x.timers\ny.register(print)", "timers"),
    "from-import handlers": ("from bpy.app import handlers\nhandlers.frame_change_pre.append(print)", "handlers"),
    "from-import timers": ("from bpy.app import timers\ntimers.register(print)", "timers"),
    "script operator via alias": ("import bpy\nops = bpy.ops\nops.script.python_file_run(filepath='x.py')", "bpy.ops.script"),
    "script operator imported by name": ("from bpy.ops.script import python_file_run\npython_file_run(filepath='x')", "bpy.ops.script"),
    "text operator": ("import bpy\nbpy.ops.text.run_script()", "bpy.ops.text"),
    "extensions operator": ("import bpy\nbpy.ops.extensions.package_install()", "bpy.ops.extensions"),
    "url_open": ("import bpy\nbpy.ops.wm.url_open(url='http://x')", "url_open"),
    "quit": ("import bpy\nbpy.ops.wm.quit_blender()", "quit_blender"),
    "save as": ("import bpy\nbpy.ops.wm.save_as_mainfile(filepath='/tmp/x.blend')", "save_as_mainfile"),
    "open file": ("import bpy\nbpy.ops.wm.open_mainfile(filepath='/tmp/x.blend')", "open_mainfile"),
    "append from another blend": ("import bpy\nbpy.ops.wm.append(filepath='x')", "wm.append"),
    "preferences": ("import bpy\nbpy.context.preferences.filepaths.script_directories", "script_directories"),
    "register_class": ("import bpy\nbpy.utils.register_class(object)", "register_class"),
    "assign to bpy.types": ("import bpy\nbpy.types.Scene.x = 1", "bpy.types"),
    "bpy.props": ("import bpy\nx = bpy.props.IntProperty()", "bpy.props"),
    "dunder ladder": ("().__class__.__bases__[0].__subclasses__()", "__subclasses__"),
    "computed getattr": ('import bpy\ngetattr(bpy, "ap" + "p")', "literal"),
    "getattr to a denied name": ('import bpy\ngetattr(bpy.app, "handlers")', "literal"),
    "getattr to a dunder": ('getattr(1, "__class__")', "literal"),
    "getattr as a value": ("f = getattr\nf(1, 'x')", "getattr"),
    "format-string traversal": ('"{0.__class__}".format(1)', "format strings"),
    "reach codecs through json": ('import json\njson.codecs.open("/etc/passwd")', "codecs"),
    "private attribute": ('import random\nrandom._os.system("id")', "private"),
    "typing import": ("import typing", "typing"),
    "pathlib import": ("import pathlib", "pathlib"),
    "texts datablock": ("import bpy\nbpy.data.texts.new('x')", "bpy.data.texts"),
    "libraries": ("import bpy\nbpy.data.libraries.load('x')", "bpy.data.libraries"),
    "driver_add": ("import bpy\nbpy.context.object.driver_add('location')", "driver_add"),
    "async def": ("async def f():\n    pass", "async"),
    "alias rebinding": ("import bpy\nbpy = 1", "bound more than once"),
    "alias rebinding in a loop": ("import bpy as b\nfor b in range(3):\n    pass", "bound more than once"),
    "vars": ("import bpy\nvars(bpy)", "vars"),
    "globals": ("globals()", "globals"),
    "breakpoint": ("breakpoint()", "breakpoint"),
    "frame escape": ("def f():\n    yield 1\ng = f()\ng.gi_frame.f_back", "f_back"),
    "relative import": ("from . import x", "not allowed"),
    "syntax error": ("def (:", "syntax error"),
    "too long": ("x = 1\n" * 100_000, "larger than"),
    "too deeply nested": ("(" * 3000 + ")" * 3000, None),
}

ok = True


def check(name, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name + (f"   <-- {extra}" if not cond and extra else ""))


for name, code in GOOD.items():
    try:
        S.validate_code(code)
        check(f"allowed: {name}", True)
    except S.SafeModeViolation as e:
        check(f"allowed: {name}", False, str(e))

for name, (code, needle) in BAD.items():
    try:
        S.validate_code(code)
        check(f"blocked: {name}", False, "was accepted")
    except S.SafeModeViolation as e:
        check(f"blocked: {name}", needle is None or needle.lower() in str(e).lower(), str(e)[:120])

# the tool-level guard is a no-op unless safe mode is on, and a clean ToolError when on
S.SAFE_MODE = False
S._guard_code("import os")
check("guard is a no-op when safe mode is off", True)
S.SAFE_MODE = True
try:
    S._guard_code("import os")
    check("guard raises ToolError when safe mode is on", False)
except S.ToolError as e:
    check("guard raises ToolError when safe mode is on", "Safe mode" in str(e) and "import of 'os'" in str(e), str(e))
S._guard_code("import bpy\nbpy.ops.mesh.primitive_cube_add()")
check("guard lets valid code through when on", True)

print("\nALL OK" if ok else "\nSOME FAILED")
sys.exit(0 if ok else 1)
