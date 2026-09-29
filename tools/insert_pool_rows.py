#!/usr/bin/env python3
"""Add display lines the extractor dropped, then renumber the pool indices.

Why this exists
---------------
``extract_pool.scan_pools`` only emits a batch of accepted runs when the batch
has >= 3 runs and >= 20 characters, so **short lines vanish**.  In entry 1 the
gap between two exported rows is exactly 16 bytes:

    0x1D0B  」フフフ“““          <- displayed by the game, never exported

The game keeps showing the Japanese there, and the player sees a stray
「フフフ」 in the middle of a Chinese line.  ``find_missing_runs.py`` finds part
of this class, but it requires hiragana in the run, so pure katakana / symbol
lines (フフフ, ハハハ, ピンポ～ン, キ～ホルダ～) never show up in its report.

How the candidates are found
----------------------------
``find_gap_runs.py`` walks every entry, decodes every NUL-terminated run that
sits in a *small gap between two exported rows* (<= 64 bytes), and reports the
ones the master does not cover.  That gap condition is what separates real
missing display lines from bitmap/table noise: real lines sit right where the
extractor skipped them.

What this tool does
-------------------
* verifies every candidate against the **Japanese** ROM (the slot must decode
  to exactly the reported text and end with a 0x0000),
* refuses candidates that overlap an existing row,
* inserts the row as a ``pool`` row (absolute offset, as ``import_script``
  expects) immediately after the row whose offset precedes it,
* gives the entry's pool rows fresh contiguous indices (table rows keep their
  index - those are real offset-table slots) and writes a remap file, and
* rewrites ``(entry, idx)`` references in the work files with that remap, so
  the historical patch files keep pointing at the same rows.

Usage
-----
  python3 tools/find_gap_runs.py                    # refresh the candidate list
  python3 tools/insert_pool_rows.py --dry-run       # report only
  python3 tools/insert_pool_rows.py                 # insert + renumber
  python3 tools/insert_pool_rows.py --remap-only    # just apply a saved remap
"""
import argparse
import collections
import csv
import glob
import os
import re
import shutil
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import gbtext as g          # noqa: E402
import mapio                # noqa: E402

MASTER = os.path.join(ROOT, 'data', 'translation.tsv')
CANDIDATES = os.path.join(ROOT, 'work', 'missing_gap_runs.tsv')
REMAP = os.path.join(ROOT, 'work', 'insert_remap.tsv')
CATALOG = os.path.join(ROOT, 'data', 'entry_catalog.tsv')
WORK_GLOBS = ['work/scenes/patch*.tsv', 'work/tm/w*/*.tsv', 'work/batches/*.tsv']

# Only seven names on purpose: a pool row carries an eighth *unnamed* value
# ('pool') that import_script reads through csv's restkey='extra'.  Naming that
# column in the header silently turns every pool row into a table row.
HEADER = ['entry', 'idx', 'offset', 'n_codes', 'n_bytes', 'jp_text',
          'translation']


def load_catalog():
    cat = {}
    for r in csv.DictReader(open(CATALOG, encoding='utf-8'), delimiter='\t'):
        if r.get('base'):
            cat[int(r['eid'])] = int(r['base'])
    return cat


def load_master():
    rows = []
    with open(MASTER, encoding='utf-8') as f:
        for line in f:
            line = line.rstrip('\n')
            if not line or not line[0].isdigit():
                continue
            rows.append(line.split('\t'))
    return rows


def decode_slot(d, off, n_codes):
    """Return the text stored at ``off`` when the slot is exactly NUL-ended."""
    buf = []
    for i in range(n_codes):
        p = off + 2 * i
        if p + 1 >= len(d):
            return None
        c = struct.unpack_from('<H', d, p)[0]
        if c == 0:
            return None
        ch = mapio_dec.get(c - 1 if c >= 0x20 else c)
        if ch is None:
            return None
        buf.append(ch)
    end = off + 2 * n_codes
    if end + 1 >= len(d) or struct.unpack_from('<H', d, end)[0] != 0:
        return None
    return ''.join(buf)


mapio_dec = {}


