"""Replace encoded texture payloads inside simple 3DS texture containers."""

import struct
from typing import Any, Dict, List

from texture_encoder import TextureEncodeError, encode_png_to_pica
from textures.bflim import is_bflim
from textures.decoder import FMT_RGBA8
from textures.ctpk import is_ctpk, parse_ctpk
from textures.ctxb import is_ctxb, parse_ctxb
from textures.cmb import is_cmb, extract_cmb_textures
from textures.jimg import is_jimg
from textures.shinen_tex import is_shinen_tex
from textures.stex import is_stex, parse_stex


class TextureReplaceError(RuntimeError):
    pass


def replace_texture_from_png(data: bytes, rec: Dict[str, Any], png_path: str,
                             allow_etc_transcode: bool = True,
                             etc_quality: str = "fast",
                             preserve_logical_size: bool = True) -> bytes:
    parser = (rec.get("rebuild", {}).get("parser_used") or rec.get("parser_used", "")).lower()
    if is_ctpk(data) or "ctpk" in parser:
        return replace_ctpk_texture(data, rec, png_path)
    if is_stex(data) or "stex" in parser:
        return replace_single_payload(data, rec, png_path, kind="stex")
    if is_shinen_tex(data) or "shinen_tex" in parser:
        return replace_single_payload(data, rec, png_path, kind="shinen")
    if is_bflim(data) or "bflim" in parser:
        return replace_bflim(
            data, rec, png_path,
            allow_etc_transcode=allow_etc_transcode,
            etc_quality=etc_quality,
            preserve_logical_size=preserve_logical_size,
        )
    if is_ctxb(data) or "ctxb" in parser:
        return replace_chunked_texture_table(data, rec, png_path, kind="ctxb")
    if is_cmb(data) or "cmb" in parser:
        return replace_chunked_texture_table(data, rec, png_path, kind="cmb")
    if is_jimg(data) or "jimg" in parser:
        return replace_jimg(data, rec, png_path)
    raise TextureReplaceError(f"No texture writer for parser '{parser or 'unknown'}'")


def replace_ctpk_texture(data: bytes, rec: Dict[str, Any], png_path: str) -> bytes:
    textures = parse_ctpk(data)
    if not textures:
        raise TextureReplaceError("CTPK parse failed")

    rebuild = rec.get("rebuild", {})
    target_index = rebuild.get("texture_index")
    target_name = rebuild.get("texture_name", "")
    entry = None
    if target_index is not None:
        entry = next((t for t in textures if t.get("index") == target_index), None)
    if entry is None and target_name:
        entry = next((t for t in textures if t.get("name") == target_name), None)
    if entry is None:
        entry = next((t for t in textures if t.get("width") == rec.get("width") and t.get("height") == rec.get("height")), None)
    if entry is None:
        raise TextureReplaceError("Could not match CTPK texture entry")

    fmt = entry["format"]
    encoded, width, height = encode_png_to_pica(png_path, fmt)

    tex_data_offset = struct.unpack_from("<I", data, 0x08)[0]
    tex_data_size = struct.unpack_from("<I", data, 0x0C)[0]
    data_end = min(len(data), tex_data_offset + tex_data_size)
    if tex_data_offset <= 0 or tex_data_offset > len(data) or tex_data_size <= 0:
        raise TextureReplaceError("CTPK texture data region is invalid")

    entries = sorted(textures, key=lambda t: t["data_offset"])
    out_data = bytearray()
    updated_meta = {}
    for t in entries:
        blob = encoded if t.get("index") == entry.get("index") else data[t["data_offset"]:t["data_offset"] + t["data_size"]]
        rel_off = _align(len(out_data), 0x80)
        if rel_off > len(out_data):
            out_data.extend(b"\x00" * (rel_off - len(out_data)))
        updated_meta[t["index"]] = (rel_off, len(blob), width if t is entry else t["width"], height if t is entry else t["height"])
        out_data.extend(blob)

    if len(out_data) > tex_data_size:
        raise TextureReplaceError(
            "CTPK replacement grows texture data block; resized CTPK rebuild "
            "is unsupported until all following section offsets are rewritten"
        )
    if len(out_data) < tex_data_size:
        out_data.extend(b"\x00" * (tex_data_size - len(out_data)))

    new_data = bytearray(data[:tex_data_offset])
    new_data.extend(out_data)
    new_data.extend(data[data_end:])

    struct.pack_into("<I", new_data, 0x0C, tex_data_size)
    for idx, (rel_off, size, w, h) in updated_meta.items():
        entry_off = 0x20 + idx * 0x20
        if entry_off + 0x20 <= len(new_data):
            struct.pack_into("<I", new_data, entry_off + 0x04, size)
            struct.pack_into("<I", new_data, entry_off + 0x08, rel_off)
            struct.pack_into("<H", new_data, entry_off + 0x10, w)
            struct.pack_into("<H", new_data, entry_off + 0x12, h)

    return bytes(new_data)


