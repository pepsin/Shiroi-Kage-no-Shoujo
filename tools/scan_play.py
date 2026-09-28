#!/usr/bin/env python3
"""Screen a playthrough's memory dumps for text problems.

For every `<prefix>_f<N>_mem.bin` dump this decodes the on-screen text the same
way `sprite_text.py` does (dialogue is drawn as 16x16 OBJ glyphs; static pages
can also be BG tilemaps) and reports:

  * kana        -- hiragana/katakana still on screen = untranslated text
  * unknown     -- glyph bitmap not found in the ROM font table ("□")
  * unmapped    -- glyph index with no entry in glyph_map ("?")

Usage:
  scan_play.py <prefix> [frames...]        # e.g. scan_play.py work/play/nav 800 900
  scan_play.py --glob 'work/play/nav_f*_mem.bin'
  scan_play.py --quiet ...
"""
import argparse
import collections
import csv
import glob
import os
import re
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

FONT_EID = 850
FAT = 0x15A000
BASE = 0x15C000

KANA = re.compile(r'[\u3041-\u309F\u30A0-\u30FF]')
# Full-width katakana / half-width katakana that the game legitimately shows in
# untranslated proper nouns can be allow-listed here.
ALLOW = set()


def load_dump(path):
    """Load a single *_mem.bin (its .idx sits next to it)."""
    idx = path[:-len('_mem.bin')] + '_mem.idx'
    blob = open(path, 'rb').read()
    out = {}
    for line in open(idx, encoding='utf-8'):
        if line.startswith('#'):
            continue
        name, start, size, off = line.split()
        out[name] = blob[int(off, 16):int(off, 16) + int(size, 16)]
    return out


def load_map(path):
    m = {}
    for r in csv.DictReader(open(path, encoding='utf-8')):
        ch = (r.get('char') or '').strip()
        if ch:
            m[int(r['code'], 16)] = ch
    return m


def font_blocks(rom):
    off, size = struct.unpack_from('<2I', rom, FAT + FONT_EID * 8)
    font = rom[BASE + off:BASE + off + size]
    nglyph = size // 0x80
    lookup = {}
    for order in ((0, 2, 1, 3), (0, 1, 2, 3)):
        for g in range(nglyph):
            blk = font[g * 0x80:(g + 1) * 0x80]
            tiles = [blk[0:32], blk[32:64], blk[64:96], blk[96:128]]
            lookup.setdefault(b''.join(tiles[i] for i in order), g)
    return lookup


def obj_lines(reg, lookup, gmap):
    """Visible 16x16 OBJ sprites -> {y: [(x, char_or_marker)]}."""
    vram, oam = reg['vram'], reg['oam']
    rows = collections.defaultdict(list)
    for i in range(128):
        a0, a1, a2, a3 = struct.unpack_from('<HHHH', oam, i * 8)
        if a0 & 0x300:                       # rot/scale or disabled
            continue
        shape, sz = (a0 >> 14) & 3, (a1 >> 14) & 3
        if (shape, sz) != (0, 1):            # only the 16x16 text cell
            continue
        x, y = a1 & 0x1FF, a0 & 0xFF
        if x >= 240:
            x -= 512
        if y >= 160 or x < 0 or x >= 240:
            continue
        tile = a2 & 0x3FF
        blk = vram[0x10000 + tile * 32:0x10000 + tile * 32 + 0x80]
        g = lookup.get(blk)
        if g is None:
            rows[y].append((x, '□'))
        else:
            rows[y].append((x, gmap.get(g) or '?'))
    return rows


def bg_lines(reg, gmap):
    """Visible BG tilemap rows, for static (non-sprite) text pages."""
    io, vram = reg['io'], reg['vram']
    dispcnt = struct.unpack_from('<H', io, 0)[0]
    out = {}
    for bg in range(4):
        if not (dispcnt & (0x100 << bg)):
            continue
        cnt = struct.unpack_from('<H', io, 8 + 2 * bg)[0]
        char_base = ((cnt >> 2) & 3) * 0x4000
        scr_base = ((cnt >> 8) & 0x1F) * 0x800
        hofs = struct.unpack_from('<H', io, 0x10 + 4 * bg)[0] & 0x1FF
        vofs = struct.unpack_from('<H', io, 0x12 + 4 * bg)[0] & 0x1FF
        lines = []
        for gy in range(10):                 # 16x16 glyph rows
            ty = (gy * 2 + vofs // 8) % 32
            row = []
            for gx in range(30):
                tx = (gx + hofs // 8) % 32
                ent = struct.unpack_from('<H', vram, scr_base + (ty * 32 + tx) * 2)[0]
                tile = ent & 0x3FF
                if tile % 4:
                    row.append(' ')
                else:
                    row.append(gmap.get(tile // 4) or ' ')
            lines.append(''.join(row))
        txt = '\n'.join(l.rstrip() for l in lines)
        if sum(1 for c in txt if c.strip()) >= 4:
            out[bg] = lines
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('prefix', nargs='?')
    ap.add_argument('frames', nargs='*', type=int)
    ap.add_argument('--glob')
    ap.add_argument('--rom', default=os.path.join(
        ROOT, '侦探神宫寺三郎 - 白影的少女 (简中).gba'))
    ap.add_argument('--map', default=os.path.join(ROOT, 'work', 'glyph_map.ext.csv'))
    ap.add_argument('--quiet', action='store_true')
    a = ap.parse_args()

    if a.glob:
        dumps = sorted(a.glob and glob.glob(a.glob))
    else:
        if not a.prefix or not a.frames:
            ap.error('need <prefix> <frames...> or --glob')
        dumps = [f'{a.prefix}_f{n}_mem.bin' for n in a.frames]

    rom = open(a.rom, 'rb').read()
    lookup = font_blocks(rom)
    gmap = load_map(a.map)

    bad = 0
    for path in dumps:
        if not os.path.exists(path):
            print(f'MISSING {path}')
            continue
        frame = re.search(r'_f(\d+)_mem\.bin$', path).group(1)
        reg = load_dump(path)
        labels = {}
        for y in sorted(obj_lines(reg, lookup, gmap)):
            items = sorted(obj_lines(reg, lookup, gmap)[y])
            labels[y] = ''.join(c for _, c in items)
        bg = bg_lines(reg, gmap)

        problems = []
        for y, line in labels.items():
            kana = KANA.findall(line)
            if kana and not set(kana) <= ALLOW:
                problems.append(f'kana(OBJ y={y}): {line}')
            if '□' in line:
                problems.append(f'unknown glyph(OBJ y={y}): {line}')
            if '?' in line:
                problems.append(f'unmapped glyph(OBJ y={y}): {line}')
        for b, lines in bg.items():
            if len(KANA.findall(''.join(lines))) >= 15:
                # The resident glyph atlas sits in a BG tilemap too; it is not
                # text the player reads, so never report it as untranslated.
                continue
            for i, line in enumerate(lines):
                if not line.strip():
                    continue
                kana = KANA.findall(line)
                if kana and not set(kana) <= ALLOW:
                    problems.append(f'kana(BG{b} row{i}): {line.strip()}')

        tag = 'OK ' if not problems else 'BAD'
        if problems or not a.quiet:
            print(f'[{tag}] {os.path.basename(path)}')
            for y, line in sorted(labels.items()):
                if line.strip():
                    print(f'       y={y:3d} | {line}')
            for p in problems:
                print(f'    !! {p}')
        if problems:
            bad += 1
    print(f'\n{len(dumps)} dumps scanned, {bad} with problems')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
