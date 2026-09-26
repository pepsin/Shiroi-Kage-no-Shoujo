#!/usr/bin/env python3
"""Verify that every byte we changed is inside a string slot we meant to change.

`import_script` rewrites each translated string in place.  A row whose `offset`
is misread (wrong kind, wrong base) or a slot that is really bytecode would
corrupt the entry, and the game then executes garbage - which shows up as a
crash / white screen rather than as wrong text.

This walks every entry, diffs JP against CN, and reports any changed byte that
falls outside the union of the master's string slots for that entry.

Usage:
  verify_writes.py [--cn out.gba] [--jp <rom>] [--max 40]
"""
import argparse
import csv
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import gbtext as g          # noqa: E402

BASE = 0x15C000
FAT = 0x15A000


# Regions we deliberately rewrite outside any FAT-entry string slot:
# the title-menu plate strips (patch_menu_plates.py) and the save/load screen
# plates (patch_save_plates.py).  Two big dummy entries (e684/e1534) happen to
# span that area, so writes there look like out-of-slot changes.
BENIGN = [(0x44BF60, 0x44BF60 + 576), (0x44C1A0, 0x44C1A0 + 576),
          (0x44C3E0, 0x44C3E0 + 576),
          # save screen: FILE-row sprite 2 (プレイ) and sprite 3 (時間)
          (0x44ECE0, 0x44ECE0 + 1024), (0x44F0E0, 0x44F0E0 + 1024),
          # save screen menu items: セーブ / ロード / 消去 (two 32x16 halves each)
          (0x44F5A0, 0x44F5A0 + 512), (0x44F6A0, 0x44F6A0 + 512),
          (0x44F7C0, 0x44F7C0 + 512), (0x44F8C0, 0x44F8C0 + 512),
          (0x44F9E0, 0x44F9E0 + 512), (0x44FAE0, 0x44FAE0 + 512),
          # in-game investigation command menu plates (patch_save_plates.py CMD_PLATES)
          (0x66C400, 0x66C400 + 512), (0x66CA00, 0x66CA00 + 512),
          (0x66CC00, 0x66CC00 + 512), (0x66CE00, 0x66CE00 + 512),
          (0x66D000, 0x66D000 + 512), (0x66D200, 0x66D200 + 512),
          (0x66D600, 0x66D600 + 512), (0x66D800, 0x66D800 + 512)]


def benign(file_off):
    """True when a *file* offset is one we rewrite on purpose."""
    return any(lo <= file_off < hi for lo, hi in BENIGN)


def load_rows(path):
    cat = {}
    for r in csv.DictReader(open(os.path.join(ROOT, 'data', 'entry_catalog.tsv'),
                                 encoding='utf-8'), delimiter='\t'):
        if r.get('base'):
            cat[int(r['eid'])] = int(r['base'])
    slots, texts = {}, {}
    for r in csv.reader(open(path, encoding='utf-8'), delimiter='\t'):
        if not r or r[0] == 'entry':
            continue
        eid, off, n = int(r[0]), int(r[2]), int(r[3]) + 1
        base = cat.get(eid, 0)
        start = off if (len(r) > 7 and r[7] == 'pool') else base + off
        slots.setdefault(eid, []).append((start, start + 2 * n))
        texts.setdefault(eid, []).append((start, (r[6] if len(r) > 6 else '')))
    return slots, texts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cn', default=os.path.join(ROOT, 'out.gba'))
    ap.add_argument('--jp', default=g.JP_ROM)
    ap.add_argument('--master', default=os.path.join(ROOT, 'data', 'translation.tsv'))
    ap.add_argument('--max', type=int, default=40)
    a = ap.parse_args()

    slots, _ = load_rows(a.master)
    jp = open(a.jp, 'rb').read()
    cn = open(a.cn, 'rb').read()

    cat_size = {}
    for r in csv.DictReader(open(os.path.join(ROOT, 'data', 'entry_catalog.tsv'),
                                 encoding='utf-8'), delimiter='\t'):
        cat_size[int(r['eid'])] = int(r['size'] or 0)

    bad_entries = 0
    shown = 0
    total_out = 0
    for eid in range(2000):
        if eid == 850:                    # the font table is rewritten on purpose
            continue
        if not cat_size.get(eid):         # not a real entry (garbage FAT slot)
            continue
        dj = g.load_entry(jp, eid)
        dc = g.load_entry(cn, eid)
        if dj is None or dc is None:
            continue
        if len(dj) > 0x80000:             # not a script entry
            continue
        if len(dj) != len(dc):
            print(f'e{eid}: decompressed size differs JP={len(dj)} CN={len(dc)}')
            bad_entries += 1
            continue
        sp = sorted(slots.get(eid, []))
        # byte i of the entry is file offset BASE + fat_off + i when the entry
        # is uncompressed (dec=0), which is where the plate strips live
        fat_off = struct.unpack_from('<I', cn, FAT + eid * 8)[0]
        uncompressed = dj[:1] != b'\x10'
        outside = []
        for i in range(len(dj)):
            if dj[i] == dc[i]:
                continue
            if any(s <= i < e for s, e in sp):
                continue
            if uncompressed and benign(BASE + fat_off + i):
                continue
            outside.append(i)
        if outside:
            bad_entries += 1
            total_out += len(outside)
            if shown < a.max:
                shown += 1
                spans = []
                for i in outside:
                    if spans and i == spans[-1][1]:
                        spans[-1][1] = i + 1
                    else:
                        spans.append([i, i + 1])
                where = ', '.join(f'{s:#06x}..{e:#06x}' for s, e in spans[:6])
                print(f'e{eid}: {len(outside)} changed bytes OUTSIDE any slot: {where}')
    print(f'entries with changes outside their slots: {bad_entries} '
          f'({total_out} bytes)  slots known for {len(slots)} entries')
    print('RESULT:', 'OK' if bad_entries == 0 else 'PROBLEM')
    return 0 if bad_entries == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
