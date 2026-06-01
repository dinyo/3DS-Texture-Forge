"""
Output layer: strict manifest schema, PNG saving, failures/unknown tracking.

Manifest schema per texture:
  id, source_rom, source_container_chain, source_file_path, source_texture_path,
  source_offset, rebuild, detected_format, width, height, mip_count,
  raw_data_size, decoded_png_path, confidence, parser_used, notes, sha1_rgba,
  sha1_source_blob, failed_reason
"""

import os
import json
import hashlib
import logging
import numpy as np
from PIL import Image
from typing import List, Dict, Any, Optional, Tuple
from textures.decoder import get_format_name
from repack_capabilities import (
    ARCHIVE_WRITERS, TEXTURE_WRITERS, build_capability_summary,
)

logger = logging.getLogger(__name__)

AMBIGUOUS_ARCHIVE_WRITERS = {"darc_or_sarc_or_custom_arc"}


def sha1_bytes(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def sha1_rgba(rgba: np.ndarray) -> str:
    return hashlib.sha1(rgba.tobytes()).hexdigest()


def make_alpha_visible(rgba_data: np.ndarray, pica_format: int = -1) -> np.ndarray:
    """For alpha-only or white-luminance+alpha textures, make the alpha channel visible.

    When RGB is constant (all white or all one color) but alpha varies, the texture
    carries its real data in the alpha channel. This converts it so the alpha values
    become visible grayscale in RGB, making textures like the Mario "M" logo or
    shadow maps actually visible in image viewers.

    Only activates when RGB is truly constant — normal RGBA textures are unaffected.
    """
    if rgba_data.ndim != 3 or rgba_data.shape[2] != 4:
        return rgba_data

    alpha = rgba_data[:, :, 3]
    rgb = rgba_data[:, :, :3]

    # Check if RGB is constant (std < 5) but alpha has meaningful variation
    rgb_std = float(np.std(rgb.astype(np.float32)))
    alpha_std = float(np.std(alpha.astype(np.float32)))

    if rgb_std < 5.0 and alpha_std > 10.0:
        # Alpha IS the texture — make it visible as grayscale with alpha preserved
        gray = alpha
        return np.stack([gray, gray, gray, alpha], axis=2)

    return rgba_data


def save_texture_as_png(rgba_data: np.ndarray, output_path: str,
                        pica_format: int = -1) -> bool:
    try:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        # Make alpha/luminance textures visible
        rgba_data = make_alpha_visible(rgba_data, pica_format)
        if rgba_data.shape[2] == 4 and np.all(rgba_data[:, :, 3] == 255):
            img = Image.fromarray(rgba_data[:, :, :3], "RGB")
        else:
            img = Image.fromarray(rgba_data, "RGBA")
        img.save(output_path, "PNG")
        return True
    except Exception as e:
        logger.warning(f"Failed to save PNG {output_path}: {e}")
        return False


def generate_output_filename(index: int, tex_info: Dict[str, Any],
                              source_path: str = "") -> str:
    fmt_name = get_format_name(tex_info.get("format", 0))
    width = tex_info.get("width", 0)
    height = tex_info.get("height", 0)
    name = tex_info.get("name", "")
    if name:
        name = name.replace("\\", "_").replace("/", "_").replace(" ", "_")
        name = "".join(c for c in name if c.isalnum() or c in ("_", "-", "."))
        return f"tex_{index:04d}_{name}_{fmt_name}_{width}x{height}.png"
    return f"tex_{index:04d}_{fmt_name}_{width}x{height}.png"


def build_output_path(output_dir: str, source_path: str, filename: str) -> str:
    if source_path:
        clean = source_path.lstrip("/").replace("\\", "/")
        parts = clean.split("/")
        dir_part = "/".join(parts[:-1]) if len(parts) > 1 else ""
    else:
        dir_part = ""
    if dir_part:
        return os.path.join(output_dir, "textures", dir_part, filename)
    return os.path.join(output_dir, "textures", filename)


def save_raw_data(data: bytes, output_path: str) -> bool:
    try:
        bin_path = output_path.rsplit(".", 1)[0] + ".bin"
        os.makedirs(os.path.dirname(bin_path), exist_ok=True)
        with open(bin_path, "wb") as f:
            f.write(data)
        return True
    except Exception as e:
        logger.warning(f"Failed to save raw data: {e}")
        return False


# --- Strict manifest schema ---

def make_texture_record(
    tex_id: str,
    source_rom: str,
    source_container_chain: str,
    source_file_path: str,
    source_offset: int,
    detected_format: str,
    width: int,
    height: int,
    mip_count: int,
    raw_data_size: int,
    decoded_png_path: str,
    confidence: str,
    parser_used: str,
    notes: str,
    sha1_rgba_val: str,
    sha1_source_val: str,
    quality_metrics: Optional[Dict] = None,
    failed_reason: str = "",
) -> Dict[str, Any]:
    rec = {
        "id": tex_id,
        "source_rom": source_rom,
        "source_container_chain": source_container_chain,
        "source_file_path": source_file_path,
        "source_offset": source_offset,
        "detected_format": detected_format,
        "width": width,
        "height": height,
        "mip_count": mip_count,
        "raw_data_size": raw_data_size,
        "decoded_png_path": decoded_png_path,
        "confidence": confidence,
        "parser_used": parser_used,
        "notes": notes,
        "sha1_rgba": sha1_rgba_val,
        "sha1_source_blob": sha1_source_val,
        "failed_reason": failed_reason,
    }
    if quality_metrics:
        rec["quality"] = quality_metrics
    return rec


def write_manifest(output_dir: str, records: List[Dict[str, Any]],
                   rom_file: str, title_id: str, game_title: str):
    source = {
        "source_input": os.path.abspath(rom_file),
        "source_type": "romfs_folder" if os.path.isdir(rom_file) else "rom_container",
    }
    if os.path.isdir(rom_file):
        source["source_romfs_folder"] = os.path.abspath(rom_file)

    parser_used = sorted({
        r.get("parser_used", "unknown")
        for r in records
        if r.get("parser_used")
    })
    extracted_archive = _build_extracted_archive_types(records)
    compatibility = _build_rebuild_compatibility(records)
    risk_summary = _build_simple_replace_risk_summary(records)

    manifest = {
        "schema_version": 4,
        "game_title": game_title,
        "title_id": title_id,
        "rom_file": os.path.basename(rom_file),
        "source": source,
        "extracted_archive": extracted_archive,
        "parser_used": parser_used,
        "rebuild_compatibility": compatibility,
        "simple_replace_risk_summary": risk_summary,
        "repack_capabilities": build_capability_summary(),
        "texture_count": len(records),
        "textures": records,
    }
    path = os.path.join(output_dir, "manifest.json")
    os.makedirs(output_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    logger.info(f"Wrote manifest.json ({len(records)} textures)")


def _build_rebuild_compatibility(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    counts = {
        "total_records": len(records),
        "archive_member_records": 0,
        "direct_texture_records": 0,
        "archive_writer_supported": 0,
        "archive_writer_unknown_until_build": 0,
        "archive_writer_unsupported": 0,
        "texture_writer_supported": 0,
        "texture_writer_unsupported": 0,
        "fully_supported": 0,
        "unknown_until_build": 0,
        "unsupported": 0,
    }
    unsupported_reasons: Dict[str, int] = {}
    archive_parent_types: Dict[str, int] = {}

    for rec in records:
        rebuild = rec.get("rebuild", {})
        parser = str(rebuild.get("parser_used") or rec.get("parser_used", "unknown"))
        base_parser = parser.split("/", 1)[0].split("@", 1)[0]
        parent = rebuild.get("parent") or rec.get("source_file_path", "")
        is_archive_member = bool(rebuild.get("is_archive_member"))

        texture_ok = base_parser in TEXTURE_WRITERS
        archive_ok = True
        if is_archive_member:
            counts["archive_member_records"] += 1
            archive_parser = _infer_archive_parser(parent)
            archive_unknown = archive_parser in AMBIGUOUS_ARCHIVE_WRITERS
            archive_ok = archive_parser in ARCHIVE_WRITERS
            archive_parent_types[archive_parser] = archive_parent_types.get(archive_parser, 0) + 1
        else:
            counts["direct_texture_records"] += 1

        counts["texture_writer_supported" if texture_ok else "texture_writer_unsupported"] += 1
        if is_archive_member:
            if archive_parser in AMBIGUOUS_ARCHIVE_WRITERS:
                counts["archive_writer_unknown_until_build"] += 1
            else:
                counts["archive_writer_supported" if archive_ok else "archive_writer_unsupported"] += 1

        if texture_ok and archive_ok:
            counts["fully_supported"] += 1
        elif texture_ok and is_archive_member and archive_parser in AMBIGUOUS_ARCHIVE_WRITERS:
            counts["unknown_until_build"] += 1
        else:
            counts["unsupported"] += 1
            reason_parts = []
            if not texture_ok:
                reason_parts.append(f"texture_writer_missing:{base_parser}")
            if not archive_ok:
                reason_parts.append(f"archive_writer_missing:{_infer_archive_parser(parent)}")
            reason = ",".join(reason_parts) or "unknown"
            unsupported_reasons[reason] = unsupported_reasons.get(reason, 0) + 1

    return {
        "counts": counts,
        "archive_parent_types": archive_parent_types,
        "unsupported_reasons": unsupported_reasons,
        "supported_archive_writers": sorted(ARCHIVE_WRITERS),
        "supported_texture_writers": sorted(TEXTURE_WRITERS),
    }


def _build_simple_replace_risk_summary(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    summary = {
        "archive_member_records": 0,
        "safe_records": 0,
        "risky_records": 0,
        "layout_constraint_status": {},
        "risk_flags": {},
        "companion_extensions": {},
    }
    for rec in records:
        rebuild = rec.get("rebuild", {})
        if not rebuild.get("is_archive_member"):
            continue
        summary["archive_member_records"] += 1
        context = rebuild.get("archive_context", {})
        flags = context.get("risk_flags", []) if isinstance(context, dict) else []
        if flags:
            summary["risky_records"] += 1
        else:
            summary["safe_records"] += 1
        for flag in flags:
            summary["risk_flags"][flag] = summary["risk_flags"].get(flag, 0) + 1
        status = rebuild.get("layout_constraint_status", "")
        if status:
            summary["layout_constraint_status"][status] = summary["layout_constraint_status"].get(status, 0) + 1
        counts = context.get("companion_counts", {}) if isinstance(context, dict) else {}
        for ext, count in counts.items():
            if count:
                summary["companion_extensions"][ext] = summary["companion_extensions"].get(ext, 0) + 1
    summary["risk_flags"] = dict(sorted(summary["risk_flags"].items()))
    summary["layout_constraint_status"] = dict(sorted(summary["layout_constraint_status"].items()))
    summary["companion_extensions"] = dict(sorted(summary["companion_extensions"].items()))
    return summary


def _build_extracted_archive_types(records: List[Dict[str, Any]]) -> List[str]:
    types = set()
    for rec in records:
        rebuild = rec.get("rebuild", {})
        parent = rebuild.get("parent") or rec.get("source_file_path", "")
        inner = rebuild.get("inner_path", "")
        for path in (parent, inner):
            ext = _extension_name(path)
            if ext:
                types.add(ext)
    return sorted(types)


def _extension_name(path: str) -> str:
    clean = (path or "").replace("\\", "/").rsplit("/", 1)[-1]
    if "." not in clean:
        return ""
    return clean.rsplit(".", 1)[-1].lower()


def _infer_archive_parser(path: str) -> str:
    lower = (path or "").lower()
    if lower.endswith(".sarc") or lower.endswith(".szs"):
        return "sarc"
    if lower.endswith(".narc"):
        return "narc"
    if lower.endswith(".garc"):
        return "garc"
    if lower.endswith(".zar"):
        return "zar"
    if lower.endswith(".darc"):
        return "darc"
    if lower.endswith(".arc"):
        # .arc is ambiguous. DARC is common for 3DS layout archives, and the
        # build step verifies by magic bytes before writing.
        return "darc_or_sarc_or_custom_arc"
    return "unknown"


def write_failures(output_dir: str, failures: List[Dict[str, Any]]):
    path = os.path.join(output_dir, "failures.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"count": len(failures), "failures": failures}, f, indent=2)
    logger.info(f"Wrote failures.json ({len(failures)} entries)")


def write_unknown_files(output_dir: str, unknowns: List[Dict[str, Any]]):
    path = os.path.join(output_dir, "unknown_files.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"count": len(unknowns), "files": unknowns}, f, indent=2)
    logger.info(f"Wrote unknown_files.json ({len(unknowns)} entries)")


def write_summary(output_dir: str, summary: Dict[str, Any]):
    path = os.path.join(output_dir, "summary.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    logger.info(f"Wrote summary.json")


def compute_dedup_stats(output_dir: str) -> Tuple[int, int, int]:
    """
    Hash all PNGs in the textures/ subdir by file content.
    Returns (total, unique, duplicates).
    """
    hashes: Dict[str, int] = {}
    tex_dir = os.path.join(output_dir, "textures")
    if not os.path.isdir(tex_dir):
        return 0, 0, 0
    for root, dirs, files in os.walk(tex_dir):
        for f in files:
            if f.endswith(".png"):
                path = os.path.join(root, f)
                try:
                    with open(path, 'rb') as fh:
                        h = hashlib.md5(fh.read()).hexdigest()
                    hashes[h] = hashes.get(h, 0) + 1
                except OSError:
                    pass
    total = sum(hashes.values())
    unique = len(hashes)
    return total, unique, total - unique


# Legacy compat
def generate_manifest(output_dir, rom_file, title_id, game_title, textures):
    records = []
    for tex in textures:
        records.append({
            "id": f"tex_{tex.get('index', 0):04d}",
            "source_file_path": tex.get("source_file", ""),
            "detected_format": get_format_name(tex.get("format", 0)),
            "width": tex.get("width", 0),
            "height": tex.get("height", 0),
            "decoded_png_path": tex.get("output_file", ""),
        })
    write_manifest(output_dir, records, rom_file, title_id, game_title)
