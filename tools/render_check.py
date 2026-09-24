#!/usr/bin/env python3
"""Render a glyph-verification sheet: hex code + chosen glyph + unicode codepoint.

Produces a single PNG (default 8 columns) where each cell shows:
    <code label>   [glyph bitmap 16x16 at 5x]   U+XXXX

Usage: render_check.py <rom> <out.png> <code>[,<code>...] [--base 0x66F440] [--cols 8]

Codes may be given as ranges with '-' (e.g. 0x64-0x74).  The unicode label is
the codepoint of the character the code is *supposed* to stand for according to
the disputed map; the reviewer compares the bitmap against it.
"""
import argparse
from PIL import Image, ImageDraw

F = {
    '0': (0b111, 0b101, 0b101, 0b101, 0b111), '1': (0b010, 0b110, 0b010, 0b010, 0b111),
    '2': (0b111, 0b001, 0b111, 0b100, 0b111), '3': (0b111, 0b001, 0b111, 0b001, 0b111),
    '4': (0b101, 0b101, 0b111, 0b001, 0b001), '5': (0b111, 0b100, 0b111, 0b001, 0b111),
    '6': (0b111, 0b100, 0b111, 0b101, 0b111), '7': (0b111, 0b001, 0b001, 0b010, 0b010),
    '8': (0b111, 0b101, 0b111, 0b101, 0b111), '9': (0b111, 0b101, 0b111, 0b001, 0b111),
    'A': (0b010, 0b101, 0b111, 0b101, 0b101), 'B': (0b110, 0b101, 0b110, 0b101, 0b110),
    'C': (0b011, 0b100, 0b100, 0b100, 0b011), 'D': (0b110, 0b101, 0b101, 0b101, 0b110),
    'E': (0b111, 0b100, 0b110, 0b100, 0b111), 'F': (0b111, 0b100, 0b110, 0b100, 0b100),
    'U': (0b101, 0b101, 0b101, 0b101, 0b111), '+': (0b000, 0b010, 0b111, 0b010, 0b000),
}

SCALE = 5
CELL = 16 * SCALE
LABEL_H = 12


def text_img(draw, x, y, s, fill=255):
    for i, ch in enumerate(s.upper()):
        g = F.get(ch)
        if not g:
            continue
        for ry in range(5):
            for rx in range(3):
                if g[ry] >> (2 - rx) & 1:
                    draw.rectangle([x + i * 4, y + ry * 2, x + i * 4 + 1, y + ry * 2 + 1], fill=fill)


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
                    for dy in range(SCALE):
                        for dx in range(SCALE):
                            px[ox + (tx + x * 2 + n) * SCALE + dx, oy + (ty + y) * SCALE + dy] = g


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('rom')
    ap.add_argument('out')
    ap.add_argument('codes')
    ap.add_argument('--base', default='0x66F440')
    ap.add_argument('--cols', type=int, default=8)
    ap.add_argument('--mapfile', default='data/glyph_map.csv')
    a = ap.parse_args()
    rom = open(a.rom, 'rb').read()
    base = int(a.base, 0)
    gmap = {}
    try:
        import mapio
        gmap = mapio.load_map(a.mapfile)
    except Exception as e:
        print(f'(no map labels: {e})')
    codes = []
    for part in a.codes.split(','):
        if '-' in part:
            lo, hi = part.split('-')
            codes += list(range(int(lo, 0), int(hi, 0) + 1))
        else:
            codes.append(int(part, 0))
    cols = a.cols
    rows = (len(codes) + cols - 1) // cols
    W = cols * (CELL + 8)
    H = rows * (CELL + LABEL_H + 6)
    img = Image.new('L', (W, H), 30)
    d = ImageDraw.Draw(img)
    for i, c in enumerate(codes):
        ox = (i % cols) * (CELL + 8) + 4
        oy = (i // cols) * (CELL + LABEL_H + 6) + 4
        draw_glyph(img, rom[base + c * 0x80: base + c * 0x80 + 0x80], ox, oy)
        text_img(d, ox, oy + CELL + 2, f'{c:03X}')
        ch = gmap.get(c, '')
        up = f'U+{ord(ch):04X}' if ch else ''
        text_img(d, ox + 34, oy + CELL + 2, up, fill=170)
    img.save(a.out)
    # print the code list with the current labels so the reviewer knows what to check
    print(f'{a.out}: {len(codes)} cells')
    for c in codes:
        print(f'  {c:03X}  current_label={gmap.get(c, "?")!r}')


if __name__ == '__main__':
    main()
