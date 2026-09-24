#!/usr/bin/env python3
"""Extract and render the LZ77-compressed CJK fonts from the ROM.

Decodes GBA BIOS LZ77 (0x10) streams found via the runtime SWI trace and
renders them as glyph sheets.
"""
import sys
from PIL import Image

ROM = "侦探神宫寺三郎 - 白影的少女[CGP](简)(JP)(65.56Mb).gba"

def unlz77(data, off):
    if data[off] != 0x10:
        raise ValueError("not LZ77")
    size = data[off + 1] | (data[off + 2] << 8) | (data[off + 3] << 16)
    out = bytearray()
    i = off + 4
    while len(out) < size:
        flags = data[i]; i += 1
        for _ in range(8):
            if len(out) >= size:
                break
            if flags & 0x80:
                b1 = data[i]; b2 = data[i + 1]; i += 2
                length = (b1 >> 4) + 3
                dist = ((b1 & 0xF) << 8) | b2
                for _ in range(length):
                    out.append(out[len(out) - dist - 1])
            else:
                out.append(data[i]); i += 1
            flags <<= 1
    return bytes(out[:size])

def render(font, ntiles, cols, path, scale=2, tw=16, th=16):
    rows = (ntiles + cols - 1) // cols
    img = Image.new('L', (cols * tw * scale, rows * th * scale), 255)
    px = img.load()
    bpt = tw * th // 2
    for t in range(ntiles):
        b0 = t * bpt
        if b0 + bpt > len(font):
            break
        for y in range(th):
            for xb in range(tw // 2):
                byte = font[b0 + y * (tw // 2) + xb]
                for k in range(2):
                    v = (byte >> (4 * (1 - k))) & 0xF
                    if v:
                        x = xb * 2 + k
                        c = 255 - v * 17
                        for sy in range(scale):
                            for sx in range(scale):
                                px[(t % cols) * tw * scale + x * scale + sx,
                                   (t // cols) * th * scale + y * scale + sy] = c
    img.save(path)
    print(f"wrote {path} ({img.size[0]}x{img.size[1]})")

def main():
    rom = open(ROM, 'rb').read()
    targets = [
        (0x3C1070, 'font_small', 71),
        (0x588740, 'font_main_a', 640),
        (0x57DBB0, 'font_main_b', 652),
        (0x585650, 'font_main_c', 652),
    ]
    for off, name, ntiles in targets:
        try:
            font = unlz77(rom, off)
        except Exception as e:
            print(f"{name} @0x{off:06X}: FAILED {e}")
            continue
        nonempty = sum(1 for i in range(0, len(font) - 31, 32) if any(font[i:i + 32]))
        print(f"{name} @0x{off:06X}: {len(font)} bytes, nonempty 32B glyphs={nonempty}")
        render(font, ntiles, 20, f"work/dumps/{name}.png")
        open(f"work/dumps/{name}.bin", 'wb').write(font)

if __name__ == '__main__':
    main()
