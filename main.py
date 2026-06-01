"""
3ds-tex-extract: Extract textures from Nintendo 3DS ROMs.

Subcommands:
  scan        - Scan a ROM and report what's inside without extracting.
  extract     - Extract and decode textures to PNG.
  report      - Generate reports from a previous extraction.
  build-pack  - Build an Azahar/Citra custom-texture staging pack.
  import-dump - Import runtime-dumped textures from an emulator.

Backward compat: if no subcommand given and a positional ROM path is provided,
falls back to legacy extract behavior.
"""

import argparse
import hashlib
import json
import logging
import os
import struct
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Any, Optional, Tuple

from parsers.ncsd import NCSDParser
from parsers.cia import CIAParser
from parsers.ncch import NCCHParser
from parsers.romfs import RomFSParser
from textures.decoder import (
    decode_texture_fast, get_format_name, get_format_bpp,
    calculate_texture_size, FORMAT_NAMES,
)
from textures.scanner import (
    fingerprint_file, extract_textures_with_confidence, FileFingerprint,
)
from textures.tex_capcom import parse_capcom_tex_strict, TexParseResult
from output import (
    save_texture_as_png, generate_output_filename, build_output_path,
    save_raw_data, make_texture_record, write_manifest, write_failures,
    write_unknown_files, write_summary, sha1_bytes, sha1_rgba,
    compute_dedup_stats, make_alpha_visible,
)
from quality import compute_quality_metrics, generate_quality_report
from contact_sheet import generate_contact_sheet
from pack_builder import build_pack
from romfs_builder import build_romfs_from_manifest
from repack_capabilities import TEXTURE_WRITERS
from cityhash64 import cityhash64_hex

logger = logging.getLogger(__name__)


class EncryptedROMError(Exception):
    """Raised when the ROM is encrypted and cannot be processed."""
    pass


class ROMParseError(Exception):
    """Raised when the ROM format cannot be determined or parsed."""
    pass


def setup_logging(verbose: bool = False, quiet: bool = False):
    level = logging.ERROR if quiet else (logging.DEBUG if verbose else logging.INFO)
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s", stream=sys.stderr)


# ──────────────────────────────────────────────
# ROM parsing (shared across subcommands)
# ──────────────────────────────────────────────

def parse_rom(input_path: str) -> Tuple[bytes, str, str, str]:
    """
    Parse ROM -> RomFS bytes.
    Returns (romfs_data, title_id_str, product_code, container_chain).
    """
    logger.info(f"Reading: {input_path}")
    with open(input_path, "rb") as f:
        rom_data = f.read()
    logger.info(f"ROM size: {len(rom_data):,} bytes")

    ext = os.path.splitext(input_path)[1].lower()
    ncch_data = None
    title_id = 0
    chain = ""

    if ext == ".3ds":
        ncsd = NCSDParser(rom_data)
        ncch_data = ncsd.get_partition(0)
        title_id = ncsd.title_id
        chain = "NCSD/partition0/NCCH"
    elif ext == ".cia":
        cia = CIAParser(rom_data)
        ncch_data = cia.get_content(0)
        chain = "CIA/content0/NCCH"
    elif ext in (".cxi", ".app", ".ncch"):
        ncch_data = rom_data
        chain = "NCCH"
    else:
        # Auto-detect
        if len(rom_data) > 0x104 and rom_data[0x100:0x104] == b"NCSD":
            ncsd = NCSDParser(rom_data)
            ncch_data = ncsd.get_partition(0)
            title_id = ncsd.title_id
            chain = "NCSD/partition0/NCCH"
        elif len(rom_data) > 0x104 and rom_data[0x100:0x104] == b"NCCH":
            ncch_data = rom_data
            chain = "NCCH"
        else:
            try:
                cia = CIAParser(rom_data)
                ncch_data = cia.get_content(0)
                chain = "CIA/content0/NCCH"
            except Exception:
                raise ROMParseError(f"Cannot determine ROM format: {input_path}")

    if ncch_data is None:
        raise ROMParseError("Failed to extract NCCH data")

    try:
        ncch = NCCHParser(ncch_data)
    except RuntimeError as e:
        if "encrypted" in str(e).lower():
            raise EncryptedROMError(str(e))
        raise ROMParseError(str(e))
    if title_id == 0:
        title_id = ncch.title_id
    product_code = ncch.product_code
    title_id_str = f"{title_id:016X}"
    chain += "/RomFS"

    logger.info(f"Title ID: {title_id_str}  Product: {product_code}")
    romfs_data = ncch.get_romfs()
    return romfs_data, title_id_str, product_code, chain


class ArchiveRomFSSource:
    """Uniform file source for a RomFS embedded in a ROM container."""

    is_directory = False

    def __init__(self, romfs_data: bytes):
        self.romfs_data = romfs_data
        self._romfs = RomFSParser(romfs_data)
        self._files = self._romfs.list_files()

    def list_files(self) -> List[Tuple[str, int, int]]:
        return self._files

    def read_file_by_index(self, index: int) -> Tuple[str, bytes]:
        return self._romfs.read_file_by_index(index)

    def peek_file_by_index(self, index: int, size: int = 0x30) -> bytes:
        _path, offset, file_size = self._files[index]
        if file_size <= 0 or offset <= 0:
            return b""
        return self.romfs_data[offset:offset + min(file_size, size)]


class DirectoryRomFSSource:
    """Uniform file source for an already-extracted RomFS directory."""

    is_directory = True
    romfs_data = b""

    def __init__(self, root_path: str):
        self.root_path = os.path.abspath(root_path)
        self._files: List[Tuple[str, int, int]] = []
        self._real_paths: List[str] = []
        self._scan()

    def _scan(self):
        for root, dirs, files in os.walk(self.root_path):
            dirs.sort()
            for fname in sorted(files):
                real_path = os.path.join(root, fname)
                rel = os.path.relpath(real_path, self.root_path).replace("\\", "/")
                romfs_path = "/" + rel.lstrip("/")
                try:
                    size = os.path.getsize(real_path)
                except OSError:
                    continue
                self._real_paths.append(real_path)
                self._files.append((romfs_path, 0, size))

    def list_files(self) -> List[Tuple[str, int, int]]:
        return self._files

    def read_file_by_index(self, index: int) -> Tuple[str, bytes]:
        path = self._files[index][0]
        with open(self._real_paths[index], "rb") as f:
            return path, f.read()

    def peek_file_by_index(self, index: int, size: int = 0x30) -> bytes:
        try:
            with open(self._real_paths[index], "rb") as f:
                return f.read(size)
        except OSError:
            return b""


def load_input_source(input_path: str):
    """
    Load either a ROM container or an already-extracted RomFS directory.
    Returns (source, title_id, product_code, container_chain).
    """
    if os.path.isdir(input_path):
        logger.info(f"Scanning RomFS folder: {input_path}")
        source = DirectoryRomFSSource(input_path)
        return source, "ROMFS_FOLDER", "RomFS Folder", "RomFS directory"

    romfs_data, title_id, product_code, chain = parse_rom(input_path)
    return ArchiveRomFSSource(romfs_data), title_id, product_code, chain


def input_size_mb(input_path: str) -> float:
    if os.path.isdir(input_path):
        total = 0
        for root, _dirs, files in os.walk(input_path):
            for fname in files:
                try:
                    total += os.path.getsize(os.path.join(root, fname))
                except OSError:
                    pass
        return round(total / (1024 * 1024), 2)
    return round(os.path.getsize(input_path) / (1024 * 1024), 2)


def attach_rebuild_metadata(rec: Dict[str, Any], tex_info: Dict[str, Any],
                            romfs_file_path: str,
                            archive_context: Optional[Dict[str, Any]] = None):
    source_texture_path = tex_info.get("source_file") or romfs_file_path
    parent_path = romfs_file_path
    inner_path = ""

    if source_texture_path != romfs_file_path:
        if source_texture_path.startswith(romfs_file_path):
            inner_path = source_texture_path[len(romfs_file_path):].lstrip(">/")
        elif ">" in source_texture_path:
            parent_path, inner_path = source_texture_path.split(">", 1)
        elif source_texture_path.startswith(romfs_file_path.rstrip("/") + "/"):
            inner_path = source_texture_path[len(romfs_file_path.rstrip("/")) + 1:]

    rec["source_texture_path"] = source_texture_path
    rebuild = {
        "parent": parent_path,
        "inner_path": inner_path,
        "is_archive_member": bool(inner_path),
        "texture_name": tex_info.get("name", ""),
        "texture_index": tex_info.get("index"),
        "format_id": tex_info.get("format", 0),
        "container_format": tex_info.get("bflim_format"),
        "data_offset": tex_info.get("data_offset", 0),
        "data_size": tex_info.get("data_size", len(tex_info.get("data", b""))),
        "parser_used": tex_info.get("parser_used", "unknown"),
    }
    display_width = tex_info.get("display_width") or tex_info.get("crop_width")
    display_height = tex_info.get("display_height") or tex_info.get("crop_height")
    if display_width and display_height:
        rebuild["display_width"] = int(display_width)
        rebuild["display_height"] = int(display_height)
    if inner_path and archive_context:
        rebuild["archive_context"] = _compact_archive_context(archive_context)
        layout_constraints = _layout_constraints_for_inner_path(archive_context, inner_path)
        if layout_constraints:
            rebuild["layout_constraints"] = layout_constraints
            rebuild["layout_constraint_status"] = _layout_constraint_status(
                rec.get("width", 0), rec.get("height", 0), layout_constraints
            )
        else:
            rebuild["layout_constraint_status"] = "unknown_or_runtime"
        rebuild["simple_png_replace_safe"] = not bool(archive_context.get("risk_flags"))
    rec["rebuild"] = rebuild


