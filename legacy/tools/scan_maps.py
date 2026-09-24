#!/usr/bin/env python3
"""Brute-force: try every 2KB-aligned VRAM base as a 32x32 4bpp tilemap and render.

Usage: scan_maps.py <prefix>_f<N> <outdir> [color256]
"""
import sys, os, struct
from PIL import Image

def load(p):
    idx = {}
    for line in open(p + '_mem.idx'):
        if line.startswith('#'):
            continue
        n, s, sz, o = line.split()
        idx[n] = (int(s, 16), int(sz, 16), int(o, 16))
    d = open(p + '_mem.bin', 'rb').read()
    return {n: d[o:o + sz] for n, (s, sz, o) in idx.items()}

def rgb555(v):
    r = (v & 0x1F) << 3; g = ((v >> 5) & 0x1F) << 3; b = ((v >> 10) & 0x1F) << 3
    return (r | r >> 5, g | g >> 5, b | b >> 5)

def render(vram, pal, mapbase, charbase, color256, W=240, H=160):
    img = Image.new('RGB', (W, H), (0, 0, 0)); px = img.load()
    for y in range(H):
        for x in range(W):
            tx, ty = (x // 8) % 32, (y // 8) % 32
            eo = mapbase + (ty * 32 + tx) * 2
            if eo + 2 > len(vram):
                continue
            e = vram[eo] | (vram[eo + 1] << 8)
            tile = e & 0x3FF
            pal_bank = (e >> 12) & 0xF
            fx, fy = x % 8, y % 8
            if (e >> 10) & 1:
                fx = 7 - fx
            if (e >> 11) & 1:
                fy = 7 - fy
            if color256:
                to = charbase + tile * 64 + fy * 8 + fx
                if to >= len(vram):
                    continue
                ci = vram[to]
                pi = ci * 2
            else:
                to = charbase + tile * 32 + fy * 4 + (fx // 2)
                if to >= len(vram):
                    continue
                byte = vram[to]
                ci = (byte >> 4) if fx % 2 == 0 else (byte & 0xF)
                if ci == 0:
                    continue
                pi = (pal_bank * 16 + ci) * 2
            if pi + 2 > len(pal):
                continue
            px[x, y] = rgb555(pal[pi] | (pal[pi + 1] << 8))
    return img

def score(img):
    cols = img.getcolors(1 << 16) or []
    return len(cols)

def main():
    prefix = sys.argv[1]
    outdir = sys.argv[2]
    color256 = len(sys.argv) > 3 and sys.argv[3] == 'color256'
    os.makedirs(outdir, exist_ok=True)
    r = load(prefix)
    vram, pal = r['vram'], r['palette']
    results = []
    for mapbase in range(0, len(vram) - 2048, 0x800):
        for charbase in (0, 0x4000, 0x8000, 0xC000):
            img = render(vram, pal, mapbase, charbase, color256)
            n = score(img)
            if n > 2:
                results.append((n, mapbase, charbase, img))
    results.sort(key=lambda t: -t[0])
    print(f"candidates with >2 colors: {len(results)}")
    for n, mb, cb, img in results[:12]:
        path = os.path.join(outdir, f"map{mb:05X}_char{cb:05X}_{n}c.png")
        img.resize((720, 480), Image.NEAREST).save(path)
        print(f"  map=0x{mb:05X} char=0x{cb:05X} colors={n} -> {path}")

if __name__ == '__main__':
    main()
