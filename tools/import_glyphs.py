#!/usr/bin/env python3
"""Import hand-edited glyph PNGs back into a ROM's font table.

Companion to export_glyphs.py.  The directory holds **one image per character**
(`{char}.png`, or `{char}（新）.png` for a glyph the Chinese build appended), so
you edit a single file per character.  A character that occupies several font
slots (some do) has those extra slots listed as `重复_{index}.png`; the edit you
make to the canonical file is written to **all** of that character's slots, so a
redrawn glyph cannot come out right in one place and stale in another.

Pixel mapping (matches the game's text palette: bg 248, body 80, shadow 104):
    light  -> 0 (transparent)
    mid    -> 2 (shadow)
    dark   -> 1 (body)

With --autoshadow the two-level style of the original font is re-applied:
every body pixel gets a 1px shadow to its right, exactly like the 1703
original glyphs.

Usage:
  import_glyphs.py --out out2.gba [--rom out.gba] [--dir data/glyph_png]
                   [--manifest data/glyph_png/manifest.tsv]
                   [--only-new] [--autoshadow] [--dry-run]
"""
import argparse
import collections
import csv
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAT = 0x15A000
BASE = 0x15C000
FONT_EID = 850
GLYPH_BYTES = 0x80
BG, BODY, SHADOW = 248, 80, 104


def levels_from_image(path):
    from PIL import Image
    img = Image.open(path).convert('L')
    if img.size != (16, 16):
        img = img.resize((16, 16), Image.BOX if img.size[0] > 16 else Image.NEAREST)
    p = img.load()
    lv = [[0] * 16 for _ in range(16)]
    for y in range(16):
        for x in range(16):
            v = p[x, y]
            if v > (BG + SHADOW) / 2:
                lv[y][x] = 0
            elif v > (BODY + SHADOW) / 2:
                lv[y][x] = 2
            else:
                lv[y][x] = 1
    return lv


def pack(lv):
    out = bytearray(GLYPH_BYTES)
    for t in range(4):
        tx, ty = (t & 1) * 8, (t >> 1) * 8
        for y in range(8):
            for x in range(4):
                lo = lv[ty + y][tx + x * 2] & 0xF
                hi = lv[ty + y][tx + x * 2 + 1] & 0xF
                out[t * 32 + y * 4 + x] = (hi << 4) | lo
    return bytes(out)


def index_of(row):
    return int(row['index'], 16)


def group_rows(rows):
    """Split manifest rows into per-character groups plus unassigned slots.

    Returns (groups, solo): groups maps char -> [rows...] (lowest index first),
    solo holds rows with no character (unused/decorative slots), handled
    one-to-one by their own file.
    """
    groups = collections.OrderedDict()
    solo = []
    for r in rows:
        ch = (r.get('char') or '').strip()
        if ch:
            groups.setdefault(ch, []).append(r)
        else:
            solo.append(r)
    for ch in groups:
        groups[ch].sort(key=index_of)
    return groups, solo


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--rom', default=os.path.join(ROOT, 'out.gba'))
    ap.add_argument('--out', default='')
    ap.add_argument('--dir', default=os.path.join(ROOT, 'data', 'glyph_png'))
    ap.add_argument('--manifest', default='')
    ap.add_argument('--only-new', action='store_true')
    ap.add_argument('--autoshadow', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    rom = bytearray(open(a.rom, 'rb').read())
    off, size = struct.unpack_from('<2I', rom, FAT + FONT_EID * 8)
    base = BASE + off
    max_slots = size // GLYPH_BYTES
    man = a.manifest or os.path.join(a.dir, 'manifest.tsv')
    rows = list(csv.DictReader(open(man, encoding='utf-8'), delimiter='\t'))
    oob = [r for r in rows if index_of(r) >= max_slots]
    if oob:
        print(f'ignoring {len(oob)} manifest row(s) beyond the font table '
              f'({max_slots} slots): {[r["file"] for r in oob[:5]]}', file=sys.stderr)
        rows = [r for r in rows if index_of(r) < max_slots]

    groups, solo = group_rows(rows)
    n_slots = n_dups = n_edit = n_sync = n_missing = n_chars = 0
    dup_report = []
    for ch, grp in groups.items():
        if a.only_new and not any(r.get('is_new') == '1' for r in grp):
            continue
        # the canonical file is authoritative; fall back to any sibling that
        # exists, so an edit made to a 重复_*.png is still honoured
        ordered = sorted(grp, key=lambda r: (r.get('role') != 'canon', index_of(r)))
        path = next((os.path.join(a.dir, r['file']) for r in ordered
                     if os.path.exists(os.path.join(a.dir, r['file']))), None)
        if path is None:
            n_missing += 1
            continue
        lv = levels_from_image(path)
        if a.autoshadow:
            for y in range(16):
                for x in range(16):
                    if lv[y][x] == 1 and x + 1 < 16 and lv[y][x + 1] == 0:
                        lv[y][x + 1] = 2
        blk = pack(lv)
        n_chars += 1
        if len(grp) > 1:
            n_dups += len(grp) - 1
            dup_report.append((ch, len(grp)))
        for r in grp:
            idx = index_of(r)
            differs = bytes(rom[base + idx * GLYPH_BYTES:base + (idx + 1) * GLYPH_BYTES]) != blk
            if differs:
                if r.get('role') == 'canon':
                    n_edit += 1
                else:
                    n_sync += 1
            rom[base + idx * GLYPH_BYTES:base + (idx + 1) * GLYPH_BYTES] = blk
            n_slots += 1
    n_solo = 0
    for r in solo:
        if a.only_new:
            continue
        path = os.path.join(a.dir, r['file'])
        if not os.path.exists(path):
            continue
        idx = index_of(r)
        rom[base + idx * GLYPH_BYTES:base + (idx + 1) * GLYPH_BYTES] = pack(
            levels_from_image(path))
        n_solo += 1

    print(f'imported {n_chars} characters -> {n_slots} slots '
          f'({n_dups} duplicate slots kept in sync), {n_solo} unassigned slots')
    print(f'  hand-edited images: {n_edit}   '
          f'duplicate slots re-synced to their canonical image: {n_sync}')
    if n_missing:
        print(f'  characters skipped (no PNG on disk): {n_missing}')
    for ch, k in sorted(dup_report, key=lambda t: -t[1])[:5]:
        print(f'  multi-slot character: {ch!r} x{k}')
    if a.dry_run:
        print('  (dry run: nothing written)')
        return
    if not a.out:
        raise SystemExit('--out is required unless --dry-run')
    open(a.out, 'wb').write(bytes(rom))
    print(f'wrote {a.out}')


if __name__ == '__main__':
    main()