def span_of(row, cat):
    eid = int(row[0])
    off = int(row[2])
    n = int(row[3]) + 1
    start = off if (len(row) > 7 and row[7] == 'pool') else cat.get(eid, 0) + off
    return start, start + 2 * n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--candidates', default=CANDIDATES)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--remap-only', action='store_true')
    ap.add_argument('--fix-truncated', action='store_true',
                    help='repair rows whose recorded offset starts mid-line: '
                         'when the candidate text ends with the row text and '
                         'the slot fits, move the row back to the real start')
    ap.add_argument('--no-remap-files', action='store_true')
    ap.add_argument('--allow-empty', action='store_true',
                    help='also insert runs we have no Chinese for (writes the '
                         'slot with an empty translation)')
    ap.add_argument('--translations', default=os.path.join(
        ROOT, 'data', 'gap_translations.tsv'),
        help='jp_text -> chinese table for the runs that have no twin in '
             'the master yet')
    a = ap.parse_args()

    global mapio_dec
    mapio_dec = mapio.load_map(os.path.join(ROOT, 'data', 'glyph_map.csv'))
    cat = load_catalog()
    rows = load_master()

    if not a.remap_only:
        cand = list(csv.DictReader(open(a.candidates, encoding='utf-8'),
                                   delimiter='\t'))
        rom = open(g.JP_ROM, 'rb').read()
        # existing spans per entry + translations of every distinct jp text
        spans = collections.defaultdict(list)
        have = collections.defaultdict(collections.Counter)
        for r in rows:
            s, e = span_of(r, cat)
            spans[int(r[0])].append((s, e))
            if r[6]:
                have[r[5]][r[6]] += 1
        for k in spans:
            spans[k].sort()
        jp_cache = {}
        manual = {}
        if os.path.exists(a.translations):
            with open(a.translations, encoding='utf-8') as f:
                for line in f:
                    if line.startswith('#') or '\t' not in line:
                        continue
                    k, v = line.rstrip('\n').split('\t')[:2]
                    manual[k] = v

        def decode(entry, off):
            if entry not in jp_cache:
                jp_cache[entry] = g.load_entry(rom, entry)
            return jp_cache[entry]

        accepted, refused = [], []
        for c in cand:
            eid, off, n = int(c['entry']), int(c['offset'], 0), int(c['n_codes'])
            txt = c['jp_text']
            d = decode(eid, off)
            if not d:
                refused.append((eid, off, txt, 'entry missing'))
                continue
            got = decode_slot(d, off, n) if n else None
            if got != txt:
                refused.append((eid, off, txt, f'decode mismatch: {got!r}'))
                continue
            cand_end = off + 2 * (n + 1)
            clash = [sp for sp in spans.get(eid, []) if off < sp[1] and sp[0] < cand_end]
            if clash:
                refused.append((eid, off, txt,
                                f'overlaps row 0x{clash[0][0]:X}-0x{clash[0][1]:X}'))
                continue
            after = [sp for sp in spans.get(eid, []) if sp[0] >= cand_end]
            next_row = None
            for r in rows:
                if int(r[0]) == eid and span_of(r, cat)[0] == (after[0][0]
                                                              if after else None):
                    next_row = r
                    break
            if next_row is not None:
                njp = next_row[5]
                if njp[:1] in '。、，' and any(
                        txt[-L:] == njp[:L] for L in range(4, min(len(txt),
                                                                  len(njp)) + 1)):
                    refused.append((eid, off, txt,
                                    f'split of row {next_row[1]!r} ({njp!r})'))
                    continue
            tr = (c.get('translation') or manual.get(txt)
                  or (have[txt].most_common(1)[0][0] if have.get(txt) else ''))
            if not tr and not a.allow_empty:
                refused.append((eid, off, txt, 'no translation available'))
                continue
            accepted.append({'entry': eid, 'offset': off, 'n_codes': n,
                             'jp': txt, 'translation': tr})
        if a.fix_truncated:
            fixed = 0
            for eid, off, txt, why in list(refused):
                if not why.startswith('overlaps'):
                    continue
                n = next(x['n_codes'] for x in cand
                         if int(x['entry']) == eid and int(x['offset'], 0) == off)
                target = next((r for r in rows if int(r[0]) == eid
                               and txt.endswith(r[5])
                               and off < span_of(r, cat)[0]
                               < off + 2 * (int(n) + 1)),
                              None)
                if target is None:
                    continue
                tr = manual.get(txt, '')
                if not tr:
                    print(f'  truncated without translation: e{eid} '
                          f'0x{off:X} {txt!r}')
                    continue
                cur_start, cur_end = span_of(target, cat)
                room = min([span_of(r, cat)[0] for r in rows
                            if int(r[0]) == eid and span_of(r, cat)[0] >= cur_end]
                           or [off + 2 * (int(n) + 1)]) - off
                if room < 2 * (int(n) + 1):
                    print(f'  truncated slot too small: e{eid} 0x{off:X} '
                          f'{txt!r} room={room}')
                    continue
                target[2], target[3] = str(off), str(n)
                target[4] = str(2 * (int(n) + 1))
                target[5], target[6] = txt, tr
                fixed += 1
                refused = [r for r in refused if not (r[0] == eid and r[1] == off)]
            print(f'truncated rows repaired: {fixed}')
        print(f'candidates: {len(cand)}  accepted: {len(accepted)}  '
              f'refused: {len(refused)}')
        for r in refused[:10]:
            print(f'  refused e{r[0]} 0x{r[1]:X} {r[2]!r}: {r[3]}')
        if a.dry_run:
            need = [x for x in accepted if not x['translation']]
            print(f'without a translation to copy: {len(need)} '
                  f'({len(set(x["jp"] for x in need))} distinct texts)')
            for t, n in collections.Counter(x['jp'] for x in need).most_common(15):
                print(f'  {n:>4}  {t}')
            return

        # ---- insert, then renumber each entry's pool rows
        by_entry = collections.defaultdict(list)
        for i, r in enumerate(rows):
            by_entry[int(r[0])].append(i)
        remap = {}
        new_rows = []
        added = collections.defaultdict(list)
        for x in accepted:
            added[x['entry']].append(x)
        for eid, idxs in by_entry.items():
            ins = sorted(added.get(eid, []), key=lambda x: x['offset'])
            pool = [i for i in idxs if len(rows[i]) > 7 and rows[i][7] == 'pool']
            table = [i for i in idxs if i not in pool]
            tab_idx = sorted(int(rows[i][1]) for i in table)
            if tab_idx != list(range(len(table))):
                print(f'  e{eid}: table indices are not 0..{len(table)-1}, '
                      f'skipping renumber')
                continue
            if not ins:
                continue
            starts = {i: span_of(rows[i], cat)[0] for i in pool}
            # Keep the extractor's scan order: scene splitting in
            # scene_index.py reads storage blocks off that order, and sorting
            # by offset instead collapses the block boundaries.
            order = sorted(pool, key=lambda i: int(rows[i][1]))
            # walk the pool rows in order and slot the new rows in by offset
            merged = []
            for i in order:
                for x in list(ins):
                    if x['offset'] < starts[i]:
                        merged.append(('new', x))
                        ins.remove(x)
                merged.append(('row', i))
            for x in ins:
                merged.append(('new', x))
            # write back: table rows keep their index, pool rows become T..N-1
            base_idx = len(table)
            for pos, (kind, item) in enumerate(merged):
                if kind == 'row':
                    old = int(rows[item][1])
                    if old != base_idx + pos:
                        remap[(eid, old)] = base_idx + pos
                    rows[item][1] = str(base_idx + pos)
                else:
                    r = dict(item)
                    new_rows.append((item, base_idx + pos))
        for x, idx in new_rows:
            rows.append([str(x['entry']), str(idx), str(x['offset']),
                         str(x['n_codes']), str(2 * (x['n_codes'] + 1)),
                         x['jp'], x['translation'], 'pool'])
        print(f'inserted rows: {len(new_rows)}  renumbered pool indices: '
              f'{len(remap)}')

        if not a.dry_run:
            shutil.copy(MASTER, MASTER + '.pre-insert')
            # regroup the file by entry, new rows next to their neighbours
            grouped = collections.OrderedDict()
            for i, r in enumerate(rows):
                grouped.setdefault(int(r[0]), []).append(r)
            with open(MASTER, 'w', encoding='utf-8') as f:
                f.write('\t'.join(HEADER) + '\n')
                for eid, rs in grouped.items():
                    rs.sort(key=lambda r: int(r[1]))
                    for r in rs:
                        f.write('\t'.join(r) + '\n')
            if remap:
              with open(REMAP, 'w', encoding='utf-8') as f:
                f.write('entry\told_idx\tnew_idx\n')
                for (eid, old), new in sorted(remap.items()):
                    f.write(f'{eid}\t{old}\t{new}\n')
            print(f'wrote {MASTER} (backup {MASTER}.pre-insert) and {REMAP}')
    else:
        remap = {}
        for r in csv.DictReader(open(REMAP, encoding='utf-8'), delimiter='\t'):
            remap[(int(r['entry']), int(r['old_idx']))] = int(r['new_idx'])

    if remap and not a.no_remap_files:
        touched = 0
        for pat in WORK_GLOBS:
            for path in glob.glob(os.path.join(ROOT, pat)):
                with open(path, encoding='utf-8') as f:
                    lines = f.readlines()
                out, changed = [], False
                for line in lines:
                    parts = line.rstrip('\n').split('\t')
                    if (len(parts) >= 3 and parts[0].isdigit()
                            and parts[1].isdigit()):
                        key = (int(parts[0]), int(parts[1]))
                        if key in remap and remap[key] != key[1]:
                            parts[1] = str(remap[key])
                            line = '\t'.join(parts) + '\n'
                            changed = True
                    out.append(line)
                if changed:
                    with open(path, 'w', encoding='utf-8') as f:
                        f.writelines(out)
                    touched += 1
        print(f'work files rewritten with the remap: {touched}')


if __name__ == '__main__':
    main()
