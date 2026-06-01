# 3DS Texture Forge: v1.1

**by ZoomiesZaggy · March 2026**

---

> **A note on how this was built:** This tool was developed entirely with Claude (Anthropic's AI assistant). Weeks of diligent prompting, oversight, and iteration. Not a weekend vibe-coding session. I want to be upfront about that because the community has complicated feelings about AI, and those feelings are valid. My position: AI writing code is a tool, the same way a compiler is a tool. AI generating art is a different conversation, one I personally land on the opposite side of. Which is exactly why this tool exists. It extracts raw source textures from 3DS ROMs so that artists, modders, and preservationists can do the painstaking, skilled, human work of redrawing, remastering, and reimagining them properly. **The extraction is automated. The artistry is yours.**

---

## Why this exists

The AYN Thor changed things. A handheld powerful enough to run Azahar at full speed, in a clamshell form factor (two screens, the way Nintendo intended) finally made 3DS games feel like a platform worth investing in again rather than a museum piece. And Azahar's custom texture support means you can actually do something with that hardware: replace the original 240p assets with hand-crafted high-resolution replacements and experience games like Ocarina of Time 3D, Fire Emblem Awakening, or Pokémon X the way they were always trying to look, constrained only by a 2011 GPU.

The problem is that getting those original textures out of a ROM was either impossible, broken, or required stitching together three different tools none of which agreed on format. 3DS Texture Forge exists to fix that. Drop in a decrypted ROM, get a folder of PNGs. That's it.

---

## Download

Download the latest release from the [Releases page](https://github.com/ZoomiesZaggy/3DS-Texture-Forge/releases).

**Windows:**
- **3DS Texture Forge.exe** -- GUI app (recommended)
- **3ds-tex-extract.exe** -- Command-line tool

**Linux x86_64:**
- **3DS-Texture-Forge-linux** -- GUI app
- **3ds-tex-extract-linux** -- Command-line tool (make executable with `chmod +x`)

No installation needed. Just download and run.

---

## How to Use (GUI)

1. **Get a decrypted 3DS ROM** -- Use GodMode9 to dump and decrypt your game
2. **Open 3DS Texture Forge** -- Double-click the .exe
3. **Drop your ROM file** onto the window (or click Browse)
4. **Click "Extract Textures"** -- Wait 10-600 seconds depending on game size
5. **Click "Open Output Folder"** -- Your textures are there as .png files

---

## How to Use (CLI)

```bash
# Basic extraction
python main.py extract "game.3ds" -o output_folder

# Extract from an already-unpacked RomFS folder
python main.py extract "romfs_folder/" -o output_folder

# With deduplication (saves disk space)
python main.py extract "game.3ds" -o output_folder --dedup

# Generate machine-readable report
python main.py extract "game.3ds" -o output_folder --report

# Scan ROM contents without extracting
python main.py scan "game.3ds" --verbose

# Scan an extracted RomFS folder
python main.py scan "romfs_folder/" --verbose

# Deep scan (process all files, not just known extensions)
python main.py extract "game.3ds" --scan-all

# Extract only textures whose parent/source file is .arc
python main.py extract "romfs_folder/" -o output_folder --only-archive arc

# Extract only BCLIM textures, regardless of parent archive
python main.py extract "romfs_folder/" -o output_folder --only-texture bclim

# Extract only BCLIM textures inside .arc parents
python main.py extract "romfs_folder/" -o output_folder --only-archive arc --only-texture bclim

# Extract only textures that build-romfs can currently repack
python main.py extract "romfs_folder/" -o output_folder --only-supported-writers

# Extract only textures that pass simple RomFS replacement diagnostics
python main.py extract "romfs_folder/" -o output_folder --skip-unsafe-simple-replace --skip-no-canvas-constraint

# Build a new mod RomFS folder from an extraction manifest
python main.py build-romfs "output_folder/manifest.json" "mod_romfs/"

# Rebuild over an existing mod_romfs folder
python main.py build-romfs "output_folder/manifest.json" "mod_romfs/" --overwrite

# Re-encode every extracted PNG, including ones unchanged since extraction
python main.py build-romfs "output_folder/manifest.json" "mod_romfs/" --rebuild-all --overwrite

# Rebuild only .arc-backed textures
python main.py build-romfs "output_folder/manifest.json" "mod_romfs/" --only-archive arc --overwrite

# Rebuild only .bclim textures
python main.py build-romfs "output_folder/manifest.json" "mod_romfs/" --only-texture bclim --overwrite

# Rebuild only records that have supported texture writers
python main.py build-romfs "output_folder/manifest.json" "mod_romfs/" --only-supported-writers --overwrite

# Skip records flagged risky by archive/layout diagnostics
python main.py build-romfs "output_folder/manifest.json" "mod_romfs/" --skip-unsafe-simple-replace --overwrite

# Skip records that do not have a readable BCLYT pane canvas
python main.py build-romfs "output_folder/manifest.json" "mod_romfs/" --skip-no-canvas-constraint --overwrite

# Combine the safety filters for a smaller and faster rebuild
python main.py build-romfs "output_folder/manifest.json" "mod_romfs/" --skip-unsafe-simple-replace --skip-no-canvas-constraint --overwrite

# ETC1/ETC1A4 BCLIM textures are preserved as compressed ETC by default
python main.py build-romfs "output_folder/manifest.json" "mod_romfs/" --only-texture bclim --overwrite

# BCLIM/BFLIM replacements write the replacement PNG dimensions into the
# texture header. When readable BCLYT layout metadata exists, the layout pane
# keeps the original on-screen canvas.
python main.py build-romfs "output_folder/manifest.json" "mod_romfs/" --only-texture bclim --no-preserve-logical-size --overwrite

# Disable the default BCLYT-aware archive behavior while testing
python main.py build-romfs "output_folder/manifest.json" "mod_romfs/" --only-texture bclim --no-layout-aware-repack --overwrite

# Use the slower ETC search mode if you want better compression quality
python main.py build-romfs "output_folder/manifest.json" "mod_romfs/" --only-texture bclim --etc-quality high --overwrite

# Build only one parent archive while testing crashes
python main.py build-romfs "output_folder/manifest.json" "mod_romfs/" --include-parent "Layout/Title*" --overwrite

# Skip very large replacement payloads while testing memory-sensitive scenes
python main.py build-romfs "output_folder/manifest.json" "mod_romfs/" --max-texture-bytes 1048576 --overwrite

# Copy the whole source RomFS instead of only manifest-referenced files
python main.py build-romfs "output_folder/manifest.json" "mod_romfs/" --full-copy --overwrite
```

`build-romfs` never overwrites the original RomFS folder. It creates a build
package with rebuilt files under `mod_romfs/romfs/`, imports replacement PNGs
where writers are available, and writes a detailed `mod_romfs/report.json`.
The output path must not overlap the source RomFS path. By default it copies
only files whose PNGs changed since extraction after filters are applied; use
`--rebuild-all` to force every PNG through the encoder, or `--full-copy` to
copy the entire source RomFS.
For archive BCLIM/BFLIM textures with readable `.bclyt` pane constraints,
layout-aware repack is enabled by default: the rebuilt texture stores the
replacement PNG's physical dimensions while BCLYT keeps the logical on-screen
canvas. Re-extract to refresh `bclan_animation_types` and layout constraint
metadata before using this on older manifests.
CTPK rebuild is intentionally conservative: replacements that fit inside the
original texture data region can be written in place, while replacements that
would shift later CTPK sections are reported as unsupported instead of producing
a file with stale internal offsets.
Extraction manifests use schema v4 with root-level `source`, `extracted_archive`
(unique extensions/types such as `arc` and `bclim`), `parser_used`, and
`rebuild_compatibility` fields for rebuild/debugging. Compatibility data is
kept as compact counts/type summaries so large projects do not bloat the
manifest with thousands of archive paths.
For archive-member textures, each `rebuild` block can also include
`archive_context` and `simple_png_replace_safe`. These flag sibling files such
as `.bclan`, `.bcmata`, model containers, fonts, and animations that may make a
simple PNG/BCLIM replacement visually unsafe even when repacking succeeds.
`.bclyt` is treated as useful layout context, not unsafe by itself. The root
`simple_replace_risk_summary` aggregates those warnings.
When a sibling `.bclyt` can be read, records may also include
`layout_constraints` and `layout_constraint_status`, which show the pane canvas
that references the texture. This is diagnostic only: runtime game code and
`.bclan` animations can still resize or override the layout after load.

Current rebuild writers:
- Texture containers: CTPK, STEX, Shin'en TEX CTR, BFLIM/BCLIM
- Archives: SARC, NARC, ZAR, GARC, darc
- Pixel encoding: uncompressed PICA200 formats (RGBA8, RGB8, RGBA5551,
  RGB565, RGBA4, LA8, HILO8, L8, A8, LA4, L4, A4)

ETC1/ETC1A4 are encoded directly for BCLIM/BFLIM imports, so compressed UI
textures stay close to their expected size instead of expanding to RGBA8.
The encoder is conservative and prioritizes size/compatibility over maximum
visual quality. Parser-only/scan-only containers are reported as unsupported
instead of being written incorrectly.

---

## Azahar/Citra Output Mode

Output textures directly in Azahar/Citra custom texture pack format:

```bash
python main.py extract "game.3ds" -o textures/ --output-mode azahar
```

This creates files named `tex1_<W>x<H>_<CityHash64>_<fmt>_mip0.png` plus a `pack.json` in a `<TitleID>/` subdirectory. The `pack.json` is required so Azahar uses the new hash format; without it, Azahar falls back to legacy hashes and dumped filenames will not match.

Runtime dumps are still the safest source of truth for final packs, because Azahar hashes the actual GPU upload bytes. If a static extraction still does not match a dump, use the import flow below.

For manual texture pack building:

```bash
python main.py extract "game.3ds" -o project/
python main.py import-dump ~/azahar/dump/textures/TITLEID project/
python main.py build-pack project/
```

---

## Supported Games

| Game | Textures | Quality | Key Formats |
|------|----------|---------|-------------|
| Pokemon Mystery Dungeon: Gates to Infinity | 164,322 | 99% | GARC, BCH |
| Pokemon X / Y | 127,074 | 98% | GARC, BCH, ETC1 |
| Pokemon Ultra Sun / Ultra Moon | 23,674 | 97% | GARC, PC v5 |
| Kid Icarus: Uprising | 58,210 | 98% | darc, BCH |
| Kirby: Triple Deluxe | 54,377 | 98% | CGFX, ETC1A4 |
| Pokemon Omega Ruby / Alpha Sapphire | 36,496 | 96% | GARC, BCH, ETC1 |
| Monster Hunter 4 Ultimate | 25,685 | 99% | Capcom ARC, TEX |
| Animal Crossing: New Leaf | 21,517 | 94% | SARC, BCH |
| Tomodachi Life | 16,651 | 97% | BCH, CGFX |
| Kirby: Planet Robobot | 16,591 | 97% | CGFX, ETC1A4 |
| Professor Layton vs. Phoenix Wright | 5,417 | 90% | ARC0, IMGC |
| Layton's Mystery Journey | 5,565 | 90% | ARC0, IMGC |
| Professor Layton: Azran Legacy | 2,882 | 90% | ARC0, IMGC |
| Professor Layton: Miracle Mask | 2,183 | 90% | XFSA, IMGC |
| Fantasy Life | 10,254 | -- | Level-5 flat, CGFX |
| Yoshi's Woolly World | 4,676 | -- | GFAC, BCH |
| Kirby's Extra Epic Yarn | 3,359 | -- | GFAC, BCH |
| Conception II | 3,074 | -- | gzip-CTPK |
| Zero Time Dilemma | 2,490 | -- | gzip-CTPK |
| Fire Emblem: Awakening | 10,295 | 87% | FE ARC, BCH |
| Fire Emblem Fates | 10,000+ | -- | FE ARC, BCH |
| Fire Emblem Echoes | 10,000+ | -- | FE ARC, BCH |
| Bravely Default | 11,908 | 96% | BCH, ETC1 |
| Dragon Quest VII | 10,000+ | -- | BCH, CTPK |
| Dragon Quest VIII | 15,000+ | -- | BCH, CTPK |
| Picross 3D: Round 2 | 12,631 | -- | BCH, ETC1 |
| Hatsune Miku: Project Mirai DX | 4,982 | -- | BCH, CGFX |
| Theatrhythm Final Fantasy | 6,966 | 91% | BCH, ETC1A4 |
| Super Mario 3D Land | 6,097 | -- | NARC, CGFX |
| Zelda: A Link Between Worlds | 18,000+ | -- | SARC, BCH |
| Zelda: Ocarina of Time 3D | 3,584 | 94% | ZAR, CMB, CTXB |
| Zelda: Majora's Mask 3D | 1,780 | -- | GAR, CMB, CTXB |
| Metal Gear Solid: Snake Eater 3D | 1,744 | -- | BCH, CGFX |
| Castlevania: Mirror of Fate | 2,363 | -- | BCH, bctex |
| RE: Revelations | 5,742 | -- | Capcom ARC, TEX |
| RE: The Mercenaries 3D | 2,151 | -- | Capcom ARC, TEX |
| Persona Q | 700+ | -- | CPK, BCH |
| Super Smash Bros. 3DS | 5,000+ | -- | dt/ls, BCH |
| Star Fox 64 3D | 500+ | -- | GDB1, BCH |
| Dead or Alive Dimensions | 4,000+ | -- | BCH |
| Nano Assault | 638 | 92% | Shin'en TEX |
| Corpse Party | 2,659 | 81% | BCH, ETC1A4 |
| Mario Kart 7 | 2,770 | 96% | CGFX, ETC1 |

200+ games supported in total. Many titles not listed above will also work.

---

## Quality Reports

Every extraction generates `quality_report.json` and `quality_report.txt` with:

- Total/valid/suspicious texture counts
- Quality score (valid / total ratio)
- Breakdown by suspicion type (solid color, low variance, extreme brightness, bad dimensions)
- Normal map detection (HILO8 format)
- Format distribution

---

## Supported Formats

### ROM Containers
- NCSD (.3ds cartridge dumps)
- CIA (.cia installable titles)
- NCCH, RomFS (internal containers)
- Extracted RomFS folders

### Extraction Archive Formats
- SARC / GARC / NARC (Nintendo archives)
- ZAR / GAR (Grezzo archives - Zelda)
- darc (Nintendo Data ARChive)
- Capcom MT Framework ARC
- Fire Emblem ARC
- CRI CPK (Persona Q, 7th Dragon)
- Level-5 ARC0 (Layton, Yo-Kai Watch)
- Level-5 XFSA (Professor Layton)
- Level-5 flat archive (Fantasy Life)
- GFAC (Good-Feel archive - Kirby, Yoshi)
- Spike Chunsoft gzip-CTPK container
- Smash Bros dt/ls archives
- Pokemon PC v5/v11 section format

### Extraction Texture Formats
- All 14 PICA200 GPU formats (RGBA8, RGB8, RGB565, RGBA4, ETC1, ETC1A4, etc.)
- BCH (Binary CTR H3D textures)
- CGFX (NintendoWare graphics)
- BFLIM / BCLIM (UI textures)
- CTPK (CTR Texture Package)
- CTXB / CMB (Grezzo containers)
- Capcom MT Framework TEX
- Shin'en TEX CTR
- jIMG (Bandai Namco)
- GDB1 (texture database)
- IMGC (Level-5)
- STEX

### Repack / build-romfs Writers

Repacking is intentionally stricter than extraction. The tool only writes formats
where it has a real archive/container writer and a compatible pixel encoder.

Archive writers:
- SARC / GARC / NARC
- ZAR
- darc

Texture/container writers:
- BFLIM / BCLIM
- CTPK
- CTXB / CMB
- Shin'en TEX CTR
- jIMG
- STEX

Pixel encoders:
- RGBA8, RGB8, RGBA5551, RGB565, RGBA4
- LA8, HILO8, L8, A8, LA4, L4, A4
- ETC1, ETC1A4

Extraction-only for now:
- GAR, Capcom ARC, Fire Emblem ARC, CPK, ARC0/XFSA, Level-5 flat, GFAC,
  gzip-CTPK, Smash dt/ls, Pokemon PC sections
- BCH, CGFX/BCMDL, Capcom TEX, GDB1, IMGC

### Compression
- Nintendo LZ10/LZ11/LZ13
- BLZ (backward LZSS)
- Yaz0/SZS
- CRILAYLA (CRI streaming)
- GFCP (Good-Feel compression)
- zlib/DEFLATE
- gzip

---

## Known Limitations

- ROMs must be decrypted. Use GodMode9 to decrypt if needed
- All 15 LEGO 3DS games: TT Games FUSE format requires executable disassembly, confirmed out of scope
- Ubisoft titles (Ghost Recon etc.): MAGM engine, confirmed dead end
- Yo-Kai Watch quality is ~80% due to a Huffman decoder edge case in Level-5 IMGC
- Luigi's Mansion: Dark Moon: accessible with `--scan-all` but not default pipeline
- Mega Man Legacy Collection: 3DST format uses proprietary compression

---

## Requirements

- A decrypted 3DS ROM file (.3ds or .cia) or an extracted RomFS folder
- **Windows**: Download .exe from Releases (no setup needed)
- **Linux/Mac**: Python 3.10+ (see below)

---

## Running on Linux / Mac ARM

### Running from source (Linux, Mac ARM M1/M2/M3/M4)

Requirements: Python 3.10+, pip

```bash
# Linux GUI prerequisite
sudo apt-get install python3-tk    # Debian/Ubuntu (only needed for GUI)
sudo pacman -S tk                  # Arch

# Clone and install
git clone https://github.com/ZoomiesZaggy/3DS-Texture-Forge.git
cd 3DS-Texture-Forge
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Run
python main.py --help      # CLI
python gui_entry.py        # GUI
```

### Platform support matrix

| Platform | GUI | CLI | Pre-built binary |
|---|---|---|---|
| Windows x64 | Yes | Yes | Yes -- download .exe |
| Linux x86_64 | Yes | Yes | Yes -- download binary |
| Mac ARM (M-series) | Yes | Yes | Run `scripts/build_mac.sh` |
| Intel Mac | Not supported | | |

---

## Building from Source (Windows)

```
pip install PySide6 Pillow numpy
python gui_entry.py                             # Run GUI
python main.py extract game.3ds -o output/     # Run CLI
```

To build .exe files:

```
pip install pyinstaller
build.bat
```

---

## Bug reports

Found a bug? [Open an issue](https://github.com/ZoomiesZaggy/3DS-Texture-Forge/issues/new) and include your ROM filename, extracted texture count, and a screenshot of what looks wrong.

---

## License

MIT
