#!/usr/bin/env python3
"""Assemble the JP glyph map from trans_jp_*.txt and audit it.

The JP ROM's font table (file 0x66F440) is a *different* table from the CN
sheet table (file 0x800000): only 218 of 1704 glyphs are byte-identical, so the
CN map cannot be reused for JP kanji.

Outputs data/glyph_map.json plus a coverage/quality report.

Usage: build_jp_map.py [--check]
"""
import argparse
import collections
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEXTDIR = os.path.join(ROOT, 'data')
OUT = os.path.join(TEXTDIR, 'glyph_map.json')
KANA = set('ぁあぃいぅうぇえぉおかがきぎくぐけげこごさざしじすずせぜそぞただちぢっつづてでとど'
           'なにぬねのはばぱひびぴふぶぷへべぺほぼぽまみむめもゃやゅゆょよらりるれろわゐゑをんーっ'
           'ァアィイゥウェエォオカガキギクグケゲコゴサザシジスズセゼソゾタダチヂッツヅテデトド'
           'ナニヌネノハバパヒビピフブプヘベペホボポマミムメモャヤュユョヨラリルレロワヰヱヲン')


def load_trans():
    m = {}
    files = sorted(f for f in os.listdir(TEXTDIR) if re.fullmatch(r'trans_jp_\d\d\.txt', f))
    for fn in files:
        for line in open(os.path.join(TEXTDIR, fn), encoding='utf-8'):
            line = line.rstrip('\n')
            if not line or '\t' not in line:
                continue
            code_s, ch = line.split('\t', 1)
            try:
                code = int(code_s.strip(), 16)
            except ValueError:
                continue
            ch = ch.strip()
            if ch:
                m.setdefault(code, ch)
    return m, files


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    a = ap.parse_args()
    m, files = load_trans()
    print(f'trans_jp files found: {len(files)} -> {", ".join(f[8:10] for f in files)}')
    print(f'entries collected: {len(m)}')
    if m:
        print(f'code range: {min(m):#05x}..{max(m):#05x}')
    # completeness per 100-code page
    missing_pages = [p for p in range(18)
                     if not any(p * 100 <= c < p * 100 + 100 for c in m)]
    if missing_pages:
        print(f'pages with no data: {missing_pages}')
    # duplicate character audit (same char at several codes is normal, but flag it)
    dup = collections.defaultdict(list)
    for c, ch in m.items():
        dup[ch].append(c)
    many = {ch: v for ch, v in dup.items() if len(v) >= 6 and ch != '■'}
    print(f'characters assigned to >=6 codes (suspicious): {len(many)}')
    for ch, v in list(many.items())[:8]:
        print(f'   {ch}: {[hex(x) for x in v]}')
    if a.check:
        return
    json.dump({'map': {f'{k:03X}': v for k, v in sorted(m.items())},
               'note': 'JP font table at 0x66F440; text codes index it directly (shift 0)'},
              open(OUT, 'w', encoding='utf-8'), ensure_ascii=False, indent=0)
    print(f'wrote {OUT}')


if __name__ == '__main__':
    main()
