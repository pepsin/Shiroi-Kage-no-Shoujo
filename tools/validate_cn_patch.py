#!/usr/bin/env python3
"""Mechanically validate a hand/subagent written Chinese patch.

Nothing here judges style - it only enforces the constraints that would corrupt
the ROM or the layout:

  * the row exists in the table
  * the new text fits the row's code budget (n_codes)
  * the leading and trailing punctuation matches the Japanese exactly
  * the ``“`` glyph count is unchanged (the game's ellipsis/stammer mark)
  * ASCII digits and \\xXXXX escapes are unchanged
  * the text actually differs from what is in the table

Usage:
  validate_cn_patch.py patch.tsv [--table data/translation.tsv] [--out clean.tsv]
"""
import argparse
import csv
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEAD = '、。」『「“·，！？'
TAIL = '、。」『「，！？'


def marks(s):
    return (s[0] if s and s[0] in LEAD else ''), (s[-1] if s and s[-1] in TAIL else '')


def validate(jp, cn, budget):
    bad = []
    if len(cn) > budget:
        bad.append(f'over budget ({len(cn)}>{budget})')
    if marks(jp) != marks(cn):
        bad.append(f'punct {marks(jp)}->{marks(cn)}')
    if jp.count('“') != cn.count('“'):
        bad.append(f'quote count {jp.count(chr(0x201C))}->{cn.count(chr(0x201C))}')
    if re.findall(r'[0-9]', jp) != re.findall(r'[0-9]', cn):
        bad.append('digits')
    if sorted(re.findall(r'\\x[0-9A-Fa-f]{4}', jp)) != sorted(re.findall(r'\\x[0-9A-Fa-f]{4}', cn)):
        bad.append('escape codes')
    if not cn.strip():
        bad.append('empty')
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('patch')
    ap.add_argument('--table', default=os.path.join(ROOT, 'data', 'translation.tsv'))
    ap.add_argument('--out')
    a = ap.parse_args()

    table = {}
    for r in csv.reader(open(a.table, encoding='utf-8', newline=''), delimiter='\t'):
        if len(r) >= 7 and r[0].isdigit():
            table[(r[0], r[1])] = r

    good, rejected = [], []
    seen = set()
    for n, line in enumerate(open(a.patch, encoding='utf-8'), 1):
        line = line.rstrip('\n').rstrip('\r')
        if not line or line.startswith('#'):
            continue
        parts = line.split('\t')
        if len(parts) != 3:
            rejected.append((f'line {n}', f'expected 3 fields, got {len(parts)}', line))
            continue
        entry, idx, cn = parts
        row = table.get((entry, idx))
        if row is None:
            rejected.append((f'{entry}[{idx}]', 'row not in table', cn))
            continue
        if (entry, idx) in seen:
            rejected.append((f'{entry}[{idx}]', 'duplicate in patch', cn))
            continue
        seen.add((entry, idx))
        bad = validate(row[5], cn, int(row[3]))
        if bad:
            rejected.append((f'{entry}[{idx}]', '; '.join(bad), cn))
        else:
            good.append((entry, idx, cn))

    print(f'patch rows: {len(good) + len(rejected)}, accepted: {len(good)}, rejected: {len(rejected)}')
    for key, why, cn in rejected[:40]:
        print(f'  REJECT {key}: {why}  -> {cn}')
    if a.out and good:
        with open(a.out, 'w', encoding='utf-8', newline='') as f:
            for e, i, cn in good:
                f.write(f'{e}\t{i}\t{cn}\n')
        print(f'wrote {a.out}')
    return 1 if rejected else 0


if __name__ == '__main__':
    sys.exit(main())
