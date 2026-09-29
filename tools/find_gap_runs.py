#!/usr/bin/env python3
"""Find display lines the master missed inside small storage gaps.

The string-pool scanner (``extract_pool.scan_pools``) only emits a batch of
accepted runs when the batch has >= 3 runs and >= 20 characters, so short lines
disappear from the master.  The game still displays them - in Japanese - which
is why the player can meet a stray 「フフフ」 in the middle of a Chinese scene.

This tool looks exactly where that happens: between two consecutive rows the
master *did* export, in a gap no larger than ``--max-gap`` bytes.  A run counts
when it

* starts right after a 0x0000 terminator (the scanner's garbage comes from
  restarting mid-run),
* decodes cleanly to glyphs and ends with another 0x0000, and
* is not covered by any row of the master for that entry.

``find_missing_runs.py`` covers part of the same ground but requires hiragana,
so pure katakana / symbol lines (フフフ, ハハハ, ピンポ～ン, キ～ホルダ～,
ライタ～) never appear there.  Candidates produced here are fed to
``insert_pool_rows.py``, which verifies each one against the Japanese ROM
before writing anything.

Usage
-----
  python3 tools/find_gap_runs.py [--out work/missing_gap_runs.tsv] [--max-gap 64]
"""
import argparse
import collections
import csv
import os
import re
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import gbtext as g          # noqa: E402
import mapio                # noqa: E402

KANA = re.compile(r'[\u3040-\u30ff]')

CATALOG = os.path.join(ROOT, 'data', 'entry_catalog.tsv')
MASTER = os.path.join(ROOT, 'data', 'translation.tsv')
DEFAULT_OUT = os.path.join(ROOT, 'work', 'missing_gap_runs.tsv')


def load_catalog():
    cat = {}
    for r in csv.DictReader(open(CATALOG, encoding='utf-8'), delimiter='\t'):
        if r.get('base'):
            cat[int(r['eid'])] = int(r['base'])
    return cat


def load_spans(cat):
    spans, seen = collections.defaultdict(list), collections.defaultdict(set)
    for r in csv.reader(open(MASTER, encoding='utf-8'), delimiter='\t'):
        if not r or r[0] == 'entry':
            continue
        eid, off, n = int(r[0]), int(r[2]), int(r[3]) + 1
        start = off if (len(r) > 7 and r[7] == 'pool') else cat.get(eid, 0) + off
        spans[eid].append((start, start + 2 * n))
        seen[eid].add(r[5])
    for eid in spans:
        spans[eid].sort()
    return spans, seen


def run_at(d, dec, start):
    """Decode the NUL-terminated run at ``start`` (must follow a NUL)."""
    if start < 2 or start + 1 >= len(d):
        return None
    if struct.unpack_from('<H', d, start - 2)[0] != 0:
        return None
    buf, i = [], start
    while i + 1 < len(d):
        c = struct.unpack_from('<H', d, i)[0]
        if c == 0:
            break
        ch = dec.get(c - 1 if c >= 0x20 else c)
        if ch is None:
            return None
        buf.append(ch)
        i += 2
    if i + 1 >= len(d) or struct.unpack_from('<H', d, i)[0] != 0:
        return None
    return ''.join(buf), len(buf), i


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=DEFAULT_OUT)
    ap.add_argument('--rom', default=g.JP_ROM)
    ap.add_argument('--max-gap', type=int, default=64)
    ap.add_argument('--keep-data', action='store_true',
                    help='keep runs without kana (digits / kanji-only). Those '
                         'display the same in Chinese or are data tables, so '
                         'they are skipped by default')
    a = ap.parse_args()

    dec = mapio.load_map(os.path.join(ROOT, 'data', 'glyph_map.csv'))
    rom = open(a.rom, 'rb').read()
    cat = load_catalog()
    spans, seen = load_spans(cat)

    rows = []
    for eid in sorted(spans):
        d = g.load_entry(rom, eid)
        if not d or len(d) > 0x40000:
            continue
        sp = spans[eid]
        bounds = [(0, sp[0][0])] + [(sp[i][1], sp[i + 1][0])
                                    for i in range(len(sp) - 1)]
        for lo, hi in bounds:
            if hi - lo <= 1 or hi - lo > a.max_gap:
                continue
            for par in (0, 1):
                off = lo + par
                while off < hi:
                    r = run_at(d, dec, off)
                    if not r:
                        off += 2
                        continue
                    txt, n, end = r
                    if (txt and txt not in seen.get(eid, set())
                            and (a.keep_data or KANA.search(txt))):
                        rows.append([str(eid), f'0x{off:X}', str(n), txt])
                    off = end + 2

    with open(a.out, 'w', encoding='utf-8', newline='') as f:
        w = csv.writer(f, delimiter='\t', lineterminator='\n')
        w.writerow(['entry', 'offset', 'n_codes', 'jp_text'])
        w.writerows(rows)
    print(f'{len(rows)} candidate runs in '
          f'{len(set(r[0] for r in rows))} entries -> {a.out}')
    for t, n in collections.Counter(r[3] for r in rows).most_common(10):
        print(f'  {n:>4}  {t}')


if __name__ == '__main__':
    main()
