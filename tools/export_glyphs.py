#!/usr/bin/env python3
"""Export every glyph of a built ROM's font table as one PNG per character.

The PNGs are rendered with the game's own text palette, so they show exactly
what the player sees: ink level 1 = the glyph body (80,80,80), level 2 = the
1px right-hand drop shadow (104,96,88), 0 = the background (248,248,248).
The original Japanese font and everything the Chinese build injects both use
only those two levels, so everything lines up in weight and colour.

Files are named after the character itself; glyphs appended by the Chinese
build (index >= 0x6A8) get a "（新）" suffix so they are easy to find.
Duplicated characters (the same char exists at several indices) and unused
slots are disambiguated with the glyph index.

Usage:
  export_glyphs.py [--rom out.gba] [--map work/glyph_map.ext.csv]
                   [--out data/glyph_png] [--scale 8] [--new-from 0x6A8]
"""
import argparse
import csv
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

FAT = 0x15A000
BASE = 0x15C000
FONT_EID = 850
GLYPH_BYTES = 0x80
BAD = {'/': '／', ':': '：', '\\': '＼', '\n': '', '\r': '', '\t': ' '}


def font_table(rom):
    off, size = struct.unpack_from('<2I', rom, FAT + FONT_EID * 8)
    assert off, 'font entry not found'
    start = BASE + off
    return rom[start:start + size], size // GLYPH_BYTES


def glyph_pixels(b):
    """128 bytes -> 16x16 list of 0..15."""
    px = [[0] * 16 for _ in range(16)]
    for t in range(4):
        tx, ty = (t & 1) * 8, (t >> 1) * 8
        for y in range(8):
            for x in range(4):
                v = b[t * 32 + y * 4 + x]
                px[ty + y][tx + x * 2] = v & 0xF
                px[ty + y][tx + x * 2 + 1] = (v >> 4) & 0xF
    return px


def safe_stem(ch, idx):
    if not ch or not ch.strip():
        return f'未使用_{idx:03X}'
    s = ch
    for k, v in BAD.items():
        s = s.replace(k, v)
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--rom', default=os.path.join(ROOT, 'out.gba'))
    ap.add_argument('--map', default=os.path.join(ROOT, 'work', 'glyph_map.ext.csv'))
    ap.add_argument('--out', default=os.path.join(ROOT, 'data', 'glyph_png'))
    ap.add_argument('--scale', type=int, default=8)
    ap.add_argument('--new-from', type=lambda s: int(s, 0), default=0x6A8)
    ap.add_argument('--clean', action='store_true', help='remove existing PNGs first')
    a = ap.parse_args()

    from PIL import Image
    rom = open(a.rom, 'rb').read()
    table, count = font_table(rom)
    gmap = {}
    with open(a.map, encoding='utf-8') as f:
        for r in csv.DictReader(f):
            gmap[int(r['dec'], 0)] = r['char']
    os.makedirs(a.out, exist_ok=True)
    if a.clean:
        for f in os.listdir(a.out):
            if f.endswith('.png'):
                os.remove(os.path.join(a.out, f))
    used = {}
    rows = []
    n_new = 0
    for idx in range(count):
        blk = table[idx * GLYPH_BYTES:(idx + 1) * GLYPH_BYTES]
        if not blk:
            continue
        px = glyph_pixels(blk)
        ink = sum(1 for row in px for v in row if v)
        ch = gmap.get(idx, '')
        stem = safe_stem(ch, idx)
        is_new = idx >= a.new_from and bool(ch.strip()) and ink > 0
        if is_new:
            stem += '（新）'
            n_new += 1
        # macOS is case-insensitive: 'C.png' and 'c.png' are the same file,
        # so uniqueness has to be tracked case-folded.
        key = stem.casefold()
        n = used.get(key, 0)
        used[key] = n + 1
        if n:
            stem = f'{stem}_{n + 1}'
        name = f'{stem}.png'
        # the palette the game uses for text (measured from a text frame)
        ramp = {0: (248, 248, 248), 1: (80, 80, 80), 2: (104, 96, 88)}
        img = Image.new('RGB', (16, 16), ramp[0])
        p = img.load()
        for y in range(16):
            for x in range(16):
                v = px[y][x]
                if v:
                    p[x, y] = ramp.get(v, (0, 0, 0))
        if a.scale != 1:
            img = img.resize((16 * a.scale, 16 * a.scale), Image.NEAREST)
        # only three colours -> store indexed, ~20x smaller on disk
        img.convert('P', palette=Image.ADAPTIVE, colors=4).save(
            os.path.join(a.out, name), optimize=True)
        rows.append({'file': name, 'index': f'{idx:03X}', 'code': f'{idx + 1:03X}',
                     'char': ch, 'is_new': int(is_new), 'ink': ink})
    with open(os.path.join(a.out, 'manifest.tsv'), 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['file', 'index', 'code', 'char', 'is_new', 'ink'],
                           delimiter='\t')
        w.writeheader()
        w.writerows(rows)
    print(f'exported {len(rows)} glyphs to {a.out}  ({n_new} marked （新）)')


if __name__ == '__main__':
    main()
