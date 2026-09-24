#!/usr/bin/env python3
"""Dual-ROM text codec for Tantei Jinguuji Saburou - Shiroi Kage no Shoujo.

Verified conventions (see docs/技术说明.md):

  * Both ROMs share the glyph table at ROM file 0x66F440 (1704 glyphs,
    16x16 4bpp, 0x80 bytes each).
  * The translated CN ROM additionally carries a redrawn table at 0x800000.
  * CN text codes index the table directly:      char = map[code]
  * JP original text codes index the table +1:   char = map[code + 1]

The glyph map (data/glyph_map.csv, loaded via mapio) is keyed by
table index.

Usage:
  gbtext.py detect <rom>                # structural script-entry scan
  gbtext.py decode <rom> <eid> [...]    # decode entries to text
  gbtext.py stats <rom>                 # coverage / kana sanity report
"""
import argparse
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

FAT = 0x15A000
BASE = 0x15C000
GLYPH_MAX = 0x6A8
MAPFILE_JP = os.path.join(ROOT, 'data', 'glyph_map.csv')

# JP original only - the CN release is no longer used anywhere in this project.
JP_ROM = os.path.join(ROOT, 'Tantei Jinguuji Saburou - Shiroi Kage no Shoujo (Japan).gba')

# Code alignment, verified per ROM against real script text:
#   JP original: code C refers to table index C+1  -> shift +1
#     (decodes to 再会するとはな / 深い哀しみを伴った重苦しい空気が)
#   CN release : code C refers to index C          -> shift 0
#     (decodes to 没想到你 / 以这样的形式再次相见 / 四周环绕着香火的气味)
# The maps are per-ROM because the font tables differ (Japanese vs redrawn kanji).
SHIFT = {'jp': 1}


def which(rom_path):
    return 'jp'


def lzdec(src):
    if len(src) < 4 or src[0] != 0x10:
        return None
    size = src[1] | (src[2] << 8) | (src[3] << 16)
    if size == 0 or size > 0x200000:
        return None
    out = bytearray()
    p = 4
    try:
        while len(out) < size:
            flags = src[p]
            p += 1
            for k in range(8):
                if len(out) >= size:
                    break
                if flags & (0x80 >> k):
                    b1, b2 = src[p], src[p + 1]
                    p += 2
                    n = (b1 >> 4) + 3
                    d = (((b1 & 0xF) << 8) | b2) + 1
                    if d > len(out):
                        return None
                    for _ in range(n):
                        out.append(out[-d])
                else:
                    out.append(src[p])
                    p += 1
    except IndexError:
        return None
    return bytes(out)


def load_entry(rom, eid):
    """Return decompressed bytes of FAT entry eid (or None)."""
    off, size = struct.unpack_from('<2I', rom, FAT + eid * 8)
    if off == 0 and size == 0:
        return None
    raw = rom[BASE + off:BASE + off + size]
    dec = lzdec(raw)
    return dec if dec is not None else raw


def load_map(kind='jp'):
    """Load the JP glyph map (CSV is the single source of truth)."""
    import mapio
    return mapio.load_map(MAPFILE_JP)


def build_decoder(rom_kind, gmap):
    sh = SHIFT[rom_kind]
    return {c + sh: v for c, v in gmap.items()}


def decode_units(codes, table):
    out = []
    for c in codes:
        if c == 0x0000:
            out.append('<end>')
        elif c == 0x000D:
            out.append('\n')
        elif c == 0x000E:
            out.append('\n\n')
        elif c in table:
            out.append(table[c])
        elif c < 0x20:
            out.append(f'<{c:02X}>')
        else:
            out.append(f'[{c:03X}]')
    return ''.join(out)


def find_offsets(d, minlen=6):
    """Locate the offset table (ascending u32 run, either 4- or 2-aligned)."""
    best = (0, [])
    for phase in (0, 2):
        p = phase
        while p + 4 * minlen <= len(d):
            vals = []
            q = p
            while q + 4 <= len(d):
                x = struct.unpack_from('<I', d, q)[0]
                if x == 0xFFFFFFFF or x > len(d):
                    break
                if vals and x <= vals[-1]:
                    break
                vals.append(x)
                q += 4
            if len(vals) >= minlen and len(vals) > best[0]:
                best = (len(vals), vals)
            p = q if q > p else p + 4
    return best[1]


def strings_for(d, base=0x7A0):
    vals = find_offsets(d)
    out = []
    for o in vals:
        off = base + o
        if off + 2 > len(d):
            continue
        n = 0
        while off + 2 * n + 2 <= len(d) and struct.unpack_from('<H', d, off + 2 * n)[0] != 0:
            n += 1
        if n >= 2:
            out.append((o, list(struct.unpack_from(f'<{n}H', d, off))))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['detect', 'decode', 'stats'])
    ap.add_argument('rom')
    ap.add_argument('eids', nargs='*')
    ap.add_argument('--out', default='')
    ap.add_argument('--min-str', type=int, default=5)
    a = ap.parse_args()
    rom = open(a.rom, 'rb').read()
    kind = which(a.rom)
    gmap = load_map(kind)
    table = build_decoder(kind, gmap)
    print(f'# ROM: {os.path.basename(a.rom)}  kind={kind}  shift={SHIFT[kind]:+d}', file=sys.stderr)
    if a.cmd == 'decode':
        outdir = a.out or os.path.join(ROOT, 'work', 'dumps', f'script_text_{kind}')
        os.makedirs(outdir, exist_ok=True)
        for eid in a.eids:
            eid = int(eid)
            d = load_entry(rom, eid)
            if d is None:
                print(f'e{eid:04d}: no entry')
                continue
            strs = strings_for(d)
            lines = [f'# {kind} entry {eid:04d} strings={len(strs)}']
            for o, codes in strs:
                lines.append(f'[{o:04X}] ' + decode_units(codes, table))
            open(os.path.join(outdir, f'e{eid:04d}.txt'), 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
            print(f'e{eid:04d}: {len(strs)} strings -> {outdir}')
    elif a.cmd == 'stats':
        import collections
        HIRA = set('ぁあぃいぅうぇえぉおかがきぎくぐけげこごさざしじすずせぜそぞただちぢっつづてでとど'
                   'なにぬねのはばぱひびぴふぶぷへべぺほぼぽまみむめもゃやゅゆょよらりるれろわゐゑをんーっ')
        freq = collections.Counter()
        nstr = 0
        for eid in range(1700):
            d = load_entry(rom, eid)
            if d is None or len(d) > 0x40000:
                continue
            for _, codes in strings_for(d):
                nstr += 1
                for c in codes:
                    if c >= 0x20:
                        freq[table.get(c, '?')] += 1
        kana = sum(n for ch, n in freq.items() if ch in HIRA)
        tot = sum(freq.values())
        print(f'strings={nstr} codes={tot} kana_share={kana / max(tot, 1):.3f}')
        print('top: ' + ' '.join(f'{ch}({n})' for ch, n in freq.most_common(20)))
        miss = sum(n for ch, n in freq.items() if ch.startswith('['))
        print(f'unmapped occurrences: {miss} ({miss / max(tot, 1):.2%})')


if __name__ == '__main__':
    main()
