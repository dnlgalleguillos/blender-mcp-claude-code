# Blender + Claude Code (MCP) — v2

Control Blender from Claude Code, and let Claude **see** what it builds. Two files:

| File | Runs where | What it does |
|---|---|---|
| `blender_mcp.py` | Inside Blender (add-on, v1.3.2) | Opens a local socket and executes commands in Blender |
| `blender_mcp_server.py` | Launched by Claude Code (v2.0) | MCP server: exposes the tools to Claude and talks to the add-on |

```
Claude Code ──MCP/stdio──► blender_mcp_server.py ──TCP 127.0.0.1:9876──► blender_mcp.py (Blender)
```

No telemetry, no analytics, no promotional text in tool replies. It is meant for the add-on in this repository; the scene, `look`, render, code and safe-mode tools were also verified against the stock `blender-mcp` 1.2 add-on (see the tests section).

## Installation

1. **Add-on:** Blender > Edit > Preferences > Add-ons > Install > `blender_mcp.py` (uninstall any previous version first), then enable it.
2. **Connect:** in the 3D viewport press `N` > **BlenderMCP** tab > **Connect to MCP server**.
3. **Server in Claude Code** (requires [uv](https://docs.astral.sh/uv/); it installs the dependencies by itself). Replace the path with the location of your copy of the repository. If you had another server called `blender` (for example the stock `uvx blender-mcp`), remove it first with `claude mcp remove blender`:

```bash
claude mcp add blender -- uv run /absolute/path/to/blender_mcp_server.py
```

4. Check the connection (with Blender open and connected):

```bash
uv run /absolute/path/to/blender_mcp_server.py --check
```

## Tools

| Tool | Purpose |
|---|---|
| `blender_status` | Diagnostics: connection, version, open file, whether code execution is allowed, safe mode |
| `get_scene_info` | Compact text facts: counts, selection, per-object placement, contents and mesh/rig **health** |
| `get_object_details` | Full state of one object: transform, modifiers, materials, mesh, camera, light |
| `look` | See the scene as images: live viewport, scene camera, auto-framed angles, animation frames |
| `render_image` | Final-quality render to a file (scene render settings are restored afterwards) |
| `execute_blender_code` | Short Python snippets inside Blender (creates a Ctrl+Z undo step first) |
| `run_blender_script` | Run a `.py` file from your repo, with real file names and line numbers in errors |
| `save_blend`, `export_scene` | Save and export (`.glb .fbx .obj .stl .ply .usd`) into your project |
| `search_assets` | Search Poly Haven, Sketchfab and Poly Pizza with one tool; optional thumbnails |
| `import_asset` | Import the asset, scaled to a real-world size, with materials/HDRIs handled |
| `rodin_generate`, `hunyuan3d_generate`, `collect_generated_model` | AI text/image-to-3D with Hyper3D Rodin and Tencent Hunyuan3D |

There is also a `blender://scene` resource and a `blender_task` prompt that starts the recommended workflow: look at the scene, work in small steps, verify with images.

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

`camera`, `angles` and `frames` render with the Workbench engine through temporary cameras, which are removed afterwards (your render settings are restored). Workbench draws surfaces with a material's *viewport* colour, so each material's Principled base colour is mirrored into it for the duration of the render and put back afterwards: a red material looks red. `shading` is `solid`, `textured`, `clay` (best to judge form), `flat` or `xray`. Use `render_image` for a fully lit, final-quality render.

### Assets

`search_assets(source, query, ...)` and `import_asset(source, id, target_size, ...)` cover three libraries:

| source | content | notes |
|---|---|---|
| `polyhaven` | HDRIs, PBR textures, realistic models (CC0) | No key. Searched from the server by name, tags, categories and description, with simple synonyms (*couch* finds sofas) and real-world sizes (`min_size_m`). Enable *Use assets from Poly Haven* in the Blender panel to import |
| `sketchfab` | Large catalogue of user-made models | API key in the Blender panel. Check each model's licence |
| `polypizza` | Stylised low-poly models (CC0 / CC-BY) | Free key from [poly.pizza/settings/api](https://poly.pizza/settings/api) in `POLYPIZZA_API_KEY`. CC-BY credit is stored on the imported object (`polypizza_attribution`) |

Library models arrive at arbitrary scale, so `import_asset` takes a `target_size` (metres, largest dimension) and reports the final dimensions and bounding box.

### Coming from v1

| v1 | v2 |
|---|---|
| `scene_overview`, `list_objects` | `get_scene_info` |
| `viewport_screenshot` | `look` (mode `viewport`) |
| `polyhaven_search/download/apply_texture`, `sketchfab_search/preview/download` | `search_assets` + `import_asset` |

## Configuration (environment variables)

Pass them with `-e` when registering the server, for example `claude mcp add blender -e BLENDER_PORT=9999 -- uv run ...`.

| Variable | Default | |
|---|---|---|
| `BLENDER_HOST` / `BLENDER_PORT` | `127.0.0.1` / `9876` | Must match the port in the Blender panel |
| `BLENDER_MCP_TOKEN` | empty | Same value as "Auth Token" in the add-on preferences |
| `BLENDER_TIMEOUT` | `30` | Seconds per command (long-running tools accept `timeout_s`) |
| `BLENDER_MCP_READONLY` | off | `1` = do not register tools that modify the scene or write files (`look` stays available) |
| `BLENDER_MCP_SAFE_MODE` | off | `1` = check the Python the model writes before it reaches Blender (see below) |
| `POLYPIZZA_API_KEY` | empty | Needed to search and import Poly Pizza models |
| `BLENDER_MCP_LOG` | `INFO` | Logs go to stderr |

## Security

- The socket only listens on `localhost`. With a token configured, every command must carry it (compared in constant time).
- `execute_blender_code` and `run_blender_script` run arbitrary Python with your permissions. You can turn them off with **Allow code execution** in the Blender panel. The inspection tools use it too, so with it off only a basic summary, screenshots and the add-on's own asset commands keep working.
- **Safe mode** (`BLENDER_MCP_SAFE_MODE=1`) statically checks the code *the model* writes. It exists for prompt injection: text from third parties (asset names and descriptions, generator replies) enters the model's context and could steer it into writing hostile code that you approve without reading. It allows `bpy`, `bmesh`, `mathutils` and a few pure standard modules, and refuses interpreter escapes (`eval`, `exec`, `open`, dunder access, computed `getattr`), modules with process/filesystem/network access, persistence inside Blender (handlers, timers, drivers, class registration, `bpy.types` assignment), code-running operators (`script`, `text`, `extensions`, preferences), and opening, appending or saving `.blend` files (use `save_blend`). It is **a guard on what the model can be talked into, not a sandbox**: the add-on's socket still accepts raw code from any local process, and files can still be written through export operators.
- `BLENDER_MCP_READONLY=1` is a convenience so Claude does not see the write tools, **not** a sandbox: the real boundary is the switch in the panel.
- Thumbnails are only fetched from the libraries' own hosts, over HTTPS, without following redirects. The Poly Pizza API key is never sent to its CDN.
- Paths are resolved on the machine running Blender. If Claude Code runs on a different one (container, WSL), screenshots, renders and exports need a shared filesystem.

## Known limitations

- Blender runs every command on its UI thread: renders, large imports and downloads freeze it until they finish.
- `look` in modes `camera`, `angles` and `frames` uses the Workbench engine: no real materials, lights or shadows. Use `render_image` for those.
- The AI generators **spend credits on your provider account** and take minutes. Claude is instructed to ask for your confirmation before the first call. Generated models arrive "raw": scale and orientation usually need adjusting.
- Hunyuan3D in **local server** mode is a single synchronous request: Blender stays frozen until it finishes.
- Poly Pizza's CDN blocks datacenter and VPN addresses behind a bot challenge; the tool says so when it happens.
- Rodin, Hunyuan3D, Sketchfab and Poly Pizza were tested against simulated API responses, not against the paid services. Poly Haven is queried live in the tests.

## Development and tests

The tests run the real add-on inside a real, headless Blender (the [`bpy`](https://pypi.org/project/bpy/) module, Python 3.11) and drive the real MCP server over stdio. They were run against Blender 5.0.1.

```bash
# Safe-mode validator (pure Python)
uv run --no-project --with "mcp>=1.10,<2" --with httpx python -I tests/test_safe_mode.py

# Scene inspection, look, render and safe mode in real Blender
uv run --no-project --python 3.11 --with bpy --with "mcp>=1.10,<2" --with httpx python -I tests/test_blender.py

# Assets, AI generation and the full MCP protocol in real Blender
uv run --no-project --python 3.11 --with bpy --with "mcp>=1.10,<2" --with httpx python -I tests/test_assets_generation.py
```

The `bpy` wheel is a few hundred megabytes. `tests/blender_harness.py` explains how the add-on is driven without Blender's event loop. Set `BLENDER_MCP_ADDON=/path/to/other_addon.py` to run `test_blender.py` against a different add-on: it passes in full with the stock `blender-mcp` 1.2 one. The asset and generation tests simulate the add-on's HTTP layer, which only this repository's add-on exposes, so they run against it alone.

## Credits and license

- `blender_mcp_server.py`, the tests, this README and all modifications to the add-on are free software under the
  **GNU General Public License, version 3 or (at your option) any later version**, see [`LICENSE`](LICENSE).
  Copyright (C) 2026 Daniel Galleguillos Cruz.
- `blender_mcp.py` is a modified version of the add-on from [ahujasid/blender-mcp](https://github.com/ahujasid/blender-mcp)
  (© 2025 Siddharth Ahuja, MIT License). The original portions keep their MIT license and the original copyright and
  license notice are preserved in [`LICENSE-blender-mcp.txt`](LICENSE-blender-mcp.txt); the MIT license is
  GPL-compatible, so the file as a whole is distributed under the GPL with that notice kept. The changes from the
  original are listed in the file header.
- Several ideas in v2 (multi-view `look`, mesh health in the scene summary, one search/import pair for several asset
  libraries, an opt-in safe mode, the Poly Pizza category table) follow the design of that project, now called
  [mcp-for-blender](https://github.com/ahujasid/mcp-for-blender). They are reimplemented here, without its telemetry
  or promotional text.
