#!/usr/bin/env python3
"""Audit every dispatched script's referenced text slots against the master.

Identity is (entry, absolute decompressed offset), never source text: identical
Japanese can have several independently stored instances. The engine's offset
tables can be odd-aligned. Only table-referenced, terminated strings are reported;
bytecode, trailing branch tables and unreferenced bitmap noise are excluded.
This is a coverage report, not authorization to translate or overwrite a slot.
"""
import argparse
from collections import defaultdict
import csv
from pathlib import Path
import struct

import export_script as ex
import gbtext as g
from extract_pool import branch_table_span
from romdbg import entry_table
from work_paths import output_file

ROOT = Path(__file__).resolve().parents[1]
MASTER = ROOT / 'data/translation.tsv'
ALLOWLIST = ROOT / 'data/script_coverage_allowlist.tsv'


def master_slots(path=MASTER):
    slots = set()
    spans = defaultdict(list)
    with open(path, encoding='utf-8', newline='') as f:
        for fields in csv.reader(f, delimiter='\t'):
            if len(fields) < 7 or not fields[0].isdigit():
                continue
            off = int(fields[2])
            if len(fields) < 8 or fields[7] != 'pool':
                off += 0x7A0
            slots.add((int(fields[0]), off))
            spans[int(fields[0])].append((off, off + 2 * (int(fields[3]) + 1)))
    for eid, intervals in spans.items():
        end = -1
        for lo, hi in sorted(intervals):
            if lo < end:
                raise ValueError(f'e{eid} 翻译槽重叠 @{lo}；完整行与误收后缀不可同时写回')
            end = hi
    return slots


def table_strings(d):
    """Yield (engine index, absolute offset, codes), after structural checks."""
    toff, vals = entry_table(d)
    if toff is None or toff < 0x7A0 + 0x15:
        return
    table_end = toff + 4 * len(vals)
    branch = branch_table_span(d)
    for idx, relative in enumerate(vals):
        off = 0x7A0 + relative
        if off < table_end or off + 2 > len(d):
            continue
        codes, p = [], off
        while p + 2 <= len(d):
            c = struct.unpack_from('<H', d, p)[0]
            p += 2
            if c == 0:
                break
            codes.append(c)
        else:
            continue
        if branch and off < branch[1] and p > branch[0]:
            continue
        if codes and all(c <= 0x6A8 or c >= 0xFE00 for c in codes):
            yield idx, off, codes


def missing_strings(eid, data, known, table, min_len=1):
    for idx, off, codes in table_strings(data):
        if len(codes) >= min_len and (eid, off) not in known:
            yield eid, idx, off, len(codes), ex.jp_decode(codes, table)


def unreviewed(rows, allowlist):
    """A retained slot is approved only at its exact entry/offset AND source."""
    with open(allowlist, encoding='utf-8', newline='') as f:
        allowed = {(int(r['entry']), int(r['offset']), r['jp_text'])
                   for r in csv.DictReader(f, delimiter='\t') if r['reason'].strip()}
    return [r for r in rows if (r[0], r[2], r[4]) not in allowed]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--rom', default=g.JP_ROM)
    ap.add_argument('--master', default=MASTER)
    ap.add_argument('--min-len', type=int, default=1)
    ap.add_argument('--show', type=int, default=30)
    ap.add_argument('--check', action='store_true', help='fail on unreviewed slots')
    ap.add_argument('--allowlist', default=ALLOWLIST)
    args = ap.parse_args(argv)
    if args.min_len < 1 or (args.check and args.min_len != 1):
        ap.error('--check 必须包含一字选项（--min-len 1）')
    rom = Path(args.rom).read_bytes()
    table, known = ex.load_table('jp'), master_slots(args.master)
    # Only entries actually dispatched by loadScript, not fonts/graphics/e0.
    entries = sorted({struct.unpack_from('<h', rom, 0x15C004 + i * 8)[0]
                      for i in range(509)})
    rows = []
    for eid in entries:
        data = g.load_entry(rom, eid)
        if data:
            rows.extend(missing_strings(eid, data, known, table, args.min_len))
    path = output_file('reports/missing-structured.tsv')
    with open(path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f, delimiter='\t', lineterminator='\n')
        writer.writerow(['entry', 'engine_idx', 'offset', 'n_codes', 'jp_text'])
        writer.writerows(rows)
    print(f'{len(entries)} dispatched entries; {len(rows)} uncovered referenced slots -> {path}')
    pending = unreviewed(rows, args.allowlist) if args.check else rows
    for r in pending[:args.show]:
        print(f'  e{r[0]} row={r[1]} @{r[2]} n={r[3]} {r[4]}')
    if args.check:
        print(f'{len(pending)} unreviewed; {len(rows) - len(pending)} explicitly retained')
    return int(args.check and bool(pending))


if __name__ == '__main__':
    raise SystemExit(main())