def _compact_archive_context(archive_context: Dict[str, Any]) -> Dict[str, Any]:
    """Keep per-record manifest context small while preserving safety signals."""
    compact = dict(archive_context)
    compact.pop("texture_canvases", None)
    return compact


def build_archive_rebuild_context(file_data: bytes, file_path: str,
                                  textures: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not any((t.get("source_file") or file_path) != file_path for t in textures):
        return None

    entries = _archive_entries(file_data)
    if not entries:
        return None
    names = [name for name, _blob in entries]

    ext_counts: Dict[str, int] = {}
    for name in names:
        ext = path_ext(name)
        if ext:
            ext_counts[ext] = ext_counts.get(ext, 0) + 1

    bclan_animation_types = _archive_bclan_animation_types(entries)
    risk_flags = []
    if "CLTS" in bclan_animation_types:
        risk_flags.append("texture_srt_animation:bclan")
    if "CLTP" in bclan_animation_types:
        risk_flags.append("texture_pattern_animation:bclan")
    if ext_counts.get("bclan") and not bclan_animation_types:
        risk_flags.append("unknown_layout_animation:bclan")
    if ext_counts.get("bcmata"):
        risk_flags.append("material_animation:bcmata")
    if ext_counts.get("bcmdl") or ext_counts.get("bch") or ext_counts.get("cgfx"):
        risk_flags.append("model_or_graphics_container")
    if ext_counts.get("bcskla") or ext_counts.get("bcma") or ext_counts.get("bcanm"):
        risk_flags.append("animation_container")
    if ext_counts.get("bcfnt"):
        risk_flags.append("font_sibling")

    companion_exts = sorted(
        ext for ext in ext_counts
        if ext not in {"bclim", "bflim", "png", "bin"}
    )
    texture_canvases = _archive_texture_canvases(entries)
    return {
        "archive_member_count": len(names),
        "companion_extensions": companion_exts,
        "companion_counts": {ext: ext_counts[ext] for ext in companion_exts},
        "has_bclan": bool(ext_counts.get("bclan")),
        "has_bclyt": bool(ext_counts.get("bclyt")),
        "bclan_animation_types": bclan_animation_types,
        "texture_canvas_count": len(texture_canvases),
        "texture_canvases": texture_canvases,
        "risk_flags": risk_flags,
        "risk_level": "high" if any("texture_" in flag or flag.startswith("unknown_") for flag in risk_flags)
        else ("medium" if risk_flags else "low"),
    }


def _layout_constraints_for_inner_path(archive_context: Dict[str, Any], inner_path: str) -> List[Dict[str, Any]]:
    key = _texture_key(inner_path)
    canvases = archive_context.get("texture_canvases", {})
    return list(canvases.get(key, [])) if isinstance(canvases, dict) else []


def _layout_constraint_status(width: int, height: int, constraints: List[Dict[str, Any]]) -> str:
    if not constraints:
        return "unknown_or_runtime"
    for c in constraints:
        cw = c.get("canvas_width", 0)
        ch = c.get("canvas_height", 0)
        if cw and ch and (abs(float(cw) - float(width)) > 0.5 or abs(float(ch) - float(height)) > 0.5):
            return "explicit_canvas_differs_from_texture"
    return "explicit_canvas_matches_texture"


def _texture_key(path: str) -> str:
    base = os.path.basename(str(path).replace("\\", "/")).lower()
    if base.endswith(".bclim"):
        base = base[:-6]
    if base.endswith(".bflim"):
        base = base[:-6]
    return base


def _archive_texture_canvases(entries: List[Tuple[str, bytes]]) -> Dict[str, List[Dict[str, Any]]]:
    result: Dict[str, List[Dict[str, Any]]] = {}
    try:
        from layout_analyzer import analyze_bclyt_texture_canvases, is_bclyt
        for name, blob in entries:
            if not is_bclyt(blob):
                continue
            canvases = analyze_bclyt_texture_canvases(blob, layout_name=name)
            for tex_key, pane_infos in canvases.items():
                result.setdefault(tex_key, []).extend(pane_infos)
    except Exception:
        return result
    return result


def _archive_bclan_animation_types(entries: List[Tuple[str, bytes]]) -> List[str]:
    found = set()
    try:
        from layout_analyzer import analyze_bclan_animation_types, is_bclan
        for _name, blob in entries:
            if is_bclan(blob):
                found.update(analyze_bclan_animation_types(blob))
    except Exception:
        return []
    return sorted(found)


def _archive_entries(data: bytes) -> List[Tuple[str, bytes]]:
    if len(data) < 4:
        return []
    if data[:4] == b"darc" and data[4:6] == b"\xff\xfe":
        from parsers.darc import parse_darc
        return list(parse_darc(data))
    try:
        if data[:4] == b"SARC":
            from parsers.sarc import parse_sarc
            return parse_sarc(data)
        if data[:4] == b"NARC":
            from parsers.narc import parse_narc
            return parse_narc(data)
        if data[:4] == b"ZAR\x01":
            from parsers.zar import parse_zar
            return parse_zar(data)
        if data[:4] == b"CRAG":
            from parsers.garc import parse_garc
            return parse_garc(data)
    except Exception:
        return []
    return []


def _texture_skipped_by_rebuild_safety_filter(
    tex_info: Dict[str, Any],
    file_path: str,
    archive_context: Optional[Dict[str, Any]],
    skip_unsafe_simple_replace: bool = False,
    skip_no_canvas_constraint: bool = False,
) -> bool:
    source_texture_path = tex_info.get("source_file") or file_path
    if source_texture_path == file_path:
        return skip_no_canvas_constraint

    if skip_unsafe_simple_replace and archive_context:
        if archive_context.get("risk_flags"):
            return True

    if skip_no_canvas_constraint:
        if not archive_context:
            return True
        constraints = _layout_constraints_for_inner_path(
            archive_context,
            _inner_path_from_source(source_texture_path, file_path),
        )
        if not constraints:
            return True

    return False


def _inner_path_from_source(source_texture_path: str, parent_file_path: str) -> str:
    if source_texture_path.startswith(parent_file_path):
        return source_texture_path[len(parent_file_path):].lstrip(">/")
    if ">" in source_texture_path:
        return source_texture_path.split(">", 1)[1]
    if source_texture_path.startswith(parent_file_path.rstrip("/") + "/"):
        return source_texture_path[len(parent_file_path.rstrip("/")) + 1:]
    return source_texture_path


def normalize_ext_filter(values) -> set:
    result = set()
    for value in values or []:
        for part in str(value).split(","):
            part = part.strip().lower().lstrip(".")
            if part:
                result.add(part)
    return result


def path_ext(path: str) -> str:
    clean = (path or "").replace("\\", "/").rsplit("/", 1)[-1]
    if "." not in clean:
        return ""
    return clean.rsplit(".", 1)[-1].lower()


# Extensions that NEVER contain textures — skip immediately before any I/O.
_SKIP_EXTENSIONS = {
    ".bcstm", ".bcwav", ".bcsnd", ".bcsar",   # Audio
    ".bgm", ".brsar", ".bfsar", ".bfstm",     # Audio
    ".moflex", ".mods",                        # Video
    ".msbt", ".bmg",                           # Message/text
    ".bfttf", ".bcfnt",                        # Fonts
    ".shbin", ".bcsv",                         # Shaders/CSV
    ".txt", ".xml", ".json", ".lua",           # Script/config
    ".db", ".sav",                             # Database/save
    ".mp4", ".aac", ".ogg", ".wav",            # Media
    ".ips", ".bps",                            # Patches
}

_PROCESS_EXTENSIONS = {
    ".tex", ".bch", ".bcres", ".bflim", ".bclim", ".ctpk", ".cptk", ".ctxb", ".cmb", ".cgfx",
    ".bcmdl", ".bctex", ".bcmcla", ".szs", ".zar", ".zsi",
    ".bccam", ".bcsdr", ".bcptl", ".bhres", ".bhtex", ".cbres",
    ".lz", ".cmp",
    ".chres", ".chtex", ".stex",
    ".gar", ".lzs",
    ".zrc",
    ".fs",
    ".texturegdb",
    ".bin", ".raw", ".dat", ".img", ".arc", ".sarc", ".garc", ".narc",
    ".nwmdl", ".nwtex", ".pmnweffb", ".nwenv", ".nwlyt",
    ".fa",
    ".cpk",
    ".jtex", ".jarc",
    ".data",
    ".ctpk",
    ".gfa",
}

_PROCESS_DIRS = {
    "/tex/", "/texture/", "/textures/", "/gui/", "/effect/",
    "/model/", "/chr/", "/bg/", "/ui/", "/font/", "/a/",
    "/kart/", "/course/", "/driver/", "/menu",
    "/actor/", "/scene/", "/kankyo/", "/ending/", "/misc/",
}


def should_process_file(file_path: str, scan_all: bool,
                        file_data: bytes = None) -> bool:
    if scan_all:
        return True
    ext = ""
    if "." in file_path:
        ext = "." + file_path.rsplit(".", 1)[-1].lower()
    if ext in _SKIP_EXTENSIONS:
        return False
    if ext in _PROCESS_EXTENSIONS:
        return True
    path_lower = file_path.lower()
    for d in _PROCESS_DIRS:
        if d in path_lower:
            return True
    # For files with unknown extensions (or no extension), check magic bytes
    if file_data and len(file_data) >= 4:
        magic4 = file_data[:4]
        if magic4 in (b'CRAG', b'SARC', b'NARC', b'darc', b'Yaz0',
                      b'CGFX', b'BCH\x00', b'CTPK', b'CTXB',
                      b'ARC\x00', b'ARC0', b'CPK ', b'ZAR\x01',
                      b'GAR2', b'jIMG', b'GDB1', b'3DST',
                      b'GFAC', b'XFSA'):
            return True
        # LZ compression magic (single-byte check)
        if file_data[0] in (0x10, 0x11, 0x13):
            return True
        # Check for gzip-compressed containers (4-byte size prefix + gzip)
        if len(file_data) >= 6 and file_data[4:6] == b'\x1f\x8b':
            return True
        # Check for BFLIM/BCLIM footer magic (in last 0x28 bytes)
        if len(file_data) >= 0x28:
            if file_data[-0x28:-0x24] in (b'FLIM', b'CLIM'):
                return True
    return False


# ──────────────────────────────────────────────
# SCAN subcommand
# ──────────────────────────────────────────────

def cmd_scan(args):
    setup_logging(args.verbose, args.quiet)
    t0 = time.time()

    source, title_id, product_code, chain = load_input_source(args.input)
    files = source.list_files()

    type_counts: Dict[str, int] = {}
    ext_counts: Dict[str, int] = {}
    tex_file_candidates = 0

    for file_idx, (path, offset, size) in enumerate(files):
        ext = ""
        if "." in path:
            ext = "." + path.rsplit(".", 1)[-1].lower()
        ext_counts[ext] = ext_counts.get(ext, 0) + 1

        peek_data = source.peek_file_by_index(file_idx, 0x30)
        if should_process_file(path, args.scan_all, file_data=peek_data):
            tex_file_candidates += 1
            # Quick fingerprint
            _, fdata = source.read_file_by_index(file_idx)
            fp = fingerprint_file(fdata, path)
            t = fp.detected_type or "unknown"
            type_counts[t] = type_counts.get(t, 0) + 1

    elapsed = time.time() - t0

    print(f"\n--- Scan Results ---")
    print(f"ROM:        {args.input}")
    print(f"Title ID:   {title_id}")
    print(f"Product:    {product_code}")
    print(f"Container:  {chain}")
    print(f"Total files: {len(files)}")
    print(f"Texture candidates: {tex_file_candidates}")
    print(f"\nExtension breakdown:")
    for ext, cnt in sorted(ext_counts.items(), key=lambda x: -x[1]):
        print(f"  {ext or '(none)':12s} {cnt:5d}")
    print(f"\nDetected container types:")
    for t, cnt in sorted(type_counts.items(), key=lambda x: -x[1]):
        print(f"  {t:16s} {cnt:5d}")
    print(f"\nElapsed: {elapsed:.1f}s")


# ──────────────────────────────────────────────
# EXTRACT subcommand
# ──────────────────────────────────────────────

def write_azahar_pack_config(output_dir: str) -> None:
    """Write the pack config that tells Azahar to use the new hash format."""
    pack_json = os.path.join(output_dir, "pack.json")
    if os.path.exists(pack_json):
        return

    config = {
        "author": "3DS Texture Forge",
        "version": "1.0.0",
        "description": "Generated texture pack",
        "options": {
            "skip_mipmap": False,
            "flip_png_files": True,
            "use_new_hash": True,
        },
        "textures": {},
    }
    with open(pack_json, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


def cmd_extract(args, progress_callback=None):
    _verbose = getattr(args, 'verbose', False)
    _quiet = getattr(args, 'quiet', False)
    # In CLI progress-bar mode (no external callback, not verbose, not quiet),
    # suppress INFO logs so they don't mix with the progress bar line.
    _bar_mode = progress_callback is None and not _verbose and not _quiet
    setup_logging(_verbose, _quiet or _bar_mode)
    t0 = time.time()

    # Thread pool for parallel PNG saves (I/O-bound; GIL released for file writes).
    _png_pool = ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 4))
    _png_futures = []  # collect futures to detect late failures

    source, title_id, product_code, chain = load_input_source(args.input)
    romfs_data = source.romfs_data
    files = source.list_files()

    azahar_mode = getattr(args, 'output_mode', 'normal') == 'azahar'
    if azahar_mode:
        _base = args.output or "output"
        output_dir = os.path.join(_base, title_id)
    else:
        output_dir = args.output or os.path.join("output", title_id)
    os.makedirs(output_dir, exist_ok=True)
    if azahar_mode:
        write_azahar_pack_config(output_dir)
    logger.info(f"Output: {output_dir}" + (" [azahar mode]" if azahar_mode else ""))

    if args.list_files:
        print(f"\nRomFS: {len(files)} files\n")
        for p, _, s in sorted(files):
            print(f"  {s:>10,d}  {p}")
        return

    # Pre-pass: build lookup for .texturebin companions (Star Fox 64 3D GDB1 pairs)
    _texturebin_map: Dict[str, int] = {}  # lowercase base_name -> file_idx
    for _idx, (_fp, _fo, _fs) in enumerate(files):
        if _fp.lower().endswith(".texturebin"):
            _base = _fp.rsplit(".", 1)[0].lower()
            _texturebin_map[_base] = _idx

    # Pre-pass: Smash Bros. 3DS dt/ls pair — inject virtual .bch entries
    _smash_virtual_params: Dict[int, tuple] = {}  # file_idx -> (dt_off, comp_sz)
    _smash_dt_rom_off: int = 0
    _ls_entry = None
    _dt_entry = None
    if not source.is_directory:
        _ls_entry = next(
            ((p, o, s) for p, o, s in files if p.lower().lstrip("/") == "ls"), None
        )
        _dt_entry = next(
            ((p, o, s) for p, o, s in files if p.lower().lstrip("/") == "dt"), None
        )
    if _ls_entry and _dt_entry:
        _ls_raw = romfs_data[_ls_entry[1] : _ls_entry[1] + _ls_entry[2]]
        from parsers.smash_dt import (
            parse_ls as _parse_ls,
            is_texture_resource as _is_tex_res,
            decompress_resource as _smash_decomp,
            LS_MAGIC as _LS_MAGIC,
        )
        if _ls_raw[:4] == _LS_MAGIC:
            _smash_dt_rom_off = _dt_entry[1]
            _v_base = len(files)
            logger.info("Smash Bros dt/ls detected — scanning %d entries", len(_parse_ls(_ls_raw)))
            for _si, (_sh, _sdt_off, _scomp_sz) in enumerate(_parse_ls(_ls_raw)):
                if _scomp_sz < 100 or _scomp_sz > 50 * 1024 * 1024:
                    continue
                # Quick probe: check zlib magic without full decompress
                _probe = romfs_data[
                    _smash_dt_rom_off + _sdt_off :
                    _smash_dt_rom_off + _sdt_off + min(_scomp_sz, 600)
                ]
                _has_zlib = any(
                    _probe[i : i + 2] in {b"\x78\x9C", b"\x78\xDA", b"\x78\x01", b"\x78\x5E"}
                    for i in range(min(512, len(_probe) - 1))
                )
                if not _has_zlib:
                    continue
                v_idx = _v_base + _si
                files.append((f"dt_smash_{_sh:08X}.bch", 0, _scomp_sz))
                _smash_virtual_params[v_idx] = (_sdt_off, _scomp_sz)

    # CLI progress bar — only active when no external progress_callback (GUI) and not quiet
    _bar_start = time.time()
    _bar_last_print = [0.0]
    _cli_bar_width = 30

    def _cli_progress(cur, total, _path, _a, tex_count, _b):
        if getattr(args, 'quiet', False):
            return
        now = time.time()
        if now - _bar_last_print[0] < 0.15 and cur < total:
            return
        _bar_last_print[0] = now
        elapsed_s = now - _bar_start
        pct = cur / total if total > 0 else 0.0
        filled = int(_cli_bar_width * pct)
        bar = "=" * filled + "-" * (_cli_bar_width - filled)
        eta_str = ""
        if pct > 0.01 and elapsed_s > 1:
            remaining = elapsed_s / pct * (1 - pct)
            m, s = divmod(int(remaining), 60)
            eta_str = f" | ETA: {m}:{s:02d}"
        line = (f"\r  [{bar}] {pct*100:4.0f}% | "
                f"File {cur}/{total} | {tex_count:,} textures{eta_str}   ")
        sys.stderr.write(line)
        sys.stderr.flush()
        if cur == total:
            sys.stderr.write("\n")
            sys.stderr.flush()

    if progress_callback is None:
        progress_callback = _cli_progress

    rom_basename = os.path.basename(args.input)
    records: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    unknowns: List[Dict[str, Any]] = []
    tex_reports: List[Dict[str, Any]] = []  # RE:Revelations TEX report
    only_archive_exts = normalize_ext_filter(getattr(args, "only_archive", []))
    only_texture_exts = normalize_ext_filter(getattr(args, "only_texture", []))
    only_supported_writers = getattr(args, "only_supported_writers", False)
    skip_unsafe_simple_replace = getattr(args, "skip_unsafe_simple_replace", False)
    skip_no_canvas_constraint = getattr(args, "skip_no_canvas_constraint", False)

    # Stats
    files_scanned = 0
    containers_recognized = 0
    decoded_ok = 0
    decoded_fail = 0
    suspicious_count = 0
    unknown_types = 0
    tex_global_idx = 0
    # Dedup: md5(rgba_bytes) -> tex_id of first occurrence
    seen_content_hashes: Dict[str, str] = {}
    dedup_active = getattr(args, 'dedup', False)
    # Raw-hash dedup: CityHash64(raw_pixel_bytes) -> (tex_id, content_hash) of first occurrence
    # Allows skipping decode entirely when pixel data is identical
    seen_raw_hashes: Dict[str, tuple] = {}

    for file_idx, (file_path, file_offset, file_size) in enumerate(files):
        # Quick reject by extension first, then peek magic for unknown extensions
        if not should_process_file(file_path, args.scan_all):
            # Extension not in skip or process list (or extensionless) — peek magic
            _peek_data = source.peek_file_by_index(file_idx, 0x30)
            if not should_process_file(file_path, args.scan_all, file_data=_peek_data):
                continue

        files_scanned += 1
        if progress_callback:
            progress_callback(files_scanned, len(files), file_path, "", decoded_ok, 0)

        # Fast magic pre-check: skip known non-texture formats without reading large files.
        # This avoids expensive ROM reads for game-data files (Wwise audio, etc.).
        # BG4\x00 is intentionally NOT skipped — BG4 files embed BCH textures.
        _SKIP_MAGICS = {b"BKHD", b"AKPK", b"CSAR", b"NUS3", b"RIFF"}
        if file_idx not in _smash_virtual_params and file_size > 0:
            _peek = source.peek_file_by_index(file_idx, 4)
            if _peek in _SKIP_MAGICS:
                unknown_types += 1
                unknowns.append({"path": file_path, "detected_type": None,
                                  "magic": _peek.hex(), "size": file_size})
                continue

        # Virtual Smash Bros dt resource — decompress on demand
        if file_idx in _smash_virtual_params:
            _sdt_off, _scomp_sz = _smash_virtual_params[file_idx]
            _raw = romfs_data[
                _smash_dt_rom_off + _sdt_off :
                _smash_dt_rom_off + _sdt_off + _scomp_sz
            ]
            file_data = _smash_decomp(_raw)
            if not file_data or file_data[:4] not in {b"BCH\x00", b"CGFX", b"CTPK"}:
                continue
        else:
            try:
                _, file_data = source.read_file_by_index(file_idx)
            except Exception as e:
                logger.warning(f"Cannot read {file_path}: {e}")
                continue

        if len(file_data) < 8:
            continue

        ext = ""
        if "." in file_path:
            ext = "." + file_path.rsplit(".", 1)[-1].lower()

        # --- Capcom .tex special handling for RE:Revelations ---

        if ext == ".tex" or file_data[:4] in (b"TEX\x00", b"\x00XET"):
            tex_result = parse_capcom_tex_strict(file_data, file_path,
                                                  title_id=title_id)
            tex_reports.append(tex_result.to_dict())

        # --- GDB1 pair handling (Star Fox 64 3D .texturegdb + .texturebin) ---
        try:
            if ext == ".texturegdb":
                from textures.gdb1 import is_gdb1 as _is_gdb1, parse_gdb1_pair as _parse_gdb1
                from textures.scanner import fingerprint_file as _ff_file
                fp = _ff_file(file_data, file_path)
                if _is_gdb1(file_data):
                    bin_key = file_path.rsplit(".", 1)[0].lower()
                    bin_idx = _texturebin_map.get(bin_key)
                    if bin_idx is not None:
                        try:
                            _, bin_data = source.read_file_by_index(bin_idx)
                            textures = _parse_gdb1(file_data, bin_data, file_path)
                        except Exception as _ge:
                            logger.warning(f"GDB1 pair read failed for {file_path}: {_ge}")
                            textures = []
                    else:
                        logger.debug(f"GDB1: no .texturebin companion for {file_path}")
                        textures = []
                else:
                    textures = []
                containers_recognized += 1

            else:
                # --- General extraction ---
                textures, fp = extract_textures_with_confidence(
                    file_data, file_path, scan_all=args.scan_all,
                    title_id=title_id,
                )

                if fp.detected_type:
                    containers_recognized += 1
                elif not textures:
                    unknown_types += 1
                    unknowns.append(fp.to_dict())

        except Exception as _container_exc:
            logger.warning(f"Container extraction failed for {file_path}: {_container_exc}")
            failures.append({
                "id": f"file_{files_scanned}",
                "source_file_path": file_path,
                "reason": f"container error: {_container_exc}",
                "parser_used": "unknown",
            })
            decoded_fail += 1
            continue

        archive_rebuild_context = build_archive_rebuild_context(file_data, file_path, textures)

        for tex_info in textures:
          try:
            source_texture_path = tex_info.get("source_file") or file_path
            archive_parent_path = file_path
            if source_texture_path != file_path:
                if source_texture_path.startswith(file_path):
                    archive_parent_path = file_path
                elif ">" in source_texture_path:
                    archive_parent_path = source_texture_path.split(">", 1)[0]
            if only_archive_exts:
                candidate_ext = path_ext(archive_parent_path)
                if candidate_ext not in only_archive_exts:
                    continue
            if only_texture_exts:
                candidate_ext = path_ext(source_texture_path)
                if candidate_ext not in only_texture_exts:
                    continue
            if only_supported_writers:
                parser_base = str(tex_info.get("parser_used", "unknown")).split("/", 1)[0].split("@", 1)[0]
                if parser_base not in TEXTURE_WRITERS:
                    continue
            if (
                skip_unsafe_simple_replace
                or skip_no_canvas_constraint
            ) and _texture_skipped_by_rebuild_safety_filter(
                tex_info,
                file_path,
                archive_rebuild_context,
                skip_unsafe_simple_replace=skip_unsafe_simple_replace,
                skip_no_canvas_constraint=skip_no_canvas_constraint,
            ):
                continue

            fmt = tex_info.get("format", 0)
            w = tex_info.get("width", 0)
            h = tex_info.get("height", 0)
            pixel_data = tex_info.get("data", b"")
            confidence = tex_info.get("confidence", "low")
            parser_used = tex_info.get("parser_used", "unknown")
            notes_list = tex_info.get("capcom_parse_notes", [])
            notes_str = "; ".join(notes_list) if isinstance(notes_list, list) else str(notes_list)

            tex_id = f"tex_{tex_global_idx:04d}"
            source_blob_sha1 = sha1_bytes(pixel_data) if pixel_data else ""

            if not pixel_data or w < 1 or h < 1:
                failures.append({
                    "id": tex_id,
                    "source_file_path": file_path,
                    "reason": "no pixel data or invalid dims",
                    "width": w, "height": h,
                    "parser_used": parser_used,
                })
                decoded_fail += 1
                tex_global_idx += 1
                continue

            # Phase 4a: In --dedup mode, skip decode entirely if raw pixel bytes
            # are identical to a previously decoded texture (same bytes → same output).
            if dedup_active and pixel_data:
                raw_hash_key = cityhash64_hex(pixel_data)
                if raw_hash_key in seen_raw_hashes:
                    first_tex_id, first_content_hash = seen_raw_hashes[raw_hash_key]
                    decoded_ok += 1
                    fname = generate_output_filename(tex_global_idx, tex_info, file_path)
                    rec = make_texture_record(
                        tex_id=tex_id,
                        source_rom=rom_basename,
                        source_container_chain=chain,
                        source_file_path=file_path,
                        source_offset=file_offset,
                        detected_format=get_format_name(fmt),
                        width=w, height=h,
                        mip_count=tex_info.get("mip_count", 1),
                        raw_data_size=len(pixel_data),
                        decoded_png_path="",
                        confidence=confidence,
                        parser_used=parser_used,
                        notes=notes_str,
                        sha1_rgba_val="",
                        sha1_source_val=source_blob_sha1,
                        quality_metrics={"is_suspicious": False,
                                         "pct_transparent": 0,
                                         "variance_score": 0,
                                         "flags": []},
                    )
                    rec["content_hash"] = first_content_hash
                    rec["duplicate_of"] = first_tex_id
                    rec["raw_data_hash_city64"] = raw_hash_key
                    attach_rebuild_metadata(rec, tex_info, file_path, archive_rebuild_context)
                    records.append(rec)
                    tex_global_idx += 1
                    continue
            else:
                raw_hash_key = None

            try:
                rgba = decode_texture_fast(pixel_data, w, h, fmt)
            except Exception as e:
                rgba = None
                fail_reason = str(e)

            # Crop to display dimensions if BFLIM provides them
            if rgba is not None:
                crop_w = tex_info.get("crop_width", 0)
                crop_h = tex_info.get("crop_height", 0)
                if crop_w > 0 and crop_h > 0 and (crop_w < w or crop_h < h):
                    rgba = rgba[:crop_h, :crop_w]
                    tex_info["width"] = crop_w
                    tex_info["height"] = crop_h
                    w = crop_w
                    h = crop_h

            if rgba is None:
                fail_reason = fail_reason if "fail_reason" in dir() else "decoder returned None"
                failures.append({
                    "id": tex_id,
                    "source_file_path": file_path,
                    "detected_format": get_format_name(fmt),
                    "width": w, "height": h,
                    "reason": fail_reason,
                    "parser_used": parser_used,
                })
                decoded_fail += 1
                tex_global_idx += 1
                continue

            # BCH heuristic pixel variance filter — reject decoded noise
            # The BCH parser is heuristic-based and can find false positives
            # in non-texture binary data. Two filter levels:
            # - Solid-color textures (1 unique color) are always garbage
            # - Two-color textures from the fallback scanner are also garbage
            if parser_used == "bch":
                import numpy as np

                flat = rgba.reshape(-1, 4)
                n_sample = min(2000, len(flat))
                sample = flat[:n_sample]
                # View RGBA uint8 as uint32 — 7x faster than np.unique(axis=0)
                sample_u32 = np.ascontiguousarray(sample).view(np.uint32).ravel()
                # Solid-color check: min==max in O(n) without sorting
                if sample_u32.min() == sample_u32.max():
                    logger.debug(f"BCH filter: skipping {w}x{h} (solid color)")
                    tex_global_idx += 1
                    continue
                # Thin-strip check only when needed (rare)
                aspect = max(w, h) / max(min(w, h), 1)
                if aspect > 8:
                    unique_colors = len(np.unique(sample_u32))
                    if unique_colors <= 3:
                        logger.debug(f"BCH filter: skipping {w}x{h} (thin strip, {unique_colors} colors)")
                        tex_global_idx += 1
                        continue

            # Dedup check (before quality metrics — skip expensive quality for dups)
            content_hash = hashlib.md5(rgba.tobytes()).hexdigest()
            duplicate_of = ""
            if content_hash in seen_content_hashes:
                duplicate_of = seen_content_hashes[content_hash]
            else:
                seen_content_hashes[content_hash] = tex_id

            # Quality check — only for unique (first-occurrence) textures.
            # Duplicates are not written to disk, so quality info is not needed
            # for the contact sheet or visual review.
            if not duplicate_of:
                qm = compute_quality_metrics(rgba, pica_format=fmt)
            else:
                qm = {"is_suspicious": False, "pct_transparent": 0.0,
                      "variance_score": 0.0, "unique_colors_sampled": 0,
                      "flags": []}

            # Save PNG (skip if dedup mode and this is a duplicate)
            if azahar_mode and pixel_data:
                # Azahar filename: tex1_<W>x<H>_<CityHash64>_<fmt_int>_mip<N>.png
                _city_hash = cityhash64_hex(pixel_data)
                fname = f"tex1_{w}x{h}_{_city_hash}_{fmt}_mip0.png"
                out_path = os.path.join(output_dir, fname)
            else:
                fname = generate_output_filename(tex_global_idx, tex_info, file_path)
                out_path = build_output_path(output_dir, file_path, fname)

            if dedup_active and duplicate_of:
                # Count it but don't write the file
                decoded_ok += 1
                rel_png = ""
                rec = make_texture_record(
                    tex_id=tex_id,
                    source_rom=rom_basename,
                    source_container_chain=chain,
                    source_file_path=file_path,
                    source_offset=file_offset,
                    detected_format=get_format_name(fmt),
                    width=w, height=h,
                    mip_count=tex_info.get("mip_count", 1),
                    raw_data_size=len(pixel_data),
                    decoded_png_path=rel_png,
                    confidence=confidence,
                    parser_used=parser_used,
                    notes=notes_str,
                    sha1_rgba_val="",
                    sha1_source_val=source_blob_sha1,
                    quality_metrics=qm,
                )
                rec["content_hash"] = content_hash
                rec["duplicate_of"] = duplicate_of
                if pixel_data:
                    rec["raw_data_hash_city64"] = cityhash64_hex(pixel_data)
                attach_rebuild_metadata(rec, tex_info, file_path, archive_rebuild_context)
                records.append(rec)
                tex_global_idx += 1
                continue

            # Unique texture — submit PNG save to thread pool (I/O in background).
            # Pass rgba directly; after this point the main loop creates a new
            # rgba for the next texture, so the worker gets exclusive use of this one.
            rel_png = os.path.relpath(out_path, output_dir)
            _png_futures.append(
                _png_pool.submit(save_texture_as_png, rgba, out_path, fmt)
            )

            if args.dump_raw and pixel_data:
                save_raw_data(pixel_data, out_path)

            decoded_ok += 1
            if qm["is_suspicious"]:
                suspicious_count += 1

            rec = make_texture_record(
                tex_id=tex_id,
                source_rom=rom_basename,
                source_container_chain=chain,
                source_file_path=file_path,
                source_offset=file_offset,
                detected_format=get_format_name(fmt),
                width=w,
                height=h,
                mip_count=tex_info.get("mip_count", 1),
                raw_data_size=len(pixel_data),
                decoded_png_path=rel_png,
                confidence=confidence,
                parser_used=parser_used,
                notes=notes_str,
                sha1_rgba_val=sha1_rgba(rgba),
                sha1_source_val=source_blob_sha1,
                quality_metrics=qm,
            )
            rec["content_hash"] = content_hash
            rec["sha1_png_rgba"] = sha1_rgba(make_alpha_visible(rgba.copy(), fmt))
            if pixel_data:
                if raw_hash_key is None:
                    raw_hash_key = cityhash64_hex(pixel_data)
                rec["raw_data_hash_city64"] = raw_hash_key
                # Register for future raw-hash dedup (first saved occurrence)
                if dedup_active and raw_hash_key not in seen_raw_hashes:
                    seen_raw_hashes[raw_hash_key] = (tex_id, content_hash)
            attach_rebuild_metadata(rec, tex_info, file_path, archive_rebuild_context)
            records.append(rec)
            tex_global_idx += 1
          except Exception as _tex_exc:
            logger.warning(f"Texture decode error in {file_path}: {_tex_exc}")
            failures.append({
                "id": f"tex_{tex_global_idx:04d}",
                "source_file_path": file_path,
                "reason": str(_tex_exc),
                "parser_used": tex_info.get("parser_used", "unknown") if tex_info else "unknown",
            })
            decoded_fail += 1
            tex_global_idx += 1

    # Wait for all background PNG saves to complete, collect any failures.
    for future in _png_futures:
        try:
            if not future.result():
                decoded_fail += 1
        except Exception:
            decoded_fail += 1
    _png_pool.shutdown(wait=False)

    # --- Write outputs ---
    game_title = product_code or title_id
    write_manifest(output_dir, records, args.input, title_id, game_title)
    write_failures(output_dir, failures)
    write_unknown_files(output_dir, unknowns)

    # Contact sheet
    cs_path = generate_contact_sheet(records, output_dir)

    # RE:Revelations TEX report
    if tex_reports:
        tex_report_path = os.path.join(output_dir, "re_revelations_tex_report.json")
        with open(tex_report_path, "w", encoding="utf-8") as f:
            json.dump({
                "title_id": title_id,
                "tex_files_found": len(tex_reports),
                "parsed": sum(1 for t in tex_reports if t["status"] == "parsed"),
                "partial": sum(1 for t in tex_reports if t["status"] == "partial"),
                "failed": sum(1 for t in tex_reports if t["status"] == "failed"),
                "files": tex_reports,
            }, f, indent=2)
        logger.info(f"Wrote re_revelations_tex_report.json ({len(tex_reports)} files)")

    elapsed = time.time() - t0

    # Dedup stats from records
    all_hashes = [r.get("content_hash", "") for r in records if r.get("content_hash")]
    textures_unique = len(set(all_hashes)) if all_hashes else decoded_ok
    textures_duplicate = decoded_ok - textures_unique
    dup_pct = round(textures_duplicate / decoded_ok * 100, 1) if decoded_ok > 0 else 0.0

    summary = {
        "title_id": title_id,
        "game_title": game_title,
        "rom_file": rom_basename,
        "files_scanned": files_scanned,
        "containers_recognized": containers_recognized,
        "textures_decoded_ok": decoded_ok,
        "textures_unique": textures_unique,
        "textures_duplicate": textures_duplicate,
        "duplicate_pct": dup_pct,
        "textures_failed": decoded_fail,
        "suspicious_outputs": suspicious_count,
        "unknown_file_types": unknown_types,
        "elapsed_seconds": round(elapsed, 1),
        "contact_sheet": cs_path if cs_path else None,
        "dedup_mode": dedup_active,
    }
    write_summary(output_dir, summary)

    # Always generate quality report (JSON + TXT)
    fmt_dist: Dict[str, int] = {}
    for r in records:
        f = r.get("detected_format", "?")
        fmt_dist[f] = fmt_dist.get(f, 0) + 1
    quality_report = generate_quality_report(
        records=records,
        game_name=game_title,
        rom_file=args.input,
        output_dir=output_dir,
        format_distribution=fmt_dist,
    )
    summary["quality_score"] = quality_report.get("quality_score", 0.0)

    # --report: write machine-readable report.json
    if getattr(args, 'report', False):
        parser_breakdown: Dict[str, int] = {}
        format_breakdown: Dict[str, int] = {}
        for r in records:
            p = r.get("parser_used", "unknown")
            parser_breakdown[p] = parser_breakdown.get(p, 0) + 1
            f = r.get("detected_format", "?")
            format_breakdown[f] = format_breakdown.get(f, 0) + 1
        report_data = {
            "tool_version": "3.0",
            "rom_filename": rom_basename,
            "rom_size_mb": input_size_mb(args.input),
            "title_id": title_id,
            "product_code": product_code,
            "extraction_time_seconds": round(elapsed, 2),
            "romfs_files": len(files),
            "files_scanned": files_scanned,
            "textures_total": decoded_ok,
            "textures_unique": textures_unique,
            "textures_failed": decoded_fail,
            "textures_suspicious": suspicious_count,
            "duplicate_pct": dup_pct,
            "parser_breakdown": parser_breakdown,
            "format_breakdown": format_breakdown,
            "errors": [{"file": f["source_file_path"], "reason": f["reason"]}
                       for f in failures],
        }
        report_path = os.path.join(output_dir, "report.json")
        with open(report_path, "w", encoding="utf-8") as _rf:
            json.dump(report_data, _rf, indent=2)
        logger.info(f"Wrote report.json")

    # CLI output
    if dedup_active:
        written = decoded_ok - textures_duplicate
        tex_line = f"  Textures found:         {decoded_ok:,} ({written:,} written, --dedup active)"
    elif textures_duplicate > 0:
        tex_line = f"  Textures decoded OK:    {decoded_ok:,} ({textures_unique:,} unique, {dup_pct}% duplicates)"
    else:
        tex_line = f"  Textures decoded OK:    {decoded_ok:,} ({textures_unique:,} unique)"

    print(f"\n{'='*56}")
    print(f"  Extraction Results")
    print(f"{'='*56}")
    print(f"  Files scanned:          {files_scanned}")
    print(f"  Recognized containers:  {containers_recognized}")
    print(tex_line)
    print(f"  Textures failed:        {decoded_fail}")
    print(f"  Suspicious outputs:     {suspicious_count}")
    q_score = quality_report.get("quality_score", 0.0)
    q_pct = round(q_score * 100, 1)
    print(f"  Quality score:          {q_pct}%")
    print(f"  Unknown file types:     {unknown_types}")
    print(f"  Elapsed:                {elapsed:.1f}s")
    print(f"  Output:                 {output_dir}")
    if cs_path:
        print(f"  Contact sheet:          {cs_path}")
    print(f"{'='*56}")
    if q_score < 0.50:
        print(f"\n  WARNING: Only {q_pct}% of textures appear valid.")
        print(f"  This usually means a pixel decoder bug.")

    return summary, records, failures


