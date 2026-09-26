#!/usr/bin/env python3
"""Collect displayable script runs the master never captured and add them.

`extract_pool.scan_pools` drops short groups of accepted runs, so lines like
e1's `」ああ。大丈夫だ“““『` were never exported and stayed Japanese in game.

A run only counts when it is a *primary* run (starts right after a 0x0000
terminator - the scanner's garbage comes from restarting mid-run) and its
neighbours decode to real glyphs (bitmap data gives nonsense words).  Runs
whose text already exists in the master get that translation; the rest are
written with an empty translation for the next translation pass.

Usage:
  add_missing_rows.py --dry-run          # report only
  add_missing_rows.py                    # append rows to data/translation.tsv
  add_missing_rows.py --list out.tsv     # write a batch file for translators
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

HIRA = re.compile(r'[\u3040-\u309f]')
KANA = re.compile(r'[\u3040-\u30ff]')
ASCII = re.compile(r'[A-Za-z0-9]')
SKIP = {850, 851, 852, 605, 547, 571, 611, 636, 1489}


def load_master(path):
    cat = {}
    for r in csv.DictReader(open(os.path.join(ROOT, 'data', 'entry_catalog.tsv'),
                                 encoding='utf-8'), delimiter='\t'):
        if r.get('base'):
            cat[int(r['eid'])] = int(r['base'])
    spans, texts, tmap, maxidx = {}, {}, {}, {}
    for r in csv.reader(open(path, encoding='utf-8'), delimiter='\t'):
        if not r or r[0] == 'entry':
            continue
        eid, idx, off, n = int(r[0]), int(r[1]), int(r[2]), int(r[3]) + 1
        base = cat.get(eid, 0)
        start = off if (len(r) > 7 and r[7] == 'pool') else base + off
        spans.setdefault(eid, []).append((start, start + 2 * n))
        texts.setdefault(eid, set()).add(r[5])
        if len(r) > 6 and r[6].strip():
            tmap.setdefault(r[5], r[6])
        maxidx[eid] = max(maxidx.get(eid, 0), idx)
    for eid in spans:
        spans[eid].sort()
    return cat, spans, texts, tmap, maxidx


def ok_word(m, w):
    if w == 0 or w < 0x20:
        return True
    return (w - 1 if w >= 0x20 else w) in m


def covered(spans, off):
    for s, e in spans:
        if s <= off < e:
            return True
        if s > off:
            break
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--master', default=os.path.join(ROOT, 'data', 'translation.tsv'))
    ap.add_argument('--rom', default=g.JP_ROM)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--list', metavar='TSV', help='write the new rows for translation')
    ap.add_argument('--min-len', type=int, default=2)
    a = ap.parse_args()

    dec = mapio.load_map(os.path.join(ROOT, 'data', 'glyph_map.csv'))
    rom = open(a.rom, 'rb').read()
    cat, spans, texts, tmap, maxidx = load_master(a.master)
    script_entries = set(spans)

    cand = []
    for eid in sorted(script_entries | {1, 2, 3, 4, 5, 6, 7, 9, 10, 11}):
        if eid in SKIP:
            continue
        d = g.load_entry(rom, eid)
        if not d or len(d) > 0x40000:
            continue
        for par in (0, 1):
            i = par
            while i + 1 < len(d):
                if struct.unpack_from('<H', d, i)[0] == 0:
                    i += 2
                    continue
                prev = struct.unpack_from('<H', d, i - 2)[0] if i >= 2 else 0
                start = i
                buf = []
                while i + 1 < len(d):
                    c = struct.unpack_from('<H', d, i)[0]
                    if c == 0:
                        break
                    ch = dec.get(c - 1 if c >= 0x20 else c)
                    if ch is None:
                        break
                    buf.append(ch)
                    i += 2
                txt = ''.join(buf)
                nxt = start + 2 * (len(buf) + 1)
                good = (prev == 0 and nxt + 2 <= len(d)
                        and struct.unpack_from('<H', d, start + 2 * len(buf))[0] == 0
                        and a.min_len <= len(buf) <= 20
                        and not re.search(r'[X6PCN]', txt)
                        and start >= 4 and ok_word(dec, struct.unpack_from('<H', d, start - 4)[0])
                        and ok_word(dec, struct.unpack_from('<H', d, nxt)[0])
                        and not covered(spans.get(eid, []), start)
                        and txt not in texts.get(eid, set()))
                if good:
                    # kana lines are dialogue; kanji-only lines only count as
                    # menu/label text when they contain no ASCII digits/letters
                    # (those are the bitmap-noise signature)
                    if HIRA.search(txt) or (KANA.search(txt) is None and not ASCII.search(txt)):
                        cand.append((eid, start, len(buf), txt))
                i = start + 2

    seen = set()
    rows = []
    for eid, off, n, txt in sorted(cand):
        if (eid, off) in seen:
            continue
        seen.add((eid, off))
        rows.append((eid, off, n, txt))

    known = [r for r in rows if r[3] in tmap]
    unknown = [r for r in rows if r[3] not in tmap]
    print(f'candidates: {len(rows)}  (already translated elsewhere: {len(known)}, '
          f'need translation: {len(unknown)})')
    print(f'chars to translate: {sum(len(r[3]) for r in unknown)}')
    if a.list:
        with open(a.list, 'w', encoding='utf-8', newline='') as f:
            w = csv.writer(f, delimiter='\t', lineterminator='\n')
            w.writerow(['entry', 'offset', 'n_codes', 'jp_text'])
            for eid, off, n, txt in unknown:
                w.writerow([eid, off, n, txt])
        print(f'wrote {a.list}')

    if a.dry_run:
        return
    # append to the master, preserving CRLF
    raw = open(a.master, 'rb').read().decode('utf-8')
    lines = raw.split('\r\n')
    while lines and lines[-1] == '':
        lines.pop()
    for eid, off, n, txt in rows:
        idx = maxidx.get(eid, 0) + 1
        maxidx[eid] = idx
        cn = tmap.get(txt, '')
        lines.append('\t'.join([str(eid), str(idx), str(off), str(n), str(2 * (n + 1)),
                                txt, cn, 'pool']))
    open(a.master, 'wb').write(('\r\n'.join(lines) + '\r\n').encode('utf-8'))
    print(f'appended {len(rows)} rows to {a.master} '
          f'({len(rows) - len(unknown)} with a known translation)')


if __name__ == '__main__':
    main()
