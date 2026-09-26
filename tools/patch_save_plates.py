#!/usr/bin/env python3
"""Translate the pre-rendered save/load screen plates.

The save screen is not font text: `SELECT / セーブ / ロード / 消去` and the
`FILE | [] | / | プレイ 時間 分` row are 4bpp OBJ tiles stored **uncompressed**
in the ROM (that is why find_text.py cannot find セーブ/ロード anywhere).  They
are located by matching the live OBJ VRAM of a savestate against the ROM, the
same way the title menu plates were found.

Blocks and their sprite segmentation (1D OBJ mapping, 4 tiles per row for a
32px-wide sprite, 8 for a 64px-wide one):

    0x44ECE0  64x32  row sprite 2: プレイ
    0x44F0E0  64x32  row sprite 3: 時間   (分 is left alone - same in Chinese)
    0x44F5A0  32x16 + 0x44F6A0  32x16   menu item 1: セーブ
    0x44F7C0  32x16 + 0x44F8C0  32x16   menu item 2: ロード
    0x44F9E0  32x16 + 0x44FAE0  32x16   menu item 3: 消去
    (0x44E8E0  64x32  row sprite 1: FILE [] / - no text to change)

Each item is drawn by the game with several OBJ palette banks (normal /
highlighted), so one edit covers every state.

Usage:
  patch_save_plates.py --preview out.png        # before/after sheet
  patch_save_plates.py --apply in.gba out.gba   # rewrite the blocks
"""
import argparse
import os
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import pixelfont  # noqa: E402
JP_ROM = os.path.join(ROOT, 'Tantei Jinguuji Saburou - Shiroi Kage no Shoujo (Japan).gba')

# label: box=(x0,y0,x1,y1) half-open, drawn text, original text, ink/AA/fill indices
ROW_INK, ROW_FILL = 1, 14          # row labels: crisp dark strokes on the fill
ITEM_INK, ITEM_AA, ITEM_FILL = 9, 12, 15

BLOCKS = [
    # プレイ / 時間 straddle the sprite boundary: sprite 2 holds プレイ + 時,
    # sprite 3 holds 間 + 分.  They are one label, so the two 64x32 sprites are
    # merged into a single canvas and 分 (x 89) is left untouched.
    dict(name='row 游戏时间',
         segs=[(0x44ECE0, 64, 32, 0, 0), (0x44F0E0, 64, 32, 64, 0)],
         labels=[((14, 3, 80, 14), '游戏时间', 'プレイ時間', ROW_INK, None, ROW_FILL)]),
    dict(name='menu セーブ', segs=[(0x44F5A0, 32, 16, 0, 0), (0x44F6A0, 32, 16, 32, 0)],
         labels=[((8, 2, 36, 12), '保存', 'セーブ', ITEM_INK, ITEM_AA, ITEM_FILL)]),
    dict(name='menu ロード', segs=[(0x44F7C0, 32, 16, 0, 0), (0x44F8C0, 32, 16, 32, 0)],
         labels=[((8, 2, 36, 12), '读取', 'ロード', ITEM_INK, ITEM_AA, ITEM_FILL)]),
    dict(name='menu 消去', segs=[(0x44F9E0, 32, 16, 0, 0), (0x44FAE0, 32, 16, 32, 0)],
         labels=[((8, 2, 36, 12), '删除', '消去', ITEM_INK, ITEM_AA, ITEM_FILL)]),
]

# The in-game investigation command menu is pre-rendered the same way: each
# item is a 64x16 plate built from two 32x16 sprites.  Only the labels that
# differ in Chinese are listed (person names stay as they are, 推理 is the same).
CMD_PLATES = [
    (0x66C400, '抽烟', 'タバコ吸う'),      # smoke
    (0x66CA00, '周围', '周囲'),            # surroundings
    (0x66CC00, '查看', '見る'),            # look
    (0x66CE00, '持有物', '持ち物'),        # belongings
    (0x66D000, '交谈', '話す'),            # talk
    (0x66D200, '移动', '移動'),            # move
    (0x66D600, '搜索', '捜索'),            # search
    (0x66D800, '搭档', 'パートナー'),      # partner
]
CMD_INK, CMD_FILLS = 1, (4, 5, 6, 7)      # dark strokes on a light textured fill

FONTS = [
    '/System/Library/Fonts/STHeiti Medium.ttc',
    '/System/Library/Fonts/Hiragino Sans GB.ttc',
    '/System/Library/Fonts/Supplemental/Songti.ttc',
    '/System/Library/Fonts/PingFang.ttc',
]
# ink height we aim for, per block (matches the Japanese label's own height)
WANT_H = {'row 游戏时间': 9, 'menu セーブ': 9, 'menu ロード': 9,
          'menu 消去': 9}

