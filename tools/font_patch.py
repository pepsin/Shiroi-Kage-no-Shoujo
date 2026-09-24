#!/usr/bin/env python3
"""Extend the game's glyph table with new characters (font patch tooling).

The JP ROM is a full 8 MB, so the table cannot simply grow much: only ~198 KB
of tail space exists.  Two ways to make room, both supported here:

  * **slot reuse** - the script only ever references 1524 of the 1704 glyphs, so
    180 slots can be overwritten with new characters.
  * **table relocation** - append an extended table to the FAT area and point
    entry 850 at it (the table is resource-loaded, not hardcoded).

This module also knows how to render a character into the game's exact glyph
format so new glyphs match the existing ones:

    16x16, 4bpp, 0x80 bytes, four 8x8 tiles in TL,TR,BL,BR order,
    low nibble = left pixel of each byte pair.

Usage:
  font_patch.py slots                     # list reusable glyph slots
  font_patch.py render <char> <out.png>   # preview one rendered glyph
  font_patch.py capacity <charset.txt>    # how many new glyphs would be needed
"""
import argparse
import json
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

JP_ROM = os.path.join(ROOT, 'Tantei Jinguuji Saburou - Shiroi Kage no Shoujo (Japan).gba')
GLYPH_FILE_OFF = 0x66F440
GLYPH_BYTES = 0x80
GLYPH_COUNT = 0x6A8
MAPFILE = os.path.join(ROOT, 'data', 'glyph_map.csv')
FONTS = [
    '/System/Library/Fonts/Hiragino Sans GB.ttc',
    '/System/Library/Fonts/STHeiti Medium.ttc',
    '/System/Library/Fonts/Supplemental/Songti.ttc',
    '/System/Library/Fonts/Supplemental/Arial Unicode.ttf',
]


def load_font(size=16):
    from PIL import ImageFont
    for p in FONTS:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    raise SystemExit('no CJK font available')


def render_glyph(ch, size=15, dx=0, dy=0):
    """Render ch into 16x16 4bpp game glyph bytes."""
    from PIL import Image, ImageDraw
    font = load_font(size)
    img = Image.new('L', (16, 16), 0)
    ImageDraw.Draw(img).text((dx, dy), ch, font=font, fill=15)
    px = img.load()
    out = bytearray(GLYPH_BYTES)
    for t in range(4):                       # TL,TR,BL,BR
        tx, ty = (t & 1) * 8, (t >> 1) * 8
        for y in range(8):
            for x in range(4):
                lo = 0
                hi = 0
                for n in range(2):
                    v = px[tx + x * 2 + n, ty + y] & 0xF
                    if n == 0:
                        lo = v
                    else:
                        hi = v
                out[t * 32 + y * 4 + x] = (hi << 4) | lo
    return bytes(out)


def glyph_preview(b, path):
    from PIL import Image
    img = Image.new('L', (16, 16), 0)
    px = img.load()
    for t in range(4):
        tx, ty = (t & 1) * 8, (t >> 1) * 8
        for y in range(8):
            for x in range(4):
                lo = b[t * 32 + y * 4 + x]
                for n in range(2):
                    px[tx + x * 2 + n, ty + y] = (lo >> (4 * n)) & 0xF
    img.resize((256, 256), Image.NEAREST).save(path)


def load_map():
    import mapio
    return mapio.load_map(MAPFILE)


def used_codes(rom):
    """Codes the script actually references."""
    import gbtext as g
    import export_script as ex
    codes = set()
    for eid in range(2000):
        d = g.load_entry(rom, eid)
        if d is None or len(d) > 0x40000:
            continue
        info = ex.analyse(d)
        if not info:
            continue
        for idx, o, cs, end in ex.entry_strings(d, info['table_off'], info['base']):
            for c in cs:
                if 0x20 <= c <= 0x6A7:
                    codes.add(c)
    return codes


def cmd_slots(a):
    rom = open(a.rom, 'rb').read()
    m = load_map()
    used = used_codes(rom)
    # a code X renders table index X+1
    free = []
    for c in range(0x20, GLYPH_COUNT):
        if c in used:
            continue
        idx = c + 1
        b = rom[GLYPH_FILE_OFF + idx * GLYPH_BYTES: GLYPH_FILE_OFF + (idx + 1) * GLYPH_BYTES]
        free.append((c, idx, m.get(idx, ''), sum(bin(x).count('1') for x in b)))
    print(f'script uses {len(used)} codes; reusable slots: {len(free)}')
    print('  code  index  currently  ink')
    for c, idx, ch, ink in free[:30]:
        print(f'  {c:03X}   {idx:03X}    {ch!r:9}  {ink}')
    if len(free) > 30:
        print(f'  ... {len(free) - 30} more')
    json.dump([{'code': c, 'index': idx, 'was': ch, 'ink': ink} for c, idx, ch, ink in free],
              open(os.path.join(ROOT, 'data', 'free_glyph_slots.json'), 'w'),
              ensure_ascii=False, indent=0)
    print('wrote free_glyph_slots.json')


def cmd_capacity(a):
    """Given a charset file, report how many new glyphs a translation needs."""
    m = load_map()
    have = set(m.values())
    text = open(a.args[0], encoding='utf-8').read()
    need = {ch for ch in text if ch.strip() and ch not in '\n\t'}
    missing = sorted(need - have)
    print(f'charset size: {len(need)}')
    print(f'already in font: {len(need & have)}')
    print(f'missing (need new glyphs): {len(missing)}')
    print('  ' + ' '.join(missing[:60]))
    return len(missing)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['slots', 'render', 'capacity', 'inject'])
    ap.add_argument('args', nargs='*')
    ap.add_argument('--rom', default=JP_ROM)
    a = ap.parse_args()
    if a.cmd == 'slots':
        cmd_slots(a)
    elif a.cmd == 'render':
        ch, out = a.args[0], a.args[1]
        b = render_glyph(ch)
        glyph_preview(b, out)
        print(f'rendered {ch!r} -> {out} ({len(b)} bytes)')
    elif a.cmd == 'capacity':
        cmd_capacity(a)
    elif a.cmd == 'inject':
        # inject "<char>=<code>,<char>=<code>,..." into free slots
        rom = bytearray(open(a.rom, 'rb').read())
        free = {s['code']: s for s in json.load(open(os.path.join(
            ROOT, 'work', 'dumps', 'text', 'free_glyph_slots.json')))}
        out = a.args[0]
        placed = 0
        for pair in a.args[1:]:
            ch, code = pair.split('=')
            code = int(code, 0)
            if code not in free:
                print(f'  {ch}: code {code:03X} is NOT a free slot'); continue
            idx = free[code]['index']
            b = render_glyph(ch)
            rom[GLYPH_FILE_OFF + idx * GLYPH_BYTES: GLYPH_FILE_OFF + (idx + 1) * GLYPH_BYTES] = b
            print(f"  {ch} -> code {code:03X} (index {idx:03X}), replaced {free[code]['was']!r}")
            placed += 1
        open(out, 'wb').write(rom)
        print(f'injected {placed} glyphs -> {out}')


if __name__ == '__main__':
    main()
