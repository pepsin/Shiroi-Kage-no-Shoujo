#!/usr/bin/env python3
"""Render a GBA screen from a gbarun memory dump (independent of mGBA's renderer).

Reads <prefix>_f<N>_mem.bin + .idx, reconstructs BG layers + OBJ for the
configured video mode, and writes a PNG.

Usage: render_dump.py <prefix>_f<N> [out.png]
"""
import sys
import struct
from PIL import Image

def load(prefix):
    regions = {}
    with open(prefix + "_mem.idx") as f:
        for line in f:
            if line.startswith("#"):
                continue
            name, start, size, off = line.split()
            regions[name] = (int(start, 16), int(size, 16), int(off, 16))
    data = open(prefix + "_mem.bin", "rb").read()
    out = {}
    for name, (start, size, off) in regions.items():
        out[name] = memoryview(data)[off:off + size]
    return out

def rgb555(v):
    r = (v & 0x1F) << 3
    g = ((v >> 5) & 0x1F) << 3
    b = ((v >> 10) & 0x1F) << 3
    return (r | r >> 5, g | g >> 5, b | b >> 5)

def render_bg(regs, bg, W=240, H=160):
    bgcnt = regs["bgcnt"][bg]
    if not (regs["dispcnt"] >> (8 + bg)) & 1:
        return None
    priority = bgcnt & 3
    cbb = (bgcnt >> 2) & 3          # character base block (16KB units)
    mosaic = (bgcnt >> 6) & 1
    color256 = (bgcnt >> 7) & 1
    sbb = (bgcnt >> 8) & 0x1F       # screen base block (2KB units)
    size = (bgcnt >> 14) & 3
    vram = regs["vram"]
    palette = regs["palette"]

    sizes = {0: (32, 32), 1: (64, 32), 2: (32, 64), 3: (64, 64)}
    mw, mh = sizes[size]
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    px = img.load()
    for y in range(H):
        for x in range(W):
            tx = (x // 8) % mw
            ty = (y // 8) % mh
            # screenblock arrangement for 4 screenblocks
            if size == 0:
                sb = 0
            elif size == 1:
                sb = (x // 256)
            elif size == 2:
                sb = (y // 256)
            else:
                sb = (y // 256) * 2 + (x // 256)
            # each screenblock is 32x32 tiles = 1024 entries
            sbase = sbb * 2048 + sb * 2048
            ent_off = sbase + (ty * 32 + tx) * 2
            if ent_off + 2 > len(vram):
                continue
            ent = vram[ent_off] | (vram[ent_off + 1] << 8)
            tile = ent & 0x3FF
            hflip = (ent >> 10) & 1
            vflip = (ent >> 11) & 1
            pal = (ent >> 12) & 0xF
            fx, fy = x % 8, y % 8
            if hflip:
                fx = 7 - fx
            if vflip:
                fy = 7 - fy
            if color256:
                toff = cbb * 16384 + tile * 64 + fy * 8 + fx
                if toff >= len(vram):
                    continue
                ci = vram[toff]
                if ci == 0:
                    continue
                pi = ci * 2
            else:
                toff = cbb * 16384 + tile * 32 + fy * 4 + (fx // 2)
                if toff >= len(vram):
                    continue
                byte = vram[toff]
                ci = (byte >> 4) if (fx % 2 == 0) else (byte & 0xF)
                if ci == 0:
                    continue
                pi = (pal * 16 + ci) * 2
            if pi + 2 > len(palette):
                continue
            cv = palette[pi] | (palette[pi + 1] << 8)
            px[x, y] = rgb555(cv) + (255,)
    return {"img": img, "priority": priority}

def render_obj(regs, W=240, H=160):
    if not (regs["dispcnt"] >> 12) & 1:
        return None
    oam = regs["oam"]
    palette = regs["palette"]
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    px = img.load()
    for i in range(128):
        a0, a1, a2 = struct.unpack_from("<HHH", oam, i * 8)
        y = a0 & 0xFF
        if y == 0:
            continue
        affine = (a0 >> 8) & 1
        if affine:
            continue  # skip affine for now
        rot_scale = (a0 >> 9) & 1
        if rot_scale:
            continue
        mode = (a0 >> 10) & 3
        if mode == 3:
            continue
        mosaic = (a0 >> 12) & 1
        color256 = (a0 >> 13) & 1
        shape = (a0 >> 14) & 3
        x = a1 & 0x1FF
        if x >= 240:
            x -= 512
        size = (a1 >> 14) & 3
        tile = a2 & 0x3FF
        priority = (a2 >> 10) & 3
        pal = (a2 >> 12) & 0xF
        hflip = (a1 >> 12) & 1
        vflip = (a1 >> 13) & 1
        dims = {
            (0, 0): (8, 8), (0, 1): (16, 16), (0, 2): (32, 32), (0, 3): (64, 64),
            (1, 0): (16, 8), (1, 1): (32, 8), (1, 2): (32, 16), (1, 3): (64, 32),
            (2, 0): (8, 16), (2, 1): (8, 32), (2, 2): (16, 32), (2, 3): (32, 64),
        }
        ow, oh = dims.get((shape, size), (8, 8))
        y0 = y - 1 if y > 0 else 0
        vram = regs["vram"]
        for yy in range(oh):
            for xx in range(ow):
                sx, sy = x + xx, y0 + yy
                if not (0 <= sx < W and 0 <= sy < H):
                    continue
                fx = (ow - 1 - xx) if hflip else xx
                fy = (oh - 1 - yy) if vflip else yy
                if color256:
                    t = tile + (fy // 8) * (ow // 8) + (fx // 8)
                    toff = (t * 64 + (fy % 8) * 8 + (fx % 8)) & 0x1FFFF
                    if toff >= len(vram):
                        continue
                    ci = vram[toff]
                    if ci == 0:
                        continue
                    pi = 512 + ci * 2
                else:
                    t = tile + (fy // 8) * (ow // 8) * 2 + (fx // 8)
                    toff = (t * 32 + (fy % 8) * 4 + ((fx % 8) // 2)) & 0x1FFFF
                    if toff >= len(vram):
                        continue
                    byte = vram[toff]
                    ci = (byte >> 4) if (fx % 2 == 0) else (byte & 0xF)
                    if ci == 0:
                        continue
                    pi = 512 + (pal * 16 + ci) * 2
                if pi + 2 > len(palette):
                    continue
                cv = palette[pi] | (palette[pi + 1] << 8)
                px[sx, sy] = rgb555(cv) + (255,)
    return {"img": img, "priority": 0}

def main():
    prefix = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else prefix + "_render.png"
    regions = load(prefix)
    io = regions["io"]
    dispcnt = io[0] | (io[1] << 8)
    bgcnt = [(io[8 + 2 * i] | (io[9 + 2 * i] << 8)) for i in range(4)]
    mode = dispcnt & 7
    regs = {"dispcnt": dispcnt, "bgcnt": bgcnt, "vram": regions["vram"], "palette": regions["palette"]}
    if "oam" in regions:
        regs["oam"] = regions["oam"]
    print(f"DISPCNT=0x{dispcnt:04X} mode={mode} BGCNT={[hex(b) for b in bgcnt]}")
    print(f"palette nonzero={sum(1 for b in regions['palette'] if b)}")
    W, H = 240, 160
    base = Image.new("RGB", (W, H), (0, 0, 0))
    if mode == 0:
        layers = []
        for bg in range(4):
            r = render_bg(regs, bg)
            if r:
                layers.append(r)
        layers.sort(key=lambda r: r["priority"], reverse=True)
        for r in layers:
            base.paste(r["img"], (0, 0), r["img"])
        o = render_obj(regs)
        if o:
            base.paste(o["img"], (0, 0), o["img"])
    else:
        print(f"mode {mode} not supported by this quick renderer")
    base = base.resize((W * 3, H * 3), Image.NEAREST)
    base.save(out)
    print("wrote", out)

if __name__ == "__main__":
    main()
