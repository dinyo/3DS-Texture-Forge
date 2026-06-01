"""Read-only NintendoWare CLYT/BCLYT and CLAN layout diagnostics.

This module does not patch layouts. It extracts enough information to answer:
"does this texture appear to have an explicit pane canvas in the archive?"
"""

import os
import struct
from typing import Any, Dict, List


def is_bclyt(data: bytes) -> bool:
    return len(data) >= 0x14 and data[:4] == b"CLYT"


def is_bclan(data: bytes) -> bool:
    return len(data) >= 0x14 and data[:4] == b"CLAN"


def analyze_bclan_animation_types(data: bytes) -> List[str]:
    """Return known CLAN animation record types present in a BCLAN.

    The public CLAN docs are incomplete, so this is intentionally conservative:
    it scans section payloads for known four-byte animation tags. That is enough
    to separate color/visibility animation from texture SRT/pattern animation,
    which is what matters for HD BCLIM replacement safety.
    """
    if not is_bclan(data):
        return []
    known = {b"CLPA", b"CLTS", b"CLVI", b"CLVC", b"CLMC", b"CLTP"}
    found = set()
    for tag in known:
        if data.find(tag) >= 0:
            found.add(tag.decode("ascii"))
    return sorted(found)


def analyze_bclyt_texture_canvases(data: bytes, layout_name: str = "") -> Dict[str, List[Dict[str, Any]]]:
    if not is_bclyt(data):
        return {}
    endian = "<" if data[4:6] == b"\xFF\xFE" else ">"
    sections = _sections(data, endian)
    textures = _read_txl1(data, sections, endian)
    materials = _read_mat1(data, sections, endian)
    if not textures or not materials:
        return {}

    material_to_textures = {}
    for mat_index, mat in enumerate(materials):
        names = []
        for tex_index in mat["texture_indices"]:
            if 0 <= tex_index < len(textures):
                names.append(textures[tex_index])
        material_to_textures[mat_index] = names

    result: Dict[str, List[Dict[str, Any]]] = {}
    for pane in _read_panes(data, sections, endian):
        for tex_name in material_to_textures.get(pane["material_id"], []):
            key = _texture_key(tex_name)
            if not key:
                continue
            result.setdefault(key, []).append({
                "layout": layout_name,
                "pane": pane["name"],
                "pane_type": pane["type"],
                "material_id": pane["material_id"],
                "canvas_width": pane["width"],
                "canvas_height": pane["height"],
            })
    return result


def _sections(data: bytes, endian: str) -> List[Dict[str, int]]:
    count = struct.unpack_from(endian + "H", data, 0x10)[0]
    off = struct.unpack_from(endian + "H", data, 0x06)[0]
    sections = []
    for _ in range(count):
        if off + 8 > len(data):
            break
        magic = data[off:off + 4].decode("ascii", errors="replace")
        size = struct.unpack_from(endian + "I", data, off + 4)[0]
        if size < 8 or off + size > len(data):
            break
        sections.append({"magic": magic, "offset": off, "size": size})
        off += size
    return sections


def _read_txl1(data: bytes, sections: List[Dict[str, int]], endian: str) -> List[str]:
    sec = next((s for s in sections if s["magic"] == "txl1"), None)
    if not sec:
        return []
    off = sec["offset"]
    count = struct.unpack_from(endian + "I", data, off + 8)[0]
    base = off + 0x0C
    names = []
    for i in range(count):
        ptr_off = base + i * 4
        if ptr_off + 4 > off + sec["size"]:
            break
        rel = struct.unpack_from(endian + "I", data, ptr_off)[0]
        names.append(_read_c_string(data, base + rel, off + sec["size"]))
    return names


def _read_mat1(data: bytes, sections: List[Dict[str, int]], endian: str) -> List[Dict[str, Any]]:
    sec = next((s for s in sections if s["magic"] == "mat1"), None)
    if not sec:
        return []
    off = sec["offset"]
    count = struct.unpack_from(endian + "I", data, off + 8)[0]
    mats = []
    for i in range(count):
        ptr_off = off + 0x0C + i * 4
        if ptr_off + 4 > off + sec["size"]:
            break
        mat_off = off + struct.unpack_from(endian + "I", data, ptr_off)[0]
        if mat_off + 0x34 > off + sec["size"]:
            continue
        flags = struct.unpack_from(endian + "I", data, mat_off + 0x30)[0]
        tex_map_count = flags & 0x3
        cursor = mat_off + 0x34
        tex_indices = []
        for _ in range(tex_map_count):
            if cursor + 4 > off + sec["size"]:
                break
            tex_indices.append(struct.unpack_from(endian + "H", data, cursor)[0])
            cursor += 4
        mats.append({"texture_indices": tex_indices})
    return mats


def _read_panes(data: bytes, sections: List[Dict[str, int]], endian: str) -> List[Dict[str, Any]]:
    panes = []
    for sec in sections:
        if sec["magic"] not in ("pic1", "wnd1"):
            continue
        off = sec["offset"]
        size_end = off + sec["size"]
        if off + 0x60 > size_end:
            continue
        name = _read_fixed_string(data, off + 0x0C, 0x18)
        width = struct.unpack_from(endian + "f", data, off + 0x44)[0]
        height = struct.unpack_from(endian + "f", data, off + 0x48)[0]
        mat_off = off + (0x5C if sec["magic"] == "pic1" else 0x78)
        if mat_off + 2 > size_end:
            continue
        material_id = struct.unpack_from(endian + "H", data, mat_off)[0]
        if not (_sane_float(width) and _sane_float(height)):
            continue
        panes.append({
            "type": sec["magic"],
            "name": name,
            "width": width,
            "height": height,
            "material_id": material_id,
        })
    return panes


def _texture_key(path: str) -> str:
    base = os.path.basename(str(path).replace("\\", "/")).lower()
    if base.endswith(".bclim"):
        base = base[:-6]
    return base


def _read_c_string(data: bytes, offset: int, limit: int) -> str:
    if offset < 0 or offset >= limit:
        return ""
    end = data.find(b"\x00", offset, limit)
    if end < 0:
        end = limit
    return data[offset:end].decode("utf-8", errors="replace")


def _read_fixed_string(data: bytes, offset: int, size: int) -> str:
    raw = data[offset:offset + size]
    raw = raw.split(b"\x00", 1)[0]
    return raw.decode("utf-8", errors="replace")


def _sane_float(value: float) -> bool:
    return -100000.0 < value < 100000.0
