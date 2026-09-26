#!/usr/bin/env python3
"""List script runs that the game can display but the master never captured.

`extract_pool.scan_pools` keeps its retry-based scanner honest with a grouping
rule: a batch of accepted runs is emitted only when it has >= 3 runs and >= 20
characters.  Short groups therefore vanish - e.g. entry e1 holds

    0x1835  」ああ。大丈夫だ“““『        <- displayed line, never exported
    0x184f  」“““““『                   <- too short, breaks the group

and the game happily shows text we never translated.

A run that really is a string starts immediately after a 0x0000 terminator;
the scanner's garbage comes from restarting mid-run (`i = start + 2`), so
requiring "preceded by NUL" separates the two cleanly.

Usage:
  find_missing_runs.py [--out work/missing_runs.tsv] [--min-len 2]
"""
import argparse
import bisect
import csv
import os
import re
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import gbtext as g          # noqa: E402
import mapio                # noqa: E402

HIRA = re.compile(r'[\u3040-\u309f]')
KANA = re.compile(r'[\u3040-\u30ff]')
# entries that hold no displayable text (font tables, dictionaries, bitmaps)
SKIP = {850, 851, 852, 605, 547, 571, 611, 636, 1489}


def load_master(path):
    """Return (intervals per entry, texts per entry)."""
    cat = {}
    for r in csv.DictReader(open(os.path.join(ROOT, 'data', 'entry_catalog.tsv'),
                                 encoding='utf-8'), delimiter='\t'):
        if r.get('base'):
            cat[int(r['eid'])] = int(r['base'])
    spans, texts = {}, {}
    for r in csv.reader(open(path, encoding='utf-8'), delimiter='\t'):
        if not r or r[0] == 'entry':
            continue
        eid, off, n = int(r[0]), int(r[2]), int(r[3]) + 1
        base = cat.get(eid, 0)
        start = off if (len(r) > 7 and r[7] == 'pool') else base + off
        spans.setdefault(eid, []).append((start, start + 2 * n))
        texts.setdefault(eid, set()).add(r[5])
    for eid in spans:
        spans[eid].sort()
    return spans, texts


def primary_runs(d, dec, minlen):
    out = []
    for par in (0, 1):
        i = par
        while i + 1 < len(d):
            if struct.unpack_from('<H', d, i)[0] == 0:
                i += 2
                continue
            prev = struct.unpack_from('<H', d, i - 2)[0] if i >= 2 else 0
            start = i
            buf = []
            while i + 1 < len(d):
                c = struct.unpack_from('<H', d, i)[0]
                if c == 0:
                    break
                ch = dec.get(c - 1 if c >= 0x20 else c)
                if ch is None:
                    break
                buf.append(ch)
                i += 2
            txt = ''.join(buf)
            if (prev == 0 and i + 1 < len(d) and struct.unpack_from('<H', d, i)[0] == 0
                    and len(buf) >= minlen and HIRA.search(txt)
                    and not re.search(r'[X6PCN]', txt)):
                out.append((start, txt, len(buf)))
            i = start + 2
    return out


def covered(spans, off):
    for s, e in spans:
        if s <= off < e:
            return True
        if s > off:
            break
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=os.path.join(ROOT, 'work', 'missing_runs.tsv'))
    ap.add_argument('--min-len', type=int, default=2)
    ap.add_argument('--rom', default=g.JP_ROM)
    a = ap.parse_args()

    dec = mapio.load_map(os.path.join(ROOT, 'data', 'glyph_map.csv'))
    rom = open(a.rom, 'rb').read()
    spans, texts = load_master(os.path.join(ROOT, 'data', 'translation.tsv'))

    rows, per_entry = [], {}
    for eid in range(2000):
        if eid in SKIP:
            continue
        d = g.load_entry(rom, eid)
        if not d or len(d) > 0x40000:
            continue
        seen = texts.get(eid, set())
        sp = spans.get(eid, [])
        for off, txt, n in primary_runs(d, dec, a.min_len):
            if txt in seen:
                continue
            if covered(sp, off):
                continue
            rows.append([str(eid), str(off), str(n), txt])
            per_entry[eid] = per_entry.get(eid, 0) + 1

    rows.sort(key=lambda r: (int(r[0]), int(r[1])))
    with open(a.out, 'w', encoding='utf-8', newline='') as f:
        w = csv.writer(f, delimiter='\t', lineterminator='\n')
        w.writerow(['entry', 'offset', 'n_codes', 'jp_text'])
        w.writerows(rows)
    print(f'{len(rows)} missing runs in {len(per_entry)} entries -> {a.out}')
    chars = sum(len(r[3]) for r in rows)
    print(f'total chars: {chars}')
    for eid in sorted(per_entry, key=lambda e: -per_entry[e])[:15]:
        eg = [r[3] for r in rows if int(r[0]) == eid][:3]
        print(f'  e{eid}: {per_entry[eid]:4d} runs, e.g. {eg}')


if __name__ == '__main__':
    main()