def replace_single_payload(data: bytes, rec: Dict[str, Any], png_path: str, kind: str) -> bytes:
    if kind == "stex":
        parsed = parse_stex(data)
        if not parsed:
            raise TextureReplaceError("STEX parse failed")
        info = parsed[0]
        data_offset = info.get("data_offset", 0)
        old_size = len(info.get("data", b""))
        fmt = info["format"]
        encoded, width, height = encode_png_to_pica(png_path, fmt)
        out = bytearray(data[:data_offset])
        out.extend(encoded)
        out.extend(data[data_offset + old_size:])
        struct.pack_into("<I", out, 0x0C, width)
        struct.pack_into("<I", out, 0x10, height)
        struct.pack_into("<I", out, 0x1C, len(encoded))
        return bytes(out)

    if kind == "shinen":
        data_offset = struct.unpack_from("<I", data, 0x18)[0] if len(data) >= 0x1C else 0x80
        if data_offset == 0:
            data_offset = 0x80
        fmt = struct.unpack_from("<I", data, 0x10)[0] & 0xFF
        encoded, width, height = encode_png_to_pica(png_path, fmt)
        out = bytearray(data[:data_offset])
        out.extend(encoded)
        struct.pack_into("<H", out, 0x0C, width)
        struct.pack_into("<H", out, 0x0E, height)
        return bytes(out)

    raise TextureReplaceError(f"Unsupported single-payload container: {kind}")


def replace_bflim(data: bytes, rec: Dict[str, Any], png_path: str,
                  allow_etc_transcode: bool = True,
                  etc_quality: str = "fast",
                  preserve_logical_size: bool = True) -> bytes:
    footer_offset = len(data) - 0x28
    if footer_offset <= 0:
        raise TextureReplaceError("BFLIM footer not found")
    imag_offset = footer_offset + 0x14
    if data[imag_offset:imag_offset + 4] != b"imag":
        imag_offset = data.find(b"imag", footer_offset)
    if imag_offset < 0:
        raise TextureReplaceError("BFLIM imag section not found")

    rebuild = rec.get("rebuild", {})
    fmt = rebuild.get("format_id", 0)
    container_fmt = rebuild.get("container_format")
    png_width, png_height = _png_dimensions(png_path)
    original_width = int(rebuild.get("display_width") or rec.get("width") or 0)
    original_height = int(rebuild.get("display_height") or rec.get("height") or 0)
    if preserve_logical_size and original_width > 0 and original_height > 0:
        display_width, display_height = original_width, original_height
    else:
        display_width, display_height = png_width, png_height
    storage_width = _storage_dim(png_width)
    storage_height = _storage_dim(png_height)
    encoded, width, height = encode_png_to_pica(
        png_path, fmt,
        storage_width=storage_width,
        storage_height=storage_height,
        etc_quality=etc_quality,
    )

    footer = bytearray(data[footer_offset:])
    rel_imag = imag_offset - footer_offset
    is_le = footer[4:6] == b"\xFF\xFE"
    endian = "<" if is_le else ">"
    image_end = _align(len(encoded), 0x10)
    total_size = image_end + len(footer)
    struct.pack_into(endian + "I", footer, 0x0C, total_size)
    struct.pack_into(endian + "H", footer, rel_imag + 0x08, display_width)
    struct.pack_into(endian + "H", footer, rel_imag + 0x0A, display_height)
    if data[footer_offset:footer_offset + 4] == b"CLIM":
        if container_fmt is not None:
            struct.pack_into(endian + "I", footer, rel_imag + 0x0C, int(container_fmt))
        struct.pack_into(endian + "I", footer, rel_imag + 0x10, len(encoded))
    else:
        # BFLIM stores format as u8 at imag+0x0E.
        if container_fmt is not None:
            footer[rel_imag + 0x0E] = int(container_fmt) & 0xFF
        if rel_imag + 0x14 <= len(footer):
            struct.pack_into(endian + "I", footer, rel_imag + 0x10, len(encoded))

    padding = b"\x00" * (image_end - len(encoded))
    return encoded + padding + bytes(footer)


