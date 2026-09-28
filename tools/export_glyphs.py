#!/usr/bin/env python3
"""Export the font table as one PNG per character (hand-editing workflow).

Naming rules — one image per *character*, so you always know which file to edit:

  {char}.png            the canonical slot for that character: the slot the
                        encoder actually writes (the lowest index holding it)
  {char}（新）.png       same, for a glyph the Chinese build appended
                        (index >= 0x6A8), i.e. one the JP font never had
  重复_{index}.png       a duplicate slot that holds the same character; edits
                        made to the canonical file are written to these too
  未使用_{index}.png     a slot with no character assigned

PNGs use the game's own text palette (bg 248, body 80, shadow 104) so they show
exactly what the player sees.  `manifest.tsv` records file <-> index <-> char,
plus `role` (canon/dup/unused) and `is_new`.

Stale PNGs from earlier exports are deleted, so the directory always matches
the current ROM.

Safety check: if a PNG in the output directory differs from the ROM bitmap of
its slot, that is an un-imported hand edit.  The script then refuses to
overwrite it and tells you to run import_glyphs.py first (or pass --force).

Usage:
  export_glyphs.py [--rom out.gba] [--map work/glyph_map.ext.csv]
                   [--out data/glyph_png] [--scale 8] [--new-from 0x6A8]
                   [--force]
"""
import argparse
import collections
import csv
import os
import struct
import sys
import unicodedata

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
from import_glyphs import levels_from_image, pack  # noqa: E402  (round-trip check)

FAT = 0x15A000
BASE = 0x15C000
FONT_EID = 850
GLYPH_BYTES = 0x80
BAD = {'/': '／', ':': '：', '\\': '＼', '\n': '', '\r': '', '\t': ' '}
RAMP = {0: (248, 248, 248), 1: (80, 80, 80), 2: (104, 96, 88)}


def font_table(rom):
    off, size = struct.unpack_from('<2I', rom, FAT + FONT_EID * 8)
    assert off, 'font entry not found'
    start = BASE + off
    return rom[start:start + size], size // GLYPH_BYTES


def glyph_pixels(b):
    """128 bytes -> 16x16 list of 0..15."""
    px = [[0] * 16 for _ in range(16)]
    for t in range(4):
        tx, ty = (t & 1) * 8, (t >> 1) * 8
        for y in range(8):
            for x in range(4):
                v = b[t * 32 + y * 4 + x]
                px[ty + y][tx + x * 2] = v & 0xF
                px[ty + y][tx + x * 2 + 1] = (v >> 4) & 0xF
    return px


