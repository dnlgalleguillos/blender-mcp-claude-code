# Blender + Claude Code (MCP) — v2

Control Blender from Claude Code, and let Claude **see** what it builds. Two files, both written from scratch:

| File | Runs where | What it does |
|---|---|---|
| `blender_mcp.py` | Inside Blender (add-on "Blender MCP Bridge" 2.0) | A tiny local door: runs Python, answers `ping`, takes viewport screenshots |
| `blender_mcp_server.py` | Launched by Claude Code (server 2.0) | The MCP server: all tools, all network access, all API keys |

```
Claude Code ──MCP/stdio──► blender_mcp_server.py ──TCP 127.0.0.1:9876──► blender_mcp.py (Blender)
                                   │
                                   └──HTTPS──► Poly Haven · Sketchfab · Poly Pizza · Hyper3D Rodin · Hunyuan3D
```

The add-on does no networking, stores nothing in your `.blend` files, and collects and sends nothing. Downloads, API
keys and asset imports are handled by the server, so Blender never freezes while waiting for a provider and your keys
never live inside a Blender file. No telemetry, no analytics, no promotional text in tool replies.

## Installation

1. **Add-on:** Blender > Edit > Preferences > Add-ons > Install > `blender_mcp.py`, then enable it. If you had a previous
   Blender MCP add-on (any version), remove it first and restart Blender.
