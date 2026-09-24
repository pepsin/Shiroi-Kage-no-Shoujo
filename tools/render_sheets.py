#!/usr/bin/env python3
"""Render glyph-table regions into labelled sheets for transcription.

The global glyph table lives at ROM file offset 0x66F440 (0x80 bytes per
16x16 4bpp glyph, tile order TL,TR,BL,BR, codes 0x000..0x6A7 = 1704 glyphs).
A second identical copy starts at file 0x800000 but is cut short by EOF at
code 0x644, so 0x66F440 is the canonical source.

Usage:
  render_sheets.py <rom> <outdir> [--base 0x66F440] [--first 0x644] [--count 100] [--tag tail]

Each sheet: 10x10 glyphs at 4x scale, 3-digit hex code under each glyph.
"""
import os, argparse
from PIL import Image

# tiny 3x5 digit/hex font
F = {
    '0': (0b111, 0b101, 0b101, 0b101, 0b111),
    '1': (0b010, 0b110, 0b010, 0b010, 0b111),
    '2': (0b111, 0b001, 0b111, 0b100, 0b111),
    '3': (0b111, 0b001, 0b111, 0b001, 0b111),
    '4': (0b101, 0b101, 0b111, 0b001, 0b001),
    '5': (0b111, 0b100, 0b111, 0b001, 0b111),
    '6': (0b111, 0b100, 0b111, 0b101, 0b111),
    '7': (0b111, 0b001, 0b001, 0b010, 0b010),
    '8': (0b111, 0b101, 0b111, 0b101, 0b111),
    '9': (0b111, 0b101, 0b111, 0b001, 0b111),
    'A': (0b010, 0b101, 0b111, 0b101, 0b101),
    'B': (0b110, 0b101, 0b110, 0b101, 0b110),
    'C': (0b011, 0b100, 0b100, 0b100, 0b011),
    'D': (0b110, 0b101, 0b101, 0b101, 0b110),
    'E': (0b111, 0b100, 0b110, 0b100, 0b111),
    'F': (0b111, 0b100, 0b110, 0b100, 0b100),
}
S = 4          # glyph scale
LBL = 8 * 2    # label height (5px font x2 + gap)
CELL = 16 * S  # 64
COLS = ROWS = 10


def draw_label(img, x, y, text):
    for i, ch in enumerate(text):
        g = F[ch]
        for ry in range(5):
            for rx in range(3):
                if g[ry] >> (2 - rx) & 1:
                    for dy in range(2):
                        for dx in range(2):
                            img.putpixel((x + i * 8 + rx * 2 + dx, y + ry * 2 + dy), 255)


def draw_glyph(img, b, ox, oy):
    for t in range(4):  # TL,TR,BL,BR
        tx, ty = (t & 1) * 8, (t >> 1) * 8
        for y in range(8):
            for x in range(4):
                lo = b[t * 32 + y * 4 + x]
                for n in range(2):
                    v = (lo >> (4 * n)) & 0xF
                    g = (15 - v) * 17
                    for dy in range(S):
                        for dx in range(S):
                            img.putpixel((ox + (tx + x * 2 + n) * S + dx,
                                          oy + (ty + y) * S + dy), g)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('rom')
    ap.add_argument('outdir')
    ap.add_argument('--base', default='0x800000')
    ap.add_argument('--first', default='0x000')
    ap.add_argument('--count', type=int, default=0, help='0 = to end of ROM')
    ap.add_argument('--tag', default='')
    a = ap.parse_args()
    rom = open(a.rom, 'rb').read()
    base = int(a.base, 0)
    first = int(a.first, 0)
    avail = (len(rom) - base) // 0x80
    ng = a.count if a.count else avail
    ng = min(ng, avail - first)
    os.makedirs(a.outdir, exist_ok=True)
    per = COLS * ROWS
    nsheets = (ng + per - 1) // per
    for s in range(nsheets):
        img = Image.new('L', (COLS * CELL, ROWS * (CELL + LBL)), 40)
        for k in range(per):
            gid = first + s * per + k
            if gid >= first + ng:
                break
            b = rom[base + gid * 0x80: base + gid * 0x80 + 0x80]
            ox = (k % COLS) * CELL
            oy = (k // COLS) * (CELL + LBL)
            draw_glyph(img, b, ox, oy)
            draw_label(img, ox + 8, oy + CELL + 2, f'{gid:03X}')
        name = f'sheet_{a.tag}{s:02d}.png' if a.tag else f'sheet_{s:02d}.png'
        img.save(os.path.join(a.outdir, name))
    print(f'wrote {nsheets} sheets, {ng} glyphs (codes {first:#05x}..{first+ng-1:#05x}) from base {base:#x}')


if __name__ == '__main__':
    main()
