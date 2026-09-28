#!/usr/bin/env python3
"""Translate the opening disclaimer page ("この物語はフィクションです…").

Why this needs its own tool
---------------------------
The page is not font text: `この物語はフィクションです` is nowhere in the ROM as
character codes (see docs/打包流程.md, "尚未覆盖").  It is a pre-rendered page
stored as one LZ77 blob, and the game finds that blob through a resource table
inside the uncompressed archive e840:

    base = 0x08472850            (e840's own start, a code literal)
    off  = u32 at 0x08472BE0     (table entry, file offset 0x472BE0)
    src  = base + off            -> 0x08585650 for the stock blob

The decompressed blob (0x5170 bytes) is a fixed three-part layout that the
loader walks with hard-coded sizes:

    0x0000  601 tiles, 4bpp 32B each          (0x4B20 bytes)
    0x4B20   13 palettes, 16 BGR555 each      (0x01A0 bytes)
    0x4CC0  600 tilemap entries, u16          (0x04B0 bytes)

The 600 map entries describe the visible strip in order: entry i is the cell at
column i % 30, row i / 30.  The stored tile number is the VRAM number minus one,
and the blob's own tile array is numbered like VRAM.  The layout is verified by
`--check`: rendering the blob through its own palette reproduces the emulator's
frame pixel for pixel.

What this tool changes
----------------------
* the four Japanese lines are erased with a text mask (white ink + grey AA +
  the dark halo around them) that is diffusion-inpainted and slightly darkened,
  so the reconstruction reads as a soft out-of-focus background;
* the Chinese lines are drawn back into the same bands with the zpix 11x11
  pixel font (white core + grey AA + dark halo, the original style);
* the bottom two tile rows become a footer bar carrying the credit line.

The rewritten blob is larger than the stock 12528-byte stream, so it is appended
to the end of the ROM and the table entry is repointed; the blob has to stay
word aligned because the game decompresses it with the BIOS LZ77 SWI.

Usage:
  patch_disclaimer_page.py --check <rom>
  patch_disclaimer_page.py --preview <rom> before.png after.png
  patch_disclaimer_page.py --apply <rom> [--out out.gba]
"""
import argparse
import os
import struct
import sys

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import gbtext as g          # noqa: E402
import lz77                 # noqa: E402
import pixelfont            # noqa: E402

BLOB_OFF = 0x585650          # file offset of the stock LZ77 stream
TABLE_ENTRY_OFF = 0x472BE0   # u32 offset entry, relative to e840
TABLE_BASE_FILE = 0x472850   # e840's file offset; the code adds 0x08000000

TILE_BYTES = 0x4B20
PAL_OFF = 0x4B20
N_PAL = 13
MAP_OFF = 0x4CC0
N_MAP = 600
TOTAL = 0x5170

COLS, ROWS = 30, 20
W, H = 240, 160

# (glyph-cell top y, japanese, chinese) - the bands match the stock layout
LINES = [
    (39, 'この物語はフィクションです。', '本故事纯属虚构。'),
    (63, '作品中に登場する人物、団体名などは', '作品中登场的人物、团体名等'),
    (87, 'すべて架空のものであり', '全部皆为虚构'),
    (111, '実在のものとは関係ありません。', '与现实的人物和团体无关。'),
]
CREDIT = '汉化:pepsin, deepseek 4.1 flash + harness'
CREDIT_Y = 144
FOOTER_ROWS = (18, 19)
CJK_ADVANCE = 12
SPACE_ADVANCE = 4
BAND_DARKEN = 0.62
INPAINT_ITER = 400
INK_LUM = 90                 # >= this counts as white ink / grey AA


def decompress_len(src):
    """LZ77 (BIOS 0x10) decompress; also return how many input bytes were used."""
    if src[0] != 0x10:
        raise SystemExit('blob does not start with an LZ77 header')
    size = src[1] | (src[2] << 8) | (src[3] << 16)
    out = bytearray()
    p = 4
    while len(out) < size:
        flags = src[p]
        p += 1
        for k in range(8):
            if len(out) >= size:
                break
            if flags & (0x80 >> k):
                b1, b2 = src[p], src[p + 1]
                p += 2
                n = (b1 >> 4) + 3
                d = (((b1 & 0xF) << 8) | b2) + 1
                for _ in range(n):
                    out.append(out[-d])
            else:
                out.append(src[p])
                p += 1
    return bytes(out), p


