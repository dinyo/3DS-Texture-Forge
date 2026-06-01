"""Single source of truth for rebuild/repack support.

Extraction support is intentionally broader than rebuild support. Rebuilds need
both a valid archive writer and a valid texture encoder/container writer.
"""

ARCHIVE_EXTRACTORS = {
    "sarc", "garc", "narc", "zar", "gar", "darc", "capcom_arc", "fe_arc",
    "cpk", "arc0", "xfsa", "l5_flat", "gfac", "gzip_container",
    "smash_dt_ls", "pokemon_pc",
}

ARCHIVE_WRITERS = {
    "sarc", "garc", "narc", "zar", "darc",
}

TEXTURE_EXTRACTORS = {
    "bch", "cgfx", "bflim", "ctpk", "ctxb", "cmb", "capcom_tex",
    "shinen_tex", "jimg", "gdb1", "imgc", "stex",
}

TEXTURE_WRITERS = {
    "bflim", "ctpk", "ctxb", "cmb", "jimg", "shinen_tex", "stex",
}

PIXEL_ENCODERS = {
    "RGBA8", "RGB8", "RGBA5551", "RGB565", "RGBA4",
    "LA8", "HILO8", "L8", "A8", "LA4", "L4", "A4",
    "ETC1", "ETC1A4",
}

PIXEL_ENCODER_NOTES = {
    "ETC1": "Conservative block encoder; keeps compressed size, lower quality than specialized compressors",
    "ETC1A4": "Conservative block encoder with 4-bit alpha; keeps compressed size",
}

TEXTURE_WRITER_NOTES = {
    "ctpk": "Conservative in-place rebuild; replacement texture data must fit the original CTPK data region",
    "bflim": "Writes replacement PNG dimensions into the texture header; BCLYT keeps layout canvas when available",
}

UNSUPPORTED_REPACK = {
    "archives": {
        "gar": "scanner-only extraction; no structural writer",
        "capcom_arc": "requires Capcom ARC recompression/table writer",
        "fe_arc": "requires Nintendo LZ recompression and entry table writer",
        "cpk": "requires CRI UTF table writer and CRILAYLA handling",
        "arc0": "requires Level-5 ARC0/XFSA structural writer",
        "xfsa": "requires Level-5 ARC0/XFSA structural writer",
        "l5_flat": "requires Level-5 flat archive writer and LZ recompression",
        "gfac": "requires GFAC/GFCP writer",
        "gzip_container": "planned wrapper; not yet globally wired",
        "smash_dt_ls": "requires dt/ls resource table and zlib recompression",
        "pokemon_pc": "texture section format can be encoded only inside owning container writer",
    },
    "textures": {
        "bch": "complex model/texture container writer not implemented",
        "cgfx": "complex NintendoWare graphics/BCMDL writer not implemented safely",
        "capcom_tex": "Capcom TEX variants need dedicated encoder",
        "gdb1": "paired .texturegdb/.texturebin writer not implemented",
        "imgc": "Level-5 IMGC tile-map compressor not implemented",
    },
    "pixel_formats": {},
}


def build_capability_summary():
    return {
        "archive_extractors": sorted(ARCHIVE_EXTRACTORS),
        "archive_writers": sorted(ARCHIVE_WRITERS),
        "texture_extractors": sorted(TEXTURE_EXTRACTORS),
        "texture_writers": sorted(TEXTURE_WRITERS),
        "pixel_encoders": sorted(PIXEL_ENCODERS),
        "pixel_encoder_notes": PIXEL_ENCODER_NOTES,
        "texture_writer_notes": TEXTURE_WRITER_NOTES,
        "unsupported_repack": UNSUPPORTED_REPACK,
    }
