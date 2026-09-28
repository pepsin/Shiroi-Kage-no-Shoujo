#!/usr/bin/env python3
"""Import the cover artwork for the title screen from an edited PNG.

The title screen is the same kind of pre-rendered page as the disclaimer screen:
one LZ77 blob that the game finds through e840's resource table.

    base = 0x08472850            (e840's start, a code literal)
    entry at 0x08472B88          (file offset 0x472B88, value 0x107CD0)
    src  = base + entry          -> 0x0857A520 for the stock blob

The decompressed blob (0x5170 bytes) is a fixed three-part layout:

    0x0000  601 tiles, 4bpp 32B each          (0x4B20)
    0x4B20   13 palettes, 16 BGR555 each      (0x01A0)
    0x4CC0  600 tilemap entries, u16          (0x04B0)

Entry i of the map is the cell at column i % 30, row i / 30 of the 240x160
screen, and it carries the tile number (stored minus one) plus the palette bank
that tile is drawn with.

This tool treats `docs/title/标题画面_BG3原画.png` as the **master artwork**: it
imports that 240x160 image into the blob.  Only the cells whose 8x8 block differs
from what the blob already renders are touched, and each of those picks the
palette bank that reproduces its pixels best, so unedited artwork stays
bit-identical and the palettes themselves are never rewritten.

The stock blob is left in place; the rewritten stream is written in place while
it fits the old size, otherwise it is appended and e840's table entry repointed
(the blob must stay word aligned - the game decompresses it with the BIOS LZ77
SWI).

Usage:
  patch_title_page.py <rom> --check
  patch_title_page.py <rom> --preview before.png after.png
  patch_title_page.py <rom> --apply [--out out.gba]
"""
import argparse
import collections
import os
import struct
import sys

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import gbtext as g          # noqa: E402
import lz77                 # noqa: E402

TABLE_ENTRY_OFF = 0x472B88   # u32 offset entry in e840 (title page blob)
TABLE_BASE_FILE = 0x472850   # e840's file offset
STOCK_BLOB_OFF = 0x57A520    # where the stock blob lives

ART = os.path.join(ROOT, 'docs', 'title', '标题画面_BG3原画.png')

TILE_BYTES = 0x4B20
PAL_OFF = 0x4B20
N_PAL = 13
MAP_OFF = 0x4CC0
N_MAP = 600
TOTAL = 0x5170

COLS, ROWS = 30, 20
W, H = 240, 160


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
    """The title page: tiles + palettes + tilemap, as stored in the blob."""

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

    # ---- tile access ----------------------------------------------------
    def tile_index(self, cell):
        return (self.map[cell] & 0x3FF) + 1

    def bank(self, cell):
        return (self.map[cell] >> 12) & 0xF

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

    def render_cell(self, cell):
        """8x8 RGB block of a cell as the blob currently defines it."""
        tile, bank = self.tile_index(cell), self.bank(cell)
        pal = [self.color(bank, p) for p in range(16)]
        return [[pal[self.get_px(tile, x, y)] for x in range(8)] for y in range(8)]

    def render(self):
        img = Image.new('RGB', (W, H))
        px = img.load()
        for cell in range(N_MAP):
            row, col = divmod(cell, COLS)
            block = self.render_cell(cell)
            for y in range(8):
                for x in range(8):
                    px[col * 8 + x, row * 8 + y] = block[y][x]
        return img

    # ---- quantisation ---------------------------------------------------
    def best_bank(self, block):
        """Bank (and pen assignment) that reproduces an 8x8 RGB block best.

        Pen 0 is transparent, and the stock artwork never uses it, so only pens
        1..15 take part.
        """
        best = None
        for bank in range(N_PAL):
            pens = [self.color(bank, p) for p in range(16)]
            table = {}
            err = 0
            assign = [[0] * 8 for _ in range(8)]
            for y in range(8):
                for x in range(8):
                    c = block[y][x]
                    key = c
                    p = table.get(key)
                    if p is None:
                        p = min(range(1, 16),
                                key=lambda q: sum((a - b) ** 2
                                                  for a, b in zip(c, pens[q])))
                        table[key] = p
                    assign[y][x] = p
                    err += sum((a - b) ** 2 for a, b in zip(c, pens[p]))
            if best is None or err < best[0]:
                best = (err, bank, assign)
        return best