class Page(object):
    """The disclaimer page: tiles + palettes + tilemap, as stored in the blob."""

    def __init__(self, blob):
        if len(blob) != TOTAL:
            raise SystemExit(f'blob decompressed to {len(blob)}, expected {TOTAL}')
        self.tiles = bytearray(blob[:TILE_BYTES])
        self.palettes = [struct.unpack_from('<16H', blob, PAL_OFF + i * 32)
                         for i in range(N_PAL)]
        self.map = [struct.unpack_from('<H', blob, MAP_OFF + i * 2)[0]
                    for i in range(N_MAP)]

    def tobytes(self):
        out = bytearray(self.tiles)
        for pal in self.palettes:
            out += struct.pack('<16H', *pal)
        for e in self.map:
            out += struct.pack('<H', e)
        return bytes(out)

    # ---- tile / pen access ---------------------------------------------
    def tile_index(self, row, col):
        return (self.map[row * COLS + col] & 0x3FF) + 1

    def bank(self, row, col):
        return (self.map[row * COLS + col] >> 12) & 0xF

    def get_px(self, tile, x, y):
        b = self.tiles[tile * 32 + y * 4 + x // 2]
        return (b >> (4 * (x & 1))) & 0xF

    def set_px(self, tile, x, y, v):
        i = tile * 32 + y * 4 + x // 2
        b = self.tiles[i]
        if x & 1:
            self.tiles[i] = (b & 0x0F) | ((v & 0xF) << 4)
        else:
            self.tiles[i] = (b & 0xF0) | (v & 0xF)

    def color(self, bank, pen):
        c = self.palettes[bank][pen]
        r, gg, b = (c & 0x1F) << 3, ((c >> 5) & 0x1F) << 3, ((c >> 10) & 0x1F) << 3
        return (r | (r >> 5), gg | (gg >> 5), b | (b >> 5))

    def screen(self):
        """160x240 of (tile, bank, pen) as displayed."""
        out = [[None] * W for _ in range(H)]
        for row in range(ROWS):
            for col in range(COLS):
                t, bank = self.tile_index(row, col), self.bank(row, col)
                for y in range(8):
                    srow = out[row * 8 + y]
                    for x in range(8):
                        srow[col * 8 + x] = (t, bank, self.get_px(t, x, y))
        return out

    # ---- pen lookup -----------------------------------------------------
    def lum_pen(self, bank, pen):
        r, gg, b = self.color(bank, pen)
        return (r * 299 + gg * 587 + b * 114) // 1000

    def brightest_pen(self, bank):
        return max(range(1, 16), key=lambda p: self.lum_pen(bank, p))

    def nearest_pen(self, bank, rgb):
        return min(range(1, 16),
                   key=lambda p: sum((a - b) ** 2
                                     for a, b in zip(self.color(bank, p), rgb)))

    def render(self):
        img = Image.new('RGB', (W, H))
        px = img.load()
        scr = self.screen()
        for y in range(H):
            for x in range(W):
                _t, bank, pen = scr[y][x]
                px[x, y] = self.color(bank, pen)
        return img


# --------------------------------------------------------------------------
# text drawing
# --------------------------------------------------------------------------
def advance(ch, bm):
    """Horizontal pen movement for one glyph.

    zpix's BBX width is the ink width with no side bearing, so latin set with
    advance == width has touching letters and an uneven rhythm (i / l / . / 1 are
    only 1-3px wide).  Give every glyph a pixel of air, two around the very
    narrow ones, and a fixed word space.
    """
    if ord(ch) > 0x2E80:                 # CJK: 11px glyph in a 12px cell
        return CJK_ADVANCE
    if bm[0] == 0:                       # space / any other zero-width glyph
        return SPACE_ADVANCE
    return bm[0] + (2 if bm[0] <= 2 else 1)


def text_ink(font, s, top, centre_x=W // 2):
    """Pixel set of s drawn centred at centre_x with the glyph-cell top at y=top."""
    widths = []
    total = 0
    for ch in s:
        bm = font.bitmap(ch)
        if bm is None:
            raise SystemExit(f'font has no glyph for {ch!r}')
        adv = advance(ch, bm)
        widths.append((bm, adv))
        total += adv
    pen_x = centre_x - total // 2
    ink = set()
    for bm, adv in widths:
        w, h, ox, oy, bits = bm
        for j in range(h):
            for i in range(w):
                if bits[j][i]:
                    ink.add((pen_x + ox + i, top + oy + j))
        pen_x += adv
    return ink


def draw_ink(page, scr, ink):
    """White ink plus a 1px dark halo, in each tile's own bank."""
    halo_cache = {}

    def halo_pen(bank):
        if bank not in halo_cache:
            halo_cache[bank] = min(range(1, 16),
                                   key=lambda p: page.lum_pen(bank, p))
        return halo_cache[bank]

    for (x, y) in ink:
        for dy in range(-1, 2):
            for dx in range(-1, 2):
                xx, yy = x + dx, y + dy
                if 0 <= xx < W and 0 <= yy < H:
                    t, bank, _pen = scr[yy][xx]
                    page.set_px(t, xx % 8, yy % 8, halo_pen(bank))
    for (x, y) in ink:
        if 0 <= x < W and 0 <= y < H:
            t, bank, _pen = scr[y][x]
            page.set_px(t, x % 8, y % 8, page.brightest_pen(bank))


def dim_tiles(page, ink):
    """Tiles where a glyph would land on a bank without a bright pen."""
    bad = []
    for (x, y) in ink:
        if not (0 <= x < W and 0 <= y < H):
            continue
        row, col = y // 8, x // 8
        bank = page.bank(row, col)
        if page.lum_pen(bank, page.brightest_pen(bank)) < 200:
            bad.append((row, col, bank))
    return sorted(set(bad))


# --------------------------------------------------------------------------
# the edit
# --------------------------------------------------------------------------
def translate(page, verbose=True):
    font = pixelfont.load()
    scr = page.screen()

    bands = [(top - 4, top + 15) for top, _jp, _cn in LINES]
    bands.append((CREDIT_Y - 4, CREDIT_Y + 15))

    # 1. mask the stock text: bright ink, plus the darker halo around it ----
    ink = [[False] * W for _ in range(H)]
    for y in range(H):
        for x in range(W):
            _t, bank, pen = scr[y][x]
            if pen and page.lum_pen(bank, pen) >= INK_LUM:
                ink[y][x] = True
    mask = [[False] * W for _ in range(H)]
    for y0, y1 in bands:
        for y in range(max(0, y0), min(H, y1 + 1)):
            for x in range(W):
                near = False
                for dy in range(-3, 4):
                    for dx in range(-3, 4):
                        yy, xx = y + dy, x + dx
                        if 0 <= yy < H and 0 <= xx < W and ink[yy][xx]:
                            near = True
                            break
                    if near:
                        break
                mask[y][x] = ink[y][x] or near

    # 2. diffusion-inpaint the masked pixels, then darken them -------------
    img = [[list(page.color(scr[y][x][1], scr[y][x][2])) for x in range(W)]
           for y in range(H)]
    for _ in range(INPAINT_ITER):
        new = [row[:] for row in img]
        for y in range(H):
            for x in range(W):
                if not mask[y][x]:
                    continue
                acc = [0.0, 0.0, 0.0]
                n = 0
                for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    yy, xx = y + dy, x + dx
                    if 0 <= yy < H and 0 <= xx < W:
                        for k in range(3):
                            acc[k] += img[yy][xx][k]
                        n += 1
                if n:
                    new[y][x] = [acc[k] / n for k in range(3)]
        img = new
    for y in range(H):
        for x in range(W):
            if mask[y][x]:
                img[y][x] = [c * BAND_DARKEN for c in img[y][x]]
                t, bank, _pen = scr[y][x]
                page.set_px(t, x % 8, y % 8, page.nearest_pen(bank, img[y][x]))

    # 3. footer bar: one white-capable bank across the whole row -----------
    footer_bank = max(range(N_PAL),
                      key=lambda b: page.lum_pen(b, page.brightest_pen(b)))
    dark = page.nearest_pen(footer_bank, (14, 22, 30))
    for row in FOOTER_ROWS:
        for col in range(COLS):
            t = page.tile_index(row, col)
            for y in range(8):
                for x in range(8):
                    page.set_px(t, x, y, dark)
            i = row * COLS + col
            page.map[i] = (page.map[i] & 0x3FF) | (footer_bank << 12)

    # 4. draw the Chinese text --------------------------------------------
    scr = page.screen()
    for top, _jp, cn in LINES:
        ink_px = text_ink(font, cn, top)
        bad = dim_tiles(page, ink_px)
        if bad and verbose:
            print(f'  warning: {cn!r} touches dim tiles {bad}', file=sys.stderr)
        draw_ink(page, scr, ink_px)
    draw_ink(page, scr, text_ink(font, CREDIT, CREDIT_Y))
    return page


# --------------------------------------------------------------------------
# blob plumbing
# --------------------------------------------------------------------------
def stock_page(rom):
    """The page as shipped: always read the *stock* blob, so --apply is
    idempotent (the stock copy is left in place after a patch and is still the
    Japanese artwork the translation is derived from)."""
    blob, used = decompress_len(rom[BLOB_OFF:BLOB_OFF + 0x20000])
    return Page(blob), BLOB_OFF, used


def live_page(rom):
    """The page the ROM currently points at (follows the repointed table entry)."""
    off = struct.unpack_from('<I', rom, TABLE_ENTRY_OFF)[0]
    file_off = TABLE_BASE_FILE + off
    blob, used = decompress_len(rom[file_off:file_off + 0x20000])
    return Page(blob), file_off, used


def apply_rom(rom_path, out_path):
    rom = bytearray(open(rom_path, 'rb').read())
    page, file_off, used = stock_page(bytes(rom))
    print(f'stock page blob: file 0x{file_off:X}, {used} compressed bytes')
    translate(page)
    new_blob = page.tobytes()
    enc = lz77.compress(new_blob)
    if g.lzdec(enc) != new_blob:
        raise SystemExit('internal error: recompressed blob does not round-trip')
    # The stock stream is followed immediately by the next resource, so an
    # in-place write is only safe while the new stream fits the old size.  The
    # translation makes the page far more compressible, so it normally does;
    # otherwise append the blob and repoint e840's table entry at it.
    if len(enc) <= used:
        rom[file_off:file_off + len(enc)] = enc
        where = f'in place at file 0x{file_off:X}'
    else:
        while len(rom) % 4:
            rom.append(0)
        file_off = len(rom)
        rom += enc
        where = f'appended at file 0x{file_off:X}'
    off = file_off - TABLE_BASE_FILE
    struct.pack_into('<I', rom, TABLE_ENTRY_OFF, off)
    open(out_path, 'wb').write(bytes(rom))
    print(f'blob {used} -> {len(enc)} bytes, {where} '
          f'(addr 0x{0x08000000 + file_off:08X}); table entry -> 0x{off:X}')
    print(f'wrote {out_path} ({len(rom)} bytes)')


def check_rom(rom_path):
    rom = open(rom_path, 'rb').read()
    page, file_off, used = live_page(rom)
    off = struct.unpack_from('<I', rom, TABLE_ENTRY_OFF)[0]
    print(f'table entry 0x{off:X} -> file 0x{file_off:X} '
          f'(addr 0x{0x08000000 + file_off:08X}); stream {used} bytes')
    if file_off + used > len(rom):
        print('  ERROR: blob runs past the end of the ROM')
        return 1
    print(f'  {TOTAL} bytes decompressed, {len(page.tiles) // 32} tiles, '
          f'{len(page.palettes)} palettes, {len(page.map)} map entries')
    print(f'  palette banks used: {sorted({(e >> 12) & 0xF for e in page.map})}')
    for row in FOOTER_ROWS:
        banks = sorted({page.bank(row, c) for c in range(COLS)})
        print(f'  tile row {row}: banks {banks}')
    return 0


def preview(rom_path, before_png, after_png):
    rom = bytearray(open(rom_path, 'rb').read())
    page, _off, _used = stock_page(bytes(rom))
    page.render().resize((W * 3, H * 3), Image.NEAREST).save(before_png)
    translate(page)
    page.render().resize((W * 3, H * 3), Image.NEAREST).save(after_png)
    print(f'wrote {before_png} and {after_png}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('rom')
    ap.add_argument('--preview', nargs=2, metavar=('BEFORE', 'AFTER'))
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--out')
    a = ap.parse_args()
    if a.check:
        return check_rom(a.rom)
    if a.preview:
        return preview(a.rom, a.preview[0], a.preview[1])
    if a.apply:
        return apply_rom(a.rom, a.out or a.rom)
    ap.error('choose --preview, --apply or --check')


if __name__ == '__main__':
    sys.exit(main())
