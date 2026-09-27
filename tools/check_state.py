#!/usr/bin/env python3
"""Tell whether an mGBA savestate was written by a build with the sprite bug.

The game draws text as 16x16 OBJ sprites: it copies a glyph bitmap out of the
font table into four 8x8 VRAM tiles and then points an OAM entry at them.  A
glyph tile is *sparse* - a 16x16 kanji fills at most a few dozen of its 128
bytes, so a tile group that is non-zero in **every single byte** cannot be a
glyph: it is leftover bitmap data that the game never overwrote.

States saved before the 2026-09-27 花屏 fixes contain such sprites: their OAM
pairs the right glyph tiles with the right glyph indices but leaves the
position fields and part of the tile list from an older screen, so the glyphs
are drawn at wrong coordinates / from never-uploaded tiles, in OBJ palette
bank 14 - the "flower" garble.

Because a savestate is a frozen VRAM+OAM+RAM snapshot, loading it into a fixed
ROM reproduces the old picture: the static screens never rebuild their sprites.
Nothing in the ROM can repair it - the fix is to not reuse pre-fix states.

Usage:  check_state.py <state.ssN> ...
Exit code 0 when every state looks clean.
"""
import argparse
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

from render_state import read_state  # noqa: E402

OBJ_BASE = 0x10000
DIMS = {0: {0: (8, 8), 1: (16, 16), 2: (32, 32), 3: (64, 64)}}


def inspect(path):
    reg = read_state(path)
    oam, vram = reg['oam'], reg['vram']
    bad, checked = [], 0
    for i in range(128):
        a0, a1, a2, a3 = struct.unpack_from('<HHHH', oam, i * 8)
        if a0 & 0x200:                      # disabled
            continue
        y, x = a0 & 0xFF, a1 & 0x1FF
        if x >= 240:
            x -= 512
        shape, size = (a0 >> 14) & 3, (a1 >> 14) & 3
        if (shape, size) != (0, 1):         # only 16x16 = one text glyph
            continue
        if not (0 <= x < 240 and y < 160):
            continue
        tile, bank = a2 & 0x3FF, (a2 >> 12) & 0xF
        checked += 1
        group = [vram[OBJ_BASE + (tile + k) * 32:OBJ_BASE + (tile + k) * 32 + 32]
                 for k in range(4)]
        if all(all(b) for b in group):
            bad.append((i, x, y, tile, bank))
    return checked, bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('states', nargs='+')
    a = ap.parse_args()
    rc = 0
    for p in a.states:
        checked, bad = inspect(p)
        name = os.path.basename(p)
        if bad:
            rc = 1
            print(f'{name}: {len(bad)} of {checked} text sprites draw '
                  f'never-uploaded (all-non-zero) tiles -> pre-fix savestate')
            for i, x, y, t, bank in bad[:12]:
                print(f'    oam#{i:3d} at ({x},{y}) tiles {t}..{t + 3} bank {bank}')
        else:
            print(f'{name}: OK ({checked} text sprites, all backed by real glyphs)')
    return rc


if __name__ == '__main__':
    sys.exit(main())
