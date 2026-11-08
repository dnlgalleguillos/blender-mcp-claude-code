# Code created by Daniel Galleguillos: https://github.com/dnlgalleguillos
# Copyright (C) 2026 Daniel Galleguillos Cruz
# SPDX-License-Identifier: GPL-3.0-or-later
"""Scene inspection, look, render and safe-mode tests against a real headless Blender (see blender_harness.py)."""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import blender_harness as h  # noqa: E402
from blender_harness import bpy, check, raises  # noqa: E402

import mathutils  # noqa: E402


def png_size(data: bytes):
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def png_mean_rgb(data: bytes):
    """Average colour of an 8-bit RGB(A) PNG, decoded by hand (PNG bytes also carry noise, so never compare bytes)."""
    import struct
    import zlib
    pos, idat = 8, b""
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, depth, ctype = struct.unpack(">IIBB", body[:10])
            assert depth == 8 and ctype in (2, 6), (depth, ctype)
        elif kind == b"IDAT":
            idat += body
    raw, bpp = zlib.decompress(idat), (3 if ctype == 2 else 4)
    stride, prev, i, sums = width * bpp, bytearray(width * bpp), 0, [0, 0, 0]
    for _ in range(height):
        f, line = raw[i], bytearray(raw[i + 1:i + 1 + stride])
        i += 1 + stride
        for x in range(stride):
            a = line[x - bpp] if x >= bpp else 0
            b, c = prev[x], (prev[x - bpp] if x >= bpp else 0)
            if f == 1:
                line[x] = (line[x] + a) & 255
            elif f == 2:
                line[x] = (line[x] + b) & 255
            elif f == 3:
                line[x] = (line[x] + ((a + b) >> 1)) & 255
            elif f == 4:
                p_ = a + b - c
                pa, pb, pc = abs(p_ - a), abs(p_ - b), abs(p_ - c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else (b if pb <= pc else c))) & 255
        for k in range(3):
            sums[k] += sum(line[k::bpp])
        prev = line
    return [v / (width * height) / 255 for v in sums]


def build_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.frame_start, sc.frame_end = 1, 50
    sc.render.resolution_x, sc.render.resolution_y = 1920, 1080
    active = lambda: bpy.context.view_layer.objects.active  # noqa: E731

    bpy.ops.mesh.primitive_cube_add(size=2, location=(0, 0, 1))
    crate = active()
    crate.name = "Crate"
    mat = bpy.data.materials.new("CrateMat")
    mat.use_nodes = True
    mat.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.8, 0.2, 0.1, 1)
    crate.data.materials.append(mat)
    crate.modifiers.new("Bevel", "BEVEL")

    bpy.ops.mesh.primitive_uv_sphere_add(radius=0.5, location=(3, 0, 2))
    ball = active()
    ball.name = "Ball"
    ball.location.z = 2
    ball.keyframe_insert("location", index=2, frame=1)
    ball.location.z = 5
    ball.keyframe_insert("location", index=2, frame=50)
    sc.frame_set(1)

    bpy.ops.mesh.primitive_plane_add(size=4, location=(0, 3, 0))
    active().name = "Floor"

    bpy.ops.object.camera_add(location=(9, -9, 6))
    cam = active()
    cam.name = "Cam"
    cam.rotation_euler = (mathutils.Vector((0, 0, 1)) - cam.location).to_track_quat("-Z", "Y").to_euler()
    sc.camera = cam
    bpy.ops.object.light_add(type="SUN", location=(0, 0, 10))
    active().name = "Sun"

    rig = bpy.data.objects.new("Rig", None)
    sc.collection.objects.link(rig)
    rig.location = (6, 0, 0)
    bpy.ops.mesh.primitive_cube_add(size=1, location=(6, 0, 0.5))
    wheel = active()
    wheel.name = "Wheel"
    wheel.parent = rig
    wheel.matrix_parent_inverse = rig.matrix_world.inverted()

    bpy.ops.object.armature_add(location=(-4, 0, 0))
    arm = active()
    arm.name = "Skeleton"
    bpy.ops.object.mode_set(mode="EDIT")
    eb = arm.data.edit_bones.new("Bone2")
    eb.head, eb.tail = (0, 0, 1), (0, 0, 2)
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.mesh.primitive_cube_add(size=2, location=(-4, 0, 1))
    body = active()
    body.name = "Body"
    vg = body.vertex_groups.new(name="Bone")
    vg.add([v.index for v in body.data.vertices if v.co.z > 0], 1.0, "REPLACE")
    body.modifiers.new("Armature", "ARMATURE").object = arm

    for o in bpy.context.scene.objects:
        o.select_set(False)
    crate.select_set(True)
    bpy.context.view_layer.objects.active = crate
    bpy.context.view_layer.update()


