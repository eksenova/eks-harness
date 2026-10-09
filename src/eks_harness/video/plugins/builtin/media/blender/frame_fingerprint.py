"""Per-frame scene fingerprints for BlenderScene's frame cache (runs inside Blender; bpy and numpy only).

A frame's fingerprint hashes everything that can change its pixels: render
settings, the active camera and focus distance, every visible object's
transform, flags, geometry (order-independent topology), modifiers and used
materials' node trees, the world and compositor, image files, and the exact
movie frames sampled (via a ``.framemd5`` sidecar next to a movie texture),
at three subframes so motion blur is covered. A frame whose fingerprint
matches the one recorded with its existing image on the same platform is not
rendered again. ``EKS_HARNESS_FP_DUMP=/tmp/fp`` writes the hashed state for
debugging cache misses.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import bpy
import numpy as np

SUBFRAMES = (-0.25, 0.0, 0.25)
OBJECT_FLAGS = ("hide_render", "visible_camera", "visible_diffuse", "visible_glossy", "visible_transmission",
                "visible_volume_scatter", "visible_shadow", "is_holdout", "is_shadow_catcher")
SKIP_PROPS = {"rna_type", "name_full", "session_uid", "is_evaluated", "original", "users", "use_fake_user",
              "is_embedded_data", "is_missing", "is_runtime_data", "tag", "is_library_indirect", "library",
              "library_weak_reference", "asset_data", "override_library", "preview", "id_type", "use_extra_user",
              "is_dirty", "bindcode", "has_data", "is_float", "is_editmode", "frame_current", "frame_current_final",
              "frame_float", "frame_subframe", "threads", "threads_mode", "device", "denoiser",
              "denoising_use_gpu", "filepath"}


def _value(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, (set, frozenset)):
        return tuple(sorted(value))
    if hasattr(value, "__len__") and not isinstance(value, str):
        try:
            return tuple(_value(v) for v in value)
        except TypeError:
            return str(value)
    if isinstance(value, bpy.types.ID):
        return f"ID:{value.name}"
    return value


def rna(struct: Any, depth: int = 1) -> list:
    if struct is None:
        return [None]
    out = [struct.bl_rna.identifier]
    for prop in struct.bl_rna.properties:
        key = prop.identifier
        if key in SKIP_PROPS:
            continue
        try:
            value = getattr(struct, key)
        except Exception:
            continue
        if prop.type == "POINTER":
            if isinstance(value, bpy.types.ID):
                out.append((key, value.name))
            elif depth > 0 and value is not None:
                out.append((key, rna(value, depth - 1)))
        elif prop.type == "COLLECTION":
            continue
        else:
            out.append((key, _value(value)))
    return out


def _topology(mesh: bpy.types.Mesh) -> bytes:
    """Vertices, topology and UVs independent of element order, which Blender does not keep stable between runs."""

    co = np.empty(len(mesh.vertices) * 3, np.float32)
    mesh.vertices.foreach_get("co", co)
    co = np.round(co.reshape(-1, 3).astype(np.float64), 6)
    vert_order = np.lexsort(co.T[::-1])
    vert_rank = np.empty(len(co), np.int64)
    vert_rank[vert_order] = np.arange(len(co))
    n_poly = len(mesh.polygons)
    if n_poly == 0:
        return co[vert_order].tobytes()
    totals = np.empty(n_poly, np.int32)
    mesh.polygons.foreach_get("loop_total", totals)
    corner_vert = np.empty(len(mesh.loops), np.int32)
    mesh.loops.foreach_get("vertex_index", corner_vert)
    corner_vert = vert_rank[corner_vert]
    poly_of_corner = np.repeat(np.arange(n_poly), totals)
    starts = np.concatenate(([0], np.cumsum(totals)[:-1]))
    width = int(totals.max())
    keys = np.full((n_poly, width), -1, np.int64)
    within = np.arange(len(corner_vert)) - starts[poly_of_corner]
    keys[poly_of_corner, within] = corner_vert
    keys = -np.sort(-keys, axis=1)
    poly_order = np.lexsort(keys.T[::-1])
    rank = np.empty(n_poly, np.int64)
    rank[poly_order] = np.arange(n_poly)
    corner_order = np.lexsort((corner_vert, rank[poly_of_corner]))
    parts = [co[vert_order].tobytes(), keys[poly_order].tobytes(), corner_vert[corner_order].tobytes()]
    if mesh.uv_layers.active:
        uv = np.empty(len(mesh.loops) * 2, np.float32)
        mesh.uv_layers.active.data.foreach_get("uv", uv)
        parts.append(np.round(uv.reshape(-1, 2)[corner_order], 5).tobytes())
    return b"".join(parts)


class Fingerprinter:
    def __init__(self, scene: bpy.types.Scene) -> None:
        self.scene = scene
        self.file_hashes: dict[str, str] = {}
        self.frame_md5: dict[str, list[str] | None] = {}
        self.static_geometry: dict[str, str] = {}
        self.static = self._static()

    def _file_hash(self, path: str) -> str:
        if path not in self.file_hashes:
            file = Path(bpy.path.abspath(path))
            self.file_hashes[path] = hashlib.sha1(file.read_bytes()).hexdigest() if file.exists() else "missing"
        return self.file_hashes[path]

    def _movie_frames(self, path: str) -> list[str] | None:
        if path not in self.frame_md5:
            sidecar = Path(bpy.path.abspath(path) + ".framemd5")
            if sidecar.exists():
                lines = [ln for ln in sidecar.read_text().splitlines() if ln and not ln.startswith("#")]
                self.frame_md5[path] = [ln.rsplit(",", 1)[-1].strip() for ln in lines]
            else:
                self.frame_md5[path] = None
        return self.frame_md5[path]

    def _static(self) -> str:
        scene = self.scene
        parts: list = [bpy.app.version_string, rna(scene.render, 1), rna(scene.cycles, 0), rna(scene.eevee, 0),
                       rna(scene.view_settings, 1), rna(scene.display_settings, 0), rna(scene.render.image_settings, 1)]
        for layer in scene.view_layers:
            parts.append(rna(layer, 0))
        for image in bpy.data.images:
            if image.source in {"FILE", "SEQUENCE"} and image.filepath:
                parts.append((image.name, image.source, self._file_hash(image.filepath)))
            elif image.source == "MOVIE":
                md5 = self._movie_frames(image.filepath)
                parts.append((image.name, "MOVIE", len(md5) if md5 else self._file_hash(image.filepath)))
        if os.environ.get("EKS_HARNESS_FP_DUMP"):
            Path(os.environ["EKS_HARNESS_FP_DUMP"] + ".static").write_text(repr(parts).replace("), (", "),\n("))
        return hashlib.sha1(repr(parts).encode()).hexdigest()

    def _data_hash(self, obj: bpy.types.Object, depsgraph: bpy.types.Depsgraph) -> str:
        data = obj.data
        key = f"{type(data).__name__}:{data.name}"
        if key not in self.static_geometry:
            if isinstance(data, bpy.types.Mesh):
                digest = hashlib.sha1(_topology(data)).hexdigest()
            else:
                base = bpy.data.objects.new("_eks_fp_probe", data)
                bpy.context.scene.collection.objects.link(base)
                try:
                    mesh = base.evaluated_get(bpy.context.evaluated_depsgraph_get()).to_mesh()
                    digest = hashlib.sha1(_topology(mesh) if mesh else b"none").hexdigest()
                finally:
                    bpy.data.objects.remove(base)
            self.static_geometry[key] = digest
        return self.static_geometry[key]

    def _geometry(self, obj: bpy.types.Object, depsgraph: bpy.types.Depsgraph) -> list:
        data = obj.data
        parts: list = [self._data_hash(obj, depsgraph)]
        if data.animation_data or not isinstance(data, bpy.types.Mesh):
            parts.append(rna(data, 0))
        keys = getattr(data, "shape_keys", None)
        if keys:
            parts.append([(k.name, round(k.value, 6), k.mute) for k in keys.key_blocks])
        for mod in obj.modifiers:
            parts.append(rna(mod, 0))
        parts.append([s.material.name if s.material else None for s in obj.material_slots])
        return parts

    def _node_tree(self, tree: bpy.types.NodeTree | None, frame: int) -> list:
        if tree is None:
            return [None]
        parts: list = []
        for node in tree.nodes:
            entry = [node.bl_idname, node.name, rna(node, 0)]
            for socket in node.inputs:
                if hasattr(socket, "default_value") and not socket.is_linked:
                    entry.append((socket.identifier, _value(socket.default_value)))
            image = getattr(node, "image", None)
            if image is not None and image.source == "MOVIE":
                entry.append(("movie_frame", self._movie_frame_md5(image, node.image_user, frame)))
            if node.bl_idname == "ShaderNodeGroup":
                entry.append(self._node_tree(node.node_tree, frame))
            parts.append(entry)
        parts.append(sorted((ln.from_node.name, ln.from_socket.identifier, ln.to_node.name, ln.to_socket.identifier)
                            for ln in tree.links))
        return parts

    def _movie_frame_md5(self, image: bpy.types.Image, user: bpy.types.ImageUser, frame: int) -> Any:
        md5 = self._movie_frames(image.filepath)
        if md5 is None:
            return self._file_hash(image.filepath)
        length = user.frame_duration or len(md5)
        picks = []
        for f in (frame - 1, frame, frame + 1):
            n = f - user.frame_start + 1
            if user.use_cyclic and length:
                n = (n - 1) % length + 1
            n = min(max(n, 1), length) + user.frame_offset
            picks.append(md5[min(max(n - 1, 0), len(md5) - 1)])
        return tuple(picks)

    def _state(self, frame: int) -> list:
        depsgraph = bpy.context.evaluated_depsgraph_get()
        active = self.scene.camera
        parts: list = [("camera", active.name if active else None)]
        if active and active.data.dof.use_dof and active.data.dof.focus_object:
            distance = (active.data.dof.focus_object.matrix_world.translation - active.matrix_world.translation).length
            parts.append(("focus", round(distance, 5)))
        for obj in sorted(self.scene.objects, key=lambda o: o.name):
            if obj.type == "EMPTY" or (obj.type == "CAMERA" and obj != active):
                continue
            flags = tuple(getattr(obj, flag, None) for flag in OBJECT_FLAGS)
            if obj.hide_render:
                parts.append([obj.name, obj.type, flags])
                continue
            entry = [obj.name, obj.type, tuple(round(v, 6) for row in obj.matrix_world for v in row), flags]
            if obj.type in {"MESH", "CURVE", "FONT", "SURFACE", "META"}:
                entry.append(self._geometry(obj, depsgraph))
            elif obj.type in {"LIGHT", "CAMERA"}:
                entry.append(rna(obj.data, 1))
            parts.append(entry)
        used = sorted({slot.material.name for obj in self.scene.objects if not obj.hide_render
                       for slot in obj.material_slots if slot.material})
        for name in used:
            mat = bpy.data.materials[name]
            parts.append((mat.name, rna(mat, 0), self._node_tree(mat.node_tree, frame)))
        world = self.scene.world
        parts.append(("world", self._node_tree(world.node_tree if world else None, frame)))
        compositor = getattr(self.scene, "compositing_node_group", None) or getattr(self.scene, "node_tree", None)
        if compositor is not None:
            parts.append(("compositor", self._node_tree(compositor, frame)))
        return parts

    def frame(self, frame: int) -> str:
        h = hashlib.sha1(self.static.encode())
        for sub in SUBFRAMES:
            whole = frame + int(sub // 1)
            self.scene.frame_set(whole, subframe=sub % 1)
            state = repr(self._state(frame))
            dump = os.environ.get("EKS_HARNESS_FP_DUMP")
            if dump:
                Path(f"{dump}.{frame}.{sub}").write_text(state.replace("), (", "),\n("))
            h.update(state.encode())
        self.scene.frame_set(frame)
        return h.hexdigest()[:20]