# ──────────────────────────────────────────────
# REPORT subcommand
# ──────────────────────────────────────────────

def cmd_report(args):
    setup_logging(args.verbose, args.quiet)
    project_dir = args.project_dir

    manifest_path = os.path.join(project_dir, "manifest.json")
    if not os.path.isfile(manifest_path):
        logger.error(f"No manifest.json in {project_dir}")
        sys.exit(1)

    with open(manifest_path, "r") as f:
        manifest = json.load(f)

    textures = manifest.get("textures", [])
    print(f"\n--- Report for {project_dir} ---")
    print(f"Title ID:     {manifest.get('title_id', '?')}")
    print(f"Game:         {manifest.get('game_title', '?')}")
    print(f"Textures:     {len(textures)}")

    # Confidence breakdown
    conf = {}
    fmt_counts = {}
    suspicious = 0
    for t in textures:
        c = t.get("confidence", "unknown")
        conf[c] = conf.get(c, 0) + 1
        f = t.get("detected_format", "?")
        fmt_counts[f] = fmt_counts.get(f, 0) + 1
        if t.get("quality", {}).get("is_suspicious"):
            suspicious += 1

    print(f"\nConfidence breakdown:")
    for c, n in sorted(conf.items()):
        print(f"  {c:12s} {n:5d}")

    print(f"\nFormat breakdown:")
    for f, n in sorted(fmt_counts.items(), key=lambda x: -x[1]):
        print(f"  {f:12s} {n:5d}")

    print(f"\nSuspicious:   {suspicious}")

    # Failures
    fail_path = os.path.join(project_dir, "failures.json")
    if os.path.isfile(fail_path):
        with open(fail_path, "r") as f:
            fail_data = json.load(f)
        print(f"Failures:     {fail_data.get('count', 0)}")

    # Unknowns
    unk_path = os.path.join(project_dir, "unknown_files.json")
    if os.path.isfile(unk_path):
        with open(unk_path, "r") as f:
            unk_data = json.load(f)
        print(f"Unknown files: {unk_data.get('count', 0)}")

    # TEX report
    tex_path = os.path.join(project_dir, "re_revelations_tex_report.json")
    if os.path.isfile(tex_path):
        with open(tex_path, "r") as f:
            tex_data = json.load(f)
        print(f"\nCapcom TEX report:")
        print(f"  Files found:  {tex_data.get('tex_files_found', 0)}")
        print(f"  Parsed:       {tex_data.get('parsed', 0)}")
        print(f"  Partial:      {tex_data.get('partial', 0)}")
        print(f"  Failed:       {tex_data.get('failed', 0)}")


