# Code created by Daniel Galleguillos: https://github.com/dnlgalleguillos
# Copyright (C) 2026 Daniel Galleguillos Cruz
# SPDX-License-Identifier: GPL-3.0-or-later
"""Test harness: the real add-on running inside a real (headless) Blender, driven by the real MCP server code.

Needs the `bpy` module (Blender as a Python library), whose wheels target Python 3.11:

    uv run --no-project --python 3.11 --with bpy --with "mcp>=1.10,<2" --with httpx python -I tests/test_blender.py

Why a harness is needed: the add-on runs commands on Blender's main thread from a bpy.app.timers callback, but a
headless `bpy` module has no event loop, so timers never fire. The add-on's `bpy` is therefore replaced by a thin
proxy whose `app.timers` just remembers the callbacks; the main thread calls them in a loop (honouring "return None
= unregister") while the async tests run in a worker thread. bpy.ops must only ever run on the main thread.
"""
import asyncio
import importlib.util
import os
import sys
import threading
import time
import traceback
from pathlib import Path

import bpy

ROOT = Path(__file__).resolve().parent.parent
_results = {"ok": True, "pass": 0, "fail": 0, "skip": 0}
_callbacks: list = []


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Timers:
    @staticmethod
    def register(fn, first_interval=0.0, persistent=False):
        _callbacks.append(fn)

    @staticmethod
    def unregister(fn):
        if fn in _callbacks:
            _callbacks.remove(fn)

    @staticmethod
    def is_registered(fn):
        return fn in _callbacks


class _App:
    def __init__(self, real):
        self._real = real
        self.timers = _Timers()

    def __getattr__(self, name):
        return getattr(self._real, name)


class _BpyProxy:
    def __init__(self, real):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_app", _App(real.app))

    def __getattr__(self, name):
        return self._app if name == "app" else getattr(self._real, name)


def load_addon():
    """The add-on module, registered, with its timers routed to this harness."""
    addon = load_module("blender_mcp_addon", ROOT / "blender_mcp.py")
    addon.bpy = _BpyProxy(bpy)
    addon.register()
    addon.log.setLevel(40)
    return addon


def load_server(port: int, **env):
    """A fresh copy of the MCP server module configured through the same environment variables a user sets."""
    keys = ("BLENDER_PORT", "BLENDER_MCP_TOKEN", "BLENDER_MCP_READONLY", "BLENDER_MCP_SAFE_MODE", "BLENDER_TIMEOUT",
            "POLYPIZZA_API_KEY", "SKETCHFAB_API_KEY", "HYPER3D_API_KEY", "HYPER3D_PLATFORM", "HUNYUAN3D_SECRET_ID",
            "HUNYUAN3D_SECRET_KEY", "HUNYUAN3D_API_URL", "HUNYUAN3D_MODE")
    for k in keys:
        os.environ.pop(k, None)
    os.environ["BLENDER_PORT"] = str(port)
    os.environ["BLENDER_TIMEOUT"] = "60"
    os.environ.update({k: str(v) for k, v in env.items()})
    return load_module(f"mcp_server_{port}_{len(sys.modules)}", ROOT / "blender_mcp_server.py")


def run(async_main):
    """Run `async_main()` in a worker thread; this (main) thread executes the queued Blender callbacks."""
    done, box = threading.Event(), {}

    def worker():
        try:
            asyncio.run(async_main())
        except BaseException as e:  # noqa: BLE001 - report everything
            box["error"] = e
            traceback.print_exc(file=sys.__stderr__)
        finally:
            done.set()

    threading.Thread(target=worker, daemon=True).start()
    while not done.is_set():
        for fn in list(_callbacks):
            if fn() is None and fn in _callbacks:
                _callbacks.remove(fn)
        time.sleep(0.001)
    if "error" in box:
        check("test run finished without an unexpected exception", False, repr(box["error"]))


def check(name, cond, extra=""):
    _results["ok"] &= bool(cond)
    _results["pass" if cond else "fail"] += 1
    print(("PASS " if cond else "FAIL ") + name + (f"   <-- {extra}" if not cond and extra else ""), file=sys.__stdout__,
          flush=True)


def skip(name, reason):
    _results["skip"] += 1
    print(f"SKIP {name}   ({reason})", file=sys.__stdout__, flush=True)


async def raises(coro, *needles):
    """(matched, message): did the coroutine raise a ToolError mentioning all needles?"""
    from mcp.server.fastmcp.exceptions import ToolError
    try:
        await coro
    except ToolError as e:
        msg = str(e)
        return all(n.lower() in msg.lower() for n in needles), msg
    return False, "no error raised"


def finish():
    print(f"\n{_results['pass']} passed, {_results['fail']} failed, {_results['skip']} skipped", file=sys.__stdout__)
    print("ALL OK" if _results["ok"] else "SOME FAILED", file=sys.__stdout__, flush=True)
    sys.exit(0 if _results["ok"] else 1)
