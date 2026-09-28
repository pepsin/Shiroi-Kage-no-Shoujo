#!/usr/bin/env python3
"""Turn (entry, idx, new chinese) patches into a JP+old-CN -> new-CN mapping.

Reused script lines are the norm here (101,795 rows are only 32,163 distinct
Japanese lines), so one reviewed fix should repair every place that line appears
with the same current wording.  Keying on the *pair* (Japanese, old Chinese) keeps
that safe: rows whose existing Chinese already differs are left alone instead of
being overwritten with wording meant for another context.

Usage:
  cn_patch_to_jp_map.py patch1.tsv [patch2.tsv ...] --out work/map.tsv
"""
import argparse
import csv
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('patches', nargs='+')
    ap.add_argument('--table', default=os.path.join(ROOT, 'data', 'translation.tsv'))
    ap.add_argument('--out', required=True)
    a = ap.parse_args()

    table = {}
    counts = {}
    for r in csv.reader(open(a.table, encoding='utf-8', newline=''), delimiter='\t'):
        if len(r) >= 7 and r[0].isdigit():
            table[(r[0], r[1])] = r
            counts[(r[5], r[6])] = counts.get((r[5], r[6]), 0) + 1

    mapping = {}
    conflicts = []
    for path in a.patches:
        for n, line in enumerate(open(path, encoding='utf-8'), 1):
            line = line.rstrip('\n').rstrip('\r')
            if not line or line.startswith('#'):
                continue
            parts = line.split('\t')
            if len(parts) != 3:
                print(f'  skip {os.path.basename(path)}:{n}: {len(parts)} fields')
                continue
            row = table.get((parts[0], parts[1]))
            if row is None:
                print(f'  skip {os.path.basename(path)}:{n}: unknown row')
                continue
            key = (row[5], row[6])
            if key in mapping and mapping[key] != parts[2]:
                conflicts.append((key, mapping[key], parts[2]))
                continue
            mapping[key] = parts[2]

    with open(a.out, 'w', encoding='utf-8', newline='') as f:
        for (jp, old), new in mapping.items():
            f.write(f'{jp}\t{old}\t{new}\n')
    total_rows = sum(counts.get(k, 0) for k in mapping)
    print(f'mapping entries: {len(mapping)} (covering {total_rows} table rows) -> {a.out}')
    for (jp, old), first, second in conflicts[:10]:
        print(f'  CONFLICT {jp!r}: {first!r} vs {second!r}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
