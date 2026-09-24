#!/usr/bin/env python3
"""Human-verifiable glyph sheet: bitmap + big readable hex code + claimed char.

Each cell shows, from top to bottom:
    <hex code>            (large, rendered with a real font)
    [glyph bitmap]        (ROM 16x16 4bpp, 5x)
    <claimed character>   (large, rendered with a CJK system font)

If the big character under a code does not match the bitmap above it, the
code->character alignment is wrong for that entry.

Usage:
  font_sheet.py <rom> <out.png> [--codes 0x0DF,0x0E3,...] [--base 0x800000]
"""
import argparse
from PIL import Image, ImageDraw, ImageFont

SCALE = 5
CELL = 16 * SCALE
FONT_CANDIDATES = [
    '/System/Library/Fonts/Hiragino Sans GB.ttc',
    '/System/Library/Fonts/STHeiti Medium.ttc',
    '/System/Library/Fonts/Supplemental/Songti.ttc',
    '/System/Library/Fonts/Supplemental/Arial Unicode.ttf',
]


def load_font(size):
    for p in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


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
    ap.add_argument('--codes', required=True)
    ap.add_argument('--base', default='0x800000')
    ap.add_argument('--cols', type=int, default=4)
    a = ap.parse_args()
    rom = open(a.rom, 'rb').read()
    base = int(a.base, 0)
    codes = [int(x, 0) for x in a.codes.split(',')]
    f_code = load_font(26)
    f_char = load_font(52)
    f_small = load_font(18)
    claimed = {}
    try:
        import json
        claimed = json.load(open('data/glyph_map.json', encoding='utf-8'))['map']
    except Exception:
        pass
    cols = a.cols
    cw, cell_h = CELL + 16, 26 + CELL + 62 + 22
    rows = (len(codes) + cols - 1) // cols
    img = Image.new('L', (cols * cw + 8, rows * cell_h + 8), 25)
    d = ImageDraw.Draw(img)
    for i, c in enumerate(codes):
        ox = (i % cols) * cw + 8
        oy = (i // cols) * cell_h + 6
        d.text((ox + CELL // 2, oy), f'{c:03X}', font=f_code, fill=255, anchor='ma')
        draw_glyph(img, rom[base + c * 0x80: base + c * 0x80 + 0x80], ox + 8, oy + 26)
        label = claimed.get(f'{c:03X}', '?')
        d.text((ox + cw // 2, oy + 26 + CELL + 2), label, font=f_char, fill=255, anchor='ma')
        d.text((ox + cw // 2, oy + 26 + CELL + 56), 'claimed', font=f_small, fill=120, anchor='ma')
    img.save(a.out)
    print(f'{a.out}: {len(codes)} cells')
    for c in codes:
        print(f'  {c:03X} claimed={claimed.get(f"{c:03X}", "?")}')


if __name__ == '__main__':
    main()
