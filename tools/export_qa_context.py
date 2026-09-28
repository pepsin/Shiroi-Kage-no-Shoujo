#!/usr/bin/env python3
"""Turn a qa_translation.py worklist into review blocks with context.

For every flagged row this emits the row plus two lines of context on each side
(so the reviewer can see the sentence the line belongs to), merged so a run of
flagged lines becomes one block.  Everything is keyed by (entry, index) so the
answer can be fed straight back to apply_cn_patch.py.

Usage:
  export_qa_context.py --worklist work/qa.tsv --out work/batch1.tsv --from 0 --count 60
  export_qa_context.py --worklist work/qa.tsv --entry 3 --out work/batch_e3.tsv

Output columns: entry, idx, flag, budget, JP, CN   (flag is FLAG or ctx)
"""
import argparse
import collections
import csv
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTEXT = 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--worklist', required=True)
    ap.add_argument('--table', default=os.path.join(ROOT, 'data', 'translation.tsv'))
    ap.add_argument('--out', required=True)
    ap.add_argument('--from', dest='start', type=int, default=0)
    ap.add_argument('--count', type=int, default=60)
    ap.add_argument('--entry')
    ap.add_argument('--context', type=int, default=CONTEXT)
    a = ap.parse_args()

    table = {}
    for r in csv.reader(open(a.table, encoding='utf-8', newline=''), delimiter='\t'):
        if len(r) >= 7 and r[0].isdigit():
            table[(r[0], int(r[1]))] = r

    flags = collections.OrderedDict()
    for r in csv.reader(open(a.worklist, encoding='utf-8', newline=''), delimiter='\t'):
        if len(r) < 6:
            continue
        if a.entry and r[0] != a.entry:
            continue
        flags[(r[0], int(r[1]))] = r[2]

    keys = list(flags.keys())
    chosen = keys[a.start:a.start + a.count]
    if not chosen:
        print(f'no flags in that slice (total {len(keys)})')
        return 1

    rows = []
    used = set()
    for key in chosen:
        entry = key[0]
        lo = key[1] - a.context
        hi = key[1] + a.context
        for i in range(lo, hi + 1):
            k = (entry, i)
            if k in used or k not in table:
                continue
            used.add(k)
            t = table[k]
            mark = flags.get(k, 'ctx')
            rows.append((entry, i, mark, t[3], t[5], t[6]))
    rows.sort(key=lambda r: (r[0], r[1]))

    os.makedirs(os.path.dirname(a.out) or '.', exist_ok=True)
    with open(a.out, 'w', encoding='utf-8', newline='') as f:
        for e, i, m, b, jp, cn in rows:
            f.write(f'{e}\t{i}\t{m}\t{b}\t{jp}\t{cn}\n')
    nflag = sum(1 for r in rows if r[2] != 'ctx')
    print(f'{a.out}: {len(rows)} lines ({nflag} flagged) from {len(chosen)} flagged rows '
          f'[{a.start}..{a.start + len(chosen)}) of {len(keys)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
