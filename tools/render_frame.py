#!/usr/bin/env python3
"""Render a GBA BG composite from a gbarun memory dump.

gbarun's own PPM capture is blank with stock libmgba, but the memory dump is
complete, so we rebuild the visible frame here (mode 0/1, 4bpp + 8bpp BGs).

Usage:
  render_frame.py <prefix> <frame> <out.png> [--scale 3] [--bg 0,1,2,3]
"""
import argparse
import os
import struct
import sys

REGIONS = ('bios', 'ewram', 'iwram', 'io', 'palette', 'vram', 'oam', 'rom')


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


def render(reg, scale=3, only=None):
    from PIL import Image
    io, pal, vram = reg['io'], reg['palette'], reg['vram']
    dispcnt = struct.unpack_from('<H', io, 0)[0]
    mode = dispcnt & 7
    if mode not in (0, 1):
        print(f'warning: display mode {mode} not supported (only 0/1)', file=sys.stderr)
    img = Image.new('RGB', (240, 160), (0, 0, 0))
    px = img.load()
    layers = []
    for bg in range(4):
        cnt = struct.unpack_from('<H', io, 8 + 2 * bg)[0]
        if not (dispcnt & (0x100 << bg)):
            continue
        if only and bg not in only:
            continue
        layers.append((cnt & 3, bg, cnt))
    layers.sort(key=lambda t: -t[0])          # low priority drawn first
    for prio, bg, cnt in layers:
        char_base = ((cnt >> 2) & 3) * 0x4000
        scr_base = ((cnt >> 8) & 0x1F) * 0x800
        bpp8 = bool(cnt & 0x80)
        size = (cnt >> 14) & 3
        w_tiles = 32 if size in (0, 1) else 64
        h_tiles = 32 if size in (0, 2) else 64
        for ty in range(h_tiles):
            for tx in range(w_tiles):
                sx, sy = tx, ty
                if size == 1 and tx >= 32:
                    sx, sy = tx - 32, ty + 32
                elif size == 2 and ty >= 32:
                    sx, sy = tx + 32, ty - 32
                elif size == 3:
                    sx = tx % 32 + (32 if tx >= 32 else 0)
                    sy = ty % 32 + (32 if ty >= 32 else 0)
                    if tx >= 32 and ty >= 32:
                        sx, sy = tx, ty
                ent = struct.unpack_from('<H', vram, scr_base + (sy * 32 + sx) * 2)[0]
                tile = ent & 0x3FF
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
                        col = struct.unpack_from('<H', pal, ci * 2)[0]
                        dx, dy = tx * 8 + x, ty * 8 + y
                        if 0 <= dx < 240 and 0 <= dy < 160:
                            px[dx, dy] = bgr555(col)
    if scale != 1:
        img = img.resize((img.width * scale, img.height * scale), Image.NEAREST)
    return img


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('prefix')
    ap.add_argument('frame')
    ap.add_argument('out')
    ap.add_argument('--scale', type=int, default=3)
    ap.add_argument('--bg', default='')
    a = ap.parse_args()
    reg = load_dump(a.prefix, a.frame)
    only = [int(x) for x in a.bg.split(',')] if a.bg else None
    img = render(reg, a.scale, only)
    img.save(a.out)
    print(f'{a.out} {img.size}')


if __name__ == '__main__':
    main()
