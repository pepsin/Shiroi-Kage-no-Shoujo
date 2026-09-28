#!/usr/bin/env python3
"""Apply a hand-written Chinese patch to data/translation.tsv.

The translation table is line-per-display-line, so a patch file lists exactly
the rows to replace:

    <entry>\\t<index>\\t<new chinese>

Rows that are already fine are simply left out.  The tool reports length changes
so a rewrite that no longer fits the original line can be spotted (the line
budget is the Japanese line's own character count - see AGENT.md).

Usage:
  apply_cn_patch.py patch.tsv [--table data/translation.tsv] [--dry-run]
"""
import argparse
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_TABLE = os.path.join(ROOT, 'data', 'translation.tsv')


def read_patch(path):
    out = []
    with open(path, encoding='utf-8') as f:
        for n, line in enumerate(f, 1):
            line = line.rstrip('\n').rstrip('\r')
            if not line or line.startswith('#'):
                continue
            parts = line.split('\t')
            if len(parts) != 3:
                raise SystemExit(f'{path}:{n}: expected 3 tab separated fields, got {len(parts)}')
            out.append((parts[0], parts[1], parts[2]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('patch')
    ap.add_argument('--table', default=DEFAULT_TABLE)
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()

    patch = read_patch(a.patch)
    with open(a.table, encoding='utf-8', newline='') as f:
        lines = f.read().split('\n')
    if lines and lines[-1] == '':
        lines.pop()

    index = {}
    for i, line in enumerate(lines):
        parts = line.split('\t')
        if len(parts) >= 7:
            index[(parts[0], parts[1])] = i

    changed = 0
    over = 0
    missing = []
    for entry, idx, new in patch:
        i = index.get((entry, idx))
        if i is None:
            missing.append((entry, idx))
            continue
        parts = lines[i].split('\t')
        old = parts[6]
        if old == new:
            continue
        parts[6] = new
        lines[i] = '\t'.join(parts)
        changed += 1
        # The pool row has a fixed slot: the Chinese may not exceed the number
        # of codes the Japanese line used, or import_script keeps the Japanese.
        budget = int(parts[3])
        flag = ''
        if len(new) > budget:
            flag = f'  <-- OVER BUDGET: {len(new)} chars vs {budget} codes'
            over += 1
        if flag or len(new) != len(old):
            print(f'  e{entry}[{idx}] {len(old)}->{len(new)} chars{flag}')

    print(f'patch rows: {len(patch)}, changed: {changed}, over budget: {over}, '
          f'not found: {len(missing)}')
    for entry, idx in missing:
        print(f'  MISSING e{entry}[{idx}]')
    if a.dry_run or not changed:
        return 1 if missing else 0

    shutil.copyfile(a.table, a.table + '.bak')
    with open(a.table, 'w', encoding='utf-8', newline='') as f:
        f.write('\n'.join(lines) + '\n')
    print(f'wrote {a.table} (backup at {a.table}.bak)')
    return 1 if (missing or over) else 0


if __name__ == '__main__':
    sys.exit(main())
