# Blender + Claude Code (MCP)

Control Blender from Claude Code. Two files:

| File | Runs where | What it does |
|---|---|---|
| `blender_mcp.py` | Inside Blender (add-on) | Opens a local socket and executes commands in Blender |
| `blender_mcp_server.py` | Launched by Claude Code | MCP server: exposes the tools to Claude and talks to the add-on |

```
Claude Code ──MCP/stdio──► blender_mcp_server.py ──TCP 127.0.0.1:9876──► blender_mcp.py (Blender)
```

## Installation

1. **Add-on:** Blender > Edit > Preferences > Add-ons > Install > `blender_mcp.py` (uninstall any previous version first), then enable it.
2. **Connect:** in the 3D viewport press `N` > **BlenderMCP** tab > **Connect to MCP server**.
3. **Server in Claude Code** (requires [uv](https://docs.astral.sh/uv/); it installs the dependencies by itself). Replace the path with the location of your copy of the repository:

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
| `blender_status` | Diagnostics: connection, version, open file, whether code execution is allowed |
| `scene_overview` | Scene summary: render settings, units, counts by type, collections, selection |
| `list_objects` | Objects with filters (type, name, collection, selection) and pagination |
| `get_object_details` | Full state of one object: transform, modifiers, materials, mesh, camera, light |
| `viewport_screenshot` | Viewport capture returned as an image so Claude can check its own work |
| `render_image` | Render to a file (scene render settings are restored afterwards) |
| `execute_blender_code` | Short Python snippets inside Blender (creates a Ctrl+Z undo step first) |
| `run_blender_script` | Run a `.py` file from your repo, with real file names and line numbers in errors |
| `save_blend`, `export_scene` | Save and export (`.glb .fbx .obj .stl .ply .usd`) into your project |
| `polyhaven_*`, `sketchfab_*` | Search, preview and import assets |
| `rodin_generate` | Generate a 3D model with Hyper3D Rodin (text and/or reference images) and import it |
| `hunyuan3d_generate` | Generate a 3D model with Tencent Hunyuan3D (text or image; official API or local server) |
| `collect_generated_model` | Fetch and import a generation job started earlier (or one whose wait timed out) |

There is also a `blender://scene` resource and a `blender_task` prompt that starts the recommended workflow:
look at the scene, work in small steps, and verify with screenshots.

**Recommended workflow in Claude Code:** ask it to write scripts in your repo (`scenes/rocks.py`) and run them with
`run_blender_script`. The code stays under version control and can be edited and re-run.

## Configuration (environment variables)

Pass them with `-e` when registering the server, for example `claude mcp add blender -e BLENDER_PORT=9999 -- uv run ...`.

| Variable | Default | |
|---|---|---|
| `BLENDER_HOST` / `BLENDER_PORT` | `127.0.0.1` / `9876` | Must match the port in the Blender panel |
| `BLENDER_MCP_TOKEN` | empty | Same value as "Auth Token" in the add-on preferences |
| `BLENDER_TIMEOUT` | `30` | Seconds per command (long-running tools accept `timeout_s`) |
| `BLENDER_MCP_READONLY` | off | `1` = do not register tools that modify the scene or write files |
| `BLENDER_MCP_LOG` | `INFO` | Logs go to stderr |

## Security

- The socket only listens on `localhost`. With a token configured, every command must carry it (compared in constant time).
- `execute_blender_code` and `run_blender_script` run arbitrary Python with your permissions. You can turn them off with
  **Allow code execution** in the Blender panel. The inspection tools use it too, so with it off only the basic
  summary, screenshots and assets keep working.
- `BLENDER_MCP_READONLY=1` is a convenience so Claude does not see the write tools, **not** a sandbox:
  the real boundary is the switch in the panel.
- Paths are resolved on the machine running Blender. If Claude Code runs on a different one (container, WSL), screenshots
  and exports need a shared filesystem.

## Known limitations

- Blender runs every command on its UI thread: renders, large imports and downloads freeze it until they finish.
- The AI generators **spend credits on your provider account** and take minutes. Claude is instructed to ask for your
  confirmation before the first call. Generated models arrive "raw": scale and orientation usually need adjusting.
- Hunyuan3D in **local server** mode is a single synchronous request: Blender stays frozen until it finishes.
- Rodin and Hunyuan3D are enabled and configured in the Blender panel (checkbox, platform and key). If something is
  missing, the tool says so instead of failing with a cryptic error.
- The generation integrations were tested against simulated API responses, not against the real services.

## Credits and license

- `blender_mcp_server.py`, this README and all modifications to the add-on are free software under the
  **GNU General Public License, version 3 or (at your option) any later version**, see [`LICENSE`](LICENSE).
  Copyright (C) 2026 Daniel Galleguillos Cruz.
- `blender_mcp.py` is a modified version of the add-on from [ahujasid/blender-mcp](https://github.com/ahujasid/blender-mcp)
  (© 2025 Siddharth Ahuja, MIT License). The original portions keep their MIT license and the original copyright and
  license notice are preserved in [`LICENSE-blender-mcp.txt`](LICENSE-blender-mcp.txt); the MIT license is
  GPL-compatible, so the file as a whole is distributed under the GPL with that notice kept. The changes from the
  original are listed in the file header.
