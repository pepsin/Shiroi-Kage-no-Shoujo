#!/usr/bin/env python3
"""Plan the glyph-table work for a (partially) translated script.

Reads data/translation.tsv, encodes every filled translation with the ROM's
code rule, and reports:

  * how many distinct characters the translation needs,
  * which of them the JP glyph table already has,
  * which codes/indices the translated script no longer references
    (those slots become reusable),
  * whether the current 1704-glyph table is enough or must be relocated
    and extended (docs/汉化方案设计.md, route B).

Usage:
  glyph_plan.py [--master data/translation.tsv] [--top 60]
"""
import argparse
import collections
import csv
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

GLYPH_COUNT = 0x6A8
KANA = set()
for a, b in ((0x3041, 0x3096), (0x30A1, 0x30FA), (0x30FC, 0x30FC)):
    KANA |= {chr(c) for c in range(a, b + 1)}


def load_map():
    import mapio
    return mapio.load_map(os.path.join(ROOT, 'data', 'glyph_map.csv'))


def reverse_map(m, kind='jp'):
    rev = {}
    for idx, ch in sorted(m.items(), reverse=True):
        code = idx if (kind != 'jp' or idx < 0x20) else idx + 1
        if code >= 0 and ch:
            rev[ch] = code
    return rev


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--master', default=os.path.join(ROOT, 'data', 'translation.tsv'))
    ap.add_argument('--top', type=int, default=60)
    a = ap.parse_args()
    m = load_map()
    rev = reverse_map(m)
    have = set(m.values()) - {''}
    used_chars = collections.Counter()
    used_indices = set()
    rows = list(csv.DictReader(open(a.master, encoding='utf-8'), delimiter='\t'))
    filled = 0
    unknown = collections.Counter()
    for r in rows:
        cn = (r.get('translation') or '').strip()
        if not cn:
            continue
        filled += 1
        for ch in cn:
            used_chars[ch] += 1
            code = rev.get(ch)
            if code is None:
                unknown[ch] += 1
                continue
            used_indices.add(code if code < 0x20 else code + 1)
    missing = {c for c in used_chars if c not in have}
    # indices the translated script never refers to -> reusable slots
    free = [i for i in range(0x00, GLYPH_COUNT) if i not in used_indices]
    free_new = [i for i in free if i >= 0x20]           # addressable by a code
    print(f'rows translated        : {filled} / {len(rows)}')
    print(f'distinct chars used    : {len(used_chars)}')
    print(f'  already in JP table  : {len(used_chars) - len(missing)}')
    print(f'  need a new glyph     : {len(missing)}')
    print(f'JP table capacity      : {GLYPH_COUNT}')
    print(f'codes still referenced : {len(used_indices)}')
    print(f'slots now unused       : {len(free)} (of which code-addressable: {len(free_new)})')
    need_extend = len(missing) > len(free_new)
    print(f'=> table relocation    : {"REQUIRED" if need_extend else "not needed yet"}'
          f' (deficit {max(0, len(missing) - len(free_new))})')
    print(f'missing glyphs by freq :')
    print('  ' + ' '.join(f'{c}{unknown[c] if c in unknown else ""}'
                          for c, _ in sorted(unknown.items(), key=lambda kv: -kv[1])[:a.top]))
    out = os.path.join(ROOT, 'work', 'tm', 'missing_glyphs.txt')
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, 'w', encoding='utf-8') as f:
        f.write(''.join(c for c, _ in sorted(unknown.items(), key=lambda kv: -kv[1])))
    print(f'wrote {out}')


if __name__ == '__main__':
    main()
