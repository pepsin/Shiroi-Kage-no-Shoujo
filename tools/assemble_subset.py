#!/usr/bin/env python3
"""Assemble a ROM that takes selected entries from a "full" build.

Used to bisect which entry's rewritten text makes the game crash: the base ROM
has no script writes at all (font/table patch only), while the full ROM has all
of them.  For each entry named on the command line the full ROM's FAT entry and
data are copied in.

In-place writes retain their original addresses. Relocated entries are appended
at the end of the base ROM and satisfy the build's tools/import_script.py rules:

  * the destination must be 4-byte aligned — the BIOS LZ77 SWI requires it, and
    misalignment silently breaks decompression (the "花形乱码字" bug);
  * 32 zero bytes (APPEND_GUARD) must follow the data — the game reads a little
    past the end of an entry, and without the guard that read lands in another
    entry's data, wipes EWRAM and forces blank (docs/技术说明.md 第十节).

Without these a subset ROM would crash even when the full build is fine, i.e.
exactly the false positive the bisect must not have.

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

APPEND_GUARD = 32           # 与 tools/import_script.py 的 APPEND_GUARD 保持一致


def entry_record(rom, eid):
    if not 0 <= eid < g.FAT_ENTRIES:
        raise ValueError(f'条目 {eid} 越出 FAT（0..{g.FAT_ENTRIES - 1}）')
    if len(rom) < g.BASE:
        raise ValueError('ROM 不含完整 FAT')
    off, size = struct.unpack_from('<2I', rom, g.FAT + eid * 8)
    if (off, size) != (0, 0) and g.BASE + off + size > len(rom):
        raise ValueError(f'e{eid} 的数据范围无效')
    return off, size


def differing_entries(base, full):
    """FAT equality does not imply content equality (same-size in-place writes)."""
    out = []
    for eid in range(g.FAT_ENTRIES):
        a, b = entry_record(base, eid), entry_record(full, eid)
        if b == (0, 0):
            if a != b:
                raise ValueError(f'e{eid} 被删除，不能用于条目改写二分')
            continue
        ao, az = a
        bo, bz = b
        if a != b or base[g.BASE + ao:g.BASE + ao + az] != full[g.BASE + bo:g.BASE + bo + bz]:
            out.append(eid)
    return out


def assemble(base_rom, full, eids):
    base = bytearray(base_rom)
    for eid in sorted(set(eids)):
        off, size = entry_record(full, eid)
        old_off, _ = entry_record(base_rom, eid)
        if size == 0:
            raise ValueError(f'e{eid} 是空条目')
        data = full[g.BASE + off:g.BASE + off + size]
        if off == old_off:
            # Keep in-place writes at their original address, including resources
            # that the engine references directly instead of through the FAT.
            if g.BASE + off + size > len(base):
                raise ValueError(f'e{eid} 的原槽超出参照 ROM')
            base[g.BASE + off:g.BASE + off + size] = data
            new_off = off
        else:
            base += b'\x00' * (-len(base) % 4)
            new_off = len(base) - g.BASE
            base += data + b'\x00' * APPEND_GUARD
        struct.pack_into('<II', base, g.FAT + eid * 8, new_off, size)
    return bytes(base)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', required=True)
    ap.add_argument('--full', required=True)
    ap.add_argument('--entries', default='')
    ap.add_argument('--all', action='store_true')
    ap.add_argument('--out', required=True)
    a = ap.parse_args()

    base = open(a.base, 'rb').read()
    full = open(a.full, 'rb').read()
    if a.all:
        eids = [i for i in range(g.FAT_ENTRIES) if entry_record(full, i)[1] > 0]
    else:
        eids = [int(x) for x in a.entries.split(',') if x.strip()]
    result = assemble(base, full, eids)
    with open(a.out, 'wb') as f:
        f.write(result)
    print(f'{a.out}: {len(set(eids))} entries taken from {os.path.basename(a.full)}, '
          f'size {len(result)}')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError) as exc:
        print(f'!! {exc}', file=sys.stderr)
        sys.exit(2)