# runtime OBJ palette banks (luminance of each index, read from a savestate) so
# the preview shows what the player actually sees; index 0 is transparent
BANK = {'row 游戏时间': 1, 'menu セーブ': 4, 'menu ロード': 0, 'menu 消去': 0}
LUM = {0: [0, 0, 0, 0, 0, 0, 0, 0, 0, 57, 100, 133, 189, 214, 220, 221],
       1: [76, 90, 139, 182, 182, 208, 190, 190, 252, 252, 252, 252, 252, 253, 253, 253],
       4: [0, 135, 0, 0, 255, 0, 0, 0, 170, 92, 92, 143, 158, 142, 179, 190]}


def decode_seg(data, w, h):
    img = [[0] * w for _ in range(h)]
    per_row = w // 8
    for t in range((w // 8) * (h // 8)):
        base = t * 32
        tx, ty = (t % per_row) * 8, (t // per_row) * 8
        for y in range(8):
            for x in range(8):
                img[ty + y][tx + x] = (data[base + y * 4 + x // 2] >> (4 * (x & 1))) & 0xF
    return img


def encode_seg(img, w, h, ox=0, oy=0):
    out = bytearray((w // 8) * (h // 8) * 32)
    per_row = w // 8
    for t in range((w // 8) * (h // 8)):
        base = t * 32
        tx, ty = (t % per_row) * 8, (t // per_row) * 8
        for y in range(8):
            for x in range(8):
                v = img[oy + ty + y][ox + tx + x] & 0xF
                i = base + y * 4 + x // 2
                if x % 2 == 0:
                    out[i] = (out[i] & 0xF0) | v
                else:
                    out[i] = (out[i] & 0x0F) | (v << 4)
    return bytes(out)


def load_block(src, blk):
    w = max(x + sw for _, sw, sh, x, y in blk['segs'])
    h = max(y + sh for _, sw, sh, x, y in blk['segs'])
    img = [[0] * w for _ in range(h)]
    for off, sw, sh, x, y in blk['segs']:
        seg = decode_seg(src[off:off + (sw // 8) * (sh // 8) * 32], sw, sh)
        for yy in range(sh):
            for xx in range(sw):
                img[y + yy][x + xx] = seg[yy][xx]
    return img, w, h


def save_block(data, blk, img):
    for off, sw, sh, x, y in blk['segs']:
        blk_bytes = encode_seg(img, sw, sh, x, y)
        data[off:off + len(blk_bytes)] = blk_bytes


def pick_font(text, box, want_h):
    x0, y0, x1, y1 = box
    maxw, maxh = x1 - x0, y1 - y0
    best = None
    for path in FONTS:
        if not os.path.exists(path):
            continue
        for size in range(max(8, want_h - 3), want_h + 6):
            f = ImageFont.truetype(path, size)
            tmp = Image.new('L', (maxw * 2, maxh * 2), 0)
            ImageDraw.Draw(tmp).text((maxw, maxh), text, font=f, fill=255)
            bb = tmp.getbbox()
            if not bb:
                continue
            w, h = bb[2] - bb[0], bb[3] - bb[1]
            if w <= maxw and h <= maxh:
                score = abs(h - want_h) * 10 + abs(w - (maxw - 2))
                if best is None or score < best[0]:
                    best = (score, f, path, size)
    if best is None:
        raise SystemExit(f'no font fits {text!r} in {maxw}x{maxh}')
    return best[1], best[2], best[3]


def _crop_ink(rows):
    """Trim a bitmap to its ink bounding box."""
    ys = [j for j, r in enumerate(rows) if any(r)]
    xs = [i for i in range(len(rows[0])) if any(r[i] for r in rows)]
    if not ys or not xs:
        return []
    return [[rows[j][i] for i in range(min(xs), max(xs) + 1)] for j in range(min(ys), max(ys) + 1)]


def _drop_row(rows):
    """Remove the emptiest row (pixel fonts carry a padding row we can spare)."""
    best, bi = None, 0
    for j, r in enumerate(rows):
        n = sum(r)
        if best is None or n < best:
            best, bi = n, j
    return rows[:bi] + rows[bi + 1:]


def _drop_col(rows):
    w = len(rows[0])
    best, bi = None, 0
    for i in range(w):
        n = sum(r[i] for r in rows)
        if best is None or n < best:
            best, bi = n, i
    return [r[:bi] + r[bi + 1:] for r in rows]


def auto_box(img, ink=CMD_INK, fills=CMD_FILLS, pad=1):
    """Text box of a command plate: the bounding box of its dark strokes."""
    pts = [(x, y) for y, row in enumerate(img) for x, v in enumerate(row) if v == ink]
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    h, w = len(img), len(img[0])
    x0 = max(0, min(xs) - pad); x1 = min(w, max(xs) + 1 + pad)
    y0 = max(0, min(ys) - pad); y1 = min(h, max(ys) + 1 + pad)
    import collections
    c = collections.Counter(v for row in img for v in row if v in fills)
    fill = c.most_common(1)[0][0] if c else 7
    return (x0, y0, x1, y1), fill


def draw_label(img, box, text, fill, ink, aa, pixel):
    """Draw `text` into the box using the 11x11 pixel font, centred.

    An outline font rasterised at ~13px turns into a hatch of anti-aliasing
    pixels once it is quantised to the plate's two inks -- that is exactly what
    made the save menu look corrupted.  The pixel font has no such problem: its
    bitmaps are already 1px strokes, and when a glyph is one row taller than the
    box we drop the emptiest row (pixel fonts keep a padding row).
    """
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0, y1 - y0
    for y in range(y0, y1):
        for x in range(x0, x1):
            img[y][x] = fill
    glyphs = []
    for ch in text:
        body = pixel.body(ch)          # 16x16 cell, 11x11 design box
        rows = _crop_ink([r[:] for r in body])
        if not rows:
            continue
        while len(rows) > bh:
            rows = _drop_row(rows)
        while len(rows[0]) * len(text) + (len(text) - 1) > bw:
            rows = _drop_col(rows)
        glyphs.append(rows)
    if not glyphs:
        return
    gap = 1 if bw >= sum(len(g[0]) for g in glyphs) + len(glyphs) - 1 else 0
    total = sum(len(g[0]) for g in glyphs) + gap * (len(glyphs) - 1)
    x = x0 + max(0, (bw - total) // 2)
    for rows in glyphs:
        h, w = len(rows), len(rows[0])
        oy = y0 + max(0, (bh - h) // 2)
        for j in range(h):
            for i in range(w):
                if not rows[j][i]:
                    continue
                xx, yy = x + i, oy + j
                if x0 <= xx < x1 and y0 <= yy < y1:
                    img[yy][xx] = ink
        x += w + gap
    # The original plate text is 1px core + 1px anti-aliasing on the right
    # (JP ロード uses 9 with 12); without it a 1px pixel-font glyph looks thin
    # and "hollow" next to the untouched Japanese labels.
    if aa is not None:
        for y in range(y0, y1):
            for x in range(x0, x1 - 1):
                if img[y][x] == ink and img[y][x + 1] == fill:
                    img[y][x + 1] = aa


def to_png(img, scale=5, bank=None):
    h, w = len(img), len(img[0])
    tab = LUM.get(bank, [i * 17 for i in range(16)])
    out = Image.new('RGB', (w * scale, h * scale), (255, 0, 255))
    for y in range(h):
        for x in range(w):
            v = img[y][x]
            c = (255, 0, 255) if v == 0 else (tab[v], tab[v], tab[v])
            for dy in range(scale):
                for dx in range(scale):
                    out.putpixel((x * scale + dx, y * scale + dy), c)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--rom', default=JP_ROM)
    ap.add_argument('--apply', nargs=2, metavar=('IN', 'OUT'))
    ap.add_argument('--preview', metavar='PNG')
    ap.add_argument('--pixel-font', default=pixelfont.DEFAULT)
    a = ap.parse_args()

    pixel = pixelfont.load(a.pixel_font)
    src = open(a.rom, 'rb').read()
    blocks = list(BLOCKS)
    for off, text, jp in CMD_PLATES:
        blocks.append(dict(name=f'cmd {jp}',
                           segs=[(off, 32, 16, 0, 0), (off + 0x100, 32, 16, 32, 0)],
                           labels=[('auto', text, jp, CMD_INK, None, None)]))
    results = []
    for blk in blocks:
        before, w, h = load_block(src, blk)
        after = [row[:] for row in before]
        for box, text, jp, ink, aa, fill in blk['labels']:
            if box == 'auto':
                found = auto_box(after, ink, CMD_FILLS)
                if not found:
                    print(f"{blk['name']}: no ink found, skipped")
                    continue
                box, fill = found
            draw_label(after, box, text, fill, ink, aa, pixel)
            pts = [(x, y) for y in range(len(after)) for x in range(len(after[0]))
                   if after[y][x] == ink and box[0] <= x < box[2] and box[1] <= y < box[3]]
            iw = max(p[0] for p in pts) - min(p[0] for p in pts) + 1
            ih = max(p[1] for p in pts) - min(p[1] for p in pts) + 1
            print(f"{blk['name']}: {jp} -> {text}  box={box} ink={ink} fill={fill} "
                  f"(drawn {iw}x{ih})")
        results.append((blk, w, h, before, after))

    if a.preview:
        scale, pad = 6, 4
        width = max(w for _, w, _, _, _ in results) * scale
        height = sum((h * 2 + pad) * scale for _, _, h, _, _ in results)
        sheet = Image.new('RGB', (width, height), (0, 0, 0))
        y = 0
        for blk, w, h, before, after in results:
            bank = BANK.get(blk['name'], 15)
            sheet.paste(to_png(before, scale, bank), (0, y)); y += h * scale
            sheet.paste(to_png(after, scale, bank), (0, y)); y += (h + pad) * scale
        sheet.save(a.preview)
        print(f'wrote {a.preview}')

    if a.apply:
        inp, outp = a.apply
        data = bytearray(open(inp, 'rb').read())
        for blk, w, h, before, after in results:
            save_block(data, blk, after)
        open(outp, 'wb').write(bytes(data))
        print(f'wrote {outp} ({len(data)} bytes)')


if __name__ == '__main__':
    main()
