#!/usr/bin/env python3
"""Re-render the Chinese location bitmaps frozen in data/location_labels.json.

The manifest holds 58 pre-rendered 4bpp labels (52 `status` bar strips in
e840's LZ77 children, 6 `map` OBJ plates at FAT e712-e717).  This tool redraws
the pixel region of every entry with the bundled fusion-pixel 10px face and
rewrites pixels_hex / pixels_sha256 (plus the rendering metadata); every other
byte and field is left alone, so patch_location_labels.py's prefix checks keep
passing.

Layouts (see docs/翻译作业流程.md and patch_location_labels.py):

  status: 112x16 px = 28 tiles, row-major (14 cols x 2 rows), 32 bytes/tile,
          4 bytes/row, even x in the low nibble.  Background index 13, except
          rows with y % 8 in {2, 5} which use 12.  Ink 6, AA 10 (1px
          right/down/down-right dilation onto background only).  Text is
          left-aligned at x0=1, ink band rows 2..10.

  map:    16 px tall, W = (tiles/2)*8 (64-88 px), tiles column-major (top
          tile then bottom tile per column, like patch_menu_plates.py).
          OBJ plate palette: 0 transparent outside the rounded corners,
          11 outer border, 12 inner frame / text AA, 15 fill, 9 text core.
          The right side is zero padding: the visible plate width comes from
          the clean frame rows (0, 1, 13, 14).  Only 9/12 pixels strictly
          between the left and right frame runs in rows 2..12 are reset to
          15 before the new label is drawn (centred in the visible width,
          ink rows 2..10); AA pixels only ever land on 15.

fusion-pixel at its 10px design size is a pure bitmap (0/255 coverage), so
the threshold only formalises the 1-bit split.

Usage:
  gen_location_labels.py            # re-render and rewrite the manifest
  gen_location_labels.py --check    # verify the manifest matches a re-render
  gen_location_labels.py --preview DIR  # dump before/after PNGs for review
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'data/location_labels.json'
FONT_PATH = ROOT / 'tools/fonts/fusion-pixel/fusion-pixel-10px-monospaced-zh_hans.ttf'
FONT_PX = 10
THRESHOLD = 128                 # the face is pure 0/255; any split works

# status strip
ST_W, ST_H = 112, 16
ST_X0, ST_Y0 = 1, 2             # left-aligned; ink band rows 2..10, AA row 11
ST_INK, ST_AA = 6, 10
# map plate
PL_H = 16
PL_Y0 = 2                       # ink band rows 2..10, AA row 11
ERASE_ROWS = range(2, 13)       # old labels' AA could reach row 12
PL_CORE, PL_AA, PL_FILL = 9, 12, 15

RENDERING = ('fusion-pixel-10px-monospaced-zh_hans.ttf at 10px; threshold 128; '
             'status left-aligned x0=1 y0=2, map centred y0=2; '
             'approved 4bpp pixels avoid runtime font dependence')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def text_mask(text):
    """1-bit {(x, y)} ink set of `text`, normalised to its bounding box."""
    font = ImageFont.truetype(str(FONT_PATH), FONT_PX)
    img = Image.new('L', (FONT_PX * (len(text) + 2), FONT_PX * 3), 0)
    ImageDraw.Draw(img).text((0, 0), text, font=font, fill=255)
    img = img.point(lambda v: 255 if v >= THRESHOLD else 0)
    box = img.getbbox()
    if box is None:
        return set(), 0, 0
    w, h = box[2] - box[0], box[3] - box[1]
    ink = {(x - box[0], y - box[1]) for y in range(box[1], box[3])
           for x in range(box[0], box[2]) if img.getpixel((x, y))}
    return ink, w, h


def aa_dilate(ink):
    aa = set()
    for (x, y) in ink:
        for dx, dy in ((1, 0), (0, 1), (1, 1)):
            if (x + dx, y + dy) not in ink:
                aa.add((x + dx, y + dy))
    return aa


# ---------------------------------------------------------------- status

def st_bg(y):
    return 12 if y % 8 in (2, 5) else 13


def render_status(text):
    """Return the 112x16 grid (list of rows) for a status-bar strip."""
    ink, w, h = text_mask(text)
    if w > ST_W - ST_X0 or ST_Y0 + h > 12:
        raise ValueError(f'{text!r}: {w}x{h} does not fit the status strip')
    grid = [[st_bg(y)] * ST_W for y in range(ST_H)]
    aa = aa_dilate(ink)
    for (x, y) in sorted(aa):
        grid[ST_Y0 + y][ST_X0 + x] = ST_AA
    for (x, y) in sorted(ink):
        grid[ST_Y0 + y][ST_X0 + x] = ST_INK
    return grid


def encode_status(grid):
    out = bytearray()
    for t in range(28):
        col, row = t % 14, t // 14
        for y in range(8):
            for x in range(0, 8, 2):
                out.append(grid[row * 8 + y][col * 8 + x]
                           | (grid[row * 8 + y][col * 8 + x + 1] << 4))
    return bytes(out)


# ---------------------------------------------------------------- map

def decode_map(pixels):
    n = len(pixels) // 32
    w = (n // 2) * 8
    grid = [[0] * w for _ in range(PL_H)]
    for c in range(n // 2):
        for half in range(2):
            t = c * 2 + half
            for y in range(8):
                for x in range(8):
                    b = pixels[t * 32 + y * 4 + x // 2]
                    grid[half * 8 + y][c * 8 + x] = (b >> (4 * (x & 1))) & 0xF
    return grid, w


def encode_map(grid):
    w = len(grid[0])
    out = bytearray()
    for c in range(w // 8):
        for half in range(2):
            for y in range(8):
                for x in range(0, 8, 2):
                    out.append(grid[half * 8 + y][c * 8 + x]
                               | (grid[half * 8 + y][c * 8 + x + 1] << 4))
    return bytes(out)


def visible_right(grid, w):
    """Last non-zero column of the frame-only rows (text never reaches them:
    rows 0/1/13/14 are clean, and in rows 2/12 the frame's right corner is
    still the rightmost non-zero pixel)."""
    return max(x for y in (0, 1, 2, 12, 13, 14) for x in range(w) if grid[y][x])


def erase_map_text(grid, w):
    """Reset old text pixels between the frame runs in rows 2..12 to 15.

    The frame is a contiguous run of 0/11/12 from each edge of every row, so
    anything 9, 12 or 13 strictly between the first and last fill/text pixel
    of the row is old text, its AA, or a stale artefact (one plate carries a
    stray 13 in its fill area) and goes back to fill.
    """
    for y in ERASE_ROWS:
        row = grid[y]
        try:
            lo = next(x for x in range(w) if row[x] in (PL_CORE, PL_FILL))
            hi = next(x for x in range(w - 1, -1, -1) if row[x] in (PL_CORE, PL_FILL))
        except StopIteration:
            continue
        for x in range(lo, hi + 1):
            if row[x] in (PL_CORE, PL_AA, 13):
                row[x] = PL_FILL


def render_map(pixels, text):
    """Erase the old label in `pixels` (keeping the rounded frame) and draw
    `text` centred in the visible plate width; returns the new grid."""
    grid, w = decode_map(pixels)
    right = visible_right(grid, w)
    erase_map_text(grid, w)
    ink, tw, th = text_mask(text)
    x0 = (right + 1 - tw) // 2
    if x0 < 3 or x0 + tw > right - 2 or PL_Y0 + th > 12:
        raise ValueError(f'{text!r}: {tw}x{th} does not fit plate width {right + 1}')
    aa = aa_dilate(ink)
    for (x, y) in sorted(aa):
        if grid[PL_Y0 + y][x0 + x] == PL_FILL:
            grid[PL_Y0 + y][x0 + x] = PL_AA
    for (x, y) in sorted(ink):
        if grid[PL_Y0 + y][x0 + x] != PL_FILL:
            raise ValueError(f'{text!r}: ink would overwrite the frame at '
                             f'({x0 + x}, {PL_Y0 + y})')
        grid[PL_Y0 + y][x0 + x] = PL_CORE
    return grid


# ---------------------------------------------------------------- driver

def rerender(row):
    if row['kind'] == 'status':
        grid = render_status(row['chinese'])
        return encode_status(grid)
    grid = render_map(bytes.fromhex(row['pixels_hex']), row['chinese'])
    return encode_map(grid)


def to_png(grid, scale=6):
    pal = {0: (255, 0, 255), 6: (90, 90, 90), 9: (20, 20, 20),
           10: (206, 198, 189), 11: (90, 90, 90), 12: (180, 180, 180),
           13: (255, 255, 239), 15: (235, 235, 230)}
    w, h = len(grid[0]), len(grid)
    img = Image.new('RGB', (w * scale, h * scale), (0, 0, 0))
    for y in range(h):
        for x in range(w):
            c = pal.get(grid[y][x], (128, 0, 0))
            for dy in range(scale):
                for dx in range(scale):
                    img.putpixel((x * scale + dx, y * scale + dy), c)
    return img


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--assets', type=Path, default=ASSETS)
    ap.add_argument('--check', action='store_true',
                    help='verify stored pixels match a fresh re-render')
    ap.add_argument('--preview', type=Path, metavar='DIR',
                    help='write before/after PNGs into DIR')
    a = ap.parse_args()

    manifest = json.loads(a.assets.read_text())
    rows = manifest['patches']
    bad = 0
    for row in rows:
        old = bytes.fromhex(row['pixels_hex'])
        new = rerender(row)
        key = f"{row['kind']}/{row['index']}"
        if a.check:
            if new != old or digest(new) != row['pixels_sha256']:
                print(f'{key}: STALE ({row["chinese"]})')
                bad += 1
            continue
        if a.preview:
            a.preview.mkdir(parents=True, exist_ok=True)
            w = len(old) // 32 // 2 * 8 if row['kind'] == 'map' else ST_W
            dec = decode_map(old)[0] if row['kind'] == 'map' else None
            if dec is None:
                dec = [[st_bg(y)] * ST_W for y in range(ST_H)]  # placeholder
                for t in range(28):
                    col, rr = t % 14, t // 14
                    for y in range(8):
                        for x in range(8):
                            b = old[t * 32 + y * 4 + x // 2]
                            dec[rr * 8 + y][col * 8 + x] = (b >> (4 * (x & 1))) & 0xF
            to_png(dec).save(a.preview / f'{key.replace("/", "_")}_before.png')
            newgrid = render_status(row['chinese']) if row['kind'] == 'status' \
                else render_map(old, row['chinese'])
            to_png(newgrid).save(a.preview / f'{key.replace("/", "_")}_after.png')
        if new == old:
            continue
        row['pixels_hex'] = new.hex()
        row['pixels_sha256'] = digest(new)
        print(f'{key}: re-rendered {row["japanese"]} -> {row["chinese"]}')

    if a.check:
        print('RESULT:', 'OK' if not bad else f'{bad} stale entries')
        return 1 if bad else 0
    manifest['rendering'] = RENDERING
    a.assets.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    print(f'wrote {a.assets} ({len(rows)} entries)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