2. **Connect:** in the 3D viewport press `N` > **BlenderMCP** tab > **Connect to MCP server**.
3. **Server in Claude Code** (requires [uv](https://docs.astral.sh/uv/); it installs the dependencies by itself). Replace the
   path with the location of your copy of the repository. `--scope user` makes it available in every project:

```bash
claude mcp add --scope user blender -- uv run /absolute/path/to/blender_mcp_server.py
```

4. Check the connection (with Blender open and connected):

```bash
uv run /absolute/path/to/blender_mcp_server.py --check
```

Poly Haven needs no key. To use the other libraries and the AI generators, give the server their keys as environment
variables when you register it, for example
`claude mcp add --scope user blender -e SKETCHFAB_API_KEY=... -e HYPER3D_API_KEY=... -- uv run ...`.
`blender_status` shows which ones are configured (never their values).

## Tools

| Tool | Purpose |
|---|---|
| `blender_status` | Diagnostics: connection, versions, open file, code switch, safe mode, which libraries/generators have keys |
| `get_scene_info` | Compact text facts: counts, selection, per-object placement, contents and mesh/rig **health** |
| `get_object_details` | Full state of one object: transform, modifiers, materials, mesh, camera, light |
| `look` | See the scene as images: live viewport, scene camera, auto-framed angles, animation frames |
| `render_image` | Final-quality render to a file (scene render settings are restored afterwards) |
| `execute_blender_code` | Short Python snippets inside Blender (creates a Ctrl+Z undo step first) |
| `run_blender_script` | Run a `.py` file from your repo, with real file names and line numbers in errors |
| `save_blend`, `export_scene` | Save and export (`.glb .fbx .obj .stl .ply .usd`) into your project |
| `search_assets` | Search Poly Haven, Sketchfab and Poly Pizza with one tool; optional thumbnails |
| `import_asset` | Import the asset, scaled to a real-world size, with materials and HDRIs built for you |
| `rodin_generate`, `hunyuan3d_generate`, `collect_generated_model` | AI text/image-to-3D with Hyper3D Rodin and Tencent Hunyuan3D |

There is also a `blender://scene` resource and a `blender_task` prompt that starts the recommended workflow: look at the
scene, work in small steps, verify with images.

### `get_scene_info`

One header line and one line per object, with only the fields you ask for (`fields` takes names or groups):

- **placement:** `location`, `size` (world bounding box including children), `ground` (*on ground*, *floating 0.35 m*, *below ground by 1 m*), `rotation`, `scale`, `parent`
- **contents:** `children`, `hidden`, `details` (faces, bones, light power, lens), `materials`, `modifiers`, `animation`
- **health:** `topology` (tris/quads/ngons, non-manifold and boundary edges, loose vertices, poles) and `weights` (vertices no deform bone moves, deform bones without a vertex group)
- **extras:** `settings` (engine, resolution, frames, camera, HDRI, units) and `collections`

`query="chair"` filters by name and `root="Name"` lists a hierarchy, indented.

### `look`

| mode | shows |
|---|---|
| `viewport` (default) | The live 3D viewport exactly as the user sees it |
| `camera` | Through the scene camera, at the render aspect ratio |
| `angles` | The target from several sides, auto-framed (front, right, top, three-quarter by default, or your own directions) |
| `frames` | The animation sampled over time |

`camera`, `angles` and `frames` render with the Workbench engine through temporary cameras, which are removed afterwards
(your render settings are restored). Workbench draws surfaces with a material's *viewport* colour, so each material's
Principled base colour is mirrored into it for the duration of the render and put back afterwards: a red material looks
red. `shading` is `solid`, `textured`, `clay` (best to judge form), `flat` or `xray`. Use `render_image` for a fully lit,
final-quality render.

### Assets

`search_assets(source, query, ...)` and `import_asset(source, id, target_size, ...)` cover three libraries. Everything is
downloaded by the server; only the finished import runs in Blender, and downloaded images are embedded in your file.

| source | content | key |
|---|---|---|
| `polyhaven` | HDRIs (become the world lighting), PBR textures (a material is built and can be assigned with `apply_to`), realistic glTF models (CC0) | none. Searched by name, tags, categories and description, with simple synonyms (*couch* finds sofas) and real-world sizes (`min_size_m`) |
| `sketchfab` | Large catalogue of user-made models | `SKETCHFAB_API_KEY`. Licence, author and URL are stored on the imported object |
| `polypizza` | Stylised low-poly models (CC0 / CC-BY) | `POLYPIZZA_API_KEY` (free, [poly.pizza/settings/api](https://poly.pizza/settings/api)). CC-BY credit is stored on the object (`polypizza_attribution`) |

Library models arrive at arbitrary scale, so `import_asset` takes a `target_size` (metres, largest dimension) and reports the
final dimensions and bounding box.

### AI generators

| generator | configure with |
|---|---|
| Hyper3D Rodin | `HYPER3D_API_KEY`; `HYPER3D_PLATFORM=fal` to use fal.ai instead of hyper3d.ai |
| Hunyuan3D, Tencent Cloud | `HUNYUAN3D_SECRET_ID` + `HUNYUAN3D_SECRET_KEY` |
| Hunyuan3D, your own server | `HUNYUAN3D_API_URL` (e.g. `http://localhost:8081`); `HUNYUAN3D_MODE=official` or `local` forces one when both are set |

Generation takes minutes and **spends credits on your provider account**; Claude is instructed to ask before the first call.
`wait=false` returns a job handle that `collect_generated_model` finishes later.

## Configuration (environment variables)

Pass them with `-e` when registering the server.

| Variable | Default | |
|---|---|---|
| `BLENDER_HOST` / `BLENDER_PORT` | `127.0.0.1` / `9876` | Must match the port in the Blender panel |
| `BLENDER_MCP_TOKEN` | empty | Same value as "Auth Token" in the add-on preferences |
| `BLENDER_TIMEOUT` | `30` | Seconds per command (long-running tools accept `timeout_s`) |
| `BLENDER_MCP_READONLY` | off | `1` = do not register tools that modify the scene or write files (`look` stays available) |
| `BLENDER_MCP_SAFE_MODE` | off | `1` = check the Python the model writes before it reaches Blender (see below) |
| `POLYPIZZA_API_KEY`, `SKETCHFAB_API_KEY` | empty | Library keys |
| `HYPER3D_API_KEY`, `HYPER3D_PLATFORM`, `HUNYUAN3D_*` | empty | Generator credentials (see above) |
| `BLENDER_MCP_LOG` | `INFO` | Logs go to stderr |

## Security

- The add-on only listens on `127.0.0.1`. With a token configured, every command must carry it (compared in constant time).
- `execute_blender_code` and `run_blender_script` run arbitrary Python with your permissions. **Allow code execution** in the
  Blender panel is the master switch: with it off, nothing the server needs works except `blender_status`.
- **Safe mode** (`BLENDER_MCP_SAFE_MODE=1`) statically checks the code *the model* writes. It exists for prompt injection: text
  from third parties (asset names and descriptions, generator replies) enters the model's context and could steer it into
  writing hostile code that you approve without reading. It allows `bpy`, `bmesh`, `mathutils` and a few pure standard modules,
  and refuses interpreter escapes (`eval`, `exec`, `open`, dunder access, computed `getattr`), modules with process, filesystem
  or network access, persistence inside Blender (handlers, timers, drivers, class registration, `bpy.types` assignment),
  code-running operators (`script`, `text`, `extensions`, preferences), and opening, appending or saving `.blend` files (use
  `save_blend`). It is **a guard on what the model can be talked into, not a sandbox**: the add-on's socket still accepts raw
  code from any local process, and files can still be written through export operators.
- `BLENDER_MCP_READONLY=1` is a convenience so Claude does not see the write tools, **not** a sandbox: the real boundary is the
  switch in the panel.
- Everything the server downloads is checked: HTTPS only, no local or private addresses (except a Hunyuan3D server you
  configured yourself), size caps, and archives are unpacked only into a temporary folder, refusing paths that escape it.
  Thumbnails come only from the libraries' own hosts, without following redirects. API keys are never sent to download hosts
  and never appear in tool output.
- Paths are resolved on the machine running Blender. If Claude Code runs on a different one (container, WSL), screenshots,
  renders and exports need a shared filesystem.

## Known limitations

- Blender runs every command on its UI thread: renders and the import step of a large model freeze it until they finish.
  Waiting for providers and downloading no longer does.
- `look` in modes `camera`, `angles` and `frames` uses the Workbench engine: no real materials, lights or shadows. Use
  `render_image` for those.
- `look` in mode `viewport` needs a visible Blender window with a 3D Viewport. It could not be exercised without a screen:
  the tests only check its error path.
- Poly Haven search runs against the live API in the tests, but its downloads, and every other provider, are simulated (with
  real files imported by a real Blender). The Tencent request signature is checked against known answers; none of it has been
  run against the paid services.
- Poly Pizza's CDN blocks datacenter and VPN addresses behind a bot challenge; the tool says so when it happens.

## Development and tests

The tests run the real add-on inside a real, headless Blender (the [`bpy`](https://pypi.org/project/bpy/) module, Python 3.11)
and drive the real MCP server, including over stdio with a real MCP client. They were run against Blender 5.0.1.

```bash
# Safe-mode validator (pure Python)
uv run --no-project --with "mcp>=1.10,<2" --with httpx python -I tests/test_safe_mode.py

# The add-on on its own: protocol, ordering, limits, token, panel, privacy
uv run --no-project --python 3.11 --with bpy --with "mcp>=1.10,<2" --with httpx python -I tests/test_addon.py

# Scene inspection, look, render and safe mode
uv run --no-project --python 3.11 --with bpy --with "mcp>=1.10,<2" --with httpx python -I tests/test_blender.py

# Asset libraries, AI generators and the full MCP protocol
uv run --no-project --python 3.11 --with bpy --with "mcp>=1.10,<2" --with httpx python -I tests/test_assets_generation.py
```

The `bpy` wheel is a few hundred megabytes. `tests/blender_harness.py` explains how the add-on is driven without Blender's
event loop.

## Credits and license

Free software under the **GNU General Public License, version 3 or (at your option) any later version**, see [`LICENSE`](LICENSE).
Copyright (C) 2026 Daniel Galleguillos Cruz.

Versions 1.x of this repository (see the git history) were a modified copy of the add-on from
[ahujasid/blender-mcp](https://github.com/ahujasid/blender-mcp) (© 2025 Siddharth Ahuja, MIT License), whose notice those
versions carried. Version 2 replaces it entirely: the add-on and the server are new code. The design owes ideas to that
project, now called [mcp-for-blender](https://github.com/ahujasid/mcp-for-blender): multi-view `look`, mesh health in the
scene summary, one search/import pair for several asset libraries and an opt-in safe mode. They are reimplemented here,
without its telemetry or promotional text.
