#!/usr/bin/env python3
"""Identify glyphs by template matching against a system CJK font.

For each glyph index in a range, rasterise the 16x16 4bpp bitmap and compare it
with every candidate character rendered in a system font at 16x16 (plus small
offsets/scales), picking the best match.  This gives an *independent* reading of
the font table, used to audit the human/VLM transcriptions.

Usage:
  match_glyphs.py <rom> --first 0x40 --count 0x60 [--base 0x66F440]
                   [--chars ...] [--top 3] [--out FILE]
"""
import argparse
import os
import sys

from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = [
    '/System/Library/Fonts/Hiragino Sans GB.ttc',
    '/System/Library/Fonts/STHeiti Medium.ttc',
    '/System/Library/Fonts/Supplemental/Songti.ttc',
    '/System/Library/Fonts/Supplemental/Arial Unicode.ttf',
]

# Unicode-ordered hiragana then katakana, including dakuten/handakuten pairs,
# matching the layout observed in the font table.
HIRA = ('あいうえおかがきぎくぐけげこごさざしじすずせぜそぞただちぢっつづてでとど'
        'なにぬねのはばぱひびぴふぶぷへべぺほぼぽまみむめもゃやゅゆょよらりるれろ'
        'わゐゑをんゔ')
KATA = ('アイウエオカガキギクグケゲコゴサザシジスズセゼソゾタダチヂッツヅテデトド'
        'ナニヌネノハバパヒビピフブプヘベペホボポマミムメモャヤュユョヨラリルレロ'
        'ワヰヱヲンヴ')


def load_font(size):
    for p in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    raise SystemExit('no CJK font found')


def glyph_bitmap(rom, base, idx):
    b = rom[base + idx * 0x80: base + idx * 0x80 + 0x80]
    img = Image.new('L', (16, 16), 0)
    px = img.load()
    for t in range(4):
        tx, ty = (t & 1) * 8, (t >> 1) * 8
        for y in range(8):
            for x in range(4):
                lo = b[t * 32 + y * 4 + x]
                for n in range(2):
                    px[tx + x * 2 + n, ty + y] = (lo >> (4 * n)) & 0xF
    return img


def char_bitmap(ch, font, dx=0, dy=0, scale=1.0):
    size = 16
    img = Image.new('L', (size, size), 0)
    d = ImageDraw.Draw(img)
    big = int(round(size * 4 * scale))
    f = font.font_variant(size=big) if hasattr(font, 'font_variant') else font
    tmp = Image.new('L', (big + 8, big + 8), 0)
    ImageDraw.Draw(tmp).text((4, 4), ch, font=f, fill=15)
    tmp = tmp.resize((size, size), Image.LANCZOS)
    img.paste(tmp, (dx, dy))
    return img


def score(a, b):
    pa, pb = a.load(), b.load()
    inter = union = 0
    for y in range(16):
        for x in range(16):
            va, vb = pa[x, y] > 0, pb[x, y] > 0
            if va or vb:
                union += 1
                if va and vb:
                    inter += 1
    return inter / union if union else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('rom')
    ap.add_argument('--base', default='0x66F440')
    ap.add_argument('--first', default='0x40')
    ap.add_argument('--count', default='0x60')
    ap.add_argument('--chars', default='')
    ap.add_argument('--top', type=int, default=2)
    ap.add_argument('--out', default='')
    a = ap.parse_args()
    rom = open(a.rom, 'rb').read()
    base = int(a.base, 0)
    first = int(a.first, 0)
    count = int(a.count, 0)
    chars = a.chars if a.chars else HIRA + KATA + 'ーっゃゅょっ゛゜ヶ'
    font = load_font(64)
    lines = []
    for idx in range(first, first + count):
        g = glyph_bitmap(rom, base, idx)
        if sum(g.load()[x, y] for y in range(16) for x in range(16)) == 0:
            lines.append(f'{idx:03X}\t(blank)')
            continue
        best = []
        for ch in chars:
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for sc in (0.9, 1.0, 1.1):
                        s = score(g, char_bitmap(ch, font, dx, dy, sc))
                        best.append((s, ch))
        best.sort(reverse=True)
        top = ' '.join(f'{c}:{s:.2f}' for s, c in best[:a.top])
        lines.append(f'{idx:03X}\t{best[0][1]}\t{top}')
    out = '\n'.join(lines)
    print(out)
    if a.out:
        open(a.out, 'w', encoding='utf-8').write(out + '\n')
        print(f'\nwrote {a.out}', file=sys.stderr)


if __name__ == '__main__':
    main()
