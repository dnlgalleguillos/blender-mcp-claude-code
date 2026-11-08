# Code created by Daniel Galleguillos: https://github.com/dnlgalleguillos
# Copyright (C) 2026 Daniel Galleguillos Cruz
# SPDX-License-Identifier: GPL-3.0-or-later
"""Assets, AI generation and the full MCP protocol, against a real headless Blender (see blender_harness.py).

Poly Haven is queried live (public API, no key); every other external service is simulated, but Blender, the
add-on, the glTF importer and the MCP server/SDK are the real ones.
"""
import asyncio
import glob
import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import blender_harness as h  # noqa: E402
from blender_harness import bpy, check, raises, skip  # noqa: E402

import httpx  # noqa: E402

PORT = 19980
PNG = b"\x89PNG\r\n\x1a\n" + b"fake-thumbnail-bytes" * 4

# ---- main-thread setup: an empty scene, and a real 2 m cube exported as GLB to play the part of a Poly Pizza model
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add(size=2, location=(0, 0, 1))
_tmp = tempfile.mkdtemp()
bpy.ops.export_scene.gltf(filepath=os.path.join(_tmp, "model.glb"), export_format="GLB")
GLB = Path(_tmp, "model.glb").read_bytes()
bpy.data.objects.remove(bpy.data.objects["Cube"], do_unlink=True)
addon = h.load_addon()
M = addon.BlenderMCPServer


def hresp(status=200, js=None, content=b"", headers=None, url="https://example.test/"):
    req = httpx.Request("GET", url)
    if js is not None:
        return httpx.Response(status, json=js, headers=headers, request=req)
    return httpx.Response(status, content=content, headers=headers, request=req)


class R:  # a `requests`-style response, for the add-on's own HTTP helpers
    def __init__(self, js=None, content=b"", status=200, headers=None):
        self._js, self.content, self.status_code, self.headers, self.text = js, content, status, headers or {}, str(js)

    def json(self):
        return self._js

    def raise_for_status(self):
        pass


def gather_names():
    return set(bpy.data.objects.keys())


