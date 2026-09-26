#!/usr/bin/env python3
"""Find every displayable string the master never captured - structurally.

`extract_pool.scan_pools` (and the later find_missing_runs.py heuristic) miss
short groups, so menus and one-line replies stayed Japanese in game.  Instead of
guessing with heuristics this walks each FAT entry the way the *game* does:

  * the offset table (u32, monotonically increasing, based at `base`), and
  * NUL-terminated runs in the string pool outside the table,
  * runs that start right after a 0x0000 terminator.

Anything whose (entry, offset) is not already in data/translation.tsv is a
candidate; obvious bitmap/table noise is dropped by a shape filter that mirrors
drop_noise_rows.py.

Usage:
  find_missing_structured.py [--out work/missing_all.tsv] [--min-len 2]
                             [--show 40]
"""
import argparse
import collections
import csv
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import gbtext as g          # noqa: E402
import export_script as ex  # noqa: E402

MASTER = os.path.join(ROOT, 'data', 'translation.tsv')

KANA = set('ぁあぃいぅうぇえぉおかがきぎくぐけげこごさざしじすずせぜそぞただちぢっつづてでとどなにぬねのはばぱひびぴふぶぺへべぽほぼぽまみむめもゃやゅゆょよらりるれろわをん'
           'ァアィイゥウェエォオカガキギクグケゲコゴサザシジスズセゼソゾタダチヂッツヅテデトドナニヌネノハバパヒビピフブペヘベホボポマミムメモャヤュユョヨラリルレロワヲンー')
KANJI = set(chr(c) for c in range(0x4E00, 0x9FA0))
PUNCT = set('、。，．！？「」『』（）〈〉《》・：；“”‘’‥…―ー～＋＜＞％×○●□■')
ALLOWED = KANA | KANJI | PUNCT | set('0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz /')
# table/bitmap noise shapes (see drop_noise_rows.py)
FILLER = set('‥…“”‘’・‥ ')


def plausible(text):
    """Reject byte-noise: require real kana/kanji content and a clean shape."""
    if len(text) < 2:
        return False
    if any(c not in ALLOWED for c in text):
        return False
    letters = sum(1 for c in text if c in KANA or c in KANJI)
    if letters == 0:
        return False
    if letters / len(text) < 0.34:          # mostly punctuation/spaces -> noise
        return False
    if len(text) <= 3 and all(c in FILLER for c in text):
        return False
    if len(text) <= 4 and sum(1 for c in text if c in KANA) and \
            sum(1 for c in text if c in KANJI) and len(text) == 2:
        return False                          # kana+kanji pair = table noise
    return True


def master_texts():
    """(entry, jp_text) pairs already in the master, plus a global text set.

    The master's pool rows store a different offset convention than the table
    walk, so compare on the decoded text instead of the offset.
    """
    per_entry, every = set(), set()
    if os.path.exists(MASTER):
        for r in csv.DictReader(open(MASTER, encoding='utf-8'), delimiter='\t'):
            t = (r.get('jp_text') or '').strip()
            if not t:
                continue
            every.add(t)
            try:
                per_entry.add((int(r['entry']), t))
            except (KeyError, ValueError):
                continue
    return per_entry, every


def decode(codes, table):
    """Same convention as export_script.jp_decode (that is what the master holds)."""
    return ex.jp_decode(codes, table)


def table_strings(d):
    """[(offset, codes)] for every offset-table entry."""
    info = ex.analyse(d)
    if not info or not info.get('table_off'):
        return []
    toff, base = info['table_off'], info['base']
    vals, p = [], toff
    while p + 4 <= len(d):
        x = struct.unpack_from('<I', d, p)[0]
        if x == 0xFFFFFFFF or x > len(d):
            break
        if vals and x <= vals[-1]:
            break
        vals.append(x)
        p += 4
    out = []
    for v in vals:
        off = base + v
        codes, q = [], off
        while q + 2 <= len(d):
            c = struct.unpack_from('<H', d, q)[0]
            if c == 0:
                break
            codes.append(c)
            q += 2
        if codes:
            out.append((off, codes))
    return out


def pool_runs(d):
    """NUL-terminated runs outside the table (start = 0 or after a terminator)."""
    codes = list(struct.unpack_from('<%dH' % (len(d) // 2), d, 0))
    runs = []
    i = 0
    start = 0
    while i < len(codes):
        if codes[i] == 0:
            if i > start:
                runs.append((start * 2, codes[start:i]))
            start = i + 1
        i += 1
    if start < len(codes):
        runs.append((start * 2, codes[start:]))
    return runs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=os.path.join(ROOT, 'work', 'missing_all.tsv'))
    ap.add_argument('--rom', default=g.JP_ROM)
    ap.add_argument('--min-len', type=int, default=2)
    ap.add_argument('--show', type=int, default=30)
    a = ap.parse_args()

    rom = open(a.rom, 'rb').read()
    table = ex.load_table('jp')
    per_entry, every_text = master_texts()
    rows = []
    stats = collections.Counter()
    for eid in range(1500):
        d = g.load_entry(rom, eid)
        if not d:
            continue
        cand = list(table_strings(d)) + list(pool_runs(d))
        local = set()
        for off, codes in cand:
            if len(codes) < a.min_len:
                continue
            text = decode(codes, table).strip()
            if (eid, text) in per_entry or text in every_text:
                stats['known'] += 1
                continue
            if text in local:
                continue
            local.add(text)
            if not plausible(text):
                stats['noise'] += 1
                continue
            rows.append((eid, off, len(codes), text))
            stats['candidate'] += 1
    rows.sort()
    with open(a.out, 'w', encoding='utf-8', newline='') as f:
        w = csv.writer(f, delimiter='\t')
        w.writerow(['entry', 'offset', 'n_codes', 'jp_text'])
        w.writerows(rows)
    print(f'known {stats["known"]}, filtered noise {stats["noise"]}, '
          f'candidates {stats["candidate"]} -> {a.out}')
    for r in rows[:a.show]:
        print(f'  e{r[0]} @{r[1]} n={r[2]}  {r[3]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