# bpy.ops must run on the main thread (Blender crashes otherwise), so the scene and the add-on are set up here;
# the async tests below only read bpy data. The scene is built first because loading factory settings
# fires the add-on's load_post handler.
build_scene()
addon = h.load_addon()


async def main():
    port = 19970
    srv = addon.Bridge(port=port)
    assert srv.start(), "the add-on socket did not start"
    S = h.load_server(port)
    sc = bpy.context.scene

    # ------------------------------------------------------------------ status
    d = json.loads(await S.blender_status())
    check("blender_status reports real Blender 5.x and code execution", d["blender_version"].startswith("5.")
          and d["code_execution"] is True and d["safe_mode"] is False and d["protocol"] == 2
          and d["addon"] == "blender-mcp-bridge" and d["libraries"]["polyhaven"].startswith("ready") and d["generators"] == {"hyper3d_rodin": False, "hunyuan3d": False}, d)
    wm = bpy.context.window_manager
    wm.blendermcp_allow_code = False
    d = json.loads(await S.blender_status())
    check("status still works with code execution switched off, and says so", d["code_execution"] is False, d)
    r = await raises(S.get_scene_info(), "allow code execution")
    check("with the panel switch off, tools that need code say how to turn it on", r[0], r[1])
    wm.blendermcp_allow_code = True

    # ------------------------------------------------------------------ get_scene_info
    out = await S.get_scene_info()
    lines = out.splitlines()
    check("header: counts, active and selection", lines[0].startswith('Scene "Scene" | 9 objects')
          and "active: Crate" in lines[0] and "selected: Crate" in lines[0], lines[0])
    crate_line = next(l for l in lines if l.startswith("Crate"))
    check("default fields: location, size, children, hidden",
          "loc=(0, 0, 1)" in crate_line and "size=(2, 2, 2)" in crate_line and "children=0" in crate_line
          and "hidden=no" in crate_line, crate_line)
    check("top level only by default (Wheel is under Rig)", not any(l.lstrip().startswith("Wheel") for l in lines))

    out = await S.get_scene_info(fields=["placement"])
    rows = {l.split(" ")[0]: l for l in out.splitlines()[1:]}
    check("ground: resting on ground", "ground=on ground" in rows["Crate"], rows["Crate"])
    check("ground: floating, with the distance", "ground=floating 1.5 m" in rows["Ball"], rows["Ball"])
    check("ground: a flat plane at z=0 is on ground", "ground=on ground" in rows["Floor"], rows["Floor"])
    check("rotation and scale fields", "rot=(0, 0, 0)°" in rows["Crate"] and "scale=(1, 1, 1)" in rows["Crate"], rows["Crate"])
    check("size is the world bounding box incl. children (Rig has the Wheel inside)",
          "size=(1, 1, 1)" in rows["Rig"], rows["Rig"])

    out = await S.get_scene_info(fields=["health"], query="Crate")
    check("health: topology of a cube", "topology[tris 0, quads 6, ngons 0, non-manifold edges 0, boundary edges 0, "
          "loose verts 0, poles 3-valence 8 / 5+ 0]" in out, out)
    out = await S.get_scene_info(fields=["health"], query="Floor")
    check("health: open plane has 4 boundary edges", "boundary edges 4" in out and "quads 1" in out, out)
    out = await S.get_scene_info(fields=["health"], query="Body")
    check("health: rig weights (4 vertices unweighted, Bone2 has no vertex group)",
          "weights[Skeleton: 4 vertices no deform bone moves; deform bones without vertex group: Bone2]" in out, out)

    out = await S.get_scene_info(query="ba")
    check("query filters by name", [l.split(" ")[0] for l in out.splitlines()[1:]] == ["Ball"], out)
    out = await S.get_scene_info(root="Rig")
    check("root lists the hierarchy, children indented", any(l.startswith("  Wheel") for l in out.splitlines()), out)
    out = await S.get_scene_info(fields=["contents"], query="Crate")
    check("contents: details, materials, modifiers",
          "faces=6" in out and "materials=CrateMat" in out and "modifiers=Bevel:BEVEL" in out, out)
    out = await S.get_scene_info(fields=["contents"], query="Ball")
    check("contents: animation action and frame range", "anim=" in out and " 1-50" in out, out)
    out = await S.get_scene_info(fields=["extras"], limit=1)
    check("extras: settings and collection tree", "engine " in out and "1920x1080" in out and "frames 1-50" in out
          and "camera Cam" in out and "collections: Scene Collection[" in out, out)
    out = await S.get_scene_info(limit=2)
    check("truncation notice", "(showing 2 of" in out, out)
    r = await raises(S.get_scene_info(fields=["colour"]), "unknown field", "location")
    check("unknown field -> helpful error", r[0], r[1])
    r = await raises(S.get_scene_info(root="rig2"), "not found", "Rig")
    check("unknown root -> suggestions", r[0], r[1])
    check("blender://scene resource", "settings:" in await S.scene_resource())

    # ------------------------------------------------------------------ get_object_details (kept from 1.x)
    d = json.loads(await S.get_object_details(name="Crate"))
    check("get_object_details on real Blender", d["modifiers"][0]["type"] == "BEVEL"
          and d["materials"][0]["name"] == "CrateMat" and d["mesh"]["faces"] == 6 and d["world_bbox"]["max"] == [1, 1, 2], d)

    # ------------------------------------------------------------------ get_node_tree
    groups_before, mats_before = sorted(bpy.data.node_groups.keys()), sorted(bpy.data.materials.keys())
    tmesh = bpy.data.meshes.new("NT_Mesh")
    tobj = bpy.data.objects.new("NT_Obj", tmesh)
    bpy.context.scene.collection.objects.link(tobj)

    listing = await S.get_node_tree(kind="material")
    check("get_node_tree lists the materials that use nodes", "CrateMat" in listing and "Available (material)" in listing, listing)
    out = await S.get_node_tree(kind="material", name="CrateMat")
    check("get_node_tree: material nodes, a changed value and the links",
          "Principled BSDF" in out and "Material Output" in out and "Base Color=(0.8, 0.2, 0.1, 1)" in out
          and "Principled BSDF.BSDF -> Material Output.Surface" in out, out)
    check("get_node_tree: unchanged inputs are left out", "Metallic=" not in out and "IOR=" not in out, out)
    out = await S.get_node_tree(kind="material", object_name="Crate")
    check("get_node_tree: materials of an object", 'Material "CrateMat"' in out, out)

    mat = bpy.data.materials.new("NT_Mat")
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    noise = nt.nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = 7.0
    noise.noise_dimensions = "4D"
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].position = 0.3
    math_node = nt.nodes.new("ShaderNodeMath")
    math_node.operation = "MULTIPLY"
    math_node.inputs[1].default_value = 2.5
    muted = nt.nodes.new("ShaderNodeMapping")
    muted.mute = True
    muted.label = "Skewed"
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    nt.links.new(noise.outputs[0], math_node.inputs[0])
    nt.links.new(math_node.outputs[0], ramp.inputs[0])
    nt.links.new(ramp.outputs[0], bsdf.inputs["Base Color"])
    out = await S.get_node_tree(kind="material", name="NT_Mat")
    check("get_node_tree: node settings and values that differ from the defaults",
          "Scale=7" in out and "noise_dimensions=4D" in out and "operation=MULTIPLY" in out and "Value[1]=2.5" in out
          and "stops 0.3:(0, 0, 0, 1)" in out, out)
    check("get_node_tree: same-named sockets are told apart", "Math.Value[0]" in out or "Value[0]" in out, out)
    check("get_node_tree: muted node and label", "MUTED" in out and '"Skewed"' in out, out)
    check("get_node_tree: data-flow order (the noise before the ramp before the BSDF)",
          out.index("Noise Texture") < out.index("Color Ramp") < out.index("Principled BSDF"), out)
    out = await S.get_node_tree(kind="material", name="NT_Mat", max_nodes=2)
    check("get_node_tree: max_nodes truncates and says so", "more nodes not shown" in out and out.count("\n  ") < 12, out)

    inner = bpy.data.node_groups.new("NT_Inner", "GeometryNodeTree")
    inner.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    inner.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    ig_in, ig_out = inner.nodes.new("NodeGroupInput"), inner.nodes.new("NodeGroupOutput")
    flip = inner.nodes.new("GeometryNodeFlipFaces")
    inner.links.new(ig_in.outputs[0], flip.inputs[0])
    inner.links.new(flip.outputs[0], ig_out.inputs[0])
    gn = bpy.data.node_groups.new("NT_Scatter", "GeometryNodeTree")
    gn.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    dens = gn.interface.new_socket("Density", in_out="INPUT", socket_type="NodeSocketFloat")
    dens.default_value, dens.min_value, dens.max_value = 6.0, 0.0, 100.0
    gn.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    gi, go = gn.nodes.new("NodeGroupInput"), gn.nodes.new("NodeGroupOutput")
    sub = gn.nodes.new("GeometryNodeGroup")
    sub.node_tree = inner
    dist = gn.nodes.new("GeometryNodeDistributePointsOnFaces")
    gn.links.new(gi.outputs["Geometry"], sub.inputs[0])
    gn.links.new(sub.outputs[0], dist.inputs["Mesh"])
    gn.links.new(gi.outputs["Density"], dist.inputs["Density"])
    gn.links.new(dist.outputs["Points"], go.inputs[0])
    gmod = tobj.modifiers.new("Scatter", "NODES")
    gmod.node_group = gn
    ident = next(i.identifier for i in gn.interface.items_tree if i.name == "Density" and i.in_out == "INPUT")
    try:
        gmod.properties.inputs[ident]["value"] = 12.0
    except Exception:
        gmod[ident] = 12.0
    out = await S.get_node_tree(kind="geometry_nodes", object_name="NT_Obj")
    check("get_node_tree: Geometry Nodes interface, modifier values, links and nested group",
          "Density (Float, default 6, range 0..100)" in out and "Modifier values: Density=12" in out
          and "Group Input.Density -> Distribute Points on Faces.Density" in out and "group=<NT_Inner>" in out
          and "Uses node groups: NT_Inner" in out and "Flip Faces" not in out, out)
    out = await S.get_node_tree(kind="geometry_nodes", object_name="NT_Obj", name="NT_Scatter", recurse_groups=True)
    check("get_node_tree: recurse_groups also reads the nested group", 'Node group "NT_Inner" (used inside)' in out
          and "Flip Faces" in out, out)
    out = await S.get_node_tree(kind="node_group", name="NT_Inner")
    check("get_node_tree: a node group by name", "Flip Faces" in out and "Group Input.Geometry -> Flip Faces.Mesh" in out, out)
    check("get_node_tree: lists Geometry Nodes objects and node groups",
          "NT_Obj: Scatter -> NT_Scatter" in await S.get_node_tree(kind="geometry_nodes")
          and "NT_Scatter [GeometryNodeTree]" in await S.get_node_tree(kind="node_group"))

    r = await raises(S.get_node_tree(kind="material", name="NT_Mt"), "Material not found: NT_Mt", "NT_Mat")
    check("get_node_tree: wrong material name suggests similar ones", r[0], r[1])
    r = await raises(S.get_node_tree(kind="geometry_nodes", object_name="Crate"), "no Geometry Nodes modifier")
    check("get_node_tree: object without a Geometry Nodes modifier", r[0], r[1])
    r = await raises(S.get_node_tree(kind="geometry_nodes", name="NT_Scatter"), "needs object_name")
    check("get_node_tree: geometry_nodes with a name but no object", r[0], r[1])
    r = await raises(S.get_node_tree(kind="node_group", object_name="NT_Obj", name="Nope"), "Node group not found")
    check("get_node_tree: unknown node group", r[0], r[1])
    r = await raises(S.get_node_tree(kind="world"), "no world")
    check("get_node_tree: scene without a world", r[0], r[1])
    world = bpy.data.worlds.new("NT_World")
    world.use_nodes = True
    bpy.context.scene.world = world
    out = await S.get_node_tree(kind="world")
    check("get_node_tree: world nodes", 'World "NT_World"' in out and "Background" in out and "World Output" in out, out)
    out = await S.get_node_tree(kind="compositor")
    check("get_node_tree: scene without a compositor tree says so", "no node tree" in out, out)
    mat_plain = bpy.data.materials.new("NT_Plain")
    mat_plain.use_nodes = False
    if mat_plain.node_tree is None:
        out = await S.get_node_tree(kind="material", name="NT_Plain")
        check("get_node_tree: material without nodes", "no node tree" in out, out)

    check("get_node_tree changed nothing and left no scratch data behind",
          sorted(bpy.data.node_groups.keys()) == sorted(groups_before + ["NT_Inner", "NT_Scatter"])
          and sorted(bpy.data.materials.keys()) == sorted(mats_before + ["NT_Mat", "NT_Plain"])
          and not any(k.startswith("_mcp_scratch") for k in bpy.data.node_groups.keys())
          and tobj.modifiers["Scatter"].node_group == gn, (list(bpy.data.node_groups.keys()), list(bpy.data.materials.keys())))

    bpy.context.scene.world = None
    for coll, names in ((bpy.data.objects, ["NT_Obj"]), (bpy.data.meshes, ["NT_Mesh"]), (bpy.data.node_groups, ["NT_Scatter", "NT_Inner"]),
                        (bpy.data.materials, ["NT_Mat", "NT_Plain"]), (bpy.data.worlds, ["NT_World"])):
        for nm in names:
            coll.remove(coll[nm])

    # ------------------------------------------------------------------ look
    rs = sc.render
    before = (rs.engine, rs.resolution_x, rs.resolution_y, rs.filepath, sc.camera.name, sc.frame_current,
              sc.display.shading.light, sc.display.shading.color_type, bpy.context.preferences.view.render_display_type)
    res = await S.look(mode="angles", max_size=256)
    images = [x for x in res[1:]]
    check("angles: 4 images by default + a caption", len(images) == 4 and "front, right, top, three_quarter" in res[0], res[0])
    check("angles: valid square PNGs", all(png_size(i.data) == (256, 256) for i in images))
    check("angles: the four views really differ", len({i.data for i in images}) == 4)
    check("angles: render settings restored", before == (rs.engine, rs.resolution_x, rs.resolution_y, rs.filepath,
          sc.camera.name, sc.frame_current, sc.display.shading.light, sc.display.shading.color_type,
          bpy.context.preferences.view.render_display_type), (before, rs.engine, rs.resolution_x))
    check("angles: temporary cameras removed", not [o.name for o in bpy.data.objects if o.name.startswith("_mcp_look")]
          and not [c.name for c in bpy.data.cameras if c.name.startswith("_mcp_look")])
    check("angles: objects unchanged", len(sc.objects) == 9)

    res = await S.look(mode="angles", target=["Ball"], views=["top", [1, 1, 0.5]], shading="clay", max_size=200)
    check("angles with target, custom views, clay shading", len(res) == 3 and "Ball" in res[0] and "top, dir(1,1,0.5)" in res[0], res[0])
    for shading in ("textured", "flat", "xray", "solid"):
        res = await S.look(mode="angles", views=["front"], shading=shading, max_size=160)
        check(f"shading '{shading}' renders", len(res) == 2 and png_size(res[1].data) == (160, 160))
    crate_mat = bpy.data.materials["CrateMat"]
    grey_before = tuple(crate_mat.diffuse_color)
    solid = png_mean_rgb((await S.look(mode="angles", target=["Crate"], views=["front"], shading="solid", max_size=160))[1].data)
    clay = png_mean_rgb((await S.look(mode="angles", target=["Crate"], views=["front"], shading="clay", max_size=160))[1].data)
    check("solid shows the material's real base colour (red crate looks red)", solid[0] - solid[1] > 0.05, solid)
    check("clay is one neutral colour (no tint)", abs(clay[0] - clay[1]) < 0.01 and abs(clay[1] - clay[2]) < 0.01, clay)
    check("materials' viewport colours restored after look", tuple(crate_mat.diffuse_color) == grey_before, tuple(crate_mat.diffuse_color))

    res = await S.look(mode="camera", max_size=320)
    check("camera: scene camera at the render aspect (1920x1080 -> 320x180)", png_size(res[1].data) == (320, 180)
          and "scene camera" in res[0], (res[0], png_size(res[1].data)))
    res = await S.look(mode="frames", max_size=160, frame_count=4)
    check("frames: 4 images across 1-50 through the scene camera", len(res) == 5 and "1, 17, 34, 50" in res[0], res[0])
    check("frames: the animation shows (first and last frame differ)", res[1].data != res[4].data)
    check("frames: playhead restored", sc.frame_current == 1)
    res = await S.look(mode="frames", frames=[1, 25], view="three_quarter", target=["Ball"], max_size=160)
    check("frames: explicit frames from an angle", len(res) == 3 and "three_quarter" in res[0], res[0])
    with tempfile.TemporaryDirectory() as td:
        await S.look(mode="angles", views=["front", "top"], max_size=128, save_dir=str(Path(td, "sub")))
        check("save_dir writes numbered PNGs", sorted(p.name for p in Path(td, "sub").iterdir()) == ["look_01.png", "look_02.png"])

    r = await raises(S.look(mode="angles", target=["Balll"]), "not found", "Ball")
    check("unknown target -> suggestions", r[0], r[1])
    r = await raises(S.look(mode="angles", views=["sideways"]), "unknown view")
    check("unknown view -> error", r[0], r[1])
    r = await raises(S.look(mode="angles", views=["front"] * 7), "at most 6")
    check("more than 6 views -> error", r[0], r[1])
    r = await raises(S.look(mode="viewport", shading="clay"), "viewport")
    check("viewport mode rejects render-only options", r[0], r[1])
    r = await raises(S.look(mode="viewport"))
    check("viewport without a 3D viewport -> clean error, no hang", r[0] or "no 3d viewport" in r[1].lower() or "screen" in r[1].lower(), r[1])
    saved_cam = sc.camera
    sc.camera = None
    r = await raises(S.look(mode="camera"), "no active camera")
    check("camera mode without a camera -> error", r[0], r[1])
    sc.camera = saved_cam

    # ------------------------------------------------------------------ render_image
    with tempfile.TemporaryDirectory() as td:
        res = await S.render_image(output_path=str(Path(td, "r.png")), engine="WORKBENCH", resolution_percentage=10)
        info = json.loads(res[0])
        check("render_image with WORKBENCH (engine chosen by alias)", info["engine"] == "BLENDER_WORKBENCH"
              and Path(td, "r.png").exists() and len(res) == 2 and png_size(res[1].data) == (192, 108), (info, len(res)))
        res = await S.render_image(output_path=str(Path(td, "e.png")), engine="EEVEE", resolution_percentage=5, samples=1)
        check("render_image with the EEVEE alias on this Blender", "EEVEE" in json.loads(res[0])["engine"], res[0])
        check("render_image restores the scene settings", before[:5] == (rs.engine, rs.resolution_x, rs.resolution_y,
              rs.filepath, sc.camera.name) and rs.resolution_percentage == 100)
        r = await raises(S.render_image(output_path=str(Path(td, "x.txt"))), "extension")
        check("render_image rejects unknown extensions", r[0], r[1])

    # ------------------------------------------------------------------ code tools, normal mode
    out = await S.execute_blender_code(code="print(len(bpy.data.objects), math.pi > 3, mathutils.Vector((1, 2, 3)).length > 3, bmesh.new().free())")
    check("execute_blender_code returns stdout; math, mathutils and bmesh are always available",
          out.strip() == "9 True True None", out)
    with tempfile.TemporaryDirectory() as td:
        script = Path(td, "make_box.py")
        script.write_text("import bpy\nname = SCRIPT_ARGS['name']\n\nraise ValueError('boom ' + name)\n")
        r = await raises(S.run_blender_script(path=str(script), args={"name": "x"}), "boom x", "make_box.py", "line 4")
        check("run_blender_script: real file name and line in the error", r[0], r[1])

    # ------------------------------------------------------------------ safe mode, end to end
    S2 = h.load_server(port, BLENDER_MCP_SAFE_MODE=1)
    check("blender_status shows safe mode", json.loads(await S2.blender_status())["safe_mode"] is True)
    r = await raises(S2.execute_blender_code(code="import os\nos.system('touch /tmp/pwned')"), "safe mode", "import of 'os'")
    check("safe mode blocks hostile code before it reaches Blender", r[0], r[1])
    r = await raises(S2.execute_blender_code(code="import bpy\nbpy.app.timers.register(print)"), "safe mode", "timers")
    check("safe mode blocks persistence (timers)", r[0], r[1])
    n = len(bpy.data.objects)
    await S2.execute_blender_code(code="import bpy\nbpy.ops.mesh.primitive_cube_add(size=1, location=(20, 0, 0.5))\n"
                                       "bpy.context.active_object.name = 'SafeBox'")
    check("safe mode lets legitimate modelling code run", len(bpy.data.objects) == n + 1 and "SafeBox" in bpy.data.objects)
    with tempfile.TemporaryDirectory() as td:
        bad = Path(td, "bad.py")
        bad.write_text("import subprocess\n")
        r = await raises(S2.run_blender_script(path=str(bad)), "safe mode", "subprocess")
        check("safe mode also guards run_blender_script", r[0], r[1])
    check("hostile file was never created", not Path("/tmp/pwned").exists())

    # ------------------------------------------------------------------ read-only mode registers the right tools
    S3 = h.load_server(port, BLENDER_MCP_READONLY=1)
    names = {t.name for t in await S3.mcp.list_tools()}
    check("read-only keeps looking and reading, hides writing",
          {"get_scene_info", "look", "get_object_details", "get_node_tree", "search_assets", "blender_status"} <= names
          and not ({"execute_blender_code", "run_blender_script", "import_asset", "save_blend", "render_image"} & names), sorted(names))

    srv.stop()


h.run(main)
h.finish()
