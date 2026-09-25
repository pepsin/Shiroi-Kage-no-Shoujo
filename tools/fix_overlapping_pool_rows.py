#!/usr/bin/env python3
"""Remove pool rows that merely duplicate a suffix of an offset-table row.

`extract_pool.scan_pools` rejects any run containing one of `[X6PCN]` (a
heuristic against bitmap noise).  Legitimate lines that contain a digit or a
Latin letter therefore fail the filter, and the scanner's retry step
(`i = start + 2`) restarts one code later - which can accept the *suffix* of the
same physical string.  The result is a second master row for the same bytes:

    e37 idx 101 (table) value 15386 + base 1952 = 17338
        jp '」26分“““あ～。ダメダメ'      -> written first
    e37 idx 636 (pool)  absolute 17344 (= 17338 + 3 codes)
        jp   '分“““あ～。ダメダメ'       -> written second, clobbers the tail

Both rows get translated and both get written, so the finished ROM shows text
that differs from the master (verify_rom reports it as a round-trip failure).

This tool drops the shorter, contained pool row (the table row is the complete
string) and reports any overlap it does not recognise.

Usage:
  fix_overlapping_pool_rows.py [--dry-run]
"""
import argparse
import csv
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import gbtext as g          # noqa: E402

MASTER = os.path.join(ROOT, 'data', 'translation.tsv')
CATALOG = os.path.join(ROOT, 'data', 'entry_catalog.tsv')


def load_catalog():
    cat = {}
    for r in csv.DictReader(open(CATALOG, encoding='utf-8'), delimiter='\t'):
        if r.get('base'):
            cat[int(r['eid'])] = (int(r['table_off']), int(r['base']))
    return cat


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--master', default=MASTER)
    a = ap.parse_args()

    cat = load_catalog()
    rom = open(g.JP_ROM, 'rb').read()
    raw = open(a.master, 'rb').read().decode('utf-8')
    crlf = raw.count('\r\n')
    lines = raw.split('\r\n')
    if lines and lines[-1] == '':
        lines.pop()
    head, body = lines[0], lines[1:]

    table = {}          # entry -> [(abs_start, abs_end, jp)]
    pool = []           # (entry, offset, n_codes, jp)
    for line in body:
        p = line.split('\t')
        eid, off, n = int(p[0]), int(p[2]), int(p[3])
        jp = p[5]
        if len(p) > 7 and p[7] == 'pool':
            pool.append((eid, off, n, jp, line))
        else:
            base = cat.get(eid, (0, 0))[1]
            start = base + off
            table.setdefault(eid, []).append((start, start + 2 * (n + 1), jp))

    drop = set()
    review = []
    for eid, off, n, jp, line in pool:
        end = off + 2 * (n + 1)
        for ts, te, tjp in table.get(eid, ()):
            if off < te and ts < end:
                if ts <= off and end <= te and tjp.endswith(jp):
                    drop.add(id(line))
                else:
                    review.append((eid, ts, tjp, off, jp))
                break

    keep = [l for l in body if id(l) not in drop]
    print(f'pool rows: {len(pool)}; dropped as table-suffix duplicates: {len(drop)}; '
          f'unrecognised overlaps: {len(review)}')
    for r in review[:20]:
        print(f'  REVIEW e{r[0]} table@{r[1]} {r[2]!r} vs pool@{r[3]} {r[4]!r}')

    if a.dry_run:
        return
    out = ('\r\n'.join([head] + keep) + '\r\n').encode('utf-8')
    open(a.master, 'wb').write(out)
    chk = open(a.master, 'rb').read()
    print(f'wrote {a.master}: {len(body)} -> {len(keep)} rows '
          f'(CRLF {crlf} -> {chk.count(chr(13).encode() + chr(10).encode())})')


if __name__ == '__main__':
    main()
