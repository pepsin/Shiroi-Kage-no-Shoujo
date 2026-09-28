#!/usr/bin/env python3
"""Translate the pre-rendered status-bar label `パートナー` (top right) to 助手.

The top bar (`3/11昼 | 大河原家 告別式会場 | パートナー | 洋子`) is BG0 and its
labels are pre-rendered 8x8 tile plates, not font text.  `パートナー` sits at the
tail of the uncompressed entry e833:

    0x472530 0x472550 0x472570 0x472590   top half    (4 tiles, 32px)
    0x4725B0 0x4725D0 0x4725F0            bottom half (3 tiles, 24px)

The 8th cell of the plate (x 24..31, bottom half) is not part of e833 - the game
draws the bar's border tile there - so the rewritten label has to stay inside
x 0..23 for its lower rows.

Tiles are 4bpp with the bar's runtime palette (bank 13):

    ink         6  ( 90, 90, 90)   dark grey strokes
    AA          9  (206,198,189)   one-pixel edge highlight
    background 12/13 (cream), alternating per row: rows with (y % 8) in {2, 5}
               use 12, every other row 13

So the katakana are erased back to their row's background index and 助手 is
drawn with the same ink plus a 1px AA on the right/bottom edges, like the other
plate tools.  The glyphs are zpix's 11x11 squeezed to 9x9 - the size the game
itself uses for bar text (the room name's ink band is rows 2..10 of the 16px
bar), which keeps 助手 in step with the neighbouring text.

Usage:
  patch_status_bar.py <rom> --preview before.png after.png
  patch_status_bar.py <rom> --apply [--out out.gba]
  patch_status_bar.py <rom> --check
"""
import argparse
import os
import struct
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (file offset, plate column, plate row) - 4 tiles top half, 3 tiles bottom half
TILES = [(0x472530, 0, 0), (0x472550, 1, 0), (0x472570, 2, 0), (0x472590, 3, 0),
         (0x4725B0, 0, 1), (0x4725D0, 1, 1), (0x4725F0, 2, 1)]
PLATE_W, PLATE_H = 32, 16
SAFE_W = 24                  # the bottom half only owns x 0..23

INK, AA = 6, 9
INK_RGB, AA_RGB = (90, 90, 90), (206, 198, 189)
BG_RGB = {12: (255, 247, 231), 13: (255, 255, 239)}
BG_PATTERN = {2: 12, 5: 12}  # within each 8-row tile, these rows use index 12

LABEL = '助手'
# the same face the other plate tools draw with
FONT_PATH = '/System/Library/Fonts/STHeiti Medium.ttc'
FONT_PX = 9                  # -> 8px ink, the band the katakana plate used


def bg_index(y):
    return BG_PATTERN.get(y % 8, 13)


