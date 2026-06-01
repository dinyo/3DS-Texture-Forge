"""Build a mod RomFS folder from an extraction manifest.

This module is intentionally conservative: it never mutates the source RomFS
folder and it reports unsupported reinjection targets instead of writing
partially rebuilt archives.
"""

import json
import os
import shutil
import fnmatch
import time
import hashlib
from functools import lru_cache
from typing import Any, Dict, List, Optional

from archive_writers import ArchiveWriteError, replace_archive_members
from repack_capabilities import TEXTURE_WRITERS, build_capability_summary

def _is_dir_empty(path: str) -> bool:
    return not os.path.exists(path) or (
        os.path.isdir(path) and not any(os.scandir(path))
    )


def _manifest_project_dir(manifest_path: str) -> str:
    return os.path.dirname(os.path.abspath(manifest_path))


def _texture_png_path(project_dir: str, rec: Dict[str, Any]) -> str:
    rel = rec.get("decoded_png_path", "")
    if not rel:
        return ""
    return rel if os.path.isabs(rel) else os.path.join(project_dir, rel)


def _same_or_nested_path(a: str, b: str) -> bool:
    a_real = os.path.realpath(os.path.abspath(a))
    b_real = os.path.realpath(os.path.abspath(b))
    try:
        return os.path.commonpath([a_real, b_real]) in (a_real, b_real)
    except ValueError:
        return False


@lru_cache(maxsize=65536)
def _png_info(path: str) -> Optional[Dict[str, Any]]:
    if not path or not os.path.isfile(path):
        return None
    from PIL import Image

    with Image.open(path) as img:
        return {
            "width": img.width,
            "height": img.height,
            "mode": img.mode,
        }


def _png_rgba_sha1(path: str) -> str:
    from PIL import Image

    with Image.open(path) as img:
        return hashlib.sha1(img.convert("RGBA").tobytes()).hexdigest()


def _png_is_unchanged(manifest_path: str, png_path: str, rec: Dict[str, Any]) -> bool:
    """Return True for extraction PNGs that do not need reinjection."""
    original_hashes = {
        h for h in (
            rec.get("sha1_png_rgba"),
            rec.get("sha1_rgba"),
        )
        if h
    }
    if not original_hashes:
        try:
            return os.path.getmtime(png_path) <= os.path.getmtime(manifest_path)
        except OSError:
            return False
    try:
        return _png_rgba_sha1(png_path) in original_hashes
    except Exception:
        return False


