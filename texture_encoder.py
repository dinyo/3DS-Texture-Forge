"""Encode RGBA PNG pixels back into PICA200 tiled texture bytes.

This intentionally covers the PICA formats used by many 3DS UI and texture
packages. ETC1/ETC1A4 are encoded with a conservative block encoder so rebuilt
BCLIM textures stay compressed instead of expanding to RGBA8.
"""

from typing import Optional, Tuple

from textures.decoder import (
    FMT_RGBA8, FMT_RGB8, FMT_RGBA5551, FMT_RGB565, FMT_RGBA4,
    FMT_LA8, FMT_HILO8, FMT_L8, FMT_A8, FMT_LA4, FMT_L4, FMT_A4,
    FMT_ETC1, FMT_ETC1A4, MORTON_TABLE, resolve_format,
)


SUPPORTED_ENCODE_FORMATS = {
    FMT_RGBA8, FMT_RGB8, FMT_RGBA5551, FMT_RGB565, FMT_RGBA4,
    FMT_LA8, FMT_HILO8, FMT_L8, FMT_A8, FMT_LA4, FMT_L4, FMT_A4,
    FMT_ETC1, FMT_ETC1A4,
}


class TextureEncodeError(RuntimeError):
    pass


def _luma(r: int, g: int, b: int) -> int:
    return max(0, min(255, int(round(0.299 * r + 0.587 * g + 0.114 * b))))


def _pack_pixel(r: int, g: int, b: int, a: int, fmt: int) -> bytes:
    if fmt == FMT_RGBA8:
        return bytes((a, b, g, r))
    if fmt == FMT_RGB8:
        return bytes((b, g, r))
    if fmt == FMT_RGBA5551:
        val = ((r >> 3) << 11) | ((g >> 3) << 6) | ((b >> 3) << 1) | (1 if a >= 128 else 0)
        return val.to_bytes(2, "little")
    if fmt == FMT_RGB565:
        val = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
        return val.to_bytes(2, "little")
    if fmt == FMT_RGBA4:
        val = ((r >> 4) << 12) | ((g >> 4) << 8) | ((b >> 4) << 4) | (a >> 4)
        return val.to_bytes(2, "little")
    if fmt == FMT_LA8:
        return bytes((a, _luma(r, g, b)))
    if fmt == FMT_HILO8:
        return bytes((r, g))
    if fmt == FMT_L8:
        return bytes((_luma(r, g, b),))
    if fmt == FMT_A8:
        return bytes((a,))
    if fmt == FMT_LA4:
        return bytes((((_luma(r, g, b) >> 4) << 4) | (a >> 4),))
    raise TextureEncodeError(f"Unsupported byte-packed format 0x{fmt:X}")


def encode_png_to_pica(
    png_path: str,
    fmt: int,
    storage_width: Optional[int] = None,
    storage_height: Optional[int] = None,
    etc_quality: str = "fast",
) -> Tuple[bytes, int, int]:
    fmt = resolve_format(fmt)
    if fmt not in SUPPORTED_ENCODE_FORMATS:
        raise TextureEncodeError(f"Unsupported PICA format 0x{fmt:X}")

    from PIL import Image

    with Image.open(png_path) as img:
        rgba = img.convert("RGBA")
        width, height = rgba.size
        encode_width = storage_width or width
        encode_height = storage_height or height
        if encode_width < width or encode_height < height:
            raise TextureEncodeError(
                f"storage dimensions {encode_width}x{encode_height} are smaller than PNG {width}x{height}"
            )
        px = rgba.load()

        if fmt in (FMT_ETC1, FMT_ETC1A4):
            return _encode_etc_texture(
                px, width, height, encode_width, encode_height, fmt,
                quality=etc_quality,
            ), width, height

        tiles_x = (encode_width + 7) // 8
        tiles_y = (encode_height + 7) // 8

        if fmt in (FMT_L4, FMT_A4):
            nibbles = []
            for tile_y in range(tiles_y):
                for tile_x in range(tiles_x):
                    for morton_idx in range(64):
                        local_x, local_y = _INV_MORTON[morton_idx]
                        x = tile_x * 8 + local_x
                        y = tile_y * 8 + local_y
                        if x < width and y < height:
                            r, g, b, a = px[x, y]
                        else:
                            r, g, b, a = 0, 0, 0, 0
                        nibbles.append((_luma(r, g, b) >> 4) if fmt == FMT_L4 else (a >> 4))

            out = bytearray()
            for i in range(0, len(nibbles), 2):
                lo = nibbles[i]
                hi = nibbles[i + 1] if i + 1 < len(nibbles) else 0
                out.append((hi << 4) | lo)
            return bytes(out), width, height

        out = bytearray()
        for tile_y in range(tiles_y):
            for tile_x in range(tiles_x):
                for morton_idx in range(64):
                    local_x, local_y = _INV_MORTON[morton_idx]
                    x = tile_x * 8 + local_x
                    y = tile_y * 8 + local_y
                    if x < width and y < height:
                        r, g, b, a = px[x, y]
                    else:
                        r, g, b, a = 0, 0, 0, 0
                    out.extend(_pack_pixel(r, g, b, a, fmt))

        return bytes(out), width, height