async def main():
    sc = bpy.context.scene
    srv = addon.BlenderMCPServer(port=PORT)
    srv.start()
    assert srv.running, "the add-on socket did not start"
    S = h.load_server(PORT)

    # ================================================================== Poly Haven (live public API)
    try:
        out = await S.search_assets(source="polyhaven", query="armchair", asset_type="models")
        live = True
    except Exception as e:  # noqa: BLE001
        live = False
        skip("Poly Haven live search", f"no network: {str(e)[:80]}")
    if live:
        check("polyhaven: finds the armchair by name", "ArmChair_01" in out and out.startswith("Poly Haven (CC0)"), out[:300])
        check("polyhaven: lines carry type, polycount, real size and tags", re.search(r"ArmChair_01 \| .* \| models \| .* \| \d+ polys \| size [\d.]+ x [\d.]+ x [\d.]+ m \| tags:", out), out[:400])
        out = await S.search_assets(source="polyhaven", query="couch", asset_type="models")
        check("polyhaven: synonyms (couch finds sofas)", "sofa" in out.lower(), out[:300])
        out = await S.search_assets(source="polyhaven", query="floor", asset_type="textures", min_size_m=2, limit=40)
        tiles = [max(float(x) for x in m) for m in re.findall(r"tile ([\d.]+) x ([\d.]+)", out)]
        check("polyhaven: min_size_m keeps only large real-world tiles", tiles and min(tiles) >= 2.0, (len(tiles), out[:200]))
        out = await S.search_assets(source="polyhaven", query="zzzzqqqq")
        check("polyhaven: no match -> helpful text", "Try other words" in out, out)
        res = await S.search_assets(source="polyhaven", query="armchair", asset_type="models", previews=1)
        check("polyhaven: previews attach a real thumbnail image (the CDN serves WebP)", isinstance(res, list) and len(res) == 2
              and res[1].data[:4] == b"RIFF" and res[1].data[8:12] == b"WEBP" and res[1].to_image_content().mimeType == "image/webp", [type(x).__name__ for x in res]
              if isinstance(res, list) else res)

    # ================================================================== Poly Haven import (add-on download simulated)
    sc.blendermcp_use_polyhaven = True
    calls = {}

    def fake_download(self, asset_id, asset_type, resolution="1k", file_format=None):
        calls["download"] = dict(asset_id=asset_id, asset_type=asset_type, resolution=resolution, file_format=file_format)
        if asset_type == "models":
            me = bpy.data.meshes.new("PHMesh")
            v = [(x, y, z) for x in (0, 10) for y in (0, 10) for z in (0, 10)]
            me.from_pydata(v, [], [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)])
            ob = bpy.data.objects.new("PHModel", me)
            bpy.context.scene.collection.objects.link(ob)
            return {"success": True, "message": "Model imported", "imported_objects": [ob.name]}
        if asset_type == "textures":
            bpy.data.materials.new("PHMat")
            return {"success": True, "message": "Texture imported as material", "material": "PHMat"}
        return {"success": True, "message": "HDRI imported"}

    M.download_polyhaven_asset = fake_download
    info_calls = []

    async def fake_http_get(url, *, params=None, headers=None, timeout=30, redirects=True):
        info_calls.append(url)
        if "/info/" in url:
            return hresp(js={"type": 2})
        raise AssertionError("unexpected GET " + url)

    S._http_get = fake_http_get
    out = json.loads(await S.import_asset(source="polyhaven", id="Chair_X", target_size=2.0))
    placed = out["placed"]
    check("polyhaven import: type looked up, model scaled to target_size", info_calls and calls["download"]["asset_type"] == "models"
          and abs(max(placed["dimensions_m"]) - 2.0) < 1e-3 and abs(placed["scale_applied"] - 0.2) < 1e-4, out)
    check("polyhaven import: licence reported and the object really scaled", "CC0" in out["licence"]
          and abs(bpy.data.objects["PHModel"].scale.x - 0.2) < 1e-4)
    bpy.data.objects.new("Target", bpy.data.meshes.new("TargetMesh"))
    sc.collection.objects.link(bpy.data.objects["Target"])
    out = json.loads(await S.import_asset(source="polyhaven", id="Wood_X", asset_type="textures", apply_to=["Target", "NoSuch"]))
    check("polyhaven textures: material assigned, missing objects reported", out["material"]["assigned"] == ["Target"]
          and out["material"]["skipped"] == ["NoSuch"] and bpy.data.objects["Target"].data.materials[0].name == "PHMat", out)
    bpy.data.materials.new("PHMat2")
    M.download_polyhaven_asset = lambda self, **k: {"success": True, "material": "PHMat2"}
    out = json.loads(await S.import_asset(source="polyhaven", id="Wood_Y", asset_type="textures"))
    check("polyhaven textures: unassigned material is protected from being dropped on save",
          out["material"]["kept_with_fake_user"] is True and bpy.data.materials["PHMat2"].use_fake_user and "apply_to" in out["note"], out)
    M.download_polyhaven_asset = fake_download
    out = json.loads(await S.import_asset(source="polyhaven", id="Sky_X", asset_type="hdris"))
    check("polyhaven hdri", "world lighting" in out["note"], out)
    sc.blendermcp_use_polyhaven = False
    r = await raises(S.import_asset(source="polyhaven", id="Chair_X", asset_type="models"), "poly haven", "disabled")
    check("polyhaven disabled in the panel -> clear error", r[0], r[1])

    # ================================================================== Sketchfab (through the add-on; API simulated)
    sc.blendermcp_use_sketchfab, sc.blendermcp_sketchfab_api_key = True, "sk-test"
    hits = {"thumbs": []}
    results = [
        {"uid": "u1", "name": "Old Tractor", "user": {"username": "alice"}, "faceCount": 5400, "license": {"label": "CC Attribution"},
         "thumbnails": {"images": [{"url": "https://media.sketchfab.com/a_100.jpg", "width": 100},
                                   {"url": "https://media.sketchfab.com/a_256.jpg", "width": 256}]}},
        {"uid": "u2", "name": "Evil", "user": {"username": "bob"}, "faceCount": 1, "license": None,
         "thumbnails": {"images": [{"url": "https://evil.example/x.png", "width": 256}]}},
    ]
    addon._http_get = lambda url, **kw: R({"results": results}) if "/v3/search" in url else R(status=404)

    async def fake_http_get2(url, *, params=None, headers=None, timeout=30, redirects=True):
        hits["thumbs"].append(url)
        return hresp(content=PNG, headers={"content-type": "image/png"})

    S._http_get = fake_http_get2
    res = await S.search_assets(source="sketchfab", query="tractor", limit=5, previews=2)
    check("sketchfab: results trimmed to uid / name / author / faces / licence", isinstance(res, list)
          and "u1 | Old Tractor | by alice | 5400 faces | licence: CC Attribution" in res[0], res[0] if isinstance(res, list) else res)
    check("sketchfab: previews fetch only allow-listed hosts, picking a ~256px thumbnail",
          hits["thumbs"] == ["https://media.sketchfab.com/a_256.jpg"] and len(res) == 2, hits)
    r = await raises(S.import_asset(source="sketchfab", id="u1"), "target_size")
    check("sketchfab import requires target_size", r[0], r[1])
    captured = {}
    M.download_sketchfab_model = lambda self, uid, normalize_size=False, target_size=1.0: (
        captured.update(uid=uid, normalize_size=normalize_size, target_size=target_size) or {"success": True, "imported_objects": ["X"], "dimensions": [1.5, 1, 1]})
    out = json.loads(await S.import_asset(source="sketchfab", id="u1", target_size=1.5))
    check("sketchfab import maps target_size onto the add-on's normalisation", captured == {"uid": "u1", "normalize_size": True, "target_size": 1.5}
          and out["dimensions"] == [1.5, 1, 1], (captured, out))
    sc.blendermcp_use_sketchfab = False
    r = await raises(S.search_assets(source="sketchfab", query="x"), "sketchfab", "disabled")
    check("sketchfab disabled in the panel -> clear error", r[0], r[1])

    # ================================================================== Poly Pizza (server-side; real glTF import)
    r = await raises(S.search_assets(source="polypizza", query="chair"), "POLYPIZZA_API_KEY", "poly.pizza/settings/api")
    check("polypizza without a key -> tells how to get one", r[0], r[1])
    P = h.load_server(PORT, POLYPIZZA_API_KEY="pp-key")
    seen = []
    state = {"cdn": "ok", "search_status": 200}

    async def fake_pp(url, *, params=None, headers=None, timeout=30, redirects=True):
        seen.append((url, params, headers))
        if "/search" in url:
            if state["search_status"] != 200:
                return hresp(status=state["search_status"], js={})
            return hresp(js={"total": 2, "results": [
                {"ID": "abc123", "Title": "Wooden Chair", "Creator": {"Username": "carol"}, "Licence": "CC0", "Tri Count": 300,
                 "Animated": False, "Category": "Furniture & Decor", "Tags": ["chair", "wood"], "Thumbnail": "https://static.poly.pizza/t.png"},
                {"ID": "def456", "Title": "Armchair", "Creator": {"Username": "dave"}, "Licence": "CC-BY", "Tri Count": 900,
                 "Animated": True, "Tags": [], "Thumbnail": ""}]})
        if "/model/" in url:
            return hresp(js={"ID": "abc123", "Licence": "CC-BY", "Creator": {"Username": "carol"},
                             "Attribution": "Wooden Chair by carol (poly.pizza/m/abc123) CC-BY 3.0",
                             "Download": "https://static.poly.pizza/abc123.glb"})
        if url.endswith(".glb"):
            if state["cdn"] == "cloudflare":
                return hresp(status=403, content=b"<html>Just a moment</html>", headers={"content-type": "text/html"})
            if state["cdn"] == "garbage":
                return hresp(content=b"not a glb at all")
            return hresp(content=GLB)
        raise AssertionError("unexpected GET " + url)

    P._http_get = fake_pp
    out = await P.search_assets(source="polypizza", query="low poly chair", category="Furniture & Decor", licence="CC0", animated=True, limit=10)
    url, params, headers = seen[-1]
    check("polypizza search: keyword in the path, capitalised parameters (the API ignores lowercase)",
          url.endswith("/search/low%20poly%20chair") and params == {"Limit": 10, "Category": 4, "License": 1, "Animated": 1}, (url, params))
    check("polypizza search: API key sent as x-auth-token", headers == {"x-auth-token": "pp-key"}, headers)
    check("polypizza search: result lines", "abc123 | Wooden Chair | by carol | CC0 | 300 tris | static" in out
          and "def456 | Armchair | by dave | CC-BY | 900 tris | animated" in out and "need credit to the creator" in out, out)
    await P.search_assets(source="polypizza", query="x", category="furniture")
    check("polypizza search: category aliases (furniture -> 4)", seen[-1][1]["Category"] == 4)
    r = await raises(P.search_assets(source="polypizza", query="x", category="spaceships"), "unknown", "Nature")
    check("polypizza search: unknown category lists the valid ones", r[0], r[1])
    r = await raises(P.search_assets(source="polypizza"), "search word or at least one filter")
    check("polypizza search: needs a word or a filter", r[0], r[1])
    state["search_status"] = 401
    r = await raises(P.search_assets(source="polypizza", query="x"), "authentication failed")
    check("polypizza search: bad key -> clear error", r[0], r[1])
    state["search_status"] = 200

    tmp_before = set(glob.glob(os.path.join(tempfile.gettempdir(), "blender_mcp_pp_*")))
    names_before = gather_names()
    out = json.loads(await P.import_asset(source="polypizza", id="abc123", target_size=1.0))
    new = gather_names() - names_before
    check("polypizza import: real glTF import created the objects", len(new) >= 1 and set(out["placed"]["objects"]) == new, (new, out))
    check("polypizza import: scaled to exactly the requested size (2 m cube -> 1 m)", abs(max(out["placed"]["dimensions_m"]) - 1.0) < 1e-3
          and abs(out["placed"]["scale_applied"] - 0.5) < 1e-4, out["placed"])
    root = bpy.data.objects[out["roots"][0]]
    check("polypizza import: attribution stored on the root object (survives saving)",
          root["polypizza_attribution"].startswith("Wooden Chair by carol") and root["polypizza_licence"] == "CC-BY"
          and out["credit_required"].startswith("Wooden Chair"), dict(root.items()))
    cdn_headers = [hd for (u, _, hd) in seen if u.endswith(".glb")][0]
    check("polypizza import: the API key is NOT sent to the CDN", "x-auth-token" not in (cdn_headers or {}), cdn_headers)
    check("polypizza import: temporary download folder removed", set(glob.glob(os.path.join(tempfile.gettempdir(), "blender_mcp_pp_*"))) == tmp_before)
    state["cdn"] = "cloudflare"
    r = await raises(P.import_asset(source="polypizza", id="abc123"), "bot-protection")
    check("polypizza import: Cloudflare challenge -> explained, not a cryptic failure", r[0], r[1])
    state["cdn"] = "garbage"
    r = await raises(P.import_asset(source="polypizza", id="abc123"), "not a glb")
    check("polypizza import: non-GLB file rejected before it reaches Blender", r[0], r[1])

    # ================================================================== AI generation (provider APIs simulated)
    G = h.load_server(PORT)
    G.RODIN_POLL_SECONDS = G.HUNYUAN_POLL_SECONDS = 0.02
    sc.blendermcp_use_hyper3d, sc.blendermcp_hyper3d_mode, sc.blendermcp_hyper3d_api_key = True, "MAIN_SITE", "KEY"
    sc.blendermcp_use_hunyuan3d, sc.blendermcp_hunyuan3d_mode = True, "OFFICIAL_API"
    sc.blendermcp_hunyuan3d_secret_id, sc.blendermcp_hunyuan3d_secret_key = "id", "key"
    posts, st = [], {"rodin": 0, "hy": 0, "rodin_final": "Done"}

    def fake_post(url, **kw):
        posts.append((url, kw))
        if url.endswith("/api/v2/rodin"):
            return R({"uuid": "U1", "jobs": {"uuids": ["j"], "subscription_key": "KEY1"}})
        if url.endswith("/api/v2/status"):
            st["rodin"] += 1
            return R({"jobs": [{"status": "Generating" if st["rodin"] < 3 else st["rodin_final"]}]})
        action = kw.get("headers", {}).get("X-TC-Action")
        if action == "SubmitHunyuanTo3DJob":
            return R({"Response": {"JobId": "job_J1"}})
        if action == "QueryHunyuanTo3DJob":
            st["hy"] += 1
            return R({"Response": {"Status": "RUN"}} if st["hy"] < 3 else
                     {"Response": {"Status": "DONE", "ResultFile3Ds": [{"Type": "GLB", "Url": "http://x/a.glb"}, {"Type": "OBJ", "Url": "http://x/m.zip"}]}})
        raise AssertionError("unexpected POST " + url)

    addon._http_post = fake_post
    addon._http_get = lambda url, **kw: R(content=b"URLIMAGE") if url.startswith("http://img.example/") else R(status=404)
    M.import_generated_asset_main_site = lambda self, task_uuid, name: {"succeed": True, "name": name, "task": task_uuid}
    M.import_generated_asset_hunyuan_ai = lambda self, name, zip_file_url: {"succeed": True, "name": name, "url": zip_file_url}

    d = json.loads(await G.rodin_generate(ctx=None, prompt="a chair", name="Chair", timeout_s=30))
    check("rodin: generate, poll and import", d["state"] == "imported" and d["result"]["task"] == "U1" and st["rodin"] >= 3, d)
    create = next(kw for (u, kw) in posts if u.endswith("/api/v2/rodin"))
    check("rodin: prompt sent as a form field", ("prompt", (None, "a chair")) in create["files"])
    png = Path(_tmp, "ref.PNG")
    png.write_bytes(b"\x89PNGREALBYTES")
    posts.clear()
    st["rodin"] = 0
    await G.rodin_generate(ctx=None, image_paths=[str(png)], image_urls=["http://img.example/a/pic.jpg?x=1"], prompt="vase", timeout_s=30)
    files = [v for k, v in next(kw for (u, kw) in posts if u.endswith("/api/v2/rodin"))["files"] if k == "images"]
    check("rodin: local image uploaded as real bytes, URL image downloaded and uploaded",
          files[0] == ("0000.png", b"\x89PNGREALBYTES") and files[1] == ("0001.jpg", b"URLIMAGE"), files)
    st["rodin"] = 0
    d = json.loads(await G.rodin_generate(ctx=None, prompt="x", wait=False))
    check("rodin: wait=False returns a running job handle", d["state"] == "running" and d["job"] == "rodin|main|KEY1|U1", d)
    d = json.loads(await G.collect_generated_model(ctx=None, job=d["job"], name="Late", timeout_s=30))
    check("rodin: collect_generated_model finishes it", d["state"] == "imported" and d["result"]["name"] == "Late", d)
    st["rodin"], st["rodin_final"] = 5, "Failed"
    r = await raises(G.collect_generated_model(ctx=None, job="rodin|main|KEY1|U1", timeout_s=30), "failed")
    check("rodin: failed job -> error", r[0], r[1])
    st["rodin_final"] = "Done"
    r = await raises(G.collect_generated_model(ctx=None, job="garbage"), "invalid job handle")
    check("generation: bad handle rejected", r[0], r[1])
    d = json.loads(await G.hunyuan3d_generate(ctx=None, prompt="a lamp", name="Lamp", timeout_s=30))
    check("hunyuan3d (official API): polls RUN->DONE and imports the OBJ zip", d["state"] == "imported"
          and d["result"] == {"succeed": True, "name": "Lamp", "url": "http://x/m.zip"} and st["hy"] >= 3, d)
    r = await raises(G.hunyuan3d_generate(ctx=None, prompt="a" * 201), "200")
    check("hunyuan3d: prompt longer than 200 characters rejected", r[0], r[1])

    # ================================================================== the whole thing over the real MCP protocol (stdio)
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    server_py = str(Path(__file__).resolve().parent.parent / "blender_mcp_server.py")

    def params(**env):
        base = {k: v for k, v in os.environ.items() if not k.startswith("BLENDER_") and k != "POLYPIZZA_API_KEY"}
        return StdioServerParameters(command=sys.executable, args=[server_py], env={**base, "BLENDER_PORT": str(PORT), **env})

    expected = {"blender_status", "get_scene_info", "get_object_details", "look", "render_image", "execute_blender_code",
                "run_blender_script", "save_blend", "export_scene", "search_assets", "import_asset", "rodin_generate",
                "hunyuan3d_generate", "collect_generated_model"}
    async with stdio_client(params()) as (rd, wr):
        async with ClientSession(rd, wr) as s:
            init = await s.initialize()
            tools = {t.name: t for t in (await s.list_tools()).tools}
            check("protocol: server identifies itself and ships working instructions", init.serverInfo.name == "blender"
                  and "get_scene_info" in (init.instructions or "") and "Premium" not in (init.instructions or ""))
            check("protocol: exactly the 14 tools of v2", set(tools) == expected, sorted(set(tools) ^ expected))
            look = tools["look"].inputSchema
            check("protocol: look schema has the mode enum, bounds, and no hidden Context argument",
                  set(look["properties"]["mode"]["enum"]) == {"viewport", "camera", "angles", "frames"}
                  and look["properties"]["max_size"]["maximum"] == 2048 and "ctx" not in look["properties"], look)
            check("protocol: annotations (search read-only, look non-destructive, execute destructive)",
                  tools["search_assets"].annotations.readOnlyHint is True and tools["look"].annotations.destructiveHint is False
                  and tools["execute_blender_code"].annotations.destructiveHint is True)
            check("protocol: no output schemas (plain text/image results)", all(not getattr(t, "outputSchema", None) for t in tools.values()))
            res = await s.call_tool("get_scene_info", {"fields": ["extras"], "limit": 3})
            check("protocol: get_scene_info", not res.isError and res.content[0].text.startswith("Scene "), res)
            res = await s.call_tool("look", {"mode": "angles", "views": ["front"], "max_size": 128})
            kinds = [c.type for c in res.content]
            check("protocol: look returns [text, image/png]", not res.isError and kinds == ["text", "image"]
                  and res.content[1].mimeType == "image/png", (kinds, res))
            res = await s.call_tool("get_scene_info", {"fields": ["nope"]})
            check("protocol: bad argument -> isError with the valid choices", res.isError and "Valid fields" in res.content[0].text, res)
            res = await s.call_tool("get_scene_info", {"limit": 99999})
            check("protocol: out-of-range argument rejected by the schema", res.isError, res)
            res = await s.call_tool("search_assets", {"source": "polypizza", "query": "x"})
            check("protocol: missing API key surfaces as a readable error", res.isError and "POLYPIZZA_API_KEY" in res.content[0].text, res)

    async with stdio_client(params(BLENDER_MCP_SAFE_MODE="1")) as (rd, wr):
        async with ClientSession(rd, wr) as s:
            await s.initialize()
            res = await s.call_tool("execute_blender_code", {"code": "import os"})
            check("protocol + safe mode: hostile code refused", res.isError and "Safe mode" in res.content[0].text, res)
            res = await s.call_tool("execute_blender_code", {"code": "print(1 + 1)"})
            check("protocol + safe mode: harmless code runs", not res.isError and res.content[0].text.strip() == "2", res)
    async with stdio_client(params(BLENDER_MCP_READONLY="1")) as (rd, wr):
        async with ClientSession(rd, wr) as s:
            await s.initialize()
            names = {t.name for t in (await s.list_tools()).tools}
            check("protocol + read-only: write tools hidden, looking and searching kept",
                  {"look", "get_scene_info", "search_assets"} <= names and not ({"execute_blender_code", "import_asset", "rodin_generate"} & names), sorted(names))

    srv.stop()


h.run(main)
h.finish()
