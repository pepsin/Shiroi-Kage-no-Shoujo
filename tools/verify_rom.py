#!/usr/bin/env python3
"""Verify a built Chinese ROM against the master translation table.

For every translated row it reads the string back out of the ROM (using the
ROM's own FAT/string structure) and compares it with data/translation.tsv.
It also checks that every code the script uses has a glyph in the ROM's font
table.

Usage:
  verify_rom.py <rom> [--master data/translation.tsv] [--map work/glyph_map.ext.csv]
"""
import argparse
import csv
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import gbtext as g
import export_script as ex
import mapio

# the JP-only tools cap codes at the original table size; the Chinese build
# appends glyphs past it, so widen the accepted range before analysing.
ex.GLYPH_MAX = 0x2000

FAT = 0x15A000
BASE = 0x15C000
FONT_EID = 850


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('rom')
    ap.add_argument('--master', default=os.path.join(ROOT, 'data', 'translation.tsv'))
    ap.add_argument('--map', default=os.path.join(ROOT, 'work', 'glyph_map.ext.csv'))
    a = ap.parse_args()
    rom = open(a.rom, 'rb').read()
    m = mapio.load_map(a.map)
    # decode rule (verified against the JP ROM): char = map[code] for
    # code < 0x20, else map[code - 1]
    table = {}
    for idx, ch in m.items():
        if not ch:
            continue
        table[idx if idx < 0x20 else idx + 1] = ch

    cat = {}
    for r in csv.DictReader(open(os.path.join(ROOT, 'data', 'entry_catalog.tsv'),
                                 encoding='utf-8'), delimiter='\t'):
        if r.get('base'):
            cat[int(r['eid'])] = (int(r['table_off']), int(r['base']))
    rows = list(csv.DictReader(open(a.master, encoding='utf-8'), delimiter='\t',
                               restkey='extra'))
    want = {}          # entry -> {idx: text}   (offset-table rows)
    want_pool = {}     # entry -> {absolute_offset: (text, slot_words)}
    for r in rows:
        tr = (r.get('translation') or '').strip()
        if not tr:
            continue
        eid = int(r['entry'])
        if (r.get('extra') or [''])[0] == 'pool':
            want_pool.setdefault(eid, {})[int(r['offset'])] = (tr, int(r['n_codes']) + 1)
        else:
            want.setdefault(eid, {})[int(r['idx'])] = tr

    # --- font table sanity ------------------------------------------------
    off, size = struct.unpack_from('<2I', rom, FAT + FONT_EID * 8)
    foff = BASE + off
    print(f'font entry {FONT_EID}: file 0x{foff:X} size {size} '
          f'({size // 0x80} glyphs); ROM size {len(rom)} ({len(rom) / 1048576:.2f} MB)')
    assert foff + size <= len(rom), 'font table runs past EOF'
    blank = sum(1 for i in range(size // 0x80)
                if not any(rom[foff + i * 0x80:foff + (i + 1) * 0x80]))
    print(f'blank glyph slots in table: {blank}')

    # --- every code used by the translation must have a glyph -------------
    used_codes = set()
    for eid, trs in want.items():
        d = g.load_entry(rom, eid)
        if d is None:
            continue
        if eid not in cat:
            continue
        toff, base = cat[eid]
        vals, p = [], toff
        while p + 4 <= len(d):
            x = struct.unpack_from('<I', d, p)[0]
            if x == 0xFFFFFFFF or x > len(d) or (vals and x <= vals[-1]):
                break
            vals.append(x)
            p += 4
        for idx in trs:
            if idx >= len(vals):
                continue
            o = base + vals[idx]
            n = 0
            while o + 2 * n + 2 <= len(d) and struct.unpack_from('<H', d, o + 2 * n)[0] != 0:
                n += 1
            if o + 2 * n > len(d):
                continue
            used_codes.update(struct.unpack_from(f'<{n}H', d, o))
    missing = sorted(c for c in used_codes
                     if c not in (0, 0x0D, 0x0E) and c not in table)
    print(f'codes referenced by the script: {len(used_codes)}; without a glyph: {len(missing)}')
    if missing:
        print('  e.g.', [hex(c) for c in missing[:10]])

    # --- round-trip ------------------------------------------------------
    ok = bad = 0
    samples = []
    # entries whose text lives in a NUL-separated pool (no offset table)
    for eid in sorted(want_pool):
        d = g.load_entry(rom, eid)
        if d is None:
            print(f'e{eid:04d}: missing entry')
            continue
        for off, (tr, slot) in sorted(want_pool[eid].items()):
            n = 0
            while off + 2 * n + 2 <= len(d) and struct.unpack_from('<H', d, off + 2 * n)[0] != 0:
                n += 1
            got = ''.join(table.get(c, f'[{c:03X}]')
                          for c in struct.unpack_from(f'<{n}H', d, off))
            if got == tr:
                ok += 1
            else:
                bad += 1
                if len(samples) < 40:
                    samples.append(f'e{eid}:@{off:X} want {tr!r} got {got!r}')
    for eid in sorted(want):
        d = g.load_entry(rom, eid)
        if d is None:
            print(f'e{eid:04d}: missing entry')
            continue
        if eid not in cat:
            print(f'e{eid:04d}: not in entry catalog')
            continue
        toff, base = cat[eid]
        got = {}
        for idx, o, codes, end in ex.entry_strings(d, toff, base):
            got[idx] = ''.join(table.get(c, f'[{c:03X}]') for c in codes)
        for idx, tr in want[eid].items():
            g_ = got.get(idx)
            if g_ == tr:
                ok += 1
            else:
                bad += 1
                if len(samples) < 40:
                    samples.append(f'e{eid}:{idx} want {tr!r} got {g_!r}')
    print(f'round-trip (table + pool): {ok} strings match, {bad} differ')
    for s in samples:
        print('  ', s)
    print('RESULT:', 'OK' if (bad == 0 and not missing) else 'PROBLEM')


if __name__ == '__main__':
    main()
