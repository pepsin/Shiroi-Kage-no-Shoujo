#!/usr/bin/env python3
"""Match the OBJ glyph atlas inside a savestate against the ROM font table.

The dialogue renderer copies 16x16 glyph bitmaps into OBJ VRAM and shows them
as sprites.  This tool takes a savestate, walks every sprite that is actually
on screen, and tries to identify its 128-byte bitmap in the ROM font table so
we can tell "this sprite is glyph N" from "this sprite is garbage".

Usage:
  obj_glyphs.py <state.ssN> [--rom out.gba] [--row Y]
"""
import argparse
import csv
import os
import struct
import sys
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAT, BASE, FONT_EID = 0x15A000, 0x15C000, 850
DIMS = {0: {0: (8, 8), 1: (16, 16), 2: (32, 32), 3: (64, 64)},
        1: {0: (16, 8), 1: (32, 8), 2: (32, 16), 3: (64, 32)},
        2: {0: (8, 16), 1: (8, 32), 2: (16, 32), 3: (32, 64)}}


def read_state(path):
    d = open(path, 'rb').read()
    i = 8
    while i + 8 <= len(d):
        ln = struct.unpack('>I', d[i:i + 4])[0]
        if d[i + 4:i + 8] == b'gbAs':
            raw = zlib.decompress(d[i + 8:i + 8 + ln])
            return raw[0x400:0x800], raw[0xC00:0x1000], raw[0x1000:0x19000]
        i += 12 + ln
    raise SystemExit('no gbAs chunk')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('state')
    ap.add_argument('--rom', default=os.path.join(ROOT, 'out.gba'))
    ap.add_argument('--map', default=os.path.join(ROOT, 'work', 'glyph_map.ext.csv'))
    a = ap.parse_args()
    io, oam, vram = read_state(a.state)
    rom = open(a.rom, 'rb').read()
    o, s = struct.unpack_from('<2I', rom, FAT + FONT_EID * 8)
    font = rom[BASE + o: BASE + o + s]
    tbl = {}
    for g in range(len(font) // 0x80):
        tbl.setdefault(font[g * 0x80:(g + 1) * 0x80], []).append(g)
    gmap = {}
    for r in csv.DictReader(open(a.map, encoding='utf-8')):
        ch = (r.get('char') or '').strip()
        if ch:
            gmap[int(r['code'], 16)] = ch
    dispcnt = struct.unpack_from('<H', io, 0)[0]
    one_d = bool(dispcnt & 0x40)
    print(f'{a.state}: DISPCNT=0x{dispcnt:04X} 1D={int(one_d)} objEnable={int(bool(dispcnt & 0x1000))}')
    bad = 0
    seen = []
    for i in range(128):
        a0, a1, a2, a3 = struct.unpack_from('<HHHH', oam, i * 8)
        if a0 & 0x200:
            continue
        y = a0 & 0xFF
        x = a1 & 0x1FF
        if x >= 240:
            x -= 512
        if 160 <= y < 224:
            continue
        shape, size = (a0 >> 14) & 3, (a1 >> 14) & 3
        if (a0 >> 8) & 3 == 3:
            continue
        w, h = DIMS.get(shape, {}).get(size, (8, 8))
        tile = a1 & 0x3FF
        bank = (a3 >> 12) & 0xF
        # reassemble the full sprite bitmap from its 8x8 tiles, then look for
        # the 16x16 glyph blob (4 tiles in TL,TR,BL,BR order) inside the ROM
        cols = w // 8
        n = cols * (h // 8)
        tiles = []
        for t in range(n):
            tnum = tile + t if one_d else tile + (t // 32) * 32 + (t % 32)
            tiles.append(vram[0x10000 + tnum * 32:0x10000 + tnum * 32 + 32])
        for k in range(0, n, 4):
            if k + 4 > n or w != 16 or h != 16:
                continue
            tl, tr, bl, br = tiles[k], tiles[k + 1], tiles[k + 2], tiles[k + 3]
            for order, name in ((b''.join([tl, tr, bl, br]), 'TLTRBLBR'),
                                (b''.join([tl, bl, tr, br]), 'TLBLTRBR')):
                g = tbl.get(order)
                if g:
                    seen.append((x, y, i, g[0], gmap.get(g[0], '?'), bank, name))
                    break
            else:
                bad += 1
                seen.append((x, y, i, None, '??', bank, ''))
    seen.sort(key=lambda t: (t[1], t[0]))
    for x, y, i, g, ch, bank, order in seen:
        gs = f'glyph {g:4d}' if g is not None else 'NO MATCH '
        print(f'  sprite {i:3d} x={x:3d} y={y:3d} pal={bank:2d} {gs} {ch} {order}')
    print(f'total sprites on screen: {len(seen)}, unmatched: {bad}')


if __name__ == '__main__':
    main()
