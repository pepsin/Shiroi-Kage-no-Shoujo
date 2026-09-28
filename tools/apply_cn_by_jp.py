#!/usr/bin/env python3
"""Apply a Japanese -> Chinese mapping to every row that uses that Japanese line.

The game reuses the same display line in many script pools (101,795 table rows are
only 32,163 distinct Japanese lines, and 74,800 rows are repeats), so fixing a line
once and propagating it keeps both the effort and the wording consistent.

Input is a TSV with either two or three columns:

    <japanese><TAB><new chinese>                 # every row using that line
    <japanese><TAB><old chinese><TAB><new chinese>   # only rows still saying that

Rows whose Japanese matches and whose code budget is large enough are rewritten;
rows that would not fit are reported instead of being silently skipped.

Usage:
  apply_cn_by_jp.py mapping.tsv [--table data/translation.tsv] [--dry-run]
"""
import argparse
import csv
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('mapping')
    ap.add_argument('--table', default=os.path.join(ROOT, 'data', 'translation.tsv'))
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()

    mapping = {}
    for n, line in enumerate(open(a.mapping, encoding='utf-8'), 1):
        line = line.rstrip('\n').rstrip('\r')
        if not line or line.startswith('#'):
            continue
        parts = line.split('\t')
        if len(parts) == 2:
            mapping[(parts[0], None)] = parts[1]
        elif len(parts) == 3:
            mapping[(parts[0], parts[1])] = parts[2]
        else:
            raise SystemExit(f'{a.mapping}:{n}: expected 2 or 3 fields')

    with open(a.table, encoding='utf-8', newline='') as f:
        lines = f.read().split('\n')
    if lines and lines[-1] == '':
        lines.pop()

    changed = 0
    skipped = []
    touched_jp = set()
    for i, line in enumerate(lines):
        r = line.split('\t')
        if len(r) < 7 or not r[0].isdigit():
            continue
        new = mapping.get((r[5], r[6]))
        if new is None:
            new = mapping.get((r[5], None))
        if new is None or new == r[6]:
            continue
        if len(new) > int(r[3]):
            skipped.append((r[0], r[1], r[5], new, r[3]))
            continue
        r[6] = new
        lines[i] = '\t'.join(r)
        changed += 1
        touched_jp.add(r[5])

    unused = [k for k in mapping if k[0] not in touched_jp]
    print(f'mapping lines: {len(mapping)}, table rows changed: {changed}, '
          f'distinct lines matched: {len(touched_jp)}')
    if unused:
        print(f'  {len(unused)} mapping line(s) matched nothing, e.g. {unused[:3]}')
    if skipped:
        print(f'  {len(skipped)} row(s) too long for their slot:')
        for e, i, jp, cn, b in skipped[:10]:
            print(f'    e{e}[{i}] budget {b}: {cn} (for {jp})')
    if a.dry_run or not changed:
        return 1 if skipped else 0

    shutil.copyfile(a.table, a.table + '.bak')
    # Line based rewrite: a csv round trip would escape the \xXXXX control codes.
    with open(a.table, 'w', encoding='utf-8', newline='') as f:
        f.write('\n'.join(lines) + '\n')
    print(f'wrote {a.table} (backup at {a.table}.bak)')
    return 1 if skipped else 0


if __name__ == '__main__':
    sys.exit(main())
