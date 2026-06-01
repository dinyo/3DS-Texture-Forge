"""Archive rebuilders used by build-romfs.

The handlers follow the same plugin-ish shape as Kuriimu-style tooling: detect,
list entries, rebuild with replacement entries. They favor deterministic rebuilds
over in-place byte patching so resized assets can grow safely.
"""

import struct
from typing import Callable, Dict, Iterable, List, Tuple

from parsers.garc import is_garc, parse_garc
from parsers.narc import is_narc, parse_narc
from parsers.sarc import is_sarc, parse_sarc
from parsers.zar import is_zar, parse_zar
from parsers.darc import is_darc


class ArchiveWriteError(RuntimeError):
    pass


Entry = Tuple[str, bytes]


def replace_archive_member(data: bytes, member_path: str,
                           transform: Callable[[bytes], bytes]) -> bytes:
    member_path = member_path.replace("\\", "/").lstrip("/")
    handler = _handler_for(data)
    if handler is None:
        raise ArchiveWriteError("No archive writer for this container")

    entries = handler.list_entries(data)
    idx, rest = _find_entry(entries, member_path)
    if idx is None:
        raise ArchiveWriteError(f"Archive member not found: {member_path}")

    name, old = entries[idx]
    if rest:
        new = replace_archive_member(old, rest, transform)
    else:
        new = transform(old)
    entries[idx] = (name, new)
    return handler.rebuild(data, entries)


def replace_archive_members(data: bytes,
                            transforms: Iterable[Tuple[str, Callable[[bytes], bytes]]]) -> bytes:
    handler = _handler_for(data)
    if handler is None:
        raise ArchiveWriteError("No archive writer for this container")

    entries = handler.list_entries(data)
    changed = False
    for member_path, transform in transforms:
        clean_path = member_path.replace("\\", "/").lstrip("/")
        idx, rest = _find_entry(entries, clean_path)
        if idx is None:
            raise ArchiveWriteError(f"Archive member not found: {member_path}")

        name, old = entries[idx]
        if rest:
            new = replace_archive_member(old, rest, transform)
        else:
            new = transform(old)
        if new != old:
            changed = True
        entries[idx] = (name, new)

    return handler.rebuild(data, entries) if changed else data


def _find_entry(entries: List[Entry], path: str):
    norm = path.lower()
    for i, (name, _data) in enumerate(entries):
        n = name.replace("\\", "/").lstrip("/")
        if n.lower() == norm:
            return i, ""

    # Some archives contain names with slash paths. Prefer the longest matching
    # prefix so "folder/file.ctpk" is matched before "folder".
    matches = []
    for i, (name, _data) in enumerate(entries):
        n = name.replace("\\", "/").lstrip("/")
        prefix = n.rstrip("/") + "/"
        if norm.startswith(prefix.lower()):
            matches.append((len(prefix), i, path[len(prefix):]))
    if matches:
        _length, i, rest = sorted(matches, reverse=True)[0]
        return i, rest

    basename_matches = [
        i for i, (name, _data) in enumerate(entries)
        if name.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower() == norm.rsplit("/", 1)[-1]
    ]
    if len(basename_matches) == 1:
        return basename_matches[0], ""
    return None, ""


