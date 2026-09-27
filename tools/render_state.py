#!/usr/bin/env python3
"""Render the exact frame stored inside an mGBA PNG savestate (.ssN).

Unlike render_frame.py (which needs a gbarun memory dump) this reads the
`gbAs` chunk straight out of the savestate, so the picture is exactly what the
player saw when they pressed Shift+F1.  That makes it possible to inspect a
reported glitch frame without running the emulator at all.

GBASerializedState layout (0x61000 bytes, see mgba/internal/gba/serialize.h):
    0x00400  I/O
    0x00800  palette
    0x00C00  OAM
    0x01000  VRAM
    0x19000  IWRAM
    0x21000  WRAM

Usage:
  render_state.py <state.ssN> <out.png> [--scale 3] [--layers bgs,obj]
"""
import argparse
import struct
import sys
import zlib

REGIONS = [('io', 0x400, 0x400), ('pram', 0x800, 0x400), ('oam', 0xC00, 0x400),
           ('vram', 0x1000, 0x18000), ('iwram', 0x19000, 0x8000),
           ('wram', 0x21000, 0x40000)]


def read_state(path):
    d = open(path, 'rb').read()
    if d[:8] != b'\x89PNG\r\n\x1a\n':
        return {n: d[o:o + s] for n, o, s in REGIONS}
    i = 8
    while i + 8 <= len(d):
        ln = struct.unpack('>I', d[i:i + 4])[0]
        typ = d[i + 4:i + 8]
        if typ == b'gbAs':
            raw = zlib.decompress(d[i + 8:i + 8 + ln])
            return {n: raw[o:o + s] for n, o, s in REGIONS}
        i += 12 + ln
    raise SystemExit('no gbAs chunk found')


def bgr555(c):
    r = (c & 0x1F) << 3
    g = ((c >> 5) & 0x1F) << 3
    b = ((c >> 10) & 0x1F) << 3
    return (r | (r >> 5), g | (g >> 5), b | (b >> 5))


