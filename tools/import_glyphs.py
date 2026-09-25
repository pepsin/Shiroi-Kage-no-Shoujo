#!/usr/bin/env python3
"""Import hand-edited glyph PNGs back into a ROM's font table.

Companion to export_glyphs.py: the manifest written there maps each PNG file
back to its glyph index, so a file edited in any image tool can be written
straight back.

Pixel mapping (matches the game's text palette: bg 248, body 80, shadow 104):
    light  -> 0 (transparent)
    mid    -> 2 (shadow)
    dark   -> 1 (body)

With --autoshadow the two-level style of the original font is re-applied:
every body pixel gets a 1px shadow to its right, exactly like the 1703
original glyphs.

Usage:
  import_glyphs.py [--rom out.gba] [--out out2.gba] [--dir data/glyph_png]
                   [--manifest data/glyph_png/manifest.tsv]
                   [--only-new] [--autoshadow] [--dry-run]
"""
import argparse
import csv
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAT = 0x15A000
BASE = 0x15C000
FONT_EID = 850
GLYPH_BYTES = 0x80
BG, BODY, SHADOW = 248, 80, 104


def levels_from_image(path, scale):
    from PIL import Image
    img = Image.open(path).convert('L')
    if img.size != (16, 16):
        img = img.resize((16, 16), Image.BOX if img.size[0] > 16 else Image.NEAREST)
    p = img.load()
    lv = [[0] * 16 for _ in range(16)]
    for y in range(16):
        for x in range(16):
            v = p[x, y]
            if v > (BG + SHADOW) / 2:
                lv[y][x] = 0
            elif v > (BODY + SHADOW) / 2:
                lv[y][x] = 2
            else:
                lv[y][x] = 1
    return lv


def pack(lv):
    out = bytearray(GLYPH_BYTES)
    for t in range(4):
        tx, ty = (t & 1) * 8, (t >> 1) * 8
        for y in range(8):
            for x in range(4):
                lo = lv[ty + y][tx + x * 2] & 0xF
                hi = lv[ty + y][tx + x * 2 + 1] & 0xF
                out[t * 32 + y * 4 + x] = (hi << 4) | lo
    return bytes(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--rom', default=os.path.join(ROOT, 'out.gba'))
    ap.add_argument('--out', default='')
    ap.add_argument('--dir', default=os.path.join(ROOT, 'data', 'glyph_png'))
    ap.add_argument('--manifest', default='')
    ap.add_argument('--only-new', action='store_true')
    ap.add_argument('--autoshadow', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    rom = bytearray(open(a.rom, 'rb').read())
    off, size = struct.unpack_from('<2I', rom, FAT + FONT_EID * 8)
    base = BASE + off
    man = a.manifest or os.path.join(a.dir, 'manifest.tsv')
    rows = list(csv.DictReader(open(man, encoding='utf-8'), delimiter='\t'))
    n = 0
    for r in rows:
        if a.only_new and r['is_new'] != '1':
            continue
        path = os.path.join(a.dir, r['file'])
        if not os.path.exists(path):
            continue
        lv = levels_from_image(path, 1)
        if a.autoshadow:
            for y in range(16):
                for x in range(16):
                    if lv[y][x] == 1 and x + 1 < 16 and lv[y][x + 1] == 0:
                        lv[y][x + 1] = 2
        idx = int(r['index'], 16)
        rom[base + idx * GLYPH_BYTES:base + (idx + 1) * GLYPH_BYTES] = pack(lv)
        n += 1
    print(f'imported {n} glyphs from {a.dir}')
    if a.dry_run:
        return
    if not a.out:
        raise SystemExit('--out is required unless --dry-run')
    open(a.out, 'wb').write(bytes(rom))
    print(f'wrote {a.out}')


if __name__ == '__main__':
    main()
