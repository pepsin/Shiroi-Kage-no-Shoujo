#!/usr/bin/env python3
"""Decode the 16x16 font-tilemap of a savestate BG and check every glyph.

The game keeps its loaded character set in VRAM as a 16x16 glyph atlas: a
tilemap entry points at the glyph's first 8x8 tile (tile = glyph * 4), and the
per-glyph advance table gives the source ROM table index.  This tool reads a
savestate, decodes each visible glyph with work/glyph_map.ext.csv, and reports
(a) the decoded text and (b) how many glyph bitmaps differ from the current
ROM's font table -- i.e. how many characters on screen are stale/corrupted.
"""
import argparse
import csv
import os
import struct
import sys
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGIONS = {'io': (0x400, 0x400), 'pram': (0x800, 0x400), 'oam': (0xC00, 0x400),
           'vram': (0x1000, 0x18000), 'iwram': (0x19000, 0x8000), 'wram': (0x21000, 0x40000)}
FAT, BASE, FONT_EID, ADV_EID = 0x15A000, 0x15C000, 850, 851


def read_state(path):
    d = open(path, 'rb').read()
    i = 8
    while i + 8 <= len(d):
        ln = struct.unpack('>I', d[i:i + 4])[0]
        if d[i + 4:i + 8] == b'gbAs':
            raw = zlib.decompress(d[i + 8:i + 8 + ln])
            return {n: raw[o:o + s] for n, (o, s) in REGIONS.items()}
        i += 12 + ln
    raise SystemExit('no gbAs chunk')


def load_map(path):
    m = {}
    for r in csv.DictReader(open(path, encoding='utf-8')):
        ch = (r.get('char') or '').strip()
        if ch:
            m[int(r['code'], 16)] = ch
    return m


def font_table(rom):
    o, s = struct.unpack_from('<2I', rom, FAT + FONT_EID * 8)
    return rom[BASE + o: BASE + o + s]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('state')
    ap.add_argument('--rom', default=os.path.join(ROOT, 'out.gba'))
    ap.add_argument('--map', default=os.path.join(ROOT, 'work', 'glyph_map.ext.csv'))
    ap.add_argument('--bg', default='')
    a = ap.parse_args()

    reg = read_state(a.state)
    rom = open(a.rom, 'rb').read()
    font = font_table(rom)
    gmap = load_map(a.map)
    vram, io = reg['vram'], reg['io']
    dispcnt = struct.unpack_from('<H', io, 0)[0]
    bgs = [int(x) for x in a.bg.split(',')] if a.bg else [b for b in range(4) if dispcnt & (0x100 << b)]
    for bg in bgs:
        cnt = struct.unpack_from('<H', io, 8 + 2 * bg)[0]
        char_base = ((cnt >> 2) & 3) * 0x4000
        scr_base = ((cnt >> 8) & 0x1F) * 0x800
        # BGxHOFS/BGxVOFS are 4 bytes apart (HOFS+VOFS per BG), not 2 - see
        # render_state.py; at 0x10 + 2*bg this silently reads the previous
        # background's scroll for bg >= 1.
        hofs, vofs = struct.unpack_from('<HH', io, 0x10 + 4 * bg)
        hofs &= 0x1FF
        vofs &= 0x1FF
        print(f'--- BG{bg} char_base=0x{char_base:05X} scr_base=0x{scr_base:05X} '
              f'cnt=0x{cnt:04X} hofs={hofs} vofs={vofs}')
        for gy in range(10):
            ty = (gy * 2 + vofs // 8) % 32
            line = []
            stale = 0
            for gx in range(30):
                tx = (gx + hofs // 8) % 32
                ent = struct.unpack_from('<H', vram, scr_base + (ty * 32 + tx) * 2)[0]
                tile = ent & 0x3FF
                glyph = tile // 4
                ch = gmap.get(glyph, '?')
                # compare 4 tiles of this glyph against the ROM table
                blob = b''.join(vram[char_base + (tile + t) * 32: char_base + (tile + t) * 32 + 32]
                                for t in range(4))
                ref = font[glyph * 0x80:(glyph + 1) * 0x80] if (glyph + 1) * 0x80 <= len(font) else b''
                if blob and any(blob) and blob != ref:
                    stale += 1
                    ch = '[' + ch + ']'
                line.append(ch or '.')
            print(f'{gy:2d}| ' + ''.join(line))
            if stale:
                print(f'    ^ {stale} stale glyph(s) on this row')
        print()


if __name__ == '__main__':
    main()