# ──────────────────────────────────────────────
# BUILD-PACK subcommand
# ──────────────────────────────────────────────

def cmd_build_pack(args):
    setup_logging(args.verbose, args.quiet)
    project_dir = args.project_dir

    manifest_path = os.path.join(project_dir, "manifest.json")
    if not os.path.isfile(manifest_path):
        logger.error(f"No manifest.json in {project_dir}")
        sys.exit(1)

    with open(manifest_path, "r") as f:
        manifest = json.load(f)

    title_id = manifest.get("title_id", "0000000000000000")
    textures = manifest.get("textures", [])

    # Check if any textures have dump hashes (from import-dump)
    has_hashes = any(t.get("dump_hash") for t in textures)
    mode = "mapped" if has_hashes else "staging"

    pack_dir = build_pack(project_dir, title_id, textures, mode=mode)

    print(f"\nPack built: {pack_dir}")
    print(f"Mode: {mode}")
    if mode == "staging":
        print(
            "WARNING: This is a STAGING pack. It does not contain runtime texture hashes.\n"
            "It cannot be used as a drop-in Azahar/Citra custom texture pack.\n"
            "To make it usable:\n"
            "  1. Run the emulator with texture dumping enabled.\n"
            "  2. Use 'import-dump <dump_folder> <project_dir>' to merge hashes.\n"
            "  3. Re-run 'build-pack <project_dir>'."
        )


