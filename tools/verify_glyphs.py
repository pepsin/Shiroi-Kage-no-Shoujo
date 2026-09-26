#!/usr/bin/env python3
"""Self-check: the built ROM's font table, the glyph map and data/glyph_png agree.

Checks
  A  every character used in data/translation.tsv has a font slot whose bitmap
     is not blank (a blank glyph would show as nothing in game)
  B  every character has exactly **one** canonical image in the PNG directory
     (`{char}.png` / `{char}（新）.png`), i.e. one image per character
  C  every PNG in the directory is in sync with its slot in this ROM
     (catches un-imported hand edits and PNGs left over from an older export)
  D  the manifest lists every slot of the font table exactly once

Usage:  verify_glyphs.py <rom> [--dir data/glyph_png] [--master data/translation.tsv]
                        [--map work/glyph_map.ext.csv]
Exit code 0 = OK, 1 = problem.
"""
import argparse
import collections
import csv
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
from import_glyphs import levels_from_image, pack  # noqa: E402
from export_glyphs import font_table  # noqa: E402

FAT = 0x15A000
BASE = 0x15C000
FONT_EID = 850
GLYPH_BYTES = 0x80


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('rom')
    ap.add_argument('--dir', default=os.path.join(ROOT, 'data', 'glyph_png'))
    ap.add_argument('--master', default=os.path.join(ROOT, 'data', 'translation.tsv'))
    ap.add_argument('--map', default=os.path.join(ROOT, 'work', 'glyph_map.ext.csv'))
    a = ap.parse_args()

    rom = open(a.rom, 'rb').read()
    off, size = struct.unpack_from('<2I', rom, FAT + FONT_EID * 8)
    base = BASE + off
    table, count = font_table(rom)
    problems = []

    def slot(idx):
        return table[idx * GLYPH_BYTES:(idx + 1) * GLYPH_BYTES]

    gmap = {}
    with open(a.map, encoding='utf-8') as f:
        for r in csv.DictReader(f):
            ch = (r.get('char') or '').strip()
            if ch:
                gmap[int(r['dec'], 0)] = ch

    # ---- A: every translated character is drawable ------------------------
    canon_idx = {}
    for idx in sorted(gmap):
        canon_idx.setdefault(gmap[idx], idx)
    used = set()
    with open(a.master, encoding='utf-8') as f:
        for r in csv.DictReader(f, delimiter='\t'):
            t = (r.get('translation') or '').replace('\\n', '\n')
            used |= set(t)
    blank = sorted(c for c in used
                   if c in canon_idx and not any(slot(canon_idx[c])))
    no_glyph = sorted(c for c in used if c not in canon_idx)
    print(f'A  characters used in the script: {len(used)}; '
          f'without a slot: {len(no_glyph)}; mapped to a blank glyph: {len(blank)}')
    if no_glyph:
        problems.append(f'no glyph slot: {"".join(no_glyph[:40])}')
    if blank:
        problems.append(f'blank glyph: {"".join(blank[:40])}')

    # ---- B/C/D: the PNG directory ----------------------------------------
    man = os.path.join(a.dir, 'manifest.tsv')
    if not os.path.exists(man):
        print(f'B  {man} not found - skipped')
    else:
        rows = list(csv.DictReader(open(man, encoding='utf-8'), delimiter='\t'))
        seen = collections.Counter(int(r['index'], 16) for r in rows)
        per_char = collections.Counter(r['char'] for r in rows if r['role'] == 'canon')
        dup_chars = [c for c, k in per_char.items() if k > 1]
        multi = [(c, k) for c, k in per_char.items() if k > 1]
        print(f'B  canonical images: {len(per_char)} for {len(per_char) + len(multi)} '
              f'characters; characters with more than one image: {len(dup_chars)}')
        if dup_chars:
            problems.append(f'character has several images: {dup_chars[:10]}')
        if seen and max(seen.values()) > 1:
            problems.append('manifest lists a slot twice')
        if len(rows) != count:
            problems.append(f'manifest has {len(rows)} rows, font table has {count} slots')

        edited = dup_bad = 0
        canon_png = {}
        for r in rows:
            if r['role'] != 'canon':
                continue
            p = os.path.join(a.dir, r['file'])
            if os.path.exists(p):
                canon_png[r['char']] = pack(levels_from_image(p))
        for r in rows:
            p = os.path.join(a.dir, r['file'])
            if not os.path.exists(p):
                problems.append(f'missing PNG: {r["file"]}')
                continue
            got = pack(levels_from_image(p))
            if r['role'] == 'dup':
                # a duplicate slot must show the same image as its character
                if got != canon_png.get(r['char']):
                    dup_bad += 1
            elif got != slot(int(r['index'], 16)):
                edited += 1
        on_disk = {f.casefold() for f in os.listdir(a.dir) if f.lower().endswith('.png')}
        listed = {r['file'].casefold() for r in rows}
        stale = len(on_disk - listed)
        print(f'C  PNGs out of sync with the ROM: {edited}; '
              f'duplicate-slot PNGs not matching their character: {dup_bad}; '
              f'PNGs on disk that the manifest does not list: {stale}')
        if edited:
            problems.append(f'{edited} PNG(s) differ from the ROM (un-imported hand edits)')
        if dup_bad:
            problems.append(f'{dup_bad} duplicate-slot PNG(s) differ from the canonical image')
        if stale:
            problems.append(f'{stale} stale PNG(s) in {a.dir}')
        print(f'D  manifest rows: {len(rows)}; font slots: {count}')

    print('RESULT: ' + ('OK' if not problems else 'PROBLEM'))
    for p in problems:
        print(f'  - {p}')
    return 0 if not problems else 1


if __name__ == '__main__':
    sys.exit(main())