# --------------------------------------------------------------------------
# import
# --------------------------------------------------------------------------
def load_art():
    if not os.path.exists(ART):
        raise SystemExit(f'master artwork missing: {ART}')
    img = Image.open(ART).convert('RGB')
    if img.size != (W, H):
        raise SystemExit(f'{ART} is {img.size}, expected {(W, H)}')
    px = img.load()
    return [[px[x, y] for x in range(W)] for y in range(H)]


def import_art(page, art):
    """Write every cell that differs from the current page; returns (cells, err)."""
    changed = 0
    worst = 0
    for cell in range(N_MAP):
        row, col = divmod(cell, COLS)
        block = [[art[row * 8 + y][col * 8 + x] for x in range(8)] for y in range(8)]
        if all(block[y][x] == page.render_cell(cell)[y][x]
               for y in range(8) for x in range(8)):
            continue
        err, bank, assign = page.best_bank(block)
        worst = max(worst, err)
        tile = page.tile_index(cell)
        for y in range(8):
            for x in range(8):
                page.set_px(tile, x, y, assign[y][x])
        page.map[cell] = (page.map[cell] & 0x3FF) | (bank << 12)
        changed += 1
    return changed, worst


def diff_stats(page, art):
    """How far the page's render is from the master image.

    A page tile draws with ONE of the 13 fixed 15-colour palettes, so an edit
    that mixes colours from two banks cannot be represented exactly; the numbers
    below say how much was lost in that quantisation.
    """
    buckets = collections.Counter()
    total = 0
    worst = 0.0
    for cell in range(N_MAP):
        row, col = divmod(cell, COLS)
        block = page.render_cell(cell)
        for y in range(8):
            for x in range(8):
                c = art[row * 8 + y][col * 8 + x]
                d = sum((a - b) ** 2 for a, b in zip(c, block[y][x]))
                if not d:
                    continue
                total += 1
                dist = d ** 0.5
                worst = max(worst, dist)
                key = ('<5' if dist < 5 else '<10' if dist < 10 else
                       '<20' if dist < 20 else '<40' if dist < 40 else '>=40')
                buckets[key] += 1
    return total, worst, buckets


# --------------------------------------------------------------------------
# blob plumbing
# --------------------------------------------------------------------------
def read_page(rom):
    """The page the ROM currently points at (follows the repointed table entry)."""
    off = struct.unpack_from('<I', rom, TABLE_ENTRY_OFF)[0]
    file_off = TABLE_BASE_FILE + off
    blob, used = decompress_len(rom[file_off:file_off + 0x20000])
    return Page(blob), file_off, used


def apply_rom(rom_path, out_path):
    rom = bytearray(open(rom_path, 'rb').read())
    page, file_off, used = read_page(bytes(rom))
    art = load_art()
    print(f'title page blob: file 0x{file_off:X}, {used} compressed bytes')
    changed, worst = import_art(page, art)
    print(f'cells rewritten: {changed} / {N_MAP}  (worst block error {worst})')
    new_blob = page.tobytes()
    enc = lz77.compress(new_blob)
    if g.lzdec(enc) != new_blob:
        raise SystemExit('internal error: recompressed blob does not round-trip')
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
    page, file_off, used = read_page(rom)
    off = struct.unpack_from('<I', rom, TABLE_ENTRY_OFF)[0]
    print(f'table entry 0x{off:X} -> file 0x{file_off:X} '
          f'(addr 0x{0x08000000 + file_off:08X}); stream {used} bytes')
    if file_off + used > len(rom):
        print('  ERROR: blob runs past the end of the ROM')
        return 1
    art = load_art()
    total, worst, buckets = diff_stats(page, art)
    print(f'  {TOTAL} bytes decompressed, {len(page.tiles) // 32} tiles, '
          f'{len(page.palettes)} palettes, {len(page.map)} map entries')
    print(f'  render vs master artwork: {total} differing pixel(s) of {W * H}, '
          f'worst colour distance {worst:.1f}')
    if total:
        print('    distance buckets: ' + '  '.join(
            f'{k}:{buckets[k]}' for k in ('<5', '<10', '<20', '<40', '>=40')
            if buckets[k]))
    # An exact match is the normal case (the master is built from this page's own
    # palette); a few pixels can only snap when an edit mixes two banks.
    ok = total <= W * H // 20 and worst <= 80.0
    print('RESULT:', 'OK' if ok else 'PROBLEM')
    return 0 if ok else 1


def preview(rom_path, before_png, after_png):
    rom = bytearray(open(rom_path, 'rb').read())
    page, _off, _used = read_page(bytes(rom))
    page.render().resize((W * 3, H * 3), Image.NEAREST).save(before_png)
    import_art(page, load_art())
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
