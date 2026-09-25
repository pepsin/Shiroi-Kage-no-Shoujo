#!/usr/bin/env python3
"""Patch the pre-rendered menu plates (title screen: はじめる/続きから/パスワード).

These three "plates" are NOT font text: they are 72x16 px 4bpp tile strips
(9 sprites of 8x16, two tiles each) stored **uncompressed** in the ROM, loaded
verbatim to OBJ VRAM at 0x06010000 + tile*32.  The Japanese labels are baked
into the pixels, so translating them means redrawing the strips.

Plate pixel structure (4bpp indices, resolved through the OBJ palette bank):
    0  transparent (outside the rounded corners)
    10 outer border          11 corner highlight
    12 inner frame / text AA 15 fill
    9  text core
The same strip serves the selected and unselected states; the OBJ palette bank
(11 = selected/white, 15 = normal/grey) recolours it.

Usage:
  patch_menu_plates.py --preview out.png            # before/after sheet
  patch_menu_plates.py --apply  in.gba out.gba      # rewrite the strips
  patch_menu_plates.py --scan  rom.gba              # list plate-like strips
"""
import argparse
import os
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

STRIP_BYTES = 576          # 18 tiles
STRIP_W, STRIP_H = 72, 16
HEADER = bytes.fromhex('00aaaaaaa0cbccccba')   # left cap, identical on every plate

# (offset, simplified-Chinese label, what the Japanese said)
PLATES = [
    (0x44BF60, '开始游戏', 'はじめる'),
    (0x44C1A0, '继续游戏', '続きから'),
    (0x44C3E0, '输入密码', 'パスワード'),
]

TEXT_BOX = (3, 68, 2, 13)          # x0, x1, y0, y1 - the drawable fill area
CORE, AA, FILL = 9, 12, 15

FONTS = [
    '/System/Library/Fonts/STHeiti Medium.ttc',
    '/System/Library/Fonts/Hiragino Sans GB.ttc',
    '/System/Library/Fonts/Supplemental/Songti.ttc',
    '/System/Library/Fonts/PingFang.ttc',
]
SIZES = [13, 12, 14, 11]


def decode(strip):
    img = [[0] * STRIP_W for _ in range(STRIP_H)]
    for s in range(9):
        for half in range(2):
            t = s * 2 + half
            for y in range(8):
                for x in range(8):
                    b = strip[t * 32 + y * 4 + x // 2]
                    img[half * 8 + y][s * 8 + x] = (b >> (4 * (x & 1))) & 0xF
    return img


def encode(img):
    out = bytearray(STRIP_BYTES)
    for s in range(9):
        for half in range(2):
            t = s * 2 + half
            for y in range(8):
                for x in range(8):
                    v = img[half * 8 + y][s * 8 + x] & 0xF
                    i = t * 32 + y * 4 + x // 2
                    if x % 2 == 0:
                        out[i] = (out[i] & 0xF0) | v
                    else:
                        out[i] = (out[i] & 0x0F) | (v << 4)
    return bytes(out)


def pick_font(texts, box=TEXT_BOX):
    """First (font, size) whose ink box fits every label - one size for all plates."""
    x0, x1, y0, y1 = box
    maxw, maxh = x1 - x0 + 1, y1 - y0 + 1
    for path in FONTS:
        if not os.path.exists(path):
            continue
        for size in SIZES:
            f = ImageFont.truetype(path, size)
            ok = True
            for text in texts:
                tmp = Image.new('L', (STRIP_W * 2, STRIP_H * 2), 0)
                ImageDraw.Draw(tmp).text((STRIP_W // 2, STRIP_H // 2), text, font=f,
                                         fill=255, anchor='lt')
                bb = tmp.getbbox()
                if not bb or bb[2] - bb[0] > maxw or bb[3] - bb[1] > maxh:
                    ok = False
                    break
            if ok:
                return f, path, size
    raise SystemExit(f'no font fits {texts!r} in {maxw}x{maxh}')


def render(img, text, font):
    """Erase the baked-in label and draw `text` in the plate's own two tones."""
    x0, x1, y0, y1 = TEXT_BOX
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            img[y][x] = FILL
    tmp = Image.new('L', (STRIP_W, STRIP_H), 0)
    d = ImageDraw.Draw(tmp)
    bb = d.textbbox((0, 0), text, font=font)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    dx = x0 + ((x1 - x0 + 1) - tw) // 2 - bb[0]
    dy = y0 + ((y1 - y0 + 1) - th) // 2 - bb[1]
    d.text((dx, dy), text, font=font, fill=255)
    px = tmp.load()
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            v = px[x, y]
            if v >= 140:
                img[y][x] = CORE
            elif v >= 60:
                img[y][x] = AA
    return img


def to_png(img, scale=6, col=None):
    col = col or {0: (255, 0, 255), 9: (20, 20, 20), 10: (90, 90, 90),
                  11: (150, 150, 150), 12: (180, 180, 180), 15: (235, 235, 230)}
    out = Image.new('RGB', (STRIP_W * scale, STRIP_H * scale), (0, 0, 0))
    for y in range(STRIP_H):
        for x in range(STRIP_W):
            c = col.get(img[y][x], (128, 0, 0))
            for dy in range(scale):
                for dx in range(scale):
                    out.putpixel((x * scale + dx, y * scale + dy), c)
    return out


def scan(data):
    hits = []
    p = 0
    while True:
        p = data.find(HEADER, p)
        if p < 0:
            return hits
        hits.append(p)
        p += 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--rom', default=os.path.join(
        ROOT, 'Tantei Jinguuji Saburou - Shiroi Kage no Shoujo (Japan).gba'),
        help='source ROM holding the original plates')
    ap.add_argument('--apply', nargs=2, metavar=('IN', 'OUT'),
                    help='write a patched copy of IN to OUT')
    ap.add_argument('--preview', metavar='PNG', help='write a before/after sheet')
    ap.add_argument('--scan', metavar='ROM', help='list plate-like strips and exit')
    a = ap.parse_args()

    if a.scan:
        data = open(a.scan, 'rb').read()
        for off in scan(data):
            print(f'{off:#08x}')
        return

    src = open(a.rom, 'rb').read()
    strips = []
    font, fontpath, size = pick_font([t for _, t, _ in PLATES])
    for off, text, jp in PLATES:
        orig = src[off:off + STRIP_BYTES]
        if len(orig) != STRIP_BYTES or not orig.startswith(HEADER):
            raise SystemExit(f'plate at {off:#x} does not look like a menu plate')
        before = decode(orig)
        after = render([row[:] for row in before], text, font)
        strips.append((off, text, jp, before, after, os.path.basename(fontpath), size))
        print(f'{off:#08x}  {jp} -> {text}   ({os.path.basename(fontpath)} {size}px)')

    if a.preview:
        scale = 6
        sheet = Image.new('RGB', (STRIP_W * scale, len(strips) * (STRIP_H * 2 + 2) * scale),
                          (0, 0, 0))
        y = 0
        for off, text, jp, before, after, _, _ in strips:
            sheet.paste(to_png(before, scale), (0, y)); y += STRIP_H * scale
            sheet.paste(to_png(after, scale), (0, y)); y += (STRIP_H + 2) * scale
        sheet.save(a.preview)
        print(f'wrote {a.preview}')

    if a.apply:
        inp, outp = a.apply
        data = bytearray(open(inp, 'rb').read())
        for off, text, jp, before, after, _, _ in strips:
            data[off:off + STRIP_BYTES] = encode(after)
        open(outp, 'wb').write(bytes(data))
        print(f'wrote {outp} ({len(data)} bytes)')


if __name__ == '__main__':
    main()
