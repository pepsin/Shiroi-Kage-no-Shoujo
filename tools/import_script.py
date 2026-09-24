#!/usr/bin/env python3
"""Import translations and rebuild a ROM.

Filling in a `translation` column of data/translation.tsv and
running this tool rewrites the affected FAT entries:

  * the string pool is rebuilt from the translated strings (lengths change),
  * the entry's bytecode stays put; the offset table only has to be rewritten
    when the table no longer sits immediately before the pool,
  * each entry is re-compressed with the game's LZ77 and written back; if the
    new compressed block does not fit, it is appended and the FAT is repointed.

Usage:
  import_script.py --master data/translation.tsv \
                   --rom "Tantei Jinguuji Saburou - Shiroi Kage no Shoujo (Japan).gba" \
                   --out out.gba [--report-only]
"""
import argparse
import csv
import json
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import gbtext as g
import export_script as ex
import lz77

TEXTDIR = os.path.join(ROOT, 'data')


def load_maps():
    jp = {int(k, 16): v for k, v in
          json.load(open(os.path.join(TEXTDIR, 'glyph_map.json'), encoding='utf-8'))['map'].items()}
    return jp, None


def reverse_map(m, kind='jp'):
    """character -> the code to write for it (inverts the ROM's decode rule).

    A few characters exist at two indices; prefer the higher index because that
    is the one the game's text actually uses for the dakuten-form glyphs.
    """
    rev = {}
    for idx, ch in sorted(m.items(), reverse=True):
        code = idx if (kind != 'jp' or idx < 0x20) else idx - 1
        if code >= 0:
            rev[ch] = code
    return rev


def encode_text(text, rev):
    """Encode a translated string to u16 codes. Returns (codes, missing)."""
    codes = []
    missing = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == '\n':
            codes.append(0x0000 if kind == 'jp' else 0x000D)
            i += 1
            continue
        if ch == '\\' and i + 4 < len(text) and text[i + 1] == 'x':
            try:
                codes.append(int(text[i + 2:i + 5], 16))
                i += 5
                continue
            except ValueError:
                pass
        code = rev.get(ch)
        if code is None:
            missing.append(ch)
            code = 0x0000   # placeholder; caller reports
        codes.append(code)
        i += 1
    return codes, missing


def rebuild_entry(d, translations, rev, kind):
    """Rewrite strings in place, preserving the bytecode, offset table and every
    offset value.  A translation that does not fit the original slot is
    reported and skipped (padding fills the remainder).

    translations: {string_index: new_text}
    """
    info = ex.analyse(d)
    if not info:
        return None, 'no script structure', []
    toff, base = info['table_off'], info['base']
    vals = []
    p = toff
    while p + 4 <= len(d):
        x = struct.unpack_from('<I', d, p)[0]
        if x == 0xFFFFFFFF or x > len(d):
            break
        if vals and x <= vals[-1]:
            break
        vals.append(x)
        p += 4
    out = bytearray(d)
    missing_all = []
    too_long = []
    for idx, text in sorted(translations.items()):
        if idx >= len(vals):
            too_long.append((idx, 'no such string slot'))
            continue
        off = base + vals[idx]
        if off + 2 > len(d):
            too_long.append((idx, 'offset out of range'))
            continue
        # original slot: codes plus the 0x0000 terminator
        n = 0
        while off + 2 * n + 2 <= len(d) and struct.unpack_from('<H', d, off + 2 * n)[0] != 0:
            n += 1
        slot_words = n + 1
        new_codes, miss = encode_text(text, rev)
        missing_all += miss
        if any(c == 0 for c in new_codes):
            too_long.append((idx, 'contains unmapped character'))
            continue
        if len(new_codes) + 1 > slot_words:
            too_long.append((idx, f'needs {len(new_codes) + 1} words, slot has {slot_words}'))
            continue
        # write codes, then pad with 0x0000 to the end of the slot
        pos = off
        for c in new_codes:
            struct.pack_into('<H', out, pos, c)
            pos += 2
        while pos < off + 2 * slot_words:
            struct.pack_into('<H', out, pos, 0)
            pos += 2
    if len(out) != len(d):
        return None, 'internal error: size changed', []
    return bytes(out), None, too_long


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--master', default=os.path.join(TEXTDIR, 'translation.tsv'))
    ap.add_argument('--rom', default=g.JP_ROM)
    ap.add_argument('--out', default='')
    ap.add_argument('--report-only', action='store_true')
    a = ap.parse_args()
    jp_map, _ = load_maps()
    kind = g.which(a.rom)
    rev = reverse_map(jp_map, kind)
    rows = list(csv.DictReader(open(a.master, encoding='utf-8'), delimiter='\t'))
    todo = {}
    empty = 0
    for r in rows:
        tr = (r.get('translation') or '').strip()
        if not tr:
            empty += 1
            continue
        todo.setdefault(int(r['entry']), {})[int(r['idx'])] = tr
    print(f'master rows: {len(rows)}; with translation: {len(rows) - empty}; entries touched: {len(todo)}')
    if not todo:
        print('nothing to import (translation column empty)')
        return
    rom = bytearray(open(a.rom, 'rb').read())
    nfit = napp = 0
    miss_total = 0
    for eid, trs in sorted(todo.items()):
        d = g.load_entry(bytes(rom), eid)
        if d is None:
            print(f'e{eid:04d}: missing'); continue
        nd, err, too_long = rebuild_entry(d, trs, rev, kind)
        if nd is None:
            print(f'e{eid:04d}: {err}'); continue
        if too_long:
            print(f'e{eid:04d}: {len(too_long)} strings skipped, e.g. {too_long[:2]}')
        enc = lz77.compress(nd)
        off, size = struct.unpack_from('<2I', rom, g.FAT + eid * 8)
        if len(enc) <= size:
            rom[g.BASE + off:g.BASE + off + len(enc)] = enc
            struct.pack_into('<I', rom, g.FAT + eid * 8 + 4, len(enc))
            nfit += 1
        else:
            new_off = len(rom) - g.BASE
            rom += enc
            struct.pack_into('<II', rom, g.FAT + eid * 8, new_off, len(enc))
            napp += 1
    print(f'entries patched in place: {nfit}; appended: {napp}; unmapped chars: {miss_total}')
    if a.report_only:
        return
    if not a.out:
        raise SystemExit('--out is required unless --report-only')
    open(a.out, 'wb').write(rom)
    print(f'wrote {a.out} ({len(rom)} bytes)')


if __name__ == '__main__':
    main()
