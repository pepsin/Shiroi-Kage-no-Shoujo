#!/usr/bin/env python3
"""Composite a full GBA Mode 0 frame from a gbarun memory dump (BG0-3 + OBJ).

Usage: frame0.py <prefix>_f<N> <out.png>
"""
import sys, struct
from PIL import Image

def load(p):
    idx = {}
    for line in open(p + '_mem.idx'):
        if line.startswith('#'):
            continue
        n, s, sz, o = line.split()
        idx[n] = (int(s, 16), int(sz, 16), int(o, 16))
    d = open(p + '_mem.bin', 'rb').read()
    return {n: d[o:o + sz] for n, (s, sz, o) in idx.items()}, idx

def rgb555(v):
    R = (v & 31) << 3; G = ((v >> 5) & 31) << 3; B = ((v >> 10) & 31) << 3
    return (R | R >> 5, G | G >> 5, B | B >> 5)

BG_SIZES = {0: (32, 32), 1: (64, 32), 2: (32, 64), 3: (64, 64)}

def render_bg(vram, pal, cnt, W=240, H=160):
    prio = cnt & 3
    cbb = ((cnt >> 2) & 3) * 0x4000
    color256 = (cnt >> 7) & 1
    sbb = ((cnt >> 8) & 0x1F) * 0x800
    size = (cnt >> 14) & 3
    mw, mh = BG_SIZES[size]
    img = Image.new('RGBA', (W, H), (0, 0, 0, 0)); px = img.load()
    for y in range(H):
        for x in range(W):
            tx, ty = x // 8, y // 8
            if size == 0:
                sb = 0
            elif size == 1:
                sb = x // 256
            elif size == 2:
                sb = y // 256
            else:
                sb = (y // 256) * 2 + (x // 256)
            ttx, tty = tx % 32, ty % 32
            eo = sbb + sb * 0x800 + (tty * 32 + ttx) * 2
            if eo + 2 > len(vram):
                continue
            e = vram[eo] | (vram[eo + 1] << 8)
            tile = e & 0x3FF
            pb = (e >> 12) & 0xF
            fx, fy = x % 8, y % 8
            if (e >> 10) & 1: fx = 7 - fx
            if (e >> 11) & 1: fy = 7 - fy
            if color256:
                to = cbb + tile * 64 + fy * 8 + fx
                if to >= len(vram): continue
                ci = vram[to]
                if ci == 0: continue
                pi = ci * 2
            else:
                to = cbb + tile * 32 + fy * 4 + fx // 2
                if to >= len(vram): continue
                b = vram[to]
                ci = (b >> 4) if fx % 2 == 0 else (b & 0xF)
                if ci == 0: continue
                pi = (pb * 16 + ci) * 2
            if pi + 2 > len(pal): continue
            px[x, y] = rgb555(pal[pi] | (pal[pi + 1] << 8)) + (255,)
    return img, prio

OBJ_DIMS = {
    (0, 0): (8, 8), (0, 1): (16, 16), (0, 2): (32, 32), (0, 3): (64, 64),
    (1, 0): (16, 8), (1, 1): (32, 8), (1, 2): (32, 16), (1, 3): (64, 32),
    (2, 0): (8, 16), (2, 1): (8, 32), (2, 2): (16, 32), (2, 3): (32, 64),
}

def render_obj(vram, pal, oam, W=240, H=160):
    img = Image.new('RGBA', (W, H), (0, 0, 0, 0)); px = img.load()
    for i in range(128):
        a0, a1, a2 = struct.unpack_from('<HHH', oam, i * 8)
        if not (a0 & 0x100):          # not enabled (bit8 of attr0 = affine)
            pass
        y = a0 & 0xFF
        mode = (a0 >> 10) & 3
        if mode == 3:
            continue
        affine = (a0 >> 8) & 1
        shape = (a0 >> 14) & 3
        x = a1 & 0x1FF
        if x >= 256: x -= 512
        size = (a1 >> 14) & 3
        tile = a2 & 0x3FF
        prio = (a2 >> 10) & 3
        pb = (a2 >> 12) & 0xF
        color256 = (a0 >> 13) & 1
        hflip = (a1 >> 12) & 1
        vflip = (a1 >> 13) & 1
        ow, oh = OBJ_DIMS.get((shape, size), (8, 8))
        y0 = y - 1 if y > 0 else 0
        if y == 160 and not affine:
            continue
        for yy in range(oh):
            for xx in range(ow):
                sx, sy = x + xx, y0 + yy
                if not (0 <= sx < W and 0 <= sy < H):
                    continue
                fx = (ow - 1 - xx) if hflip else xx
                fy = (oh - 1 - yy) if vflip else yy
                if color256:
                    t = tile + (fy // 8) * (ow // 8) + (fx // 8)
                    to = (t * 64 + (fy % 8) * 8 + (fx % 8)) & 0x1FFFF
                    if to >= len(vram): continue
                    ci = vram[to]
                    if ci == 0: continue
                    pi = 512 + ci * 2
                else:
                    t = tile + (fy // 8) * (ow // 8) * 2 + (fx // 8)
                    to = (t * 32 + (fy % 8) * 4 + (fx % 8) // 2) & 0x1FFFF
                    if to >= len(vram): continue
                    b = vram[to]
                    ci = (b >> 4) if fx % 2 == 0 else (b & 0xF)
                    if ci == 0: continue
                    pi = 512 + (pb * 16 + ci) * 2
                if pi + 2 > len(pal): continue
                px[sx, sy] = rgb555(pal[pi] | (pal[pi + 1] << 8)) + (255,)
    return img, 0

def main():
    prefix = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else prefix + '_frame.png'
    r, idx = load(prefix)
    io = r['io']; vram = r['vram']; pal = r['palette']
    dispcnt = io[0] | (io[1] << 8)
    mode = dispcnt & 7
    print(f"DISPCNT=0x{dispcnt:04X} mode={mode}")
    W, H = 240, 160
    base = Image.new('RGB', (W, H), (0, 0, 0))
    layers = []
    for bg in range(4):
        if not (dispcnt >> (8 + bg)) & 1:
            continue
        cnt = io[8 + 2 * bg] | (io[9 + 2 * bg] << 8)
        img, prio = render_bg(vram, pal, cnt)
        layers.append((prio, img))
        print(f"  BG{bg}CNT=0x{cnt:04X} prio={prio}")
    layers.sort(key=lambda t: -t[0])
    for _, img in layers:
        base.paste(img, (0, 0), img)
    if (dispcnt >> 12) & 1 and 'oam' in r:
        o, _ = render_obj(vram, pal, r['oam'])
        base.paste(o, (0, 0), o)
    base.resize((W * 3, H * 3), Image.NEAREST).save(out)
    print("wrote", out, "colors:", len(base.getcolors(1 << 16) or []))

if __name__ == '__main__':
    main()
