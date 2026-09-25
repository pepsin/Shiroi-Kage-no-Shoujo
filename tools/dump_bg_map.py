#!/usr/bin/env python3
"""Render a whole GBA BG map (all tiles, not just the 240x160 window) to PNG.

Used to inspect pre-rendered background art (title screen menu plates etc.)
that is stored as a tile strip rather than font glyphs.

Usage:
  dump_bg_map.py <prefix> <frame> <out.png> [--bg N] [--scale 2] [--map-only]
"""
import argparse
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))


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


def bgr555(c):
    r = (c & 0x1F) << 3
    g = ((c >> 5) & 0x1F) << 3
    b = ((c >> 10) & 0x1F) << 3
    return (r | (r >> 5), g | (g >> 5), b | (b >> 5))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('prefix')
    ap.add_argument('frame')
    ap.add_argument('out')
    ap.add_argument('--bg', type=int, default=3)
    ap.add_argument('--scale', type=int, default=2)
    ap.add_argument('--map-only', action='store_true',
                    help='dump the raw tile-index map as greyscale instead of pixels')
    a = ap.parse_args()

    from PIL import Image
    reg = load_dump(a.prefix, a.frame)
    io, pal, vram = reg['io'], reg['palette'], reg['vram']
    bg = a.bg
    cnt = struct.unpack_from('<H', io, 8 + 2 * bg)[0]
    char_base = ((cnt >> 2) & 3) * 0x4000
    scr_base = ((cnt >> 8) & 0x1F) * 0x800
    bpp8 = bool(cnt & 0x80)
    size = (cnt >> 14) & 3
    w_tiles = 32 if size in (0, 1) else 64
    h_tiles = 32 if size in (0, 2) else 64
    hofs = struct.unpack_from('<H', io, 0x10 + 2 * bg)[0] & 0x1FF
    vofs = struct.unpack_from('<H', io, 0x12 + 2 * bg)[0] & 0x1FF
    print(f'BG{bg} cnt={cnt:#06x} charBase={char_base:#x} scrBase={scr_base:#x} '
          f'{w_tiles}x{h_tiles} tiles, hofs={hofs} vofs={vofs}')

    img = Image.new('RGB', (w_tiles * 8, h_tiles * 8), (0, 0, 0))
    px = img.load()
    used = set()
    for ty in range(h_tiles):
        for tx in range(w_tiles):
            ent = struct.unpack_from('<H', vram, scr_base + (ty * 32 + tx) * 2)[0]
            tile = ent & 0x3FF
            used.add(tile)
            if a.map_only:
                v = (tile * 37) % 256
                for y in range(8):
                    for x in range(8):
                        px[tx * 8 + x, ty * 8 + y] = (v, v, v)
                continue
            hf, vf = bool(ent & 0x400), bool(ent & 0x800)
            bank = (ent >> 12) & 0xF if not bpp8 else 0
            base = char_base + tile * (64 if bpp8 else 32)
            for y in range(8):
                for x in range(8):
                    xx = 7 - x if hf else x
                    yy = 7 - y if vf else y
                    if bpp8:
                        ci = vram[base + yy * 8 + xx]
                    else:
                        byte = vram[base + yy * 4 + xx // 2]
                        ci = (byte >> (4 * (xx & 1))) & 0xF
                        if ci:
                            ci += bank * 16
                    if ci == 0:
                        continue
                    px[tx * 8 + x, ty * 8 + y] = bgr555(struct.unpack_from('<H', pal, ci * 2)[0])
    if a.scale != 1:
        img = img.resize((img.width * a.scale, img.height * a.scale), Image.NEAREST)
    img.save(a.out)
    print(f'wrote {a.out} ({img.width}x{img.height}); distinct tiles used: {len(used)}')


if __name__ == '__main__':
    main()
