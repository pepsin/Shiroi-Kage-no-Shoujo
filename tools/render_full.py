#!/usr/bin/env python3
"""Composite the full GBA screen (all BGs + OBJ sprites) from a gbarun dump.

render_frame.py ignores BG scroll and the sprite layer, which is fine for
reading the text layer but hides sprite-drawn UI (the title menu plates) and
mis-places anything whose scroll is not 0.  This renders what the player sees.

Usage:
  render_full.py <prefix> <frame> <out.png> [--scale 3] [--no-obj]
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


DIMS = {
    0: {0: (8, 8), 1: (16, 16), 2: (32, 32), 3: (64, 64)},
    1: {0: (16, 8), 1: (32, 8), 2: (32, 16), 3: (64, 32)},
    2: {0: (8, 16), 1: (16, 32), 2: (32, 64), 3: (64, 64)},
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('prefix')
    ap.add_argument('frame')
    ap.add_argument('out')
    ap.add_argument('--scale', type=int, default=3)
    ap.add_argument('--no-obj', action='store_true')
    ap.add_argument('--only-bg', type=int, default=None,
                    help='render only this BG layer (others stay transparent)')
    a = ap.parse_args()

    from PIL import Image
    reg = load_dump(a.prefix, a.frame)
    io, pal, vram, oam = reg['io'], reg['palette'], reg['vram'], reg['oam']
    bgpal, objpal = pal[:0x200], pal[0x200:0x400]
    dispcnt = struct.unpack_from('<H', io, 0)[0]
    mode = dispcnt & 7
    W, H = 240, 160
    canvas = [None] * (W * H)          # None = backdrop

    def put(x, y, col):
        if 0 <= x < W and 0 <= y < H:
            canvas[y * W + x] = col

    if mode in (0, 1):
        layers = []
        for bg in range(4):
            cnt = struct.unpack_from('<H', io, 8 + 2 * bg)[0]
            if not (dispcnt & (0x100 << bg)):
                continue
            if a.only_bg is not None and bg != a.only_bg:
                continue
            layers.append((cnt & 3, bg, cnt))
        layers.sort(key=lambda t: -t[0])           # 3 = lowest priority: draw first
        for prio, bg, cnt in layers:
            char_base = ((cnt >> 2) & 3) * 0x4000
            scr_base = ((cnt >> 8) & 0x1F) * 0x800
            bpp8 = bool(cnt & 0x80)
            size = (cnt >> 14) & 3
            w_tiles = 32 if size in (0, 1) else 64
            h_tiles = 32 if size in (0, 2) else 64
            # BGxHOFS/VOFS are 4 bytes apart, BGxCNT 2 - see render_state.py
            hofs = struct.unpack_from('<H', io, 0x10 + 4 * bg)[0] & 0x1FF
            vofs = struct.unpack_from('<H', io, 0x12 + 4 * bg)[0] & 0x1FF
            for sy in range(H):
                my = (sy + vofs) % (h_tiles * 8)
                ty = my // 8
                for sx in range(W):
                    if canvas[sy * W + sx] is not None:
                        continue                     # a higher-priority layer won
                    mx = (sx + hofs) % (w_tiles * 8)
                    tx = mx // 8
                    bx, by = tx, ty
                    if size == 1 and tx >= 32:
                        bx, by = tx - 32, ty + 32
                    elif size == 2 and ty >= 32:
                        bx, by = tx + 32, ty - 32
                    base = scr_base + (by * 32 + bx) * 2
                    if base + 2 > len(vram):
                        continue
                    ent = struct.unpack_from('<H', vram, base)[0]
                    tile = ent & 0x3FF
                    hf, vf = bool(ent & 0x400), bool(ent & 0x800)
                    bank = (ent >> 12) & 0xF if not bpp8 else 0
                    px = 7 - (mx % 8) if hf else mx % 8
                    py = 7 - (my % 8) if vf else my % 8
                    off = char_base + tile * (64 if bpp8 else 32) + py * (8 if bpp8 else 4) + (px if bpp8 else px // 2)
                    if off >= len(vram):
                        continue
                    if bpp8:
                        ci = vram[off]
                    else:
                        ci = (vram[off] >> (4 * (px & 1))) & 0xF
                        if ci:
                            ci += bank * 16
                    if ci == 0:
                        continue
                    put(sx, sy, bgr555(struct.unpack_from('<H', bgpal, ci * 2)[0]))

    if not a.no_obj and (dispcnt & 0x1000):
        one_d = bool(dispcnt & 0x40)
        sprites = []
        for i in range(128):
            a0, a1, a2, a3 = struct.unpack_from('<HHHH', oam, i * 8)
            if a0 & 0x200 or a0 & 0x100:
                continue
            shape, size = (a0 >> 14) & 3, (a1 >> 14) & 3
            if shape not in DIMS:
                continue
            w, h = DIMS[shape][size]
            x, y = a1 & 0x1FF, a0 & 0xFF
            if x >= 240:
                x -= 512
            sprites.append(((a2 >> 10) & 3, i, x, y, w, h, a2 & 0x3FF,
                            bool(a0 & 0x2000), bool(a1 & 0x1000), bool(a1 & 0x2000),
                            (a2 >> 12) & 0xF))
        sprites.sort(key=lambda t: (t[0], t[1]))     # low priority first, then OAM order
        for prio, i, x, y, w, h, tile, bpp8, hf, vf, bank in sprites:
            for tj in range(h // 8):
                for ti in range(w // 8):
                    t = tile + ti + tj * (w // 8)
                    off = 0x10000 + t * (64 if bpp8 else 32)
                    for yy in range(8):
                        for xx in range(8):
                            sxx = 7 - xx if hf else xx
                            syy = 7 - yy if vf else yy
                            if bpp8:
                                ci = vram[off + syy * 8 + sxx]
                            else:
                                ci = (vram[off + syy * 4 + sxx // 2] >> (4 * (sxx & 1))) & 0xF
                                if ci:
                                    ci += bank * 16
                            if ci == 0:
                                continue
                            put(x + ti * 8 + xx, y + tj * 8 + yy,
                                bgr555(struct.unpack_from('<H', objpal, ci * 2)[0]))

    img = Image.new('RGB', (W, H), (0, 0, 0))
    px = img.load()
    for y in range(H):
        for x in range(W):
            c = canvas[y * W + x]
            if c is not None:
                px[x, y] = c
    if a.scale != 1:
        img = img.resize((W * a.scale, H * a.scale), Image.NEAREST)
    img.save(a.out)
    print(f'wrote {a.out} ({img.width}x{img.height}) mode={mode} '
          f'dispcnt={dispcnt:#06x}')


if __name__ == '__main__':
    main()