_INV_MORTON = [None] * 64
for _ly in range(8):
    for _lx in range(8):
        _INV_MORTON[MORTON_TABLE[_ly * 8 + _lx]] = (_lx, _ly)


_ETC1_MODIFIER_TABLE = [
    (2, 8),
    (5, 17),
    (9, 29),
    (13, 42),
    (18, 60),
    (24, 80),
    (33, 106),
    (47, 183),
]


def _encode_etc_texture(px, width: int, height: int, encode_width: int,
                        encode_height: int, fmt: int, quality: str = "fast") -> bytes:
    out = bytearray()
    high_quality = str(quality).lower() in {"hq", "high", "slow", "best"}

    for bx, by in _iterate_etc_blocks_morton(encode_width, encode_height):
        block = []
        alpha = []
        for y in range(4):
            row = []
            alpha_row = []
            for x in range(4):
                src_x = bx * 4 + x
                src_y = by * 4 + y
                if src_x < width and src_y < height:
                    r, g, b, a = px[src_x, src_y]
                else:
                    r, g, b, a = 0, 0, 0, 0
                row.append((r, g, b))
                alpha_row.append(a)
            block.append(row)
            alpha.append(alpha_row)

        if fmt == FMT_ETC1A4:
            out.extend(_encode_etc1a4_alpha(alpha))
        out.extend(_encode_etc1_block(block, high_quality=high_quality))
    return bytes(out)


