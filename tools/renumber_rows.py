#!/usr/bin/env python3
"""Renumber rows so that (entry, idx) is unique.

Pool rows used to be numbered from 0 for every extracted run, so an entry that
got rows from two extraction passes ended up with duplicated idx values (and
therefore duplicated row ids).  This rebuilds the idx column:

  * offset-table rows keep their idx (they are referenced by the bytecode),
  * pool rows are renumbered from the first free idx, ordered by offset.

Usage:  renumber_rows.py [--master data/translation.tsv] [--dry-run]
"""
import argparse
import collections
import csv
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--master', default=os.path.join(ROOT, 'data', 'translation.tsv'))
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    lines = open(a.master, encoding='utf-8', newline='').read().split('\n')
    if lines and lines[-1] == '':
        lines.pop()
    crlf = lines[1].endswith('\r') if len(lines) > 1 else False
    hdr, data = lines[0], lines[1:]
    parsed = []
    for ln in data:
        f = ln.rstrip('\r').split('\t')
        parsed.append(f)
    byent = collections.defaultdict(list)
    for i, f in enumerate(parsed):
        byent[f[0]].append(i)
    changed = 0
    dup_before = 0
    for eid, idxs in byent.items():
        table = [i for i in idxs if not (len(parsed[i]) > 7 and parsed[i][7] == 'pool')]
        pool = [i for i in idxs if len(parsed[i]) > 7 and parsed[i][7] == 'pool']
        seen = collections.Counter(parsed[i][1] for i in idxs)
        dup_before += sum(v - 1 for v in seen.values() if v > 1)
        if not pool:
            continue
        used = {int(parsed[i][1]) for i in table}
        nxt = (max(used) + 1) if used else 0
        for i in sorted(pool, key=lambda k: int(parsed[k][2])):      # by offset
            if int(parsed[i][1]) != nxt:
                parsed[i][1] = str(nxt)
                changed += 1
            nxt += 1
    print(f'renumbered {changed} pool rows; duplicate ids before: {dup_before}')
    if a.dry_run:
        return
    out = [hdr] + ['\t'.join(f) + ('\r' if crlf else '') for f in parsed]
    open(a.master, 'w', encoding='utf-8', newline='').write('\n'.join(out) + '\n')
    print(f'wrote {a.master}')


if __name__ == '__main__':
    main()
