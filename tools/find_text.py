#!/usr/bin/env python3
"""Locate a Japanese string inside the ROM (entry + offset).

Given the text the player sees on screen (read off a photo, or dumped from a
savestate), this finds where it lives so the entry can be extracted and
translated.  It searches every FAT entry after decompression, at both byte
parities, plus the raw ROM.

Usage:
  find_text.py "彼女は御苑洋子"
  find_text.py --file missing.txt        # one candidate per line
"""
import argparse
import os
import re
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import gbtext as g
import mapio


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('text', nargs='*')
    ap.add_argument('--file')
    ap.add_argument('--master', default=os.path.join(ROOT, 'data', 'translation.tsv'))
    a = ap.parse_args()
    gm = mapio.load_map(os.path.join(ROOT, 'data', 'glyph_map.csv'))
    idx = {}
    for i, ch in sorted(gm.items(), reverse=True):
        if ch:
            idx.setdefault(ch, i)

    def enc(s):
        out = []
        for ch in s:
            i = idx.get(ch)
            if i is None:
                return None
            out.append(i if i < 0x20 else i + 1)
        return b''.join(struct.pack('<H', c) for c in out)

    import csv
    known = {}
    for r in csv.reader(open(a.master, encoding='utf-8'), delimiter='\t'):
        if len(r) >= 6:
            known.setdefault(r[0], set()).add(r[5])

    rom = open(g.JP_ROM, 'rb').read()
    entries = {}
    for eid in range(2000):
        d = g.load_entry(rom, eid)
        if d and len(d) <= 0x40000:
            entries[eid] = d
    targets = list(a.text) if a.text else [l.strip() for l in open(a.file, encoding='utf-8')
                                           if l.strip()]
    for t in targets:
        pb = enc(t)
        print(f'=== {t!r}')
        if pb is None:
            missing = [c for c in t if c not in idx]
            print(f'   无法编码（字库无这些字）: {missing}')
            continue
        hits = []
        for eid, d in entries.items():
            for par in (0, 1):
                pos = d.find(pb, par)
                while pos != -1 and pos % 2 == par:
                    hits.append((eid, pos, par))
                    pos = d.find(pb, pos + 2)
        raw = [m.start() for m in re.finditer(re.escape(pb), rom)][:5]
        if not hits:
            print('   ROM 里没有（可能是压缩条目的片段、或被拆成多串）')
            if raw:
                print(f'   裸数据命中: {[hex(x) for x in raw]}')
            continue
        for eid, pos, par in hits[:6]:
            intab = '串池' if t in known.get(str(eid), set()) else '★未收录'
            print(f'   e{eid:<5} @0x{pos:X} (parity {par})  {intab}')


if __name__ == '__main__':
    main()