def tile_pixels(vram, base, bpp8, pal, bank):
    """Return 8x8 list of (r,g,b) or None for index 0."""
    px = []
    for y in range(8):
        row = []
        for x in range(8):
            if bpp8:
                ci = vram[base + y * 8 + x]
            else:
                byte = vram[base + y * 4 + x // 2]
                ci = (byte >> (4 * (x & 1))) & 0xF
                if ci:
                    ci += bank * 16
            row.append(bgr555(struct.unpack_from('<H', pal, ci * 2)[0]) if ci else None)
        px.append(row)
    return px


def render_bg(reg, bg, only):
    io, pal, vram = reg['io'], reg['pram'], reg['vram']
    cnt = struct.unpack_from('<H', io, 8 + 2 * bg)[0]
    char_base = ((cnt >> 2) & 3) * 0x4000
    scr_base = ((cnt >> 8) & 0x1F) * 0x800
    size = (cnt >> 14) & 3
    bpp8 = bool(cnt & 0x80)
    hofs, vofs = struct.unpack_from('<HH', io, 0x10 + 2 * bg)
    hofs &= 0x1FF
    vofs &= 0x1FF
    w_tiles = 32 if size in (0, 1) else 64
    h_tiles = 32 if size in (0, 2) else 64
    out = [[None] * 240 for _ in range(160)]
    for sy in range(20):
        y = (sy + vofs // 8) % h_tiles
        for sx in range(30):
            x = (sx + hofs // 8) % w_tiles
            mx, my = x, y
            if size == 1 and x >= 32:
                mx, my = x - 32, y + 32
            elif size == 2 and y >= 32:
                mx, my = x + 32, y - 32
            ent = struct.unpack_from('<H', vram, scr_base + (my * 32 + mx) * 2)[0]
            tile = ent & 0x3FF
            hf, vf = bool(ent & 0x400), bool(ent & 0x800)
            bank = (ent >> 12) & 0xF
            px = tile_pixels(vram, char_base + tile * (64 if bpp8 else 32), bpp8, pal, bank)
            for ty in range(8):
                for tx in range(8):
                    c = px[7 - ty if vf else ty][7 - tx if hf else tx]
                    if c is None:
                        continue
                    dx, dy = sx * 8 + tx, sy * 8 + ty
                    if 0 <= dx < 240 and 0 <= dy < 160:
                        out[dy][dx] = c
    return out


def render_obj(reg):
    pal, vram, oam = reg['pram'], reg['vram'], reg['oam']
    io = reg['io']
    dispcnt = struct.unpack_from('<H', io, 0)[0]
    one_d = bool(dispcnt & 0x40)
    obj_base = 0x10000  # OBJ tiles always live at VRAM 0x10000 (4bpp: 0x10000)
    out = [[None] * 240 for _ in range(160)]
    for i in range(128):
        o = i * 8
        a0, a1, a2, a3 = struct.unpack_from('<HHHH', oam, o)
        if a0 & 0x200:      # disabled
            continue
        if (a0 & 0x300) == 0x200:
            continue
        y = a0 & 0xFF
        x = a1 & 0x1FF
        if x >= 240:
            x -= 512
        # The game parks unused sprites at y=160, which the GBA clips; only
        # y >= 224 wraps around to the top of the screen.
        if 160 <= y < 224:
            continue
        shape = (a0 >> 14) & 3
        size = (a1 >> 14) & 3
        DIMS = {0: {0: (8, 8), 1: (16, 16), 2: (32, 32), 3: (64, 64)},
                1: {0: (16, 8), 1: (32, 8), 2: (32, 16), 3: (64, 32)},
                2: {0: (8, 16), 1: (8, 32), 2: (16, 32), 3: (32, 64)}}
        w, h = DIMS.get(shape, {}).get(size, (8, 8))
        if (a0 >> 8) & 3 == 3:          # affine sprite: skip (game uses normal)
            continue
        # OAM attribute 2 holds tile (0-9), priority (10-11) and OBJ palette
        # bank (12-15); attribute 1 holds X in its low 9 bits.  Reading the
        # tile out of attribute 1 silently returns the X coordinate instead,
        # which makes every sprite look empty.
        tile = a2 & 0x3FF
        bank = (a2 >> 12) & 0xF
        hf, vf = bool(a1 & 0x1000), bool(a1 & 0x2000)
        tiles_across = w // 8
        n = (w // 8) * (h // 8)
        for t in range(n):
            tdx, tdy = t % tiles_across, t // tiles_across
            if one_d:
                tnum = tile + t
            else:
                tnum = tile + (t // 32) * 32 + (t % 32)
            px = tile_pixels(vram, obj_base + tnum * 32, False, pal, bank)
            for ty in range(8):
                for tx in range(8):
                    c = px[7 - ty if vf else ty][7 - tx if hf else tx]
                    if c is None:
                        continue
                    dx = x + tdx * 8 + tx
                    dy = y + tdy * 8 + ty
                    if 0 <= dx < 240 and 0 <= dy < 160:
                        out[dy][dx] = c
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('state')
    ap.add_argument('out')
    ap.add_argument('--scale', type=int, default=3)
    ap.add_argument('--layers', default='bg0,bg1,bg2,bg3,obj')
    a = ap.parse_args()
    reg = read_state(a.state)
    want = a.layers.split(',')
    from PIL import Image
    img = Image.new('RGB', (240, 160), (0, 0, 0))
    px = img.load()
    for layer in want:
        if layer.startswith('bg'):
            bg = int(layer[2:])
            dispcnt = struct.unpack_from('<H', reg['io'], 0)[0]
            if not (dispcnt & (0x100 << bg)):
                continue
            grid = render_bg(reg, bg, want)
        elif layer == 'obj':
            dispcnt = struct.unpack_from('<H', reg['io'], 0)[0]
            if not (dispcnt & 0x1000):
                continue
            grid = render_obj(reg)
        else:
            continue
        for y in range(160):
            for x in range(240):
                if grid[y][x]:
                    px[x, y] = grid[y][x]
    if a.scale != 1:
        img = img.resize((img.width * a.scale, img.height * a.scale), Image.NEAREST)
    img.save(a.out)
    print(f'{a.out} {img.size}')


if __name__ == '__main__':
    main()