def get_px(rom, off, x, y):
    b = rom[off + y * 4 + x // 2]
    return (b >> (4 * (x & 1))) & 0xF


def set_px(rom, off, x, y, v):
    i = off + y * 4 + x // 2
    b = rom[i]
    if x & 1:
        rom[i] = (b & 0x0F) | ((v & 0xF) << 4)
    else:
        rom[i] = (b & 0xF0) | (v & 0xF)


def read_plate(rom):
    """{(x, y): pen} for the tiles this tool owns (the 8th cell stays None)."""
    plate = {}
    for off, col, row in TILES:
        for y in range(8):
            for x in range(8):
                plate[(col * 8 + x, row * 8 + y)] = get_px(rom, off, x, y)
    return plate


def write_plate(rom, plate):
    for off, col, row in TILES:
        for y in range(8):
            for x in range(8):
                set_px(rom, off, x, y, plate[(col * 8 + x, row * 8 + y)])


def label_mask():
    """Ink plus its 1px right/bottom AA, in plate coordinates.

    The glyphs come from STHeiti at 9px (the same font the other plate tools
    draw with) thresholded to 1-bit: that gives an 8px-tall ink band, exactly
    what the katakana plate used, so the label keeps the bar's rhythm.  zpix is
    11x11 and squeezing it to 8-9px merges 助's strokes.
    """
    font = ImageFont.truetype(FONT_PATH, FONT_PX)
    img = Image.new('L', (4 * FONT_PX, 2 * FONT_PX), 0)
    ImageDraw.Draw(img).text((0, 0), LABEL, font=font, fill=255)
    img = img.point(lambda v: 255 if v >= 110 else 0)
    box = img.getbbox()
    if box is None:
        raise SystemExit(f'could not rasterise {LABEL!r}')
    w, h = box[2] - box[0], box[3] - box[1]
    # centre inside the 32px plate, but never past the 24px the plate owns
    x0 = min((PLATE_W - w) // 2, SAFE_W - w)
    y0 = 2                                   # the bar's text band starts at row 2
    ink, aa = set(), set()
    for y in range(h):
        for x in range(w):
            if img.getpixel((box[0] + x, box[1] + y)):
                ink.add((x0 + x, y0 + y))
    for (x, y) in ink:
        for dx, dy in ((1, 0), (0, 1), (1, 1)):
            p = (x + dx, y + dy)
            if p not in ink and p[0] < SAFE_W:
                aa.add(p)
    return ink, aa


def translate(rom):
    """Erase the katakana and draw 助手; returns the new plate."""
    plate = read_plate(rom)
    ink, aa = label_mask()
    # 1. wipe the old glyph pixels back to the row's background index
    for (x, y), pen in list(plate.items()):
        if pen in (INK, AA):
            plate[(x, y)] = bg_index(y)
    # 2. draw AA first, then the ink on top
    for (x, y) in sorted(aa):
        if (x, y) in plate:
            plate[(x, y)] = AA
    for (x, y) in sorted(ink):
        if (x, y) in plate:
            plate[(x, y)] = INK
    write_plate(rom, plate)
    return plate


def render(plate, scale=14):
    img = Image.new('RGB', (PLATE_W, PLATE_H), (255, 255, 255))
    px = img.load()
    for (x, y), pen in sorted(plate.items()):
        if pen == INK:
            c = INK_RGB
        elif pen == AA:
            c = AA_RGB
        else:
            c = BG_RGB.get(pen, (255, 0, 255))
        px[x, y] = c
    return img.resize((PLATE_W * scale, PLATE_H * scale), Image.NEAREST)


def check(rom):
    plate = read_plate(rom)
    ink, aa = label_mask()
    got_ink = {(x, y) for (x, y), pen in plate.items() if pen == INK}
    got_aa = {(x, y) for (x, y), pen in plate.items() if pen == AA}
    ok = (got_ink == ink)
    print(f'label {LABEL!r}: ink {len(got_ink)} px (expected {len(ink)}), '
          f'AA {len(got_aa)} px (expected {len(aa)})')
    if not ok:
        missing = sorted(ink - got_ink)[:6]
        extra = sorted(got_ink - ink)[:6]
        print(f'  MISMATCH: missing {missing} extra {extra}')
    # the katakana must be gone: no INK/AA outside the new label
    stray = {(x, y) for (x, y), pen in plate.items()
             if pen in (INK, AA) and (x, y) not in ink and (x, y) not in aa}
    if stray:
        print(f'  stray ink/AA outside the label: {sorted(stray)[:8]}')
        ok = False
    print('RESULT:', 'OK' if ok else 'PROBLEM')
    return 0 if ok else 1


def apply_rom(rom_path, out_path):
    rom = bytearray(open(rom_path, 'rb').read())
    translate(rom)
    open(out_path, 'wb').write(bytes(rom))
    print(f'status bar label {LABEL!r} written to the 7 tiles at '
          f'0x{TILES[0][0]:X}..0x{TILES[-1][0] + 32:X}')
    print(f'wrote {out_path} ({len(rom)} bytes)')


def preview(rom_path, before_png, after_png):
    rom = bytearray(open(rom_path, 'rb').read())
    render(read_plate(rom)).save(before_png)
    translate(rom)
    render(read_plate(rom)).save(after_png)
    print(f'wrote {before_png} and {after_png}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('rom')
    ap.add_argument('--preview', nargs=2, metavar=('BEFORE', 'AFTER'))
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--out')
    a = ap.parse_args()
    if a.check:
        return check(open(a.rom, 'rb').read())
    if a.preview:
        return preview(a.rom, a.preview[0], a.preview[1])
    if a.apply:
        return apply_rom(a.rom, a.out or a.rom)
    ap.error('choose --preview, --apply or --check')


if __name__ == '__main__':
    sys.exit(main())