def build_romfs_from_manifest(
    manifest_path: str,
    output_folder: str,
    source_romfs_folder: str = "",
    overwrite: bool = False,
    progress_callback=None,
    only_archive_exts=None,
    only_texture_exts=None,
    only_supported_writers: bool = False,
    full_copy: bool = False,
    allow_etc_transcode: bool = True,
    include_parent_patterns=None,
    exclude_parent_patterns=None,
    max_replacement_width: int = 0,
    max_replacement_height: int = 0,
    max_scale: float = 0.0,
    max_texture_bytes: int = 0,
    etc_quality: str = "fast",
    skip_unsafe_simple_replace: bool = False,
    skip_no_canvas_constraint: bool = False,
    preserve_logical_size: bool = True,
    layout_aware_repack: bool = True,
    rebuild_all: bool = False,
) -> Dict[str, Any]:
    t_start = time.perf_counter()
    manifest_path = os.path.abspath(manifest_path)
    output_folder = os.path.abspath(output_folder)
    romfs_output_folder = os.path.join(output_folder, "romfs")
    project_dir = _manifest_project_dir(manifest_path)

    with open(manifest_path, "r", encoding="utf-8-sig") as f:
        manifest = json.load(f)

    source = manifest.get("source") or manifest.get("parent", {})
    source_romfs = source_romfs_folder or source.get("source_romfs_folder", "")
    if not source_romfs:
        raise RuntimeError(
            "Manifest does not include source.source_romfs_folder. "
            "Re-extract from a RomFS folder or pass --source-romfs."
        )
    source_romfs = os.path.abspath(source_romfs)
    if not os.path.isdir(source_romfs):
        raise RuntimeError(f"Source RomFS folder not found: {source_romfs}")
    if _same_or_nested_path(output_folder, source_romfs):
        raise RuntimeError(
            "Output folder must not overlap the source RomFS folder. "
            "Choose a separate build folder so the source RomFS is never modified."
        )
    if _same_or_nested_path(output_folder, project_dir):
        raise RuntimeError(
            "Output folder must not overlap the extraction project folder. "
            "Choose a separate build folder so manifest files and replacement PNGs "
            "are never deleted by --overwrite."
        )

    if os.path.exists(output_folder) and not _is_dir_empty(output_folder) and not overwrite:
        raise RuntimeError(
            f"Output folder already exists and is not empty: {output_folder}"
        )

    textures: List[Dict[str, Any]] = manifest.get("textures", [])
    only_archive_exts = _normalize_ext_filter(only_archive_exts)
    only_texture_exts = _normalize_ext_filter(only_texture_exts)
    if only_archive_exts:
        textures = [
            rec for rec in textures
            if _record_archive_ext(rec) in only_archive_exts
        ]
    if only_texture_exts:
        textures = [
            rec for rec in textures
            if _record_texture_ext(rec) in only_texture_exts
        ]
    if only_supported_writers:
        textures = [
            rec for rec in textures
            if _record_parser_base(rec) in TEXTURE_WRITERS
        ]
    textures, skipped_by_filter = _apply_build_filters(
        textures,
        project_dir,
        include_parent_patterns=include_parent_patterns,
        exclude_parent_patterns=exclude_parent_patterns,
        max_replacement_width=max_replacement_width,
        max_replacement_height=max_replacement_height,
        max_scale=max_scale,
        max_texture_bytes=max_texture_bytes,
        allow_etc_transcode=allow_etc_transcode,
        skip_unsafe_simple_replace=skip_unsafe_simple_replace,
        skip_no_canvas_constraint=skip_no_canvas_constraint,
    )

    t_after_filter = time.perf_counter()
    report: Dict[str, Any] = {
        "manifest": manifest_path,
        "source_romfs_folder": source_romfs,
        "output_folder": output_folder,
        "romfs_folder": romfs_output_folder,
        "copy_mode": "full_romfs" if full_copy else "manifest_files",
        "copied_source_romfs": full_copy,
        "copied_files_count": 0,
        "repack_capabilities": build_capability_summary(),
        "texture_records": len(textures),
        "filters": {
            "only_archive": sorted(only_archive_exts),
            "only_texture": sorted(only_texture_exts),
            "only_supported_writers": only_supported_writers,
            "full_copy": full_copy,
            "allow_etc_transcode": allow_etc_transcode,
            "include_parent": _normalize_pattern_filter(include_parent_patterns),
            "exclude_parent": _normalize_pattern_filter(exclude_parent_patterns),
            "max_replacement_width": max_replacement_width,
            "max_replacement_height": max_replacement_height,
            "max_scale": max_scale,
            "max_texture_bytes": max_texture_bytes,
            "etc_quality": etc_quality,
            "skip_unsafe_simple_replace": skip_unsafe_simple_replace,
            "skip_no_canvas_constraint": skip_no_canvas_constraint,
            "preserve_logical_size": preserve_logical_size,
            "layout_aware_repack": layout_aware_repack,
            "rebuild_all": rebuild_all,
        },
        "planned_replacements": 0,
        "applied_replacements": 0,
        "failed_replacements": 0,
        "missing_replacements_count": 0,
        "unsupported_replacements_count": 0,
        "unsupported_replacements": [],
        "applied": [],
        "missing_replacements": [],
        "skipped_by_filter_count": len(skipped_by_filter),
        "skipped_by_filter": skipped_by_filter,
        "unchanged_replacements_count": 0,
        "unchanged_replacements": [],
        "copy_planning": {
            "source_copy_after_png_check": not full_copy,
            "parents_with_existing_replacements": 0,
        },
        "timings_seconds": {
            "manifest_filter": round(t_after_filter - t_start, 3),
            "replacement_planning": 0.0,
            "source_copy": 0.0,
            "rebuild": 0.0,
            "size_summary": 0.0,
            "total": 0.0,
        },
        "notes": [
            "Supported writers: PNG to PICA formats including ETC1/ETC1A4; "
            "CTPK, STEX, Shin'en TEX, BFLIM/BCLIM texture containers; SARC, "
            "NARC, ZAR, GARC, and darc archive rebuilds.",
            "Layout-aware repack is enabled by default: archive BCLIMs with "
            "readable BCLYT pane constraints are written with replacement "
            "physical dimensions while the BCLYT canvas remains the logical "
            "on-screen size."
        ],
    }

    work_items = []
    for idx, rec in enumerate(textures, 1):
        rebuild = rec.get("rebuild", {})
        current_target = rebuild.get("parent") or rec.get("source_file_path", "")
        if progress_callback:
            progress_callback(idx, len(textures), rec, "checking", current_target)

        png_path = _texture_png_path(project_dir, rec)
        png = _png_info(png_path)
        if not png:
            report["missing_replacements"].append({
                "id": rec.get("id", ""),
                "decoded_png_path": rec.get("decoded_png_path", ""),
                "reason": "replacement PNG not found",
            })
            report["missing_replacements_count"] += 1
            report["failed_replacements"] += 1
            if progress_callback:
                progress_callback(idx, len(textures), rec, "missing", current_target)
            continue
        if not rebuild_all and _png_is_unchanged(manifest_path, png_path, rec):
            report["unchanged_replacements_count"] += 1
            report["unchanged_replacements"].append({
                "id": rec.get("id", ""),
                "decoded_png_path": rec.get("decoded_png_path", ""),
                "parent": current_target,
                "reason": "PNG appears unchanged since extraction",
            })
            if progress_callback:
                progress_callback(idx, len(textures), rec, "unchanged", current_target)
            continue
        report["planned_replacements"] += 1
        work_items.append({
            "index": idx,
            "rec": rec,
            "png_path": png_path,
            "png": png,
            "target": _build_target(rec, png_path, png),
        })

    t_after_plan = time.perf_counter()
    report["timings_seconds"]["replacement_planning"] = round(t_after_plan - t_after_filter, 3)
    report["copy_planning"]["parents_with_existing_replacements"] = len({
        _record_parent_path(item["rec"]).lstrip("/\\").replace("\\", "/")
        for item in work_items
        if _record_parent_path(item["rec"]).lstrip("/\\")
    })

    if os.path.exists(output_folder):
        shutil.rmtree(output_folder)
    os.makedirs(romfs_output_folder, exist_ok=True)
    copied_files = _copy_romfs_inputs(
        source_romfs,
        romfs_output_folder,
        textures,
        full_copy,
        work_items=work_items,
    )
    t_after_copy = time.perf_counter()
    report["timings_seconds"]["source_copy"] = round(t_after_copy - t_after_plan, 3)
    report["copied_files_count"] = copied_files

    by_parent: Dict[str, List[Dict[str, Any]]] = {}
    for item in work_items:
        parent_rel = _record_parent_path(item["rec"]).lstrip("/\\")
        by_parent.setdefault(parent_rel.replace("\\", "/"), []).append(item)

    for parent_rel, items in sorted(by_parent.items()):
        if not parent_rel:
            for item in items:
                _record_failed(report, item["target"], "missing parent target path")
            continue
        parent_out = os.path.join(romfs_output_folder, parent_rel.replace("/", os.sep))
        if not os.path.isfile(parent_out):
            for item in items:
                _record_failed(report, item["target"], f"parent file not found in copied RomFS: {parent_rel}")
            continue

        archive_items = [item for item in items if item["rec"].get("rebuild", {}).get("is_archive_member")]
        archive_item_ids = {id(item) for item in archive_items}
        direct_items = [item for item in items if id(item) not in archive_item_ids]

        if archive_items:
            if progress_callback:
                progress_callback(archive_items[0]["index"], len(textures), archive_items[0]["rec"], "archive batch", parent_rel)
            _apply_archive_batch(
                parent_out, archive_items, report,
                allow_etc_transcode=allow_etc_transcode,
                etc_quality=etc_quality,
                preserve_logical_size=preserve_logical_size,
                layout_aware_repack=layout_aware_repack,
                progress_callback=progress_callback,
                total=len(textures),
            )

        for item in direct_items:
            if progress_callback:
                progress_callback(item["index"], len(textures), item["rec"], "processing", parent_rel)
            _apply_direct_item(
                parent_out, item, report,
                allow_etc_transcode=allow_etc_transcode,
                etc_quality=etc_quality,
                preserve_logical_size=preserve_logical_size,
                layout_aware_repack=layout_aware_repack,
                progress_callback=progress_callback,
                total=len(textures),
            )

    t_after_rebuild = time.perf_counter()
    report["timings_seconds"]["rebuild"] = round(t_after_rebuild - t_after_copy, 3)
    report["success"] = report["failed_replacements"] == 0
    report["summary"] = {
        "texture_records": report["texture_records"],
        "replacement_pngs_found": report["planned_replacements"],
        "applied": report["applied_replacements"],
        "failed": report["failed_replacements"],
        "missing": report["missing_replacements_count"],
        "unsupported": report["unsupported_replacements_count"],
        "skipped_by_filter": report["skipped_by_filter_count"],
        "unchanged": report["unchanged_replacements_count"],
    }
    report["size_summary"] = _build_size_summary(source_romfs, romfs_output_folder)
    t_after_size = time.perf_counter()
    report["timings_seconds"]["size_summary"] = round(t_after_size - t_after_rebuild, 3)
    report["timings_seconds"]["total"] = round(t_after_size - t_start, 3)

    report_path = os.path.join(output_folder, "report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    report["report_path"] = report_path
    return report


def _build_target(rec: Dict[str, Any], png_path: str, png: Dict[str, Any]) -> Dict[str, Any]:
    rebuild = rec.get("rebuild", {})
    return {
        "id": rec.get("id", ""),
        "png": png_path,
        "source_file_path": rec.get("source_file_path", ""),
        "source_texture_path": rec.get("source_texture_path", rec.get("source_file_path", "")),
        "parent": rebuild.get("parent", rec.get("source_file_path", "")),
        "inner_path": rebuild.get("inner_path", ""),
        "parser_used": rebuild.get("parser_used", rec.get("parser_used", "")),
        "original_width": rec.get("width", 0),
        "original_height": rec.get("height", 0),
        "replacement_width": png["width"],
        "replacement_height": png["height"],
    }


def _apply_archive_batch(parent_out: str, items: List[Dict[str, Any]], report: Dict[str, Any],
                         allow_etc_transcode: bool, etc_quality: str,
                         preserve_logical_size: bool,
                         layout_aware_repack: bool,
                         progress_callback=None, total: int = 0) -> None:
    from texture_replacer import replace_texture_from_png

    texture_transforms = []
    for item in items:
        rec = item["rec"]
        inner_path = rec.get("rebuild", {}).get("inner_path", "")
        if not inner_path:
            _record_failed(report, item["target"], "missing archive member path")
            continue

        item_preserve_logical = _preserve_logical_for_item(
            rec,
            preserve_logical_size=preserve_logical_size,
            layout_aware_repack=layout_aware_repack,
        )
        item["target"]["logical_size_mode"] = (
            _logical_size_mode_for_item(rec, item_preserve_logical)
        )

        def _make_transform(_rec=rec, _png_path=item["png_path"], _preserve=item_preserve_logical):
            def _transform(member_data: bytes) -> bytes:
                return replace_texture_from_png(
                    member_data, _rec, _png_path,
                    allow_etc_transcode=allow_etc_transcode,
                    etc_quality=etc_quality,
                    preserve_logical_size=_preserve,
                )
            return _transform

        texture_transforms.append((inner_path, _make_transform(), item))

    if not texture_transforms:
        return

    try:
        with open(parent_out, "rb") as f:
            archive_data = f.read()
        transforms = [(inner_path, transform) for inner_path, transform, _item in texture_transforms]
        new_archive = replace_archive_members(archive_data, transforms)
        with open(parent_out, "wb") as f:
            f.write(new_archive)

        for _inner_path, _transform, item in texture_transforms:
            target = item["target"]
            target["status"] = "applied_archive_member"
            report["applied_replacements"] += 1
            report["applied"].append(target)
            if progress_callback:
                progress_callback(item["index"], total, item["rec"], target["status"], target.get("parent", ""))
    except (ArchiveWriteError, RuntimeError, OSError, Exception) as exc:
        for item in items:
            _record_failed(report, item["target"], str(exc))
            if progress_callback:
                progress_callback(item["index"], total, item["rec"], "failed", item["target"].get("parent", ""))


def _apply_direct_item(parent_out: str, item: Dict[str, Any], report: Dict[str, Any],
                       allow_etc_transcode: bool, etc_quality: str,
                       preserve_logical_size: bool,
                       layout_aware_repack: bool,
                       progress_callback=None, total: int = 0) -> None:
    from texture_replacer import replace_texture_from_png

    target = item["target"]
    item_preserve_logical = _preserve_logical_for_item(
        item["rec"],
        preserve_logical_size=preserve_logical_size,
        layout_aware_repack=layout_aware_repack,
    )
    target["logical_size_mode"] = (
        _logical_size_mode_for_item(item["rec"], item_preserve_logical)
    )
    try:
        with open(parent_out, "rb") as f:
            file_data = f.read()
        new_file = replace_texture_from_png(
            file_data, item["rec"], item["png_path"],
            allow_etc_transcode=allow_etc_transcode,
            etc_quality=etc_quality,
            preserve_logical_size=item_preserve_logical,
        )
        with open(parent_out, "wb") as f:
            f.write(new_file)
        target["status"] = "applied_direct"
        report["applied_replacements"] += 1
        report["applied"].append(target)
        if progress_callback:
            progress_callback(item["index"], total, item["rec"], target["status"], target.get("parent", ""))
    except (RuntimeError, OSError, Exception) as exc:
        _record_failed(report, target, str(exc))
        if progress_callback:
            progress_callback(item["index"], total, item["rec"], "failed", target.get("parent", ""))


def _record_failed(report: Dict[str, Any], target: Dict[str, Any], reason: str) -> None:
    failed = dict(target)
    failed["reason"] = reason
    report["unsupported_replacements"].append(failed)
    report["unsupported_replacements_count"] += 1
    report["failed_replacements"] += 1


def _preserve_logical_for_item(rec: Dict[str, Any], preserve_logical_size: bool,
                               layout_aware_repack: bool) -> bool:
    rebuild = rec.get("rebuild", {})
    parser = str(rebuild.get("parser_used") or rec.get("parser_used", "")).lower()
    if "bflim" not in parser:
        return True
    if not preserve_logical_size:
        return False
    if not layout_aware_repack:
        return False
    context = rebuild.get("archive_context", {})
    if not isinstance(context, dict):
        return False
    if not (context.get("has_bclyt") and rebuild.get("layout_constraints")):
        return False
    # In a readable BCLYT layout, pane size is the logical canvas. For HD BCLIM
    # payloads, write the BCLIM's physical dimensions so the game allocates the
    # correct texture, while the layout keeps the on-screen size unchanged.
    return False


def _logical_size_mode_for_item(rec: Dict[str, Any], preserve_logical: bool) -> str:
    if preserve_logical:
        return "preserve_bclim_logical_size"
    rebuild = rec.get("rebuild", {})
    context = rebuild.get("archive_context", {})
    if (
        isinstance(context, dict)
        and context.get("has_bclyt")
        and rebuild.get("layout_constraints")
    ):
        return "layout_canvas_physical_bclim"
    return "replacement_png_dimensions"


def _normalize_ext_filter(values) -> set:
    result = set()
    for value in values or []:
        for part in str(value).split(","):
            part = part.strip().lower().lstrip(".")
            if part:
                result.add(part)
    return result


def _normalize_pattern_filter(values) -> List[str]:
    result = []
    for value in values or []:
        for part in str(value).split(","):
            part = part.strip().replace("\\", "/")
            if part:
                result.append(part)
    return result


def _apply_build_filters(
    textures: List[Dict[str, Any]],
    project_dir: str,
    include_parent_patterns=None,
    exclude_parent_patterns=None,
    max_replacement_width: int = 0,
    max_replacement_height: int = 0,
    max_scale: float = 0.0,
    max_texture_bytes: int = 0,
    allow_etc_transcode: bool = True,
    skip_unsafe_simple_replace: bool = False,
    skip_no_canvas_constraint: bool = False,
) -> tuple:
    include_parent = _normalize_pattern_filter(include_parent_patterns)
    exclude_parent = _normalize_pattern_filter(exclude_parent_patterns)
    kept = []
    skipped = []

    for rec in textures:
        parent = _record_parent_path(rec)
        reason = ""
        png = None

        if include_parent and not _path_matches_any(parent, include_parent):
            reason = "parent does not match --include-parent"
        elif exclude_parent and _path_matches_any(parent, exclude_parent):
            reason = "parent matches --exclude-parent"
        elif skip_unsafe_simple_replace and _record_is_unsafe_simple_replace(rec):
            reason = "simple PNG/BCLIM replacement is flagged unsafe"
        elif skip_no_canvas_constraint and not _record_has_canvas_constraint(rec):
            reason = "no explicit BCLYT pane canvas constraint"
        elif max_replacement_width or max_replacement_height or max_scale or max_texture_bytes:
            png = _png_info(_texture_png_path(project_dir, rec))
            if png:
                if max_replacement_width and png["width"] > max_replacement_width:
                    reason = f"replacement width {png['width']} exceeds {max_replacement_width}"
                elif max_replacement_height and png["height"] > max_replacement_height:
                    reason = f"replacement height {png['height']} exceeds {max_replacement_height}"
                elif max_scale and _record_scale_exceeds(rec, png, max_scale):
                    reason = f"replacement scale exceeds {max_scale:g}x"
                elif max_texture_bytes:
                    estimated = _estimate_replacement_bytes(rec, png, allow_etc_transcode)
                    if estimated and estimated > max_texture_bytes:
                        reason = f"estimated texture bytes {estimated} exceeds {max_texture_bytes}"

        if reason:
            skipped.append(_filter_skip_record(rec, parent, reason, png))
            continue
        kept.append(rec)

    return kept, skipped


def _record_is_unsafe_simple_replace(rec: Dict[str, Any]) -> bool:
    rebuild = rec.get("rebuild", {})
    if rebuild.get("simple_png_replace_safe") is False:
        return True
    context = rebuild.get("archive_context", {})
    return bool(context.get("risk_flags")) if isinstance(context, dict) else False


def _record_has_canvas_constraint(rec: Dict[str, Any]) -> bool:
    rebuild = rec.get("rebuild", {})
    constraints = rebuild.get("layout_constraints", [])
    return bool(constraints)


def _path_matches_any(path: str, patterns: List[str]) -> bool:
    clean = path.strip("/").lower()
    for pattern in patterns:
        pat = pattern.strip("/").lower()
        if any(ch in pat for ch in "*?[]"):
            if fnmatch.fnmatch(clean, pat):
                return True
        elif pat in clean:
            return True
    return False


def _filter_skip_record(rec: Dict[str, Any], parent: str, reason: str,
                        png: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    item = {
        "id": rec.get("id", ""),
        "parent": parent,
        "inner_path": rec.get("rebuild", {}).get("inner_path", ""),
        "parser_used": rec.get("rebuild", {}).get("parser_used", rec.get("parser_used", "")),
        "original_width": rec.get("width", 0),
        "original_height": rec.get("height", 0),
        "reason": reason,
    }
    if png:
        item["replacement_width"] = png["width"]
        item["replacement_height"] = png["height"]
    return item


def _record_scale_exceeds(rec: Dict[str, Any], png: Dict[str, Any],
                          max_scale: float) -> bool:
    original_width = rec.get("width") or 0
    original_height = rec.get("height") or 0
    if original_width > 0 and png["width"] / original_width > max_scale:
        return True
    if original_height > 0 and png["height"] / original_height > max_scale:
        return True
    return False


def _estimate_replacement_bytes(rec: Dict[str, Any], png: Dict[str, Any],
                                allow_etc_transcode: bool) -> int:
    fmt = rec.get("rebuild", {}).get("format_id", 0)
    bpp = {
        0x00: 32, 0x01: 24, 0x02: 16, 0x03: 16, 0x04: 16,
        0x05: 16, 0x06: 16, 0x07: 8, 0x08: 8, 0x09: 8,
        0x0A: 4, 0x0B: 4, 0x0C: 4, 0x0D: 8,
    }.get(fmt, 0)
    if not bpp:
        return 0
    width = _storage_dim(png["width"])
    height = _storage_dim(png["height"])
    return (width * height * bpp + 7) // 8


def _storage_dim(value: int) -> int:
    value = max(8, int(value or 0))
    return 1 << (value - 1).bit_length()


def _record_archive_ext(rec: Dict[str, Any]) -> str:
    parent = _record_parent_path(rec)
    clean = (parent or "").replace("\\", "/").rsplit("/", 1)[-1]
    if "." not in clean:
        return ""
    return clean.rsplit(".", 1)[-1].lower()


def _record_parent_path(rec: Dict[str, Any]) -> str:
    rebuild = rec.get("rebuild", {})
    return (rebuild.get("parent") or rec.get("source_file_path", "") or "").replace("\\", "/")


def _copy_romfs_inputs(source_romfs: str, romfs_output_folder: str,
                       textures: List[Dict[str, Any]], full_copy: bool,
                       work_items: Optional[List[Dict[str, Any]]] = None) -> int:
    if full_copy:
        copied = 0
        for root, _dirs, files in os.walk(source_romfs):
            rel_root = os.path.relpath(root, source_romfs)
            out_root = romfs_output_folder if rel_root == "." else os.path.join(romfs_output_folder, rel_root)
            os.makedirs(out_root, exist_ok=True)
            for fname in files:
                shutil.copy2(os.path.join(root, fname), os.path.join(out_root, fname))
                copied += 1
        return copied

    source_records = [item["rec"] for item in (work_items or [])] if work_items is not None else textures
    parents = sorted({
        (rec.get("rebuild", {}).get("parent") or rec.get("source_file_path", "")).lstrip("/\\")
        for rec in source_records
        if (rec.get("rebuild", {}).get("parent") or rec.get("source_file_path", ""))
    })
    copied = 0
    for rel in parents:
        rel_norm = rel.replace("/", os.sep).replace("\\", os.sep)
        src = os.path.join(source_romfs, rel_norm)
        dst = os.path.join(romfs_output_folder, rel_norm)
        if not os.path.isfile(src):
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1
    return copied


def _build_size_summary(source_romfs: str, romfs_output_folder: str) -> Dict[str, Any]:
    source_total = 0
    output_total = 0
    largest_growth = []
    for root, _dirs, files in os.walk(romfs_output_folder):
        for fname in files:
            out_path = os.path.join(root, fname)
            rel = os.path.relpath(out_path, romfs_output_folder)
            src_path = os.path.join(source_romfs, rel)
            out_size = os.path.getsize(out_path)
            src_size = os.path.getsize(src_path) if os.path.isfile(src_path) else 0
            output_total += out_size
            source_total += src_size
            largest_growth.append({
                "path": rel.replace(os.sep, "/"),
                "source_bytes": src_size,
                "output_bytes": out_size,
                "growth_bytes": out_size - src_size,
            })

    largest_growth.sort(key=lambda item: item["growth_bytes"], reverse=True)
    return {
        "source_bytes": source_total,
        "output_bytes": output_total,
        "growth_ratio": (output_total / source_total) if source_total else None,
        "largest_growth": largest_growth[:20],
    }


def _record_texture_ext(rec: Dict[str, Any]) -> str:
    rebuild = rec.get("rebuild", {})
    inner = rebuild.get("inner_path") or rec.get("source_texture_path", "") or rec.get("source_file_path", "")
    clean = (inner or "").replace("\\", "/").rsplit("/", 1)[-1]
    if "." not in clean:
        return ""
    return clean.rsplit(".", 1)[-1].lower()


def _record_parser_base(rec: Dict[str, Any]) -> str:
    rebuild = rec.get("rebuild", {})
    parser = str(rebuild.get("parser_used") or rec.get("parser_used", "unknown"))
    return parser.split("/", 1)[0].split("@", 1)[0].lower()