def _encode_etc_texture_fast(px, width: int, height: int, encode_width: int,
                             encode_height: int, fmt: int) -> bytes:
    out = bytearray()
    for bx, by in _iterate_etc_blocks_morton(encode_width, encode_height):
        sums = [[0, 0, 0, 0], [0, 0, 0, 0]]
        alpha_value = 0
        for py in range(4):
            for px_i in range(4):
                src_x = bx * 4 + px_i
                src_y = by * 4 + py
                if src_x < width and src_y < height:
                    r, g, b, a = px[src_x, src_y]
                else:
                    r, g, b, a = 0, 0, 0, 0
                sub = 0 if px_i < 2 else 1
                sums[sub][0] += r
                sums[sub][1] += g
                sums[sub][2] += b
                sums[sub][3] += 1
                if fmt == FMT_ETC1A4:
                    alpha_idx = px_i * 4 + py
                    nibble = max(0, min(15, int((a + 8) // 17)))
                    alpha_value |= nibble << (alpha_idx * 4)

        if fmt == FMT_ETC1A4:
            out.extend(alpha_value.to_bytes(8, "little"))
        out.extend(_encode_etc1_block_solid(sums))
    return bytes(out)


def _encode_etc1_block_solid(sums) -> bytes:
    # ETC1 has no zero modifier. Table 0/index 0 applies +2, so bias the
    # quantized base down slightly to land closer to the average color.
    colors = []
    for r_sum, g_sum, b_sum, count in sums:
        count = max(1, count)
        colors.append((
            _quant4((r_sum / count) - 2),
            _quant4((g_sum / count) - 2),
            _quant4((b_sum / count) - 2),
            0,
        ))
    r1, g1, b1, table1 = colors[0]
    r2, g2, b2, table2 = colors[1]
    word1 = (
        (r1 << 28) | (r2 << 24) |
        (g1 << 20) | (g2 << 16) |
        (b1 << 12) | (b2 << 8) |
        (table1 << 5) | (table2 << 2)
    )
    return (word1 << 32).to_bytes(8, "little")


def _iterate_etc_blocks_morton(width: int, height: int):
    block_w = (width + 3) // 4
    block_h = (height + 3) // 4
    macro_w = (block_w + 1) // 2
    macro_h = (block_h + 1) // 2
    for macro_y in range(macro_h):
        for macro_x in range(macro_w):
            for sub_x, sub_y in ((0, 0), (1, 0), (0, 1), (1, 1)):
                bx = macro_x * 2 + sub_x
                by = macro_y * 2 + sub_y
                if bx < block_w and by < block_h:
                    yield bx, by


def _encode_etc1a4_alpha(alpha) -> bytes:
    value = 0
    for py in range(4):
        for px in range(4):
            alpha_idx = px * 4 + py
            nibble = max(0, min(15, int(round(alpha[py][px] / 17.0))))
            value |= nibble << (alpha_idx * 4)
    return value.to_bytes(8, "little")


def _encode_etc1_block(block, high_quality: bool = False) -> bytes:
    if high_quality:
        best = None
        for flip in (0, 1):
            sub_a, sub_b = _etc_subblocks(block, flip)
            err_a, word_a, idx_a = _encode_etc_subblock(sub_a, high_quality=True)
            err_b, word_b, idx_b = _encode_etc_subblock(sub_b, high_quality=True)
            err = err_a + err_b
            if best is None or err < best[0]:
                best = (err, flip, word_a, word_b, idx_a, idx_b)
    else:
        # Fast mode prioritizes build speed. UI textures are usually arranged
        # as small icons/tiles where a fixed vertical split is acceptable, and
        # it avoids a variance pass over every 4x4 block.
        flip = 0
        sub_a, sub_b = _etc_subblocks(block, flip)
        err_a, word_a, idx_a = _encode_etc_subblock(sub_a, high_quality=False)
        err_b, word_b, idx_b = _encode_etc_subblock(sub_b, high_quality=False)
        best = (err_a + err_b, flip, word_a, word_b, idx_a, idx_b)

    _err, flip, word_a, word_b, idx_a, idx_b = best
    r1, g1, b1, table1 = word_a
    r2, g2, b2, table2 = word_b
    word1 = (
        (r1 << 28) | (r2 << 24) |
        (g1 << 20) | (g2 << 16) |
        (b1 << 12) | (b2 << 8) |
        (table1 << 5) | (table2 << 2) |
        flip
    )

    word2 = 0
    for py in range(4):
        for px in range(4):
            sub = 0 if ((px < 2) if flip == 0 else (py < 2)) else 1
            idx = idx_a[(px, py)] if sub == 0 else idx_b[(px, py)]
            bit_pos = px * 4 + py
            lsb = idx & 1
            msb = (idx >> 1) & 1
            word2 |= lsb << bit_pos
            word2 |= msb << (bit_pos + 16)

    return ((word1 << 32) | word2).to_bytes(8, "little")


def _etc_subblocks(block, flip: int):
    left_or_top = []
    right_or_bottom = []
    for py in range(4):
        for px in range(4):
            item = (px, py, block[py][px])
            if (px < 2) if flip == 0 else (py < 2):
                left_or_top.append(item)
            else:
                right_or_bottom.append(item)
    return left_or_top, right_or_bottom


def _encode_etc_subblock(items, high_quality: bool):
    avg = [0.0, 0.0, 0.0]
    for _px, _py, (r, g, b) in items:
        avg[0] += r
        avg[1] += g
        avg[2] += b
    count = max(1, len(items))
    avg = [v / count for v in avg]

    r4 = _quant4(avg[0])
    g4 = _quant4(avg[1])
    b4 = _quant4(avg[2])
    base = (_expand4(r4), _expand4(g4), _expand4(b4))

    if high_quality:
        best = None
        tables = range(len(_ETC1_MODIFIER_TABLE))
    else:
        spread = _estimate_luma_spread(items)
        table_idx = 0
        for i, (_small, large) in enumerate(_ETC1_MODIFIER_TABLE):
            if large * 2 <= spread + 16:
                table_idx = i
        tables = (table_idx,)

    for table_idx in tables:
        err, indices = _choose_etc_indices(items, base, _ETC1_MODIFIER_TABLE[table_idx])
        if high_quality:
            if best is None or err < best[0]:
                best = (err, table_idx, indices)
        else:
            best = (err, table_idx, indices)

    err, table_idx, indices = best
    return err, (r4, g4, b4, table_idx), indices


def _estimate_luma_spread(items) -> int:
    values = []
    for _px, _py, (r, g, b) in items:
        values.append(_luma(r, g, b))
    return (max(values) - min(values)) if values else 0


def _choose_etc_indices(items, base, modifiers):
    choices = (
        (0, modifiers[0]),
        (1, modifiers[1]),
        (2, -modifiers[0]),
        (3, -modifiers[1]),
    )
    total = 0
    indices = {}
    for px, py, (r, g, b) in items:
        best = None
        for idx, mod in choices:
            cr = _clamp(base[0] + mod)
            cg = _clamp(base[1] + mod)
            cb = _clamp(base[2] + mod)
            err = (r - cr) * (r - cr) + (g - cg) * (g - cg) + (b - cb) * (b - cb)
            if best is None or err < best[0]:
                best = (err, idx)
        total += best[0]
        indices[(px, py)] = best[1]
    return total, indices


def _quant4(value: float) -> int:
    return max(0, min(15, int(round(value / 17.0))))


def _expand4(value: int) -> int:
    return (value << 4) | value


def _clamp(value: int) -> int:
    return max(0, min(255, int(round(value))))