# ──────────────────────────────────────────────
# IMPORT-DUMP subcommand
# ──────────────────────────────────────────────

def cmd_build_romfs(args):
    setup_logging(args.verbose, args.quiet)
    bar_width = 30
    last_print = [0.0]
    start_time = time.time()

    def _progress(cur, total, rec, status, target):
        if getattr(args, "quiet", False):
            return
        now = time.time()
        if now - last_print[0] < 0.10 and cur < total and status == "processing":
            return
        last_print[0] = now
        pct = cur / total if total else 1.0
        filled = int(bar_width * pct)
        bar = "=" * filled + "-" * (bar_width - filled)
        elapsed = now - start_time
        eta = ""
        if pct > 0.01 and elapsed > 1 and cur < total:
            remaining = elapsed / pct * (1 - pct)
            m, s = divmod(int(remaining), 60)
            eta = f" | ETA: {m}:{s:02d}"
        short_target = (target or rec.get("source_file_path", "") or rec.get("id", "")).replace("\\", "/")
        if len(short_target) > 52:
            short_target = "..." + short_target[-49:]
        line = (
            f"\r  [{bar}] {pct*100:4.0f}% | "
            f"{cur}/{total} | {status:22s} | {short_target}{eta}   "
        )
        sys.stderr.write(line)
        sys.stderr.flush()
        if cur == total and status != "processing":
            sys.stderr.write("\n")
            sys.stderr.flush()

    report = build_romfs_from_manifest(
        args.manifest,
        args.output_folder,
        source_romfs_folder=getattr(args, "source_romfs", ""),
        overwrite=getattr(args, "overwrite", False),
        progress_callback=_progress,
        only_archive_exts=getattr(args, "only_archive", []),
        only_texture_exts=getattr(args, "only_texture", []),
        only_supported_writers=getattr(args, "only_supported_writers", False),
        full_copy=getattr(args, "full_copy", False),
        allow_etc_transcode=not getattr(args, "no_etc_transcode", False),
        include_parent_patterns=getattr(args, "include_parent", []),
        exclude_parent_patterns=getattr(args, "exclude_parent", []),
        max_replacement_width=getattr(args, "max_replacement_width", 0),
        max_replacement_height=getattr(args, "max_replacement_height", 0),
        max_scale=getattr(args, "max_scale", 0.0),
        max_texture_bytes=getattr(args, "max_texture_bytes", 0),
        etc_quality=getattr(args, "etc_quality", "fast"),
        skip_unsafe_simple_replace=getattr(args, "skip_unsafe_simple_replace", False),
        skip_no_canvas_constraint=getattr(args, "skip_no_canvas_constraint", False),
        preserve_logical_size=not getattr(args, "no_preserve_logical_size", False),
        layout_aware_repack=not getattr(args, "no_layout_aware_repack", False),
        rebuild_all=getattr(args, "rebuild_all", False),
    )

    print(f"\nBuilt RomFS package: {report['output_folder']}")
    print(f"RomFS folder: {report['romfs_folder']}")
    print(f"Copied source RomFS: {report['source_romfs_folder']}")
    print(f"Copy mode: {report['copy_mode']} ({report['copied_files_count']} files)")
    print(f"Texture records: {report['texture_records']}")
    print(f"Replacement PNGs found: {report['planned_replacements']}")
    print(f"Applied replacements: {report['applied_replacements']}")
    print(f"Unchanged PNGs skipped: {report.get('unchanged_replacements_count', 0)}")
    print(f"Skipped by filters: {report.get('skipped_by_filter_count', 0)}")
    print(f"Failed replacements: {report['failed_replacements']}")
    print(f"Unsupported replacements: {report['unsupported_replacements_count']}")
    print(f"Missing replacements: {report['missing_replacements_count']}")
    size_summary = report.get("size_summary") or {}
    if size_summary:
        source_mb = size_summary.get("source_bytes", 0) / (1024 * 1024)
        output_mb = size_summary.get("output_bytes", 0) / (1024 * 1024)
        ratio = size_summary.get("growth_ratio")
        ratio_text = f"{ratio:.2f}x" if ratio is not None else "n/a"
        print(f"Output size: {output_mb:.1f} MB from {source_mb:.1f} MB ({ratio_text})")
    timings = report.get("timings_seconds") or {}
    if timings:
        print(
            "Timings: "
            f"filter {timings.get('manifest_filter', 0):.1f}s, "
            f"plan {timings.get('replacement_planning', 0):.1f}s, "
            f"copy {timings.get('source_copy', 0):.1f}s, "
            f"rebuild {timings.get('rebuild', 0):.1f}s, "
            f"size {timings.get('size_summary', 0):.1f}s, "
            f"total {timings.get('total', 0):.1f}s"
        )
    print(f"Report: {report['report_path']}")
    if report["unsupported_replacements"]:
        print(
            "\nNOTE: The output RomFS is a safe copy. Archive/container reinjection "
            "is reported but not yet written."
        )


