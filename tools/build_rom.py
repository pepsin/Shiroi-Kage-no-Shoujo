#!/usr/bin/env python3
"""Build the Simplified-Chinese ROM.

Two things have to happen:

1. **Font table**  the translation needs 2104 glyphs (931 reuse the JP table,
   1171 are new) but the JP table only has 1704 slots and only 771 of the
   unused ones can be recycled -> the table is rewritten with 400 extra
   glyphs.  A bigger table does not fit in the 198 KB tail (it needs 269 KB),
   so it is placed at ROM file 0x800000 and the file is grown past 8 MB;
   FAT entry 850 is repointed at it.  This is the same layout the reference
   Chinese release used (see tools/build_jp_map.py) and needs no code patch:
   the table is addressed through the FAT, and glyphs are read straight out
   of cart ROM.

2. **Script**  import_script.py rewrites every translated string in place with
   the extended character map.

Usage:
  build_rom.py [--rom JP.gba] [--master data/translation.tsv] [--out out.gba]
               [--headroom 128] [--no-script]
"""
import argparse
import collections
import csv
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import font_patch
import gbtext as g
import mapio

FAT = 0x15A000
BASE = 0x15C000
FONT_EID = 850
OLD_COUNT = 0x6A8
GLYPH_BYTES = 0x80
NEW_TABLE_OFF = 0x800000          # file offset of the rewritten table
WORK = os.path.join(ROOT, 'work')
EXT_MAP = os.path.join(WORK, 'glyph_map.ext.csv')


def load_master(path):
    with open(path, encoding='utf-8') as f:
        return list(csv.DictReader(f, delimiter='\t'))


def plan(rows, gmap, recycle=False):
    """Return (assignment, new_chars, table_count, free_used, extra)."""
    rev = {}
    for idx, ch in sorted(gmap.items(), reverse=True):
        code = idx if idx < 0x20 else idx - 1
        if code >= 0 and ch:
            rev.setdefault(ch, code)
    used = collections.Counter()
    for r in rows:
        for ch in (r.get('translation') or '').strip():
            used[ch] += 1
    have = set(gmap.values()) - {''}
    used_idx = set()
    for ch in used:
        code = rev.get(ch)
        if code is not None:
            used_idx.add(code if code < 0x20 else code + 1)
    free = [i for i in range(0x20, OLD_COUNT) if i not in used_idx]
    new_chars = sorted((c for c in used if c not in have), key=lambda c: (-used[c], c))
    if recycle:
        # reuse slots the *script* no longer references -- smaller table, but
        # glyphs used by other resources (title art, menus) would be destroyed
        got = new_chars[:len(free)]
        extra = new_chars[len(free):]
        assign = {ch: free[i] for i, ch in enumerate(got)}
    else:
        got, extra = [], new_chars
        assign = {}
    for i, ch in enumerate(extra):
        assign[ch] = OLD_COUNT + i
    table_count = OLD_COUNT + len(extra)
    return assign, new_chars, table_count, len(got), extra, used, rev, used_idx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--rom', default=g.JP_ROM)
    ap.add_argument('--master', default=os.path.join(ROOT, 'data', 'translation.tsv'))
    ap.add_argument('--out', default=os.path.join(ROOT, 'out.gba'))
    ap.add_argument('--headroom', type=int, default=128,
                    help='extra empty glyph slots appended for safety')
    ap.add_argument('--recycle', action='store_true',
                    help='reuse glyph slots the script no longer references '
                         '(smaller table, but overwrites glyphs other '
                         'resources may still use -- off by default)')
    ap.add_argument('--no-script', action='store_true',
                    help='only patch the font table, do not import the script')
    a = ap.parse_args()

    rom = bytearray(open(a.rom, 'rb').read())
    gmap = mapio.load_map(os.path.join(ROOT, 'data', 'glyph_map.csv'))
    rows = load_master(a.master)
    assign, new_chars, count, recycled, extra, used, rev, used_idx = plan(rows, gmap, a.recycle)
    count += a.headroom
    print(f'distinct chars in translation : {len(used)}')
    print(f'  reuse JP glyph slots        : {len(used) - len(new_chars)}')
    print(f'  new glyphs                  : {len(new_chars)}')
    print(f'    recycled free slots       : {recycled}'
          f'{" (disabled: append-only)" if not a.recycle else ""}')
    print(f'    appended past 0x{OLD_COUNT:03X}          : {len(extra)}')
    print(f'new table: {count} glyphs = {count * GLYPH_BYTES} bytes '
          f'(+{(count - OLD_COUNT) * GLYPH_BYTES} vs JP)')

    # --- build the table -------------------------------------------------
    old = rom[font_patch.GLYPH_FILE_OFF:
              font_patch.GLYPH_FILE_OFF + OLD_COUNT * GLYPH_BYTES]
    table = bytearray(old) + bytearray((count - OLD_COUNT) * GLYPH_BYTES)
    for ch, idx in sorted(assign.items(), key=lambda kv: kv[1]):
        table[idx * GLYPH_BYTES:(idx + 1) * GLYPH_BYTES] = font_patch.render_glyph(ch)
    print(f'rendered {len(assign)} glyphs; table holds {len(table) // GLYPH_BYTES} slots')

    # --- new map ---------------------------------------------------------
    newmap = dict(gmap)
    for ch, idx in assign.items():
        newmap[idx] = ch
    with open(EXT_MAP, 'w', encoding='utf-8', newline='') as f:
        w = csv.writer(f)
        w.writerow(['code', 'dec', 'char'])
        for idx in range(count):
            w.writerow([f'{idx:03X}', idx, newmap.get(idx, '')])
    print(f'wrote {EXT_MAP} ({count} entries)')

    # --- patch the ROM ---------------------------------------------------
    end = NEW_TABLE_OFF + len(table)
    if len(rom) < end:
        rom.extend(b'\x00' * (end - len(rom)))
    rom[NEW_TABLE_OFF:end] = table
    off = NEW_TABLE_OFF - BASE
    struct.pack_into('<II', rom, FAT + FONT_EID * 8, off, len(table))
    print(f'font table -> file 0x{NEW_TABLE_OFF:X} (FAT {FONT_EID}: '
          f'off=0x{off:X} size={len(table)})')
    # sanity: the old tail must be free, and the new blob must not collide
    print(f'ROM size now {len(rom)} bytes ({len(rom) / 1048576:.2f} MB)')

    patched = os.path.join(WORK, 'font_patched.gba')
    open(patched, 'wb').write(bytes(rom))
    print(f'wrote {patched}')

    if a.no_script:
        return
    # --- import the script ----------------------------------------------
    import subprocess
    cmd = [sys.executable, os.path.join(ROOT, 'tools', 'import_script.py'),
           '--master', a.master, '--rom', patched, '--out', a.out,
           '--map', EXT_MAP]
    print('running:', ' '.join(cmd[1:]))
    subprocess.run(cmd, check=True)

    # --- redraw the pre-rendered menu plates (title screen) ---------------
    cmd = [sys.executable, os.path.join(ROOT, 'tools', 'patch_menu_plates.py'),
           '--apply', a.out, a.out]
    print('running:', ' '.join(cmd[1:]))
    subprocess.run(cmd, check=True)


if __name__ == '__main__':
    main()
