#!/usr/bin/env python3
"""glyph_map.csv is the single source of truth for the JP glyph map.

CSV format (UTF-8, no header-quoting tricks, one row per table index):

    code,dec,char
    040,64,あ
    041,65,

  * code  : 3-digit uppercase hex table index (0x000..0x6A7, 1704 rows)
  * dec   : decimal table index (readability aid only)
  * char  : the character the glyph stands for; EMPTY = unmapped/unknown

All tools load the map through here; glyph_map.json is retired.
"""
import csv
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSVFILE = os.path.join(ROOT, 'data', 'glyph_map.csv')
GLYPH_COUNT = 0x6A8


def load_map(path=CSVFILE):
    """Return {table_index: char}; rows with an empty char are omitted."""
    m = {}
    with open(path, encoding='utf-8', newline='') as f:
        for row in csv.DictReader(f):
            ch = (row.get('char') or '').strip()
            if ch:
                m[int(row['code'], 16)] = ch
    return m


def save_map(m, path=CSVFILE):
    """Write all GLYPH_COUNT rows; indices missing from m get an empty char."""
    with open(path, 'w', encoding='utf-8', newline='') as f:
        w = csv.writer(f)
        w.writerow(['code', 'dec', 'char'])
        for i in range(GLYPH_COUNT):
            w.writerow([f'{i:03X}', i, m.get(i, '')])
