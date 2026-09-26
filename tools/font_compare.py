#!/usr/bin/env python3
"""Render sample text the way the game will show it, for font comparison.

For every character it uses exactly the source the game uses:

  * a character that already exists in the Japanese font is drawn from the JP
    ROM's glyph table (those slots are reused untouched),
  * a character this build appends is drawn with the renderer under test
    (outline font downscale vs. a pixel font such as zpix).

Usage:
  font_compare.py --out cmp.png [--text 你好] [--variant outline|pixel|both]
  font_compare.py --out cmp.png --chars 汉字测试            # one cell per char
"""
import argparse
import csv
import os
import sys

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import font_patch as fp
import pixelfont as pf

JP_ROM = os.path.join(ROOT, 'Tantei Jinguuji Saburou - Shiroi Kage no Shoujo (Japan).gba')
EXT_MAP = os.path.join(ROOT, 'work', 'glyph_map.ext.csv')
MANIFEST = os.path.join(ROOT, 'data', 'glyph_png', 'manifest.tsv')
BG, BODY, SHADOW = (248, 248, 248), (80, 80, 80), (104, 96, 88)


def jp_table():
    rom = open(JP_ROM, 'rb').read()
    import struct
    off, size = struct.unpack_from('<2I', rom, 0x15A000 + 850 * 8)
    start = 0x15C000 + off
    return rom[start:start + size], size // 0x80


def levels_from_bytes(b):
    lv = [[0] * 16 for _ in range(16)]
    for t in range(4):
        tx, ty = (t & 1) * 8, (t >> 1) * 8
        for y in range(8):
            for x in range(4):
                v = b[t * 32 + y * 4 + x]
                lv[ty + y][tx + x * 2] = v & 0xF
                lv[ty + y][tx + x * 2 + 1] = (v >> 4) & 0xF
    return lv


def pack(levels):
    out = bytearray(0x80)
    for t in range(4):
        tx, ty = (t & 1) * 8, (t >> 1) * 8
        for y in range(8):
            for x in range(4):
                lo = levels[ty + y][tx + x * 2] & 0xF
                hi = levels[ty + y][tx + x * 2 + 1] & 0xF
                out[t * 32 + y * 4 + x] = (hi << 4) | lo
    return bytes(out)


def new_charset():
    """Characters this build appends (they need a renderer)."""
    out = set()
    if os.path.exists(MANIFEST):
        for r in csv.DictReader(open(MANIFEST, encoding='utf-8'), delimiter='\t'):
            if r.get('is_new') == '1' and r.get('char'):
                out.add(r['char'])
    return out


def render_variant(ch, variant, pixel, new_set, jp_slots):
    """16x16 palette-index image for one character (or None to skip)."""
    if ch in jp_slots:
        return levels_from_bytes(jp_slots[ch])
    if variant == 'rom':
        return levels_from_bytes(fp.render_glyph(ch))     # not in this ROM's map
    if variant == 'outline':
        return levels_from_bytes(fp.render_glyph(ch))
    body = pixel.body(ch)
    if body is None:
        return levels_from_bytes(fp.render_glyph(ch))       # font gap -> outline
    lv = [row[:] for row in body]
    for y in range(16):
        for x in range(16):
            if body[y][x] and x + 1 < 16 and lv[y][x + 1] == 0:
                lv[y][x + 1] = 2
    return lv


def glyph_image(lv, scale):
    im = Image.new('RGB', (16, 16), BG)
    p = im.load()
    for y in range(16):
        for x in range(16):
            v = lv[y][x]
            if v == 1:
                p[x, y] = BODY
            elif v:
                p[x, y] = SHADOW
    return im.resize((16 * scale, 16 * scale), Image.NEAREST)


def strip(text, variant, pixel, new_set, jp_slots, scale):
    cells = [render_variant(ch, variant, pixel, new_set, jp_slots) for ch in text]
    im = Image.new('RGB', (16 * scale * len(cells), 16 * scale), BG)
    for k, lv in enumerate(cells):
        im.paste(glyph_image(lv, scale), (k * 16 * scale, 0))
    return im


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=os.path.join(ROOT, 'work', 'font_compare.png'))
    ap.add_argument('--text', action='append', default=None,
                    help='a line to render (repeatable)')
    ap.add_argument('--chars', default=None, help='render each character in its own cell')
    ap.add_argument('--variant', choices=['outline', 'pixel', 'both'], default='both')
    ap.add_argument('--scale', type=int, default=5)
    ap.add_argument('--pixel-font', default=pf.DEFAULT)
    ap.add_argument('--rom', default=None,
                    help='render every character from this built ROM glyph table '
                         '(what the game actually draws)')
    a = ap.parse_args()

    pixel = pf.load(a.pixel_font)
    new_set = new_charset()
    table, count = jp_table()
    if a.rom:
        import export_glyphs as eg
        rom_table, rom_count = eg.font_table(open(a.rom, 'rb').read())
        table = rom_table
        count = rom_count
    # char -> slot, for characters the Japanese font already has
    jp_slots = {}
    gmap = {}
    if os.path.exists(EXT_MAP):
        for r in csv.DictReader(open(EXT_MAP, encoding='utf-8')):
            ch = (r.get('char') or '').strip()
            if ch:
                gmap[int(r['dec'], 0)] = ch
    for idx in sorted(gmap):
        ch = gmap[idx]
        if a.rom:
            if idx < count:
                jp_slots[ch] = table[idx * 0x80:(idx + 1) * 0x80]
        elif idx < 0x6A8 and ch not in jp_slots:
            jp_slots[ch] = table[idx * 0x80:(idx + 1) * 0x80]

    texts = a.text or ['、先生。您没什么事吧“““『']
    if a.chars:
        texts = [a.chars[i:i + 16] for i in range(0, len(a.chars), 16)]
    variants = (['outline', 'pixel'] if a.variant == 'both' else [a.variant])
    if a.rom:
        variants = ['outline'] if a.variant == 'outline' else ['rom']

    rows = []
    for text in texts:
        for v in variants:
            rows.append((f'{v}', strip(text, v, pixel, new_set, jp_slots, a.scale)))
    labels = [(t, v) for t in texts for v in variants]
    w = max(r.width for _, r in rows)
    h = sum(r.height + 3 * a.scale for _, r in rows)
    sheet = Image.new('RGB', (w, h), (40, 40, 40))
    y = 0
    for (text, v), (_, im) in zip(labels, rows):
        sheet.paste(im, (0, y))
        y += im.height + 3 * a.scale
    sheet.save(a.out)
    print(f'wrote {a.out} ({sheet.width}x{sheet.height}); rows: outline then pixel, '
          f'{len(texts)} line(s)')


if __name__ == '__main__':
    main()
