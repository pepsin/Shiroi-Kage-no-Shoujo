#!/usr/bin/env python3
"""BDF pixel-font reader used for rendering the Chinese glyphs.

The original Japanese font is an 11x11 pixel font, so the Chinese glyphs look
best when they come from a pixel font of the same size instead of a downscaled
outline font (which is what made the first build look blurry).

`zpix` (tools/fonts/zpix/zpix.bdf) is a 12px font whose CJK glyphs are exactly
11x11 -- the same design box the game uses -- and it covers all 1369 characters
this build appends.  The BDF is parsed directly so the bitmaps are used verbatim
(no rasteriser, no anti-aliasing, no scaling).

Placement: the cell is 16x16 and the design box is 11x11 at (0,1); a CJK glyph
(BBX 11 11 0 -2) therefore lands exactly on the design box.  Everything else is
placed by keeping the font's baseline:  y = BODY_Y + BODY_BOX - (yoff + h) - 9
so that punctuation, digits and latin keep their own offsets.

Usage:
  pixelfont.py --sample 汉字              # ASCII preview of each glyph
  pixelfont.py --png out.png 汉字ABC       # rendered with the game palette
"""
import argparse
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT = os.path.join(ROOT, 'tools', 'fonts', 'zpix', 'zpix.bdf')

BODY_BOX = 11          # 11x11 design box, like the original font
BODY_ORIGIN = (0, 1)   # top-left of that box inside the 16x16 cell
CJK_REF_TOP = 8        # yoff + h - 1 of a CJK glyph (BBX 11 11 0 -2)


class PixelFont:
    """A BDF bitmap font: codepoint -> (w, h, xoff, yoff, rows-of-bits)."""

    def __init__(self, path=DEFAULT):
        self.path = path
        self.glyphs = {}
        self.ascent = 10
        self._load(path)

    def _load(self, path):
        cp = None
        bbx = None
        rows = []
        with open(path, encoding='latin-1') as f:
            for line in f:
                if line.startswith('FONT_ASCENT '):
                    self.ascent = int(line.split()[1])
                elif line.startswith('ENCODING '):
                    cp = int(line.split()[1])
                    bbx, rows = None, []
                elif line.startswith('BBX '):
                    _, w, h, xo, yo = line.split()
                    bbx = (int(w), int(h), int(xo), int(yo))
                elif line.startswith('BITMAP'):
                    rows = []
                elif line.startswith('ENDCHAR'):
                    if cp is not None and cp > 0 and bbx:
                        self.glyphs[cp] = (*bbx, rows)
                    cp, bbx, rows = None, None, []
                elif bbx is not None:
                    rows.append(line.strip())

    def __contains__(self, ch):
        return ord(ch) in self.glyphs

    def bitmap(self, ch):
        """(w, h, x, y, [[0/1, ...]]) with x,y = top-left inside the 16x16 cell,
        or None when the character is not in the font."""
        g = self.glyphs.get(ord(ch))
        if not g:
            return None
        w, h, xo, yo, rows = g
        bits = []
        for r in rows[:h]:
            v = int(r, 16) if r else 0
            nbits = len(r) * 4          # BDF rows are left-aligned in whole bytes
            bits.append([(v >> (nbits - 1 - i)) & 1 for i in range(w)])
        x = BODY_ORIGIN[0] + xo
        # keep the font's baseline: a CJK glyph's top row sits on the design box
        y = BODY_ORIGIN[1] + CJK_REF_TOP - (yo + h - 1)
        return w, h, x, y, bits

    def body(self, ch, cell=16):
        """16x16 0/1 body mask, or None when the character is missing."""
        bm = self.bitmap(ch)
        if bm is None:
            return None
        w, h, x, y, bits = bm
        out = [[0] * cell for _ in range(cell)]
        for j in range(h):
            for i in range(w):
                if not bits[j][i]:
                    continue
                xx, yy = x + i, y + j
                if 0 <= xx < cell and 0 <= yy < cell:
                    out[yy][xx] = 1
        return out


_CACHE = {}


def load(path=DEFAULT):
    if path not in _CACHE:
        _CACHE[path] = PixelFont(path)
    return _CACHE[path]


def to_png(hexrows, scale=16, body=80, shadow=104, bg=248):
    from PIL import Image
    px = PixelFont.__new__(PixelFont)
    im = Image.new('RGB', (16, 16), (bg, bg, bg))
    p = im.load()
    for y, row in enumerate(hexrows):
        for x, ch in enumerate(row):
            if ch == '1':
                p[x, y] = (body, body, body)
    return im.resize((16 * scale, 16 * scale), Image.NEAREST)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--font', default=DEFAULT)
    ap.add_argument('--sample', help='print ASCII art for these characters')
    ap.add_argument('--png', metavar='OUT', help='render those characters to a PNG')
    ap.add_argument('--text', default='汉字测试 A1')
    ap.add_argument('--scale', type=int, default=8)
    a = ap.parse_args()
    pf = PixelFont(a.font)
    text = a.sample or a.text
    cells = []
    for ch in text:
        if ch == ' ':
            continue
        body = pf.body(ch)
        cells.append((ch, body))
    if a.sample:
        for ch, body in cells:
            print(f'--- {ch!r} {"(missing)" if body is None else ""}')
            if body:
                for row in body:
                    print('   ' + ''.join('#' if v else '.' for v in row))
        return
    from PIL import Image
    S = a.scale
    sheet = Image.new('RGB', (16 * S * len(cells), 16 * S), (248, 248, 248))
    for k, (ch, body) in enumerate(cells):
        im = Image.new('RGB', (16, 16), (248, 248, 248))
        p = im.load()
        if body:
            for y in range(16):
                for x in range(16):
                    if body[y][x]:
                        p[x, y] = (80, 80, 80)
                        if x + 1 < 16 and not body[y][x + 1]:
                            p[x + 1, y] = (104, 96, 88)
        sheet.paste(im.resize((16 * S, 16 * S), Image.NEAREST), (k * 16 * S, 0))
    sheet.save(a.png)
    print(f'wrote {a.png}')


if __name__ == '__main__':
    main()
