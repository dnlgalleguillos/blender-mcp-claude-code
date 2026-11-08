# Code created by Daniel Galleguillos: https://github.com/dnlgalleguillos
# Copyright (C) 2026 Daniel Galleguillos Cruz
# SPDX-License-Identifier: GPL-3.0-or-later
"""The Blender add-on (bridge) on its own, against a real headless Blender (see blender_harness.py)."""
import asyncio
import json
import socket
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import blender_harness as h  # noqa: E402
from blender_harness import bpy, check  # noqa: E402

PORT = 19991

# bpy.ops must run on the main thread: scene, add-on registration and the Connect operator are set up here.
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add(size=2, location=(0, 0, 1))
addon = h.load_addon()
wm = bpy.context.window_manager
wm.blendermcp_port = PORT
connect_result = bpy.ops.blendermcp.start()  # the real Connect button


class Client:
    def __init__(self, port=PORT):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=15)
        self.decoder, self.buffer = json.JSONDecoder(), ""

    def send_raw(self, data: bytes):
        self.sock.sendall(data)

    def send(self, obj):
        self.send_raw(json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def recv(self):
        while True:
            self.buffer = self.buffer.lstrip()
            if self.buffer:
                try:
                    obj, end = self.decoder.raw_decode(self.buffer)
                    self.buffer = self.buffer[end:]
                    return obj
                except json.JSONDecodeError:
                    pass
            chunk = self.sock.recv(1 << 20)
            if not chunk:
                return None
            self.buffer += chunk.decode("utf-8")

    def call(self, kind, **params):
        self.send({"type": kind, "params": params})
        return self.recv()

    def closed(self, timeout=3):
        self.sock.settimeout(timeout)
        try:
            return self.sock.recv(1) == b""
        except (ConnectionError, socket.timeout, OSError):
            return True

    def close(self):
        self.sock.close()


async def main():
    check("the Connect button started the bridge", connect_result == {"FINISHED"} and addon.bridge_running())
    c = Client()

    # ------------------------------------------------------------------ ping
    r = c.call("ping")
    p = r["result"]
    check("ping: add-on, protocol and Blender facts", r["status"] == "success" and p["addon"] == "blender-mcp-bridge"
          and p["protocol"] == 2 and p["addon_version"] == "2.0.0" and p["blender_version"] == bpy.app.version_string
          and p["scene"] == "Scene" and p["objects"] == 1 and p["code_execution"] is True, r)

    # ------------------------------------------------------------------ execute_code
    r = c.call("execute_code", code="print(len(bpy.data.objects), round(math.pi, 2), mathutils.Vector((3, 4, 0)).length)\nbmesh.new().free()")
    check("execute_code: stdout back, bpy/bmesh/mathutils/math preloaded", r["result"] == {"executed": True, "result": "1 3.14 5.0\n"}, r)
    r = c.call("execute_code", code="x = 1\ny = 2\nprint(x + zzz)\n")
    check("execute_code: error names the line of the script", r["status"] == "error" and "NameError" in r["message"]
          and "line 3: print(x + zzz)" in r["message"], r)
    r = c.call("execute_code", code="def broken(:\n    pass")
    check("execute_code: syntax errors say where", r["status"] == "error" and "SyntaxError" in r["message"] and "line 1" in r["message"], r)
    r = c.call("execute_code", code="import bpy\nbpy.data.objects['Cube'].name = 'Renamed'")
    check("execute_code: real changes reach Blender", "Renamed" in bpy.data.objects, r)
    r = c.call("execute_code", code=123)
    check("execute_code: a non-string is rejected cleanly", r["status"] == "error" and "string" in r["message"], r)
    wm.blendermcp_allow_code = False
    r = c.call("execute_code", code="print('should not run')")
    check("code execution switched off in the panel is refused", r["status"] == "error" and "disabled" in r["message"]
          and "Allow code execution" in r["message"], r)
    check("ping still works and reports the switch", c.call("ping")["result"]["code_execution"] is False)
    wm.blendermcp_allow_code = True
    check("switched on again", c.call("execute_code", code="print('ok')")["result"]["result"] == "ok\n")

    # ------------------------------------------------------------------ protocol robustness
    r = c.call("nope")
    check("unknown command", r["status"] == "error" and "Unknown command type: nope" in r["message"], r)
    c.send([1, 2, 3])
    r = c.recv()
    check("a non-object command is rejected, the connection survives", r["status"] == "error" and c.call("ping")["status"] == "success", r)
    r = c.call("ping", bogus=1)
    check("unexpected parameters give an error, not a crash", r["status"] == "error", r)

    msg = json.dumps({"type": "execute_code", "params": {"code": "print(6 * 7)"}}).encode()
    c.send_raw(msg[:12])
    time.sleep(0.15)
    c.send_raw(msg[12:])
    check("a command split across packets", c.recv()["result"]["result"] == "42\n")
    c.send_raw(msg + msg + msg)
    check("three commands in one packet: three answers", [c.recv()["result"]["result"] for _ in range(3)] == ["42\n"] * 3)
    utf = json.dumps({"type": "execute_code", "params": {"code": "print('ñandú 日本')"}}, ensure_ascii=False).encode()
    cut = utf.index("ñ".encode()) + 1
    c.send_raw(utf[:cut])
    time.sleep(0.1)
    c.send_raw(utf[cut:])
    check("a multi-byte character split across packets", c.recv()["result"]["result"] == "ñandú 日本\n")
    for i in range(1, 6):
        c.send({"type": "execute_code", "params": {"code": f"import time\ntime.sleep(0.02 * (6 - {i}))\nprint({i})"}})
    check("pipelined commands are answered in the order they were sent", [c.recv()["result"]["result"].strip() for _ in range(5)] == list("12345"))
    big = "# " + "x" * (5 * 1024 * 1024) + "\nprint('big ok')"
    check("a 5 MB script goes through", c.call("execute_code", code=big)["result"]["result"] == "big ok\n")

    a, b = Client(), Client()
    a.send({"type": "execute_code", "params": {"code": "print('A')"}})
    b.send({"type": "execute_code", "params": {"code": "print('B')"}})
    check("several clients at once, each gets its own answer", a.recv()["result"]["result"] == "A\n" and b.recv()["result"]["result"] == "B\n")
    a.close(); b.close()

    r = c.call("get_viewport_screenshot", max_size=300, filepath="/tmp/blender_mcp_test_shot.png")
    check("screenshot without a real window: a clear error, not a crash (the capture itself needs a visible Blender window)",
          r["status"] == "error" and ("Could not capture the 3D viewport" in r["message"] or "No 3D viewport" in r["message"])
          and "window" in r["message"].lower() or "open a 3D Viewport" in r["message"], r)
    r = c.call("get_viewport_screenshot")
    check("screenshot requires a file path", r["status"] == "error" and "filepath" in r["message"], r)

    # ------------------------------------------------------------------ oversize, token, lifecycle
    small = addon.Bridge(port=PORT + 1)
    small.max_message_chars = 2000
    assert small.start()
    s = Client(PORT + 1)
    s.send_raw(b'{"type": "execute_code", "params": {"code": "' + b"x" * 3000)
    r = s.recv()
    check("a message over the size limit: error, then the connection is closed", r["status"] == "error"
          and "too large" in r["message"].lower() and s.closed(), r)
    small.stop()

    secured = addon.Bridge(port=PORT + 2, token="s3cret")
    assert secured.start()
    t = Client(PORT + 2)
    check("token: missing -> Unauthorized", "Unauthorized" in t.call("ping")["message"])
    t.send({"type": "ping", "params": {}, "token": "wrong"})
    check("token: wrong -> Unauthorized", "Unauthorized" in t.recv()["message"])
    t.send({"type": "ping", "params": {}, "token": "s3cret"})
    check("token: right -> accepted", t.recv()["status"] == "success")
    t.close()
    secured.stop()

    dup = addon.Bridge(port=PORT)
    check("starting on a port that is in use returns False", dup.start() is False)
    started = time.time()
    other = Client()
    addon._bridge.stop()
    check("stop() closes connected clients promptly", c.closed() and other.closed() and time.time() - started < 3)
    await asyncio.sleep(0.2)
    check("after stop() the pump timer unregisters itself (returns None)", h._callbacks == [], h._callbacks)
    again = addon.Bridge(port=PORT)
    check("the same port can be reused right after stopping", again.start() is True)
    again.stop()
    c.close(); other.close()

    # ------------------------------------------------------------------ UI, privacy, what is (not) stored
    class Layout:
        def __init__(self, log): self.log = log
        def box(self): return self
        def row(self): return self
        def label(self, text="", **k): self.log.append(text)
        def prop(self, obj, name, **k): self.log.append(f"<prop {name}>")
        def operator(self, idname, **k): self.log.append(f"<operator {idname}>")
    shown = []
    addon.BLENDERMCP_Preferences.draw(types.SimpleNamespace(layout=Layout(shown)), None)
    text = " ".join(shown).lower()
    check("preferences: token + privacy statement, no telemetry switch", "<prop auth_token>" in text
          and "collects and sends nothing" in text and "telemetry" not in text and "terms" not in text, shown)
    check("nothing is stored in .blend files (settings live on the window manager, not the scene)",
          not any(n.startswith("blendermcp_") for n in dir(bpy.types.Scene)) and hasattr(bpy.types.WindowManager, "blendermcp_port"))


h.run(main)
addon.unregister()  # class registration also belongs on the main thread
check("unregister removes the session settings", not hasattr(bpy.types.WindowManager, "blendermcp_port"))
addon.register()
check("register again after unregister", hasattr(bpy.types.WindowManager, "blendermcp_port"))
check("the Disconnect button works when nothing is running", bpy.ops.blendermcp.stop() == {"FINISHED"})
h.finish()
