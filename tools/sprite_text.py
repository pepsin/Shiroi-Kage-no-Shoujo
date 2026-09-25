#!/usr/bin/env python3
"""Recover sprite-drawn text (and its per-character x positions) from a dump.

The game's dialogue renderer copies glyph bitmaps into the OBJ tile area and
shows them as sprites, so `dump_screen_text.py` (which reads BG tilemaps) sees
nothing.  This matches each 16x16 block of OBJ VRAM against the font table to
recover the code, then reports every visible sprite as (x, y, char).

Usage:
  sprite_text.py <prefix> <frame> [--rom ROM] [--map MAP]
"""
import argparse
import collections
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

FONT_EID = 850
FAT = 0x15A000
BASE = 0x15C000


def load_dump(prefix, frame):
    idx = open(f'{prefix}_f{frame}_mem.idx', encoding='utf-8').read().strip().split('\n')
    blob = open(f'{prefix}_f{frame}_mem.bin', 'rb').read()
    out = {}
    for line in idx:
        if line.startswith('#'):
            continue
        name, start, size, off = line.split()
        out[name] = blob[int(off, 16):int(off, 16) + int(size, 16)]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('prefix')
    ap.add_argument('frame')
    ap.add_argument('--rom', default=os.path.join(ROOT, 'out.gba'))
    ap.add_argument('--map', default=os.path.join(ROOT, 'work', 'glyph_map.ext.csv'))
    ap.add_argument('--dump-glyphs', metavar='OUT.png')
    a = ap.parse_args()

    import csv
    import mapio
    gmap = mapio.load_map(a.map)
    rom = open(a.rom, 'rb').read()
    off, size = struct.unpack_from('<2I', rom, FAT + FONT_EID * 8)
    font = rom[BASE + off:BASE + off + size]
    nglyph = size // 0x80

    # pattern -> glyph index, for both plausible tile orders
    orders = {
        'tl,bl,tr,br': (0, 2, 1, 3),
        'tl,tr,bl,br': (0, 1, 2, 3),
    }
    lookup = {}
    for order in orders.values():
        for g in range(nglyph):
            blk = font[g * 0x80:(g + 1) * 0x80]
            tiles = [blk[0:32], blk[32:64], blk[64:96], blk[96:128]]
            lookup.setdefault(b''.join(tiles[i] for i in order), g)
    print(f'font glyphs: {nglyph}; distinct 16x16 blocks: {len(lookup)}')

    reg = load_dump(a.prefix, a.frame)
    vram, oam, io = reg['vram'], reg['oam'], reg['io']
    dispcnt = struct.unpack_from('<H', io, 0)[0]

    def block_char(tile):
        """If `tile` starts a glyph block in the OBJ area, return the char."""
        p = 0x10000 + tile * 32
        blk = vram[p:p + 0x80]
        g = lookup.get(blk)
        if g is None:
            return None
        code = g if g < 0x20 else g + 1
        return gmap.get(g, ''), code

    rows = collections.defaultdict(list)
    for i in range(128):
        a0, a1, a2, a3 = struct.unpack_from('<HHHH', oam, i * 8)
        if a0 & 0x200 or a0 & 0x100:
            continue
        shape, sz = (a0 >> 14) & 3, (a1 >> 14) & 3
        if (shape, sz) != (0, 1):            # 16x16 = the text renderer's cell
            continue
        x, y = a1 & 0x1FF, a0 & 0xFF
        if x >= 240:
            x -= 512
        if y >= 160:
            continue
        tile = a2 & 0x3FF
        info = block_char(tile)
        if info is None:
            info = ('?', None)
        rows[y].append((x, tile, info))

    for y in sorted(rows):
        items = sorted(rows[y])
        line = ''.join((it[2][0] if it[2][0] else '□') for it in items)
        print(f'y={y:3d} n={len(items):2d} | {line}')
        print('        ' + ' '.join(f'{it[0]}:{it[2][0]}#{it[2][1]}' for it in items))
        xs = [it[0] for it in items]
        print('        x advances:', [xs[k + 1] - xs[k] for k in range(len(xs) - 1)])


if __name__ == '__main__':
    main()