def cmd_import_dump(args):
    setup_logging(args.verbose, args.quiet)
    dump_folder = args.dump_folder
    project_dir = args.project_dir

    if not os.path.isdir(dump_folder):
        logger.error(f"Dump folder not found: {dump_folder}")
        sys.exit(1)

    manifest_path = os.path.join(project_dir, "manifest.json")
    if not os.path.isfile(manifest_path):
        logger.error(f"No manifest.json in {project_dir}")
        sys.exit(1)

    with open(manifest_path, "r") as f:
        manifest = json.load(f)

    textures = manifest.get("textures", [])

    # Scan dump folder for PNG files
    dump_files = []
    for fname in os.listdir(dump_folder):
        if not fname.lower().endswith(".png"):
            continue
        info = _parse_dump_filename(fname)
        info["full_path"] = os.path.join(dump_folder, fname)
        dump_files.append(info)

    print(f"Found {len(dump_files)} dumped textures in {dump_folder}")

    # Try to match dump files to manifest textures by dimensions + format
    matched = 0
    unmatched_dumps = []

    for dump_info in dump_files:
        dump_w = dump_info.get("width", 0)
        dump_h = dump_info.get("height", 0)
        dump_hash = dump_info.get("hash", "")
        dump_fmt = dump_info.get("format", "")

        best_match = None
        for tex in textures:
            tw = tex.get("width", 0)
            th = tex.get("height", 0)
            if tw == dump_w and th == dump_h:
                # Prefer format match too
                if dump_fmt and dump_fmt.upper() in tex.get("detected_format", "").upper():
                    best_match = tex
                    break
                elif best_match is None:
                    best_match = tex

        if best_match and dump_hash:
            best_match["dump_hash"] = dump_hash
            matched += 1
        else:
            unmatched_dumps.append(dump_info)

    # Write updated manifest
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    # Write import report
    import_report = {
        "dump_folder": dump_folder,
        "dump_files_found": len(dump_files),
        "matched_to_manifest": matched,
        "unmatched": len(unmatched_dumps),
        "unmatched_files": [d.get("filename", "") for d in unmatched_dumps],
    }
    report_path = os.path.join(project_dir, "import_dump_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(import_report, f, indent=2)

    print(f"Matched {matched} of {len(dump_files)} dump files to manifest textures")
    print(f"Unmatched: {len(unmatched_dumps)}")
    print(f"Report: {report_path}")
    if matched > 0:
        print("Run 'build-pack' again to produce a mapped pack with real hashes.")


def _parse_dump_filename(fname: str) -> Dict[str, Any]:
    """
    Parse emulator-dumped texture filenames.

    Common patterns:
      tex1_256x256_DEADBEEF_ETC1_mip0.png
      DEADBEEF01234567.png
      0xDEADBEEF_256x256.png
    """
    info = {"filename": fname, "hash": "", "width": 0, "height": 0, "format": "", "mip": 0}

    base = fname.rsplit(".", 1)[0]
    parts = base.replace("-", "_").split("_")

    for part in parts:
        # Dimension pattern: NNNxNNN
        if "x" in part:
            try:
                wh = part.lower().split("x")
                if len(wh) == 2 and wh[0].isdigit() and wh[1].isdigit():
                    info["width"] = int(wh[0])
                    info["height"] = int(wh[1])
                    continue
            except ValueError:
                pass

        # Mip level
        if part.lower().startswith("mip"):
            try:
                info["mip"] = int(part[3:])
                continue
            except ValueError:
                pass

        # Format name
        if part.upper() in FORMAT_NAMES.values():
            info["format"] = part.upper()
            continue

        # Hash: hex string, at least 8 chars
        clean = part.lower().lstrip("0x")
        if len(clean) >= 8 and all(c in "0123456789abcdef" for c in clean):
            info["hash"] = clean
            continue

    # If the whole basename is a hex hash (Citra format)
    if not info["hash"] and len(base) >= 8:
        clean = base.lower().lstrip("0x")
        if all(c in "0123456789abcdef" for c in clean):
            info["hash"] = clean

    return info


# ──────────────────────────────────────────────
# CLI parser
# ──────────────────────────────────────────────

def build_parser():
    top = argparse.ArgumentParser(
        prog="3ds-tex-extract",
        description="Extract textures from 3DS ROMs or extracted RomFS folders for Azahar/Citra texture packs.",
    )
    sub = top.add_subparsers(dest="command")

    # --- scan ---
    p_scan = sub.add_parser("scan", help="Scan a ROM/RomFS folder and report contents")
    p_scan.add_argument("input", help="Path to ROM file or extracted RomFS folder")
    p_scan.add_argument("--scan-all", action="store_true")
    p_scan.add_argument("--verbose", action="store_true")
    p_scan.add_argument("--quiet", action="store_true")

    # --- extract ---
    p_ext = sub.add_parser("extract", help="Extract textures to PNG")
    p_ext.add_argument("input", help="Path to ROM file or extracted RomFS folder")
    p_ext.add_argument("-o", "--output", metavar="DIR")
    p_ext.add_argument("--scan-all", action="store_true")
    p_ext.add_argument("--dump-raw", action="store_true")
    p_ext.add_argument("--list-files", action="store_true")
    p_ext.add_argument("--dedup", action="store_true",
                       help="Skip writing duplicate textures (keep first copy only)")
    p_ext.add_argument("--report", action="store_true",
                       help="Write a machine-readable report.json to the output directory")
    p_ext.add_argument("--output-mode", choices=["normal", "azahar"], default="normal",
                       help="Output mode: 'normal' (default) or 'azahar' (Azahar/Citra texture pack layout)")
    p_ext.add_argument("--only-archive", action="append", default=[],
                       help="Only keep textures from parent files with this extension (repeatable or comma-separated, e.g. arc)")
    p_ext.add_argument("--only-texture", action="append", default=[],
                       help="Only keep textures whose texture/source extension matches (repeatable or comma-separated, e.g. bclim)")
    p_ext.add_argument("--only-supported-writers", action="store_true",
                       help="Only keep textures that have a supported build-romfs texture writer")
    p_ext.add_argument("--skip-unsafe-simple-replace", action="store_true",
                       help="Skip textures flagged unsafe for simple PNG/BCLIM replacement")
    p_ext.add_argument("--skip-no-canvas-constraint", action="store_true",
                       help="Skip textures without an explicit BCLYT pane canvas constraint")
    p_ext.add_argument("--verbose", action="store_true")
    p_ext.add_argument("--quiet", action="store_true")

    # --- report ---
    p_rep = sub.add_parser("report", help="Report on a previous extraction")
    p_rep.add_argument("project_dir", help="Extraction output directory")
    p_rep.add_argument("--verbose", action="store_true")
    p_rep.add_argument("--quiet", action="store_true")

    # --- build-pack ---
    p_pack = sub.add_parser("build-pack", help="Build Azahar/Citra texture pack")
    p_pack.add_argument("project_dir", help="Extraction output directory")
    p_pack.add_argument("--verbose", action="store_true")
    p_pack.add_argument("--quiet", action="store_true")

    # --- build-romfs ---
    p_romfs = sub.add_parser("build-romfs", help="Build a mod RomFS folder from manifest.json")
    p_romfs.add_argument("manifest", help="Path to extraction manifest.json")
    p_romfs.add_argument("output_folder", help="New output RomFS folder")
    p_romfs.add_argument("--source-romfs", default="",
                         help="Override source RomFS folder if manifest does not contain source.source_romfs_folder")
    p_romfs.add_argument("--overwrite", action="store_true",
                         help="Delete and recreate output folder if it already exists")
    p_romfs.add_argument("--only-archive", action="append", default=[],
                         help="Only rebuild textures from parent files with this extension (repeatable or comma-separated, e.g. arc)")
    p_romfs.add_argument("--only-texture", action="append", default=[],
                         help="Only rebuild textures whose texture/source extension matches (repeatable or comma-separated, e.g. bclim)")
    p_romfs.add_argument("--only-supported-writers", action="store_true",
                         help="Only rebuild textures that have a supported texture writer")
    p_romfs.add_argument("--full-copy", action="store_true",
                         help="Copy the entire source RomFS instead of only files referenced by the manifest/filter")
    p_romfs.add_argument("--no-etc-transcode", action="store_true",
                         help="Legacy compatibility option; ETC1/ETC1A4 are now encoded instead of transcoded")
    p_romfs.add_argument("--include-parent", action="append", default=[],
                         help="Only rebuild parent paths matching this glob/substr (repeatable or comma-separated)")
    p_romfs.add_argument("--exclude-parent", action="append", default=[],
                         help="Skip parent paths matching this glob/substr (repeatable or comma-separated)")
    p_romfs.add_argument("--max-replacement-width", type=int, default=0,
                         help="Skip replacement PNGs wider than this many pixels")
    p_romfs.add_argument("--max-replacement-height", type=int, default=0,
                         help="Skip replacement PNGs taller than this many pixels")
    p_romfs.add_argument("--max-scale", type=float, default=0.0,
                         help="Skip replacement PNGs scaled above this factor versus the original")
    p_romfs.add_argument("--max-texture-bytes", type=int, default=0,
                         help="Skip replacements whose estimated encoded texture payload exceeds this byte count")
    p_romfs.add_argument("--etc-quality", choices=("fast", "high"), default="fast",
                         help="ETC1/ETC1A4 encoder mode; fast is much quicker, high searches more")
    p_romfs.add_argument("--skip-unsafe-simple-replace", action="store_true",
                         help="Skip records flagged unsafe for simple PNG/BCLIM replacement")
    p_romfs.add_argument("--skip-no-canvas-constraint", action="store_true",
                         help="Skip records without an explicit BCLYT pane canvas constraint")
    p_romfs.add_argument("--no-preserve-logical-size", action="store_true",
                         help="Write BCLIM/BFLIM display size from replacement PNG instead of preserving original size")
    p_romfs.add_argument("--no-layout-aware-repack", action="store_true",
                         help="Disable BCLYT-aware HD BCLIM sizing for archives with readable layout canvases")
    p_romfs.add_argument("--rebuild-all", action="store_true",
                         help="Re-encode every manifest PNG, including files unchanged since extraction")
    p_romfs.add_argument("--verbose", action="store_true")
    p_romfs.add_argument("--quiet", action="store_true")

    # --- import-dump ---
    p_imp = sub.add_parser("import-dump", help="Import emulator texture dump")
    p_imp.add_argument("dump_folder", help="Folder with dumped PNGs")
    p_imp.add_argument("project_dir", help="Extraction output directory")
    p_imp.add_argument("--verbose", action="store_true")
    p_imp.add_argument("--quiet", action="store_true")

    return top


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        # Backward compat: treat first positional as ROM, run extract
        if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
            maybe_rom = sys.argv[1]
            if os.path.isfile(maybe_rom) or os.path.isdir(maybe_rom):
                sys.argv.insert(1, "extract")
                args = parser.parse_args()
            else:
                parser.print_help()
                sys.exit(1)
        else:
            parser.print_help()
            sys.exit(0)

    dispatch = {
        "scan": cmd_scan,
        "extract": cmd_extract,
        "report": cmd_report,
        "build-pack": cmd_build_pack,
        "build-romfs": cmd_build_romfs,
        "import-dump": cmd_import_dump,
    }

    handler = dispatch.get(args.command)
    if handler:
        try:
            handler(args)
        except EncryptedROMError as e:
            print(f"\n{'='*56}", file=sys.stderr)
            print(f"  ERROR: Encrypted ROM", file=sys.stderr)
            print(f"{'='*56}", file=sys.stderr)
            print(f"  This ROM is encrypted and cannot be extracted.", file=sys.stderr)
            print(f"  Decrypt it first using GodMode9 on your 3DS:", file=sys.stderr)
            print(f"", file=sys.stderr)
            print(f"    GodMode9 > [A] on game > Manage Title... > Decrypt File (SysNAND)", file=sys.stderr)
            print(f"    or: NCSD image options... > Decrypt image (0)", file=sys.stderr)
            print(f"", file=sys.stderr)
            print(f"  Guide: https://3ds.hacks.guide/godmode9-usage.html", file=sys.stderr)
            print(f"{'='*56}\n", file=sys.stderr)
            sys.exit(2)
        except ROMParseError as e:
            logger.error(str(e))
            sys.exit(1)
        except RuntimeError as e:
            logger.error(str(e))
            sys.exit(1)
        except Exception as e:
            logger.error(f"Fatal: {e}")
            if getattr(args, "verbose", False):
                import traceback
                traceback.print_exc()
            sys.exit(1)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
