#!/usr/bin/env python3
"""Render the ENTIRE JP glyph table into one labelled master image.

Single PNG: every glyph 0x000..0x6A7 at 4x (64px) with its 3-digit hex
table index printed underneath. Codes match glyph_map.csv's `code` column
row-for-row, so the map can be proofread against this image.

Usage: render_master.py <rom> <out.png> [--base 0x66F440] [--cols 32]
"""
import argparse
from PIL import Image

F = {
    '0': (0b111, 0b101, 0b101, 0b101, 0b111), '1': (0b010, 0b110, 0b010, 0b010, 0b111),
    '2': (0b111, 0b001, 0b111, 0b100, 0b111), '3': (0b111, 0b001, 0b111, 0b001, 0b111),
    '4': (0b101, 0b101, 0b111, 0b001, 0b111), '5': (0b111, 0b100, 0b111, 0b001, 0b111),
    '6': (0b111, 0b100, 0b111, 0b101, 0b111), '7': (0b111, 0b001, 0b001, 0b010, 0b010),
    '8': (0b111, 0b101, 0b111, 0b101, 0b111), '9': (0b111, 0b101, 0b111, 0b001, 0b111),
    'A': (0b010, 0b101, 0b111, 0b101, 0b101), 'B': (0b110, 0b101, 0b110, 0b101, 0b110),
    'C': (0b011, 0b100, 0b100, 0b100, 0b011), 'D': (0b110, 0b101, 0b101, 0b101, 0b110),
    'E': (0b111, 0b100, 0b110, 0b100, 0b111), 'F': (0b111, 0b100, 0b110, 0b100, 0b100),
}
S = 4                    # glyph scale -> 64px
LS = 3                   # label scale
LBL = 5 * LS + 6         # label strip height
CELL = 16 * S            # 64
COUNT = 0x6A8


def draw_label(img, cx, y, text):
    w = len(text) * (3 * LS + LS) - LS
    x = cx - w // 2
    for ch in text:
        g = F[ch]
        for ry in range(5):
            for rx in range(3):
                if g[ry] >> (2 - rx) & 1:
                    for dy in range(LS):
                        for dx in range(LS):
                            img.putpixel((x + rx * LS + dx, y + ry * LS + dy), 255)
        x += 4 * LS


def draw_glyph(img, b, ox, oy):
    px = img.load()
    for t in range(4):
        tx, ty = (t & 1) * 8, (t >> 1) * 8
        for y in range(8):
            for x in range(4):
                lo = b[t * 32 + y * 4 + x]
                for n in range(2):
                    v = (lo >> (4 * n)) & 0xF
                    g = (15 - v) * 17
                    for dy in range(S):
                        for dx in range(S):
                            px[ox + (tx + x * 2 + n) * S + dx, oy + (ty + y) * S + dy] = g


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('rom')
    ap.add_argument('out')
    ap.add_argument('--base', default='0x66F440')
    ap.add_argument('--cols', type=int, default=32)
    a = ap.parse_args()
    rom = open(a.rom, 'rb').read()
    base = int(a.base, 0)
    avail = (len(rom) - base) // 0x80
    n = min(COUNT, avail)
    cols = a.cols
    rows = (n + cols - 1) // cols
    img = Image.new('L', (cols * CELL, rows * (CELL + LBL)), 40)
    for gid in range(n):
        b = rom[base + gid * 0x80: base + gid * 0x80 + 0x80]
        ox = (gid % cols) * CELL
        oy = (gid // cols) * (CELL + LBL)
        draw_glyph(img, b, ox, oy)
        draw_label(img, ox + CELL // 2, oy + CELL + 3, f'{gid:03X}')
    img.save(a.out)
    print(f'{a.out}: {img.size[0]}x{img.size[1]}, {n} glyphs, {cols} cols x {rows} rows, base {base:#x}')


if __name__ == '__main__':
    main()
