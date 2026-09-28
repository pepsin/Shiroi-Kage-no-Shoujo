#!/usr/bin/env python3
"""Reconstruct the text currently on screen from a gbarun memory dump.

The game draws text as 16x16 glyphs: a tilemap entry points at the glyph's
first 8x8 tile, and glyph = tile / 4.  Reading the visible part of each BG
(accounting for the scroll registers) therefore recovers the exact string the
player is looking at -- which is how a screenshot turns into a ROM location.

Usage:
  dump_screen_text.py <prefix> <frame> [--map work/glyph_map.ext.csv]
                      [--all-bg] [--rows 0:32]
"""
import argparse
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

TEXTY = set('、。，！？「」『』・：；“”‘’（）〈〉《》‥…□○■×＋＜＞％')


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


def screen_rows(reg, gmap):
    import csv
    io, vram = reg['io'], reg['vram']
    dispcnt = struct.unpack_from('<H', io, 0)[0]
    out = []
    for bg in range(4):
        if not (dispcnt & (0x100 << bg)):
            continue
        cnt = struct.unpack_from('<H', io, 8 + 2 * bg)[0]
        char_base = ((cnt >> 2) & 3) * 0x4000
        scr_base = ((cnt >> 8) & 0x1F) * 0x800
        size = (cnt >> 14) & 3
        # BGxHOFS/VOFS are 4 bytes apart, BGxCNT 2 - see render_state.py
        hofs = struct.unpack_from('<H', io, 0x10 + 4 * bg)[0] & 0x1FF
        vofs = struct.unpack_from('<H', io, 0x12 + 4 * bg)[0] & 0x1FF
        w_tiles = 32 if size in (0, 1) else 64
        h_tiles = 32 if size in (0, 2) else 64
        lines = []
        for sy in range(20):                    # 160 px / 8 = 20 tile rows
            y = (sy + vofs // 8) % h_tiles
            row = []
            for sx in range(30):                # 240 px / 8 = 30 tile cols
                x = (sx + hofs // 8) % w_tiles
                # 64x32 / 32x64 maps put the second screenful after the first
                mx, my = x, y
                if size == 1 and x >= 32:
                    mx, my = x - 32, y + 32
                elif size == 2 and y >= 32:
                    mx, my = x + 32, y - 32
                elif size == 3:
                    mx = (x % 32) + (32 if y >= 32 else 0)
                    my = (y % 32) + (32 if x >= 32 else 0)
                    if x >= 32 and y >= 32:
                        mx, my = x, y
                ent = struct.unpack_from('<H', vram, scr_base + (my * 32 + mx) * 2)[0]
                tile = ent & 0x3FF
                row.append(tile)
            lines.append(row)
        out.append((bg, lines))
    return out


def rows_to_text(lines, gmap):
    """Each visible row -> decoded string (glyph = tile // 4)."""
    res = []
    for row in lines:
        s = []
        for tile in row:
            if tile % 4:
                s.append(' ')
                continue
            gi = tile // 4
            ch = gmap.get(gi, '')
            s.append(ch if ch else ' ')
        res.append(''.join(s))
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('prefix')
    ap.add_argument('frame')
    ap.add_argument('--map', default=os.path.join(ROOT, 'work', 'glyph_map.ext.csv'))
    ap.add_argument('--all-bg', action='store_true')
    a = ap.parse_args()
    import mapio
    gmap = mapio.load_map(a.map)
    reg = load_dump(a.prefix, a.frame)
    for bg, lines in screen_rows(reg, gmap):
        text = rows_to_text(lines, gmap)
        joined = '\n'.join(text).strip()
        if not joined:
            continue
        printable = sum(1 for c in joined if c.strip())
        if printable < 4 and not a.all_bg:
            continue
        print(f'--- BG{bg} ---')
        for i, line in enumerate(text):
            if line.strip():
                print(f'  {i:>2}| {line.rstrip()}')


if __name__ == '__main__':
    main()
