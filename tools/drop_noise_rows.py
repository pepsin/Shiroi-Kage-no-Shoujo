#!/usr/bin/env python3
"""Drop pool rows that are really table/bitmap bytes, not text.

The "primary run" scan (find_missing_runs / add_missing_rows) accepts any
NUL-terminated run whose neighbours decode to glyphs, which still lets a few
runs out of *data tables* through: their code words climb by a constant step
(0x99, 0xA4, 0xAF, ... -> `で1`, `で氏`, `盛挙`, ...).  Writing a translation
into such a slot corrupts the table, so they are removed again.

Two signatures are used, both only for rows that have no known translation:
  * hiragana mixed with ASCII digits/letters  (`で1`, `え8`)
  * a run of equally-spaced code words around the candidate (table data)

Usage:
  drop_noise_rows.py --dry-run
  drop_noise_rows.py
"""
import argparse
import csv
import os
import re
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import gbtext as g          # noqa: E402

HIRA = re.compile(r'[\u3040-\u309f]')
KANA = re.compile(r'[\u3040-\u30ff]')
ASCII = re.compile(r'[A-Za-z0-9]')


def const_step(seq, need):
    """True when `seq` is dominated by one small constant difference."""
    diffs = [seq[i + 1] - seq[i] for i in range(len(seq) - 1)]
    if not diffs:
        return False
    best = max(set(diffs), key=diffs.count)
    return diffs.count(best) >= need and 0 < abs(best) < 0x40


def table_like(d, off, n, span=8, need=5):
    """True when the words around `off` look like a data table.

    Two shapes show up: a plain run with a constant step (0x99, 0xA4, 0xAF...)
    and 4-byte records whose first/second word climbs by a constant
    (`□面 ～曖 』...` = {0x0A,0x60D}, {0x14,0x69B}, {0x1E,0x78F}).
    """
    lo = max(0, off - 2 * span)
    hi = min(len(d) - 1, off + 2 * (n + span))
    for stride in (2, 4):
        for base in (0, 2):
            seq = [struct.unpack_from('<H', d, a)[0]
                   for a in range(lo + base, hi, stride)
                   if a + 1 < len(d)]
            if const_step(seq, need):
                return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--master', default=os.path.join(ROOT, 'data', 'translation.tsv'))
    ap.add_argument('--rom', default=g.JP_ROM)
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()

    rom = open(a.rom, 'rb').read()
    raw = open(a.master, 'rb').read().decode('utf-8')
    lines = raw.split('\r\n')
    while lines and lines[-1] == '':
        lines.pop()
    head, body = lines[0], lines[1:]

    # texts that have a real translation somewhere in the master
    known = set()
    for l in body:
        p = l.split('\t')
        if len(p) > 6 and p[6].strip():
            known.add(p[5])

    cache = {}
    drop = []
    for l in body:
        p = l.split('\t')
        if len(p) < 8 or p[7] != 'pool':
            continue
        jp = p[5]
        if jp in known:
            continue                     # has a real translation: keep
        eid, off, n = int(p[0]), int(p[2]), int(p[3])
        # Text-shape signatures validated against the rows that were already
        # in the master (which are known to be displayed): these match almost
        # none of them, while catching the table fragments.
        bad = None
        if len(jp) <= 4 and KANA.search(jp) and ASCII.search(jp):
            bad = 'kana+ascii'
        elif len(jp) <= 3 and re.search(r'[□～‥]', jp) and '“' not in jp:
            bad = 'filler mark'
        elif re.fullmatch(r'.{1,2}‥', jp):
            bad = 'edge dots'
        elif re.fullmatch(r'[\u3040-\u309f][\u4e00-\u9fff]', jp):
            bad = 'kana+kanji pair'
        elif len(jp) <= 4:
            if eid not in cache:
                cache[eid] = g.load_entry(rom, eid)
            d = cache[eid]
            if d and 0 <= off < len(d) - 2 and table_like(d, off, n):
                bad = 'data table' 
        if bad:
            drop.append((l, bad))

    n_by = {}
    for _, why in drop:
        n_by[why] = n_by.get(why, 0) + 1
    print(f'rows to drop: {len(drop)}  {n_by}')
    seen = set()
    for l, why in drop:
        p = l.split('\t')
        if p[5] in seen:
            continue
        seen.add(p[5])
        if len(seen) > 30:
            break
        print(f'   e{p[0]}@{p[2]} {p[5]!r}  [{why}]')
    if a.dry_run:
        return
    drop_lines = {l for l, _ in drop}
    keep = [l for l in body if l not in drop_lines]
    out = ('\r\n'.join([head] + keep) + '\r\n').encode('utf-8')
    open(a.master, 'wb').write(out)
    print(f'{a.master}: {len(body)} -> {len(keep)} rows '
          f'(CRLF {raw.count(chr(13) + chr(10))} -> {out.count(b(chr(13) + chr(10))) if False else out.count(b"\\r\\n")})')


if __name__ == '__main__':
    main()
