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


def branch_table_span(d):
    """[start, end) of the entry's trailing branch-target table, or None.

    Script entries carry a bytecode jump table at the very end: a u32 at
    pool+0x11 (pool offset comes from the header field at +0x10) points at a
    u16 count followed by that many (u16 id, u32 target) pairs.  Its bytes read
    as bogus two-code "strings"; writing translations over the targets sends
    the scene's branches into the wrong bytecode and deadlocks the game, so
    this region must never be exported as text.
    """
    if len(d) < 0x14:
        return None
    pool = struct.unpack_from('<I', d, 0x10)[0]
    if pool + 0x15 > len(d):
        return None
    t = struct.unpack_from('<I', d, pool + 0x11)[0]
    start = pool + t
    if start + 2 > len(d):
        return None
    cnt = struct.unpack_from('<H', d, start)[0]
    if cnt == 0 or cnt > 40:
        return None
    end = start + 2 + 6 * cnt
    if end > len(d):
        return None
    for i in range(cnt):
        if struct.unpack_from('<I', d, start + 2 + 6 * i + 2)[0] + pool >= len(d):
            return None
    return start, end


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


def table_slots(d, toff, base):
    """[(start, end)] of every offset-table string slot, in absolute offsets.

    A run found by scan_pools that starts inside one of these slots is not a
    separate string - it is the suffix of a string the offset table already
    covers, produced when the `[X6PCN]` filter rejects the full run (a line with
    a digit in it) and the scanner retries one code later.  Writing both rows
    would clobber the longer one.
    """
    vals = []
    p = toff
    while p + 4 <= len(d):
        x = struct.unpack_from('<I', d, p)[0]
        if x == 0xFFFFFFFF or x > len(d) or (vals and x <= vals[-1]):
            break
        vals.append(x)
        p += 4
    out = []
    for o in vals:
        off = base + o
        if off + 2 > len(d):
            continue
        n = 0
        while off + 2 * n + 2 <= len(d) and struct.unpack_from('<H', d, off + 2 * n)[0] != 0:
            n += 1
        out.append((off, off + 2 * (n + 1)))
    return out


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
    cat = {}
    catpath = os.path.join(ROOT, 'data', 'entry_catalog.tsv')
    if os.path.exists(catpath):
        for r in csv.DictReader(open(catpath, encoding='utf-8'), delimiter='\t'):
            if r.get('base'):
                cat[int(r['eid'])] = (int(r['table_off']), int(r['base']))
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
        span = branch_table_span(d)
        if span:
            r = [row for row in r if not (span[0] <= row[0] < span[1])]
            if not r:
                continue
        chars = sum(len(t) for _, t, _ in r)
        got = 0
        seen = known.get(str(eid), set())
        slots = []
        if eid in cat:
            slots = table_slots(d, cat[eid][0], cat[eid][1])
        for idx, (off, txt, words) in enumerate(r):
            if (str(eid), str(off)) in have:
                continue
            if txt in seen:      # already exported through the offset table
                continue
            if any(s <= off < e2 for s, e2 in slots):
                continue         # suffix of a string the offset table owns
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