class SarcWriter:
    def matches(self, data: bytes) -> bool:
        return is_sarc(data)

    def list_entries(self, data: bytes) -> List[Entry]:
        return parse_sarc(data)

    def rebuild(self, original: bytes, entries: List[Entry]) -> bytes:
        bom = original[6:8]
        bo = ">" if bom == b"\xFE\xFF" else "<"
        hdr_len = struct.unpack_from(bo + "H", original, 4)[0]
        sfat_off = hdr_len
        hash_multiplier = struct.unpack_from(bo + "I", original, sfat_off + 8)[0] if sfat_off + 12 <= len(original) else 0x65
        original_nodes = _sarc_nodes(original, bo, sfat_off)
        if original_nodes and any(not node["has_name"] for node in original_nodes):
            raise ArchiveWriteError(
                "SARC contains hash-only entries; rebuilding would convert them "
                "to synthetic names and break hash-based lookup"
            )

        names = bytearray()
        name_offsets = []
        for name, _data in entries:
            name_offsets.append(len(names) // 4)
            names.extend(name.encode("utf-8") + b"\x00")
            while len(names) % 4:
                names.append(0)

        sfat_size = 0x0C + len(entries) * 0x10
        sfnt_size = _align(0x08 + len(names), 4)
        data_offset = _align(0x14 + sfat_size + sfnt_size, 0x80)

        data_blob = bytearray()
        nodes = []
        for (name, blob), name_off_words in zip(entries, name_offsets):
            start = _align(len(data_blob), 0x80)
            if start > len(data_blob):
                data_blob.extend(b"\x00" * (start - len(data_blob)))
            data_blob.extend(blob)
            end = len(data_blob)
            nodes.append((_sarc_hash(name, hash_multiplier), 0x01000000 | name_off_words, start, end))

        file_size = data_offset + len(data_blob)
        out = bytearray()
        out.extend(b"SARC")
        out.extend(struct.pack(bo + "H", 0x14))
        out.extend(bom if bom in (b"\xFE\xFF", b"\xFF\xFE") else b"\xFF\xFE")
        out.extend(struct.pack(bo + "I", file_size))
        out.extend(struct.pack(bo + "I", data_offset))
        out.extend(struct.pack(bo + "H", 0x0100))
        out.extend(b"\x00\x00")
        out.extend(b"SFAT")
        out.extend(struct.pack(bo + "H", 0x0C))
        out.extend(struct.pack(bo + "H", len(entries)))
        out.extend(struct.pack(bo + "I", hash_multiplier))
        for node in nodes:
            out.extend(struct.pack(bo + "IIII", *node))
        out.extend(b"SFNT")
        out.extend(struct.pack(bo + "H", 0x08))
        out.extend(b"\x00\x00")
        out.extend(names)
        while len(out) < data_offset:
            out.append(0)
        out.extend(data_blob)
        return bytes(out)


class NarcWriter:
    def matches(self, data: bytes) -> bool:
        return is_narc(data)

    def list_entries(self, data: bytes) -> List[Entry]:
        return parse_narc(data)

    def rebuild(self, original: bytes, entries: List[Entry]) -> bytes:
        bom = struct.unpack_from("<H", original, 4)[0]
        le = bom == 0xFFFE
        fmt = "<" if le else ">"
        fntb = _extract_narc_fntb(original, fmt) or _default_narc_fntb(fmt)

        image = bytearray()
        table = []
        for _name, blob in entries:
            start = _align(len(image), 4)
            if start > len(image):
                image.extend(b"\x00" * (start - len(image)))
            image.extend(blob)
            end = len(image)
            table.append((start, end))
        while len(image) % 4:
            image.append(0)

        btaf_size = 12 + len(entries) * 8
        gmif_size = 8 + len(image)
        file_size = 0x10 + btaf_size + len(fntb) + gmif_size
        out = bytearray()
        out.extend(original[:0x10])
        struct.pack_into(fmt + "I", out, 8, file_size)
        out.extend(b"BTAF")
        out.extend(struct.pack(fmt + "I", btaf_size))
        out.extend(struct.pack(fmt + "I", len(entries)))
        for start, end in table:
            out.extend(struct.pack(fmt + "II", start, end))
        out.extend(fntb)
        out.extend(b"GMIF")
        out.extend(struct.pack(fmt + "I", gmif_size))
        out.extend(image)
        return bytes(out)


class ZarWriter:
    def matches(self, data: bytes) -> bool:
        return is_zar(data)

    def list_entries(self, data: bytes) -> List[Entry]:
        return parse_zar(data)

    def rebuild(self, original: bytes, entries: List[Entry]) -> bytes:
        file_table_off = struct.unpack_from("<I", original, 0x10)[0]
        data_off = struct.unpack_from("<I", original, 0x14)[0]
        out = bytearray(original[:data_off])
        payload = bytearray()
        for i, (_name, blob) in enumerate(entries):
            entry_off = file_table_off + i * 8
            if entry_off + 4 <= len(out):
                struct.pack_into("<I", out, entry_off, len(blob))
            payload.extend(blob)
        out.extend(payload)
        struct.pack_into("<I", out, 0x04, len(out))
        return bytes(out)


class GarcWriter:
    def matches(self, data: bytes) -> bool:
        return is_garc(data)

    def list_entries(self, data: bytes) -> List[Entry]:
        return parse_garc(data)

    def rebuild(self, original: bytes, entries: List[Entry]) -> bytes:
        hdr_size = struct.unpack_from("<I", original, 4)[0]
        data_offset = struct.unpack_from("<I", original, 16)[0]
        fato_off = hdr_size
        fato_size = struct.unpack_from("<I", original, fato_off + 4)[0]
        fatb_off = fato_off + fato_size
        fatb_count = struct.unpack_from("<I", original, fatb_off + 8)[0]
        if fatb_count != len(entries):
            raise ArchiveWriteError("GARC entry count changed unexpectedly")

        out = bytearray(original[:data_offset])
        payload = bytearray()
        for i, (_name, blob) in enumerate(entries):
            start = _align(len(payload), 4)
            if start > len(payload):
                payload.extend(b"\x00" * (start - len(payload)))
            payload.extend(blob)
            end = len(payload)
            base = fatb_off + 12 + i * 16
            struct.pack_into("<III", out, base + 4, start, end, len(blob))
        out.extend(payload)
        if len(out) >= 0x10:
            struct.pack_into("<I", out, 0x0C, len(out))
        return bytes(out)


class DarcWriter:
    def matches(self, data: bytes) -> bool:
        return is_darc(data)

    def list_entries(self, data: bytes) -> List[Entry]:
        meta = _parse_darc_meta(data)
        return [(item["path"], item["data"]) for item in meta["files"]]

    def rebuild(self, original: bytes, entries: List[Entry]) -> bytes:
        meta = _parse_darc_meta(original)
        files = meta["files"]
        if len(files) != len(entries):
            raise ArchiveWriteError("DARC entry count changed unexpectedly")

        data_offset = meta["data_offset"]
        out = bytearray(original[:data_offset])
        payload = bytearray()

        for item, (_name, blob) in zip(files, entries):
            abs_off = data_offset + _align(len(payload), 0x80)
            if abs_off - data_offset > len(payload):
                payload.extend(b"\x00" * (abs_off - data_offset - len(payload)))
            payload.extend(blob)
            eoff = item["entry_offset"]
            struct.pack_into("<I", out, eoff + 4, abs_off)
            struct.pack_into("<I", out, eoff + 8, len(blob))

        out.extend(payload)
        struct.pack_into("<I", out, 0x0C, len(out))
        return bytes(out)


def _handler_for(data: bytes):
    for handler in _HANDLERS:
        if handler.matches(data):
            return handler
    return None


def _sarc_hash(name: str, multiplier: int) -> int:
    h = 0
    for ch in name.encode("utf-8"):
        h = (h * multiplier + ch) & 0xFFFFFFFF
    return h


def _extract_narc_fntb(data: bytes, fmt: str) -> bytes:
    fatb_size = struct.unpack_from(fmt + "I", data, 0x14)[0]
    fntb_off = 0x10 + fatb_size
    if fntb_off + 8 > len(data) or data[fntb_off:fntb_off + 4] != b"BTNF":
        return b""
    fntb_size = struct.unpack_from(fmt + "I", data, fntb_off + 4)[0]
    return data[fntb_off:fntb_off + fntb_size]


def _sarc_nodes(data: bytes, bo: str, sfat_off: int) -> List[Dict[str, int]]:
    if sfat_off + 0x0C > len(data) or data[sfat_off:sfat_off + 4] != b"SFAT":
        return []
    sfat_hdr_len = struct.unpack_from(bo + "H", data, sfat_off + 4)[0]
    node_count = struct.unpack_from(bo + "H", data, sfat_off + 6)[0]
    nodes_start = sfat_off + sfat_hdr_len
    nodes = []
    for i in range(node_count):
        node_off = nodes_start + i * 16
        if node_off + 16 > len(data):
            break
        attrs = struct.unpack_from(bo + "I", data, node_off + 4)[0]
        nodes.append({
            "has_name": (attrs >> 24) & 1,
            "name_offset_words": attrs & 0x00FFFFFF,
        })
    return nodes


def _default_narc_fntb(fmt: str) -> bytes:
    return b"BTNF" + struct.pack(fmt + "I", 0x10) + b"\x04\x00\x00\x00\x00\x00\x01\x00"


def _parse_darc_meta(data: bytes) -> Dict[str, object]:
    if not is_darc(data):
        raise ArchiveWriteError("Not a DARC archive")

    hdr_size = struct.unpack_from("<H", data, 6)[0]
    data_offset = struct.unpack_from("<I", data, 0x18)[0]
    if hdr_size + 12 > len(data):
        raise ArchiveWriteError("DARC header is truncated")

    num_entries = struct.unpack_from("<I", data, hdr_size + 8)[0]
    if num_entries < 1 or num_entries > 100000:
        raise ArchiveWriteError("DARC entry count is invalid")

    entry_table_end = hdr_size + num_entries * 12
    name_table_start = entry_table_end
    if name_table_start > len(data):
        raise ArchiveWriteError("DARC name table is out of bounds")

    def get_name(name_off: int) -> str:
        pos = name_table_start + name_off
        end = pos
        while end + 1 < len(data) and (data[end] != 0 or data[end + 1] != 0):
            end += 2
        if pos >= len(data) or end > len(data):
            return ""
        return data[pos:end].decode("utf-16-le", errors="replace")

    entries = []
    for idx in range(num_entries):
        eoff = hdr_size + idx * 12
        w0, w1, w2 = struct.unpack_from("<III", data, eoff)
        entries.append({
            "index": idx,
            "entry_offset": eoff,
            "is_dir": ((w0 >> 24) & 0xFF) == 0x01,
            "name": get_name(w0 & 0x00FFFFFF),
            "w1": w1,
            "w2": w2,
        })

    files = []

    def walk_dir(dir_idx: int, prefix: str):
        if dir_idx < 0 or dir_idx >= len(entries):
            return
        d = entries[dir_idx]
        start = max(d["w1"], dir_idx + 1)
        end = min(d["w2"], len(entries))
        child = start
        while child < end:
            item = entries[child]
            name = item["name"] or f"darc_{child:04d}"
            path = f"{prefix}/{name}" if prefix else name
            if item["is_dir"]:
                walk_dir(child, path)
                child = max(child + 1, min(item["w2"], end))
            else:
                file_abs = item["w1"]
                file_size = item["w2"]
                if file_size > 0 and file_abs + file_size <= len(data):
                    files.append({
                        "path": path,
                        "entry_offset": item["entry_offset"],
                        "data": data[file_abs:file_abs + file_size],
                    })
                child += 1

    walk_dir(0, "")
    return {"data_offset": data_offset, "files": files}


def _align(value: int, alignment: int) -> int:
    return (value + alignment - 1) // alignment * alignment


_HANDLERS = [SarcWriter(), NarcWriter(), ZarWriter(), GarcWriter(), DarcWriter()]