def safe_stem(ch):
    s = ch
    for k, v in BAD.items():
        s = s.replace(k, v)
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--rom', default=os.path.join(ROOT, 'out.gba'))
    ap.add_argument('--map', default=os.path.join(ROOT, 'work', 'glyph_map.ext.csv'))
    ap.add_argument('--out', default=os.path.join(ROOT, 'data', 'glyph_png'))
    ap.add_argument('--scale', type=int, default=8)
    ap.add_argument('--new-from', type=lambda s: int(s, 0), default=0x6A8)
    ap.add_argument('--keep-stale', action='store_true',
                    help='do not delete PNGs that this run does not write')
    ap.add_argument('--force', action='store_true',
                    help='overwrite PNGs that differ from the ROM (un-imported edits)')
    a = ap.parse_args()

    from PIL import Image
    rom = open(a.rom, 'rb').read()
    table, count = font_table(rom)
    gmap = {}
    with open(a.map, encoding='utf-8') as f:
        for r in csv.DictReader(f):
            ch = (r.get('char') or '').strip()
            if ch:
                gmap[int(r['dec'], 0)] = ch

    # canonical slot per character = the one the encoder writes = lowest index
    canon = {}
    for idx in sorted(gmap):
        canon.setdefault(gmap[idx], idx)

    os.makedirs(a.out, exist_ok=True)
    written = set()
    rows = []
    plans = []          # (name, idx, ch, role, px, ink, blk)
    n_new = n_dup = n_unused = 0
    canon_px = {}       # a character is drawn once; its slots share one image
    for idx in range(count):
        blk = table[idx * GLYPH_BYTES:(idx + 1) * GLYPH_BYTES]
        if len(blk) < GLYPH_BYTES:
            continue
        ch = gmap.get(idx, '')
        px = glyph_pixels(blk)
        ink = sum(1 for row in px for v in row if v)
        if not ch:
            role, stem = 'unused', f'未使用_{idx:03X}'
            n_unused += 1
        elif canon.get(ch) == idx:
            is_new = idx >= a.new_from and ink > 0
            role = 'canon'
            stem = safe_stem(ch) + ('（新）' if is_new else '')
            canon_px[ch] = px
            if is_new:
                n_new += 1
        else:
            role, stem = 'dup', f'重复_{idx:03X}'
            n_dup += 1
            # the duplicate slot shows the canonical image, so the directory
            # stays consistent with the ROM once the edits are imported
            px = canon_px[ch]
            ink = sum(1 for row in px for v in row if v)
        # macOS is case-insensitive, so uniqueness has to be case-folded
        name = f'{stem}.png'
        if name.casefold() in {w.casefold() for w in written}:
            base, ext = os.path.splitext(name)
            k = 2
            while f'{base}_{k}{ext}'.casefold() in {w.casefold() for w in written}:
                k += 1
            name = f'{base}_{k}{ext}'
        written.add(name)
        plans.append((name, idx, ch, role, px, ink, blk))

    # never silently discard hand edits that were not imported into the ROM yet
    # (a 重复_* file is a copy of its canonical image, not of its own slot, so
    # only the files that mirror a slot are compared here)
    hand = [name for name, idx, ch, role, px, ink, blk in plans
            if role != 'dup' and os.path.exists(os.path.join(a.out, name))
            and pack(levels_from_image(os.path.join(a.out, name))) != blk]
    if hand and not a.force:
        print(f'{len(hand)} PNG(s) in {a.out} differ from the ROM — hand edits '
              f'that were never imported back:', file=sys.stderr)
        for nm in hand[:10]:
            print(f'  {nm}', file=sys.stderr)
        if len(hand) > 10:
            print(f'  ... and {len(hand) - 10} more', file=sys.stderr)
        print('run tools/import_glyphs.py --out out.gba first, then re-export '
              '(or pass --force to overwrite them).', file=sys.stderr)
        raise SystemExit(2)

    for name, idx, ch, role, px, ink, blk in plans:
        img = Image.new('RGB', (16, 16), RAMP[0])
        p = img.load()
        for y in range(16):
            for x in range(16):
                v = px[y][x]
                if v:
                    p[x, y] = RAMP.get(v, (0, 0, 0))
        if a.scale != 1:
            img = img.resize((16 * a.scale, 16 * a.scale), Image.NEAREST)
        img.convert('P', palette=Image.ADAPTIVE, colors=4).save(
            os.path.join(a.out, name), optimize=True)
        rows.append({'file': name, 'index': f'{idx:03X}', 'code': f'{idx + 1:03X}',
                     'char': ch, 'is_new': int(role == 'canon' and idx >= a.new_from and ink > 0),
                     'role': role, 'ink': ink})

    if not a.keep_stale:
        # Names are compared after NFC normalisation.  A volume that stores a
        # name in decomposed form (macOS HFS+ always did, and an APFS file
        # created on one keeps that spelling) returns e.g. 'が' as 'か'+U+3099
        # from listdir, while `written` holds the composed 'が'; a raw string
        # compare then deletes a file that was just written.  That silently
        # dropped all 46 dakuten kana glyphs on every rebuild.
        keep = {unicodedata.normalize('NFC', n).casefold() for n in written}
        keep |= {'manifest.tsv', 'readme.md'}
        for f in os.listdir(a.out):
            if not f.lower().endswith('.png'):
                continue
            if unicodedata.normalize('NFC', f).casefold() not in keep:
                os.remove(os.path.join(a.out, f))
                print(f'removed stale {f}')

    with open(os.path.join(a.out, 'manifest.tsv'), 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['file', 'index', 'code', 'char', 'is_new',
                                          'role', 'ink'], delimiter='\t')
        w.writeheader()
        w.writerows(rows)

    chars = collections.Counter(r['char'] for r in rows if r['role'] == 'canon')
    print(f'exported {len(rows)} slots to {a.out}')
    print(f'  characters with one image each: {len(chars)}  ({n_new} marked （新）)')
    print(f'  duplicate slots (重复_*): {n_dup};  unused slots (未使用_*): {n_unused}')


if __name__ == '__main__':
    main()
