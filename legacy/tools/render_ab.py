#!/usr/bin/env python3
"""Render an A/B glyph comparison sheet for alignment verification.

For each requested code C the sheet shows two cells side by side:
    cell A: glyph at table index C          (label "C")
    cell B: glyph at table index C+delta    (label "C+delta")
so a reviewer can see which of the two is the character the script needs.

The right-hand label shows the character the current map claims for C, and the
expected character (if supplied) is printed beneath the code.

Usage:
  render_ab.py <rom> <out.png> --codes 0x0DF,0x0E3,... [--delta 0x10] [--expect 0x0DF=想,...]
"""
import argparse
from PIL import Image, ImageDraw
from render_check import F, SCALE, CELL, LABEL_H, text_img, draw_glyph


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('rom')
    ap.add_argument('out')
    ap.add_argument('--codes', required=True)
    ap.add_argument('--delta', default='0x10')
    ap.add_argument('--base', default='0x800000')
    ap.add_argument('--expect', default='')
    a = ap.parse_args()
    rom = open(a.rom, 'rb').read()
    base = int(a.base, 0)
    delta = int(a.delta, 0)
    codes = [int(x, 0) for x in a.codes.split(',')]
    expect = {}
    for part in filter(None, a.expect.split(',')):
        k, v = part.split('=')
        expect[int(k, 0)] = v
    cols = 2                     # 4 pairs per row
    pair_w = CELL * 2 + 26
    row_h = CELL + LABEL_H + 8
    rows = (len(codes) + cols - 1) // cols
    img = Image.new('L', (cols * pair_w + 10, rows * row_h + 10), 30)
    d = ImageDraw.Draw(img)
    for i, c in enumerate(codes):
        ox = (i % cols) * pair_w + 8
        oy = (i // cols) * row_h + 6
        draw_glyph(img, rom[base + c * 0x80: base + c * 0x80 + 0x80], ox, oy)
        draw_glyph(img, rom[base + (c + delta) * 0x80: base + (c + delta) * 0x80 + 0x80], ox + CELL + 22, oy)
        text_img(d, ox, oy + CELL + 2, f'{c:03X}')
        text_img(d, ox + CELL + 22, oy + CELL + 2, f'{c + delta:03X}', fill=170)
        if c in expect:
            text_img(d, ox + CELL // 2, oy + CELL + 2, f'={expect[c]}', fill=200)
    img.save(a.out)
    print(f'{a.out}: {len(codes)} pairs; left=index C, right=index C+{delta:#x}')
    for c in codes:
        e = f'  (script needs {expect[c]})' if c in expect else ''
        print(f'  pair {c:03X} / {c + delta:03X}{e}')


if __name__ == '__main__':
    main()
