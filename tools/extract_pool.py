#!/usr/bin/env python3
"""Extract script entries the offset-table extractor missed.

Why this exists
---------------
export_script.py only recognises entries that carry an *ascending u32 offset
table* before the string pool.  Many script entries in this game have no such
table: their strings are a run of NUL-terminated u16 codes sitting directly in
the entry (sometimes at an ODD byte offset), which is why scenes such as the
character introduction in entry 1 were never exported.

This tool finds those pools and appends their strings to data/translation.tsv
with an extra 8th column `kind` = `pool`; `offset` is then the ABSOLUTE byte
offset of the string inside the decompressed entry (the offset-table rows keep
their base-relative offset and an empty `kind`).

Usage:
  extract_pool.py [--rom JP.gba] [--master data/translation.tsv] [--dry-run]
"""
import argparse
import csv
import os
import re
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import gbtext as g
import mapio

HIRA = re.compile(r'[\u3040-\u309f]')
SKIP = {
    850, 851, 852,   # font tables and the table right after them
    605,             # kana -> kanji candidate dictionary for text input
    547,             # bitmap/font data (kana tables + noise) - false positives
    571,             # bitmap/font data (kana tables + noise) - false positives
    611,             # glyph/bitmap pool (`死ぱ`, `獲` noise), no offset table
    636,             # bitmap data (`雇雇ぱ` noise), no offset table
}


def scan_pools(d, dec):
    """Return [(offset, text, words)] for EVERY NUL-terminated string run in d.

    A script entry interleaves bytecode with several separate string runs, so
    the earlier "keep the longest run" behaviour silently dropped most of an
    entry's text.  All runs are collected instead; each string has to carry
    kana and avoid the byte patterns that bitmap data produces.
    """
    out = []
    for par in (0, 1):
        i = par
        cur = []
        while i + 1 < len(d):
            c = struct.unpack_from('<H', d, i)[0]
            if c == 0:
                i += 2
                continue
            start = i
            buf = []
            while i + 1 < len(d):
                c2 = struct.unpack_from('<H', d, i)[0]
                if c2 == 0:
                    break
                ch = dec.get(c2 - 1 if c2 >= 0x20 else c2)
                if ch is None:
                    break
                buf.append(ch)
                i += 2
            ok = (i + 1 < len(d) and struct.unpack_from('<H', d, i)[0] == 0
                  and len(buf) >= 2)
            txt = ''.join(buf)
            if ok and HIRA.search(txt) and not re.search(r'[X6PCN]', txt):
                cur.append((start, txt, len(buf) + 1))
                i += 2
                continue
            if len(cur) >= 3 and sum(len(t) for _, t, _ in cur) >= 20:
                out.extend(cur)
            cur = []
            i = start + 2
        if len(cur) >= 3 and sum(len(t) for _, t, _ in cur) >= 20:
            out.extend(cur)
    # drop duplicates found at both parities
    seen = set()
    uniq = []
    for off, txt, words in out:
        if (off, txt) in seen:
            continue
        seen.add((off, txt))
        uniq.append((off, txt, words))
    return uniq


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--rom', default=g.JP_ROM)
    ap.add_argument('--master', default=os.path.join(ROOT, 'data', 'translation.tsv'))
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    rom = open(a.rom, 'rb').read()
    # mapio maps table INDEX -> char; a text code C selects index C-1
    # (code < 0x20 selects index C directly)
    dec = mapio.load_map(os.path.join(ROOT, 'data', 'glyph_map.csv'))
    raw = open(a.master, encoding='utf-8', newline='').read()
    lines = raw.split('\n')
    if lines and lines[-1] == '':
        lines.pop()
    have = set()          # (entry, offset) already exported as a pool row
    known = {}            # entry -> set of jp_text already covered by the master
    for line in lines[1:]:
        p = line.rstrip('\r').split('\t')
        while len(p) < 7:
            p.append('')
        if len(p) >= 8 and p[7] == 'pool':
            have.add((p[0], p[2]))
        else:
            known.setdefault(p[0], set()).add(p[5])
    n_rows = 0
    n_entries = 0
    added = []
    for eid in range(2000):
        if eid in SKIP:
            continue
        d = g.load_entry(rom, eid)
        if not d or len(d) > 0x40000:
            continue
        r = scan_pools(d, dec)
        if not r:
            continue
        chars = sum(len(t) for _, t, _ in r)
        got = 0
        seen = known.get(str(eid), set())
        for idx, (off, txt, words) in enumerate(r):
            if (str(eid), str(off)) in have:
                continue
            if txt in seen:      # already exported through the offset table
                continue
            added.append([str(eid), str(idx), str(off), str(words - 1),
                          str(2 * words), txt, '', 'pool'])
            got += 1
        if got:
            n_entries += 1
            n_rows += got
            print(f'  e{eid:<5} {got:4d} strings  {chars:6d} chars')
    print(f'\n{len(lines) - 1} rows -> +{n_rows} pool rows from {n_entries} entries')
    if a.dry_run:
        return
    # the master table is CRLF; keep it uniform
    nl = '\r\n' if lines and lines[0].endswith('\r') else '\n'
    with open(a.master, 'w', encoding='utf-8', newline='') as f:
        f.write('\n'.join(lines))
        if lines:
            f.write('\n')            # close the last existing line
        for row in added:
            f.write('\t'.join(row) + nl)
    print(f'wrote {a.master}')


if __name__ == '__main__':
    main()
