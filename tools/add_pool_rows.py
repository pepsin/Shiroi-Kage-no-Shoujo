#!/usr/bin/env python3
"""Add the pool strings that `extract_pool.py`'s filters dropped.

`extract_pool.scan_pools` only keeps a run when it contains **hiragana**
(that is what separates dialogue from bitmap noise) and never looks at odd
byte offsets.  Whole categories of display text therefore never reached the
master:

  * the scene-name table of e417 (`コマ劇場前`, `アルタ前`, `メゾン西新宿`,
    `アジト`, `西新宿9丁目` ...) - it feeds the save screen and the status bar,
  * profile data (`37才`, `41代後半`, `国際企業神宮寺コンツェルン総帥`),
  * staff-roll credits (`アソシエイトプロデュ～サ～`, `デ～タ作成`),
  * and short katakana/punctuation dialogue lines (`」フン“““`).

The tool walks every NUL-terminated run of every pool-carrying entry at both
byte parities, keeps the ones whose text is pure katakana / kanji / digits /
punctuation, and appends a master row for each text listed in
`data/pool_extra.tsv` (`jp_text<TAB>translation`) that is not covered yet.
Noise is excluded simply by not listing it, and every row is validated against
the JP ROM before it is written.

Usage:
  add_pool_rows.py --dry-run     # report candidates / coverage / slot fit
  add_pool_rows.py               # append the rows to data/translation.tsv
"""
import argparse
import collections
import csv
import os
import re
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import gbtext as g          # noqa: E402
import mapio                # noqa: E402

EXTRA = os.path.join(ROOT, 'data', 'pool_extra.tsv')
HIRA = re.compile(r'[\u3040-\u309f]')
TEXTY = re.compile(r'^[\u30a0-\u30ff\u4e00-\u9fa0\u3000-\u303f0-9A-Za-z'
                   r'、。，．！？「」『』（）〈〉《》・：；“”‘’‥…―ー～＋＜＞％×○●□■]+$')
SKIP = {850, 851, 852, 605, 547, 571, 611, 636, 1489}
ESC = re.compile(r'\\x([0-9A-Fa-f]{4})')


def load_text_table():
    gmap = mapio.load_map(os.path.join(ROOT, 'data', 'glyph_map.csv'))
    return {idx if idx < 0x20 else idx + 1: ch
            for idx, ch in gmap.items() if ch}


def load_master(path):
    raw = open(path, encoding='utf-8', newline='').read()
    lines = raw.split('\r\n')
    while lines and lines[-1] == '':
        lines.pop()
    have_t, have_p = set(), set()
    known = collections.defaultdict(set)
    max_idx = collections.defaultdict(int)
    for line in lines[1:]:
        p = line.split('\t')
        if len(p) < 7:
            continue
        eid, idx = int(p[0]), int(p[1])
        known[eid].add(p[5])
        max_idx[eid] = max(max_idx[eid], idx)
        if len(p) > 7 and p[7] == 'pool':
            have_p.add((eid, int(p[2])))
        else:
            have_t.add((eid, idx))
    return lines, have_t, have_p, known, max_idx