def replace_chunked_texture_table(data: bytes, rec: Dict[str, Any], png_path: str,
                                  kind: str) -> bytes:
    textures = parse_ctxb(data) if kind == "ctxb" else extract_cmb_textures(data)
    if not textures:
        raise TextureReplaceError(f"{kind.upper()} parse failed")

    entry = _match_texture_entry(textures, rec)
    if entry is None:
        raise TextureReplaceError(f"Could not match {kind.upper()} texture entry")

    fmt = entry["format"]
    encoded, width, height = encode_png_to_pica(png_path, fmt)

    entries = sorted(textures, key=lambda t: t["data_offset"])
    region_start = min(t["data_offset"] for t in entries)
    region_end = max(t["data_offset"] + t["data_size"] for t in entries)
    out_data = bytearray()
    meta = {}
    for t in entries:
        blob = encoded if t.get("index") == entry.get("index") else data[t["data_offset"]:t["data_offset"] + t["data_size"]]
        rel = _align(len(out_data), 0x80)
        if rel > len(out_data):
            out_data.extend(b"\x00" * (rel - len(out_data)))
        meta[t["index"]] = (rel, len(blob), width if t is entry else t["width"], height if t is entry else t["height"])
        out_data.extend(blob)

    out = bytearray(data[:region_start])
    out.extend(out_data)
    out.extend(data[region_end:])

    if kind == "ctxb":
        tex_chunk_offset = struct.unpack_from("<I", out, 0x10)[0]
        entry_base = tex_chunk_offset + 0x0C
        texture_data_offset = region_start
        struct.pack_into("<I", out, 0x04, len(out))
        struct.pack_into("<I", out, 0x14, texture_data_offset)
    else:
        cmb_idx = out.find(b"cmb ")
        tex_idx = out.find(b"tex ", cmb_idx)
        entry_base = tex_idx + 0x0C

    for idx, (rel, size, w, h) in meta.items():
        entry_off = entry_base + idx * 0x24
        if entry_off + 0x24 <= len(out):
            struct.pack_into("<I", out, entry_off + 0x00, size)
            struct.pack_into("<H", out, entry_off + 0x08, w)
            struct.pack_into("<H", out, entry_off + 0x0A, h)
            struct.pack_into("<I", out, entry_off + 0x10, rel)

    return bytes(out)


def replace_jimg(data: bytes, rec: Dict[str, Any], png_path: str) -> bytes:
    if not is_jimg(data):
        raise TextureReplaceError("jIMG parse failed")
    fmt = rec.get("rebuild", {}).get("format_id", 0)
    encoded, width, height = encode_png_to_pica(png_path, fmt)
    out = bytearray(data[:0x80])
    out.extend(encoded)
    struct.pack_into("<I", out, 0x04, len(out))
    struct.pack_into("<H", out, 0x08, width)
    struct.pack_into("<H", out, 0x0A, height)
    return bytes(out)


def _match_texture_entry(textures: List[Dict[str, Any]], rec: Dict[str, Any]):
    rebuild = rec.get("rebuild", {})
    target_index = rebuild.get("texture_index")
    target_name = rebuild.get("texture_name", "")
    if target_index is not None:
        match = next((t for t in textures if t.get("index") == target_index), None)
        if match is not None:
            return match
    if target_name:
        match = next((t for t in textures if t.get("name") == target_name), None)
        if match is not None:
            return match
    return next((t for t in textures if t.get("width") == rec.get("width") and t.get("height") == rec.get("height")), None)


def _storage_dim(value: int) -> int:
    dim = 8
    while dim < value:
        dim <<= 1
    return dim


def _png_dimensions(path: str):
    from PIL import Image

    with Image.open(path) as img:
        return img.size


def _align(value: int, alignment: int) -> int:
    return (value + alignment - 1) // alignment * alignment
