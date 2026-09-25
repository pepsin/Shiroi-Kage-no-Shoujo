#!/usr/bin/env python3
"""Render the GBA OBJ (sprite) layer from a gbarun memory dump.

The title-screen menu plates are sprites, not BG text, which is why they show
up in neither the BG tilemaps nor the font table search.

Usage:
  dump_sprites.py <prefix> <frame> <out.png> [--scale 4] [--sheet]
    --sheet  dump the raw OBJ tile sheet (0x06010000, 1D mapping) instead
"""
import argparse
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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
    ap.add_argument('--scale', type=int, default=4)
    ap.add_argument('--sheet', action='store_true')
    a = ap.parse_args()

    from PIL import Image
    reg = load_dump(a.prefix, a.frame)
    io, pal, vram, oam = reg['io'], reg['palette'], reg['vram'], reg['oam']
    dispcnt = struct.unpack_from('<H', io, 0)[0]
    mode = (dispcnt >> 4) & 3           # OBJ character mapping 0=2D
    one_d = bool(dispcnt & 0x40)
    obj_pal = pal[0x200:0x400]

    def tile_1d(idx, bpp8):
        # 1D mapping: 32 bytes per 4bpp tile, 64 per 8bpp tile
        step = 64 if bpp8 else 32
        return 0x10000 + idx * step

    def tile_2d(idx):
        return 0x10000 + (idx & 0x1FF) * 32 + (idx >> 9) * 0x8000

    if a.sheet:
        img = Image.new('RGB', (16 * 8 * 2, 32 * 8), (0, 0, 0))
        px = img.load()
        for t in range(512):
            base = 0x10000 + t * 32
            tx, ty = (t % 16) * 8, (t // 16) * 8
            for y in range(8):
                for x in range(8):
                    byte = vram[base + y * 4 + x // 2]
                    ci = (byte >> (4 * (x & 1))) & 0xF
                    if ci:
                        px[tx + x, ty + y] = bgr555(struct.unpack_from('<H', obj_pal, ci * 2)[0])
        if a.scale != 1:
            img = img.resize((img.width * a.scale, img.height * a.scale), Image.NEAREST)
        img.save(a.out)
        print(f'wrote {a.out} ({img.width}x{img.height})')
        return

    img = Image.new('RGB', (240, 160), (0, 0, 0))
    px = img.load()
    # shape (attr0 bits 14-15) x size (attr1 bits 14-15) -> (w, h)
    DIMS = {
        0: {0: (8, 8), 1: (16, 16), 2: (32, 32), 3: (64, 64)},
        1: {0: (16, 8), 1: (32, 8), 2: (32, 16), 3: (64, 32)},
        2: {0: (8, 16), 1: (16, 32), 2: (32, 64), 3: (64, 64)},
    }
    order = []
    for i in range(128):
        a0, a1, a2, a3 = struct.unpack_from('<HHHH', oam, i * 8)
        if not (a0 & 0x200):                # attr0 bit 9 = OBJ disable (1 = off)
            pass
        else:
            continue
        if a0 & 0x100:                      # affine: needs the matrix, unused here
            continue
        if (a0 >> 10) & 3:                  # OBJ mode 1/2 = semi-transparent/window
            if ((a0 >> 10) & 3) == 1 and False:
                continue
        y = a0 & 0xFF
        x = a1 & 0x1FF
        if x >= 240:
            x -= 512
        shape = (a0 >> 14) & 3
        size = (a1 >> 14) & 3
        if shape not in DIMS:
            continue
        w, h = DIMS[shape][size]
        tile = a2 & 0x3FF
        bpp8 = bool(a0 & 0x2000)            # attr0 bit 13 = 256/16 colour
        hf, vf = bool(a1 & 0x1000), bool(a1 & 0x2000)
        prio = (a2 >> 10) & 3
        pal_bank = (a2 >> 12) & 0xF
        order.append((prio, i, x, y, w, h, tile, bpp8, hf, vf, pal_bank))

    order.sort(key=lambda t: (-t[0], t[1]))
    for prio, i, x, y, w, h, tile, bpp8, hf, vf, bank in order:
        for tj in range(h // 8):
            for ti in range(w // 8):
                if one_d:
                    t = tile + ti + tj * (w // 8) if bpp8 else tile + ti + tj * (w // 8)
                    base = 0x10000 + t * (64 if bpp8 else 32)
                else:
                    t = tile + ti + tj * 32
                    base = 0x10000 + (t & 0x1FF) * 32 + (t >> 9) * 0x8000
                for yy in range(8):
                    for xx in range(8):
                        sx = 7 - xx if hf else xx
                        sy = 7 - yy if vf else yy
                        if bpp8:
                            ci = vram[base + sy * 8 + sx]
                        else:
                            byte = vram[base + sy * 4 + sx // 2]
                            ci = (byte >> (4 * (sx & 1))) & 0xF
                            if ci:
                                ci += bank * 16
                        if ci == 0:
                            continue
                        dx = x + ti * 8 + xx
                        dy = y + tj * 8 + yy
                        if 0 <= dx < 240 and 0 <= dy < 160:
                            px[dx, dy] = bgr555(struct.unpack_from('<H', obj_pal, ci * 2)[0])
    if a.scale != 1:
        img = img.resize((img.width * a.scale, img.height * a.scale), Image.NEAREST)
    img.save(a.out)
    print(f'wrote {a.out} ({img.width}x{img.height}); {len(order)} sprites')


if __name__ == '__main__':
    main()