def scan(rom, table, master_entries, known, have_p):
    """Yield (eid, offset, n_codes, text) for uncovered no-hiragana pool runs."""
    out = []
    catalog = {}
    catpath = os.path.join(ROOT, 'data', 'entry_catalog.tsv')
    for r in csv.DictReader(open(catpath, encoding='utf-8'), delimiter='\t'):
        if r.get('base'):
            catalog[int(r['eid'])] = (int(r['table_off']), int(r['base']))
    for eid in sorted(master_entries):
        if eid in SKIP:
            continue
        d = g.load_entry(rom, eid)
        if not d or len(d) > 0x40000:
            continue
        # offsets owned by this entry's offset table: a run starting inside one
        # of those slots is a suffix of a table string, not a string of its own
        slots = []
        if eid in catalog:
            toff, base = catalog[eid]
            p = toff
            vals = []
            while p + 4 <= len(d):
                x = struct.unpack_from('<I', d, p)[0]
                if x == 0xFFFFFFFF or x > len(d) or (vals and x <= vals[-1]):
                    break
                vals.append(x)
                p += 4
            for o in vals:
                off = base + o
                n = 0
                while off + 2 * n + 2 <= len(d) and \
                        struct.unpack_from('<H', d, off + 2 * n)[0] != 0:
                    n += 1
                slots.append((off, off + 2 * (n + 1)))
        for par in (0, 1):
            i = par
            while i + 2 < len(d):
                if struct.unpack_from('<H', d, i)[0] != 0:
                    i += 2
                    continue
                start, buf, j = i + 2, [], i + 2
                while j + 1 < len(d):
                    c = struct.unpack_from('<H', d, j)[0]
                    if c == 0:
                        break
                    if c not in table:
                        buf = None
                        break
                    buf.append(table[c])
                    j += 2
                if buf and 2 <= len(buf) <= 24:
                    txt = ''.join(buf)
                    gap = 0                      # free zero words after the NUL
                    while gap < 8 and j + 2 * gap + 2 < len(d) and \
                            struct.unpack_from('<H', d, j + 2 * gap + 2)[0] == 0:
                        gap += 1
                    if (TEXTY.match(txt) and not HIRA.search(txt)
                            and (eid, start) not in have_p
                            and not any(s <= start < e for s, e in slots)
                            and not any(txt in k or k in txt for k in known[eid])):
                        out.append((eid, start, len(buf), txt, gap))
                    i = j if j > start else start
                else:
                    i = start
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--master', default=os.path.join(ROOT, 'data', 'translation.tsv'))
    ap.add_argument('--rom', default=g.JP_ROM)
    ap.add_argument('--extra', default=EXTRA)
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()

    rom = open(a.rom, 'rb').read()
    table = load_text_table()
    lines, have_t, have_p, known, max_idx = load_master(a.master)
    tr = {}
    for line in open(a.extra, encoding='utf-8'):
        line = line.rstrip('\n')
        if line and not line.startswith('#'):
            jp, zh = line.split('\t', 1)
            tr[jp] = zh

    cand = scan(rom, table, set(known), known, have_p)
    rows = [c for c in cand if c[3] in tr]
    skipped = sorted({c[3] for c in cand if c[3] not in tr})
    print(f'candidates: {len(cand)}; in {os.path.basename(a.extra)}: {len(rows)}; '
          f'left for a later round: {len(skipped)}')

    # Two parities can both see a NUL-terminated run and report the same bytes
    # at adjacent offsets; keep the lowest one so the writes never overlap.
    kept = []
    per_entry = collections.defaultdict(list)
    for r in sorted(rows):
        per_entry[r[0]].append(r)
    for eid, rs in per_entry.items():
        end = -1
        for eid_, off, n, text, gap in sorted(rs, key=lambda r: r[1]):
            if off < end:
                continue
            kept.append((eid_, off, n, text, gap))
            end = off + 2 * (n + 1 + gap)
    rows = kept
    print(f'rows after overlap pruning: {len(rows)}')

    new_idx = dict(max_idx)
    out = []
    for eid, off, n, text, gap in sorted(rows):
        zh = tr[text]
        parts = ESC.split(zh)
        n_codes = len(ESC.findall(zh)) + sum(len(p) for p in parts[::2])
        # the slot may grow into the zero padding that follows the NUL
        slot = n + 1 + gap
        if n_codes + 1 > slot:
            print(f'  !! does not fit: e{eid}@{off} {text!r} needs {n_codes} '
                  f'but the slot holds {slot}')
            return 1
        new_idx[eid] += 1
        out.append([str(eid), str(new_idx[eid]), str(off), str(slot - 1),
                    str(2 * slot), text, zh, 'pool'])
    print(f'rows to append: {len(out)}')
    for r in out:
        print('   ', '\t'.join(r))
    if a.dry_run:
        return 0
    with open(a.master, 'w', encoding='utf-8', newline='') as f:
        f.write('\r\n'.join(lines) + '\r\n')
        for r in out:
            f.write('\t'.join(r) + '\r\n')
    print(f'appended {len(out)} rows to {a.master}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
