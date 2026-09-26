#!/usr/bin/env python3
"""Assemble a ROM that takes selected entries from a "full" build.

Used to bisect which entry's rewritten text makes the game crash: the base ROM
has no script writes at all (font/table patch only), while the full ROM has all
of them.  For each entry named on the command line the full ROM's FAT entry and
data are copied in.

Usage:
  assemble_subset.py --base work/font_patched.gba --full out.gba \
                     --entries 1,4,5 --out work/sub.gba
  assemble_subset.py --base ... --full ... --all --out work/all.gba
"""
import argparse
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import gbtext as g          # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', required=True)
    ap.add_argument('--full', required=True)
    ap.add_argument('--entries', default='')
    ap.add_argument('--all', action='store_true')
    ap.add_argument('--out', required=True)
    a = ap.parse_args()

    base = bytearray(open(a.base, 'rb').read())
    full = open(a.full, 'rb').read()
    if a.all:
        eids = [i for i in range(1500) if g.load_entry(full, i) is not None]
    else:
        eids = [int(x) for x in a.entries.split(',') if x.strip()]
    n = 0
    for eid in eids:
        off, size = struct.unpack_from('<2I', full, g.FAT + eid * 8)
        if size == 0 or size > 0x100000:
            continue
        src = g.BASE + off
        data = full[src:src + size]
        if len(data) != size:
            continue
        new_off = len(base) - g.BASE
        base += data
        struct.pack_into('<II', base, g.FAT + eid * 8, new_off, size)
        n += 1
    open(a.out, 'wb').write(bytes(base))
    print(f'{a.out}: {n} entries taken from {os.path.basename(a.full)}, '
          f'size {len(base)}')


if __name__ == '__main__':
    main()
