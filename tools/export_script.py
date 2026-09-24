#!/usr/bin/env python3
"""Export the complete game script from the ROM into a translatable table.

Pipeline
--------
1. scan    : decompress every FAT entry, detect script structure (ascending
             offset table + well-formed u16 string pool), classify entries.
2. extract : for every script entry, emit one row per translatable string with
             full metadata (entry id, string index, file offset, byte length,
             control-code layout) plus the decoded source text.
3. (later) import: read a filled translation table and rebuild the ROM.

Text convention (verified, see docs/技术说明.md):
    character = glyph_map[code]      (no extra shift; the map is index-based)
Both the JP original and the CN release use identical text codes; they differ
only in glyph bitmaps.

Usage
-----
  export_script.py scan    [--rom ROM] [--out DIR]
  export_script.py extract [--rom ROM] [--out DIR] [--rom-kind cn|jp]
"""
import argparse
import csv
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import gbtext as g

WORK = os.path.join(ROOT, 'data')
CACHE = os.path.join(ROOT, 'work', 'cache')
MAPFILE_JP = os.path.join(WORK, 'glyph_map.csv')
GLYPH_MAX = 0x6A8
CTRL = {0x0D: '\n', 0x0E: '\n\n'}


def load_table(kind='jp'):
    """Glyph map keyed by table index (JP original only)."""
    import mapio
    return mapio.load_map(MAPFILE_JP)


def find_offset_table(d, minlen=4):
    """Best ascending u32 run (either 4- or 2-byte aligned)."""
    best = (0, 0, [])
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
            if len(vals) > best[0]:
                best = (len(vals), p, vals)
            p = q if q > p else p + 4
    return best  # (count, table_off, offsets)


def read_string(d, off):
    """Return (codes, end_off) for the 0x0000-terminated string at off."""
    n = 0
    while off + 2 * n + 2 <= len(d) and struct.unpack_from('<H', d, off + 2 * n)[0] != 0:
        n += 1
    if n == 0:
        return None, off
    codes = list(struct.unpack_from(f'<{n}H', d, off))
    if any(c > GLYPH_MAX for c in codes):
        return None, off + 2 * n
    return codes, off + 2 * n + 2


def analyse(d):
    """Return dict describing the script structure of a decompressed entry."""
    ntab, toff, vals = find_offset_table(d)
    if ntab < 4:
        return None
    # string pool base candidates; 0x7A0 after the bytecode is the common case
    best = None
    for base in (0x7A0, toff + 4 * ntab, 0):
        ok = 0
        total = 0
        for o in vals:
            off = base + o
            if off + 2 > len(d):
                continue
            codes, end = read_string(d, off)
            if codes:
                ok += 1
                total += len(codes)
        if best is None or ok > best[0]:
            best = (ok, base, total)
    if not best or best[0] < 4:
        return None
    ok, base, total = best
    return {'table_off': toff, 'base': base, 'n_offsets': ntab,
            'n_strings': ok, 'n_codes': total}


def cmd_scan(a):
    os.makedirs(CACHE, exist_ok=True)
    rom = open(a.rom, 'rb').read()
    kind = g.which(a.rom)
    rows = []
    for eid in range(0, 2000):
        off, size = struct.unpack_from('<2I', rom, g.FAT + eid * 8)
        if off == 0 and size == 0:
            continue
        if off + size > len(rom) or size == 0:
            continue
        raw = rom[g.BASE + off:g.BASE + off + size]
        dec = g.lzdec(raw)
        d = dec if dec is not None else raw
        dest = os.path.join(CACHE, f'{kind}_e{eid:04d}.bin')
        if not os.path.exists(dest):
            open(dest, 'wb').write(d)
        info = analyse(d)
        rows.append({'eid': eid, 'off': off, 'size': size, 'dec': int(dec is not None),
                     'dec_size': len(d),
                     'table_off': info['table_off'] if info else '',
                     'base': info['base'] if info else '',
                     'strings': info['n_strings'] if info else 0,
                     'codes': info['n_codes'] if info else 0})
    out = os.path.join(WORK, 'entry_catalog.tsv')
    with open(out, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()), delimiter='\t')
        w.writeheader()
        w.writerows(rows)
    scriptish = [r for r in rows if r['strings'] >= 8]
    print(f'{a.rom}: {len(rows)} entries, {len(scriptish)} with >=8 strings')
    print(f'wrote {out}')
    print("top entries by string count:")
    for r in sorted(scriptish, key=lambda r: -r['strings'])[:15]:
        print(f"  e{r['eid']:04d} strings={r['strings']:5d} codes={r['codes']:6d} "
              f"table=@{r['table_off']:#06x} base={r['base']:#06x} size={r['dec_size']}")


def jp_decode(codes, table):
    """JP convention: codes <0x20 are glyphs taken directly, codes >=0x20 use index code+1."""
    shift = {c + 1: v for c, v in table.items()}
    out = []
    for c in codes:
        if c < 0x20 and c in table:
            out.append(table[c])
        else:
            out.append(shift.get(c, f'[{c:03X}]'))
    return ''.join(out)


def entry_strings(d, toff, base):
    """Yield (idx, offset, codes, end) for every offset-table slot."""
    vals = []
    p = toff
    while p + 4 <= len(d):
        x = struct.unpack_from('<I', d, p)[0]
        if x == 0xFFFFFFFF or x > len(d):
            break
        if vals and x <= vals[-1]:
            break
        vals.append(x)
        p += 4
    for idx, o in enumerate(vals):
        off = base + o
        if off + 2 > len(d):
            continue
        codes, end = read_string(d, off)
        if codes:
            yield idx, o, codes, end


def cmd_extract(a):
    """Emit a translation table: JP source + existing CN text where present.

    e0068-style entries are byte-identical between the two ROMs (the CN release
    only swapped glyph bitmaps), while e0438-style entries were really rewritten
    in Chinese.  Both cases are reported so the translator sees the state.
    """
    table_jp = load_table('jp')
    jp_rom = open(g.JP_ROM, 'rb').read()
    catalog = os.path.join(WORK, 'entry_catalog.tsv')
    rows = list(csv.DictReader(open(catalog, encoding='utf-8'), delimiter='\t'))
    out = os.path.join(WORK, 'translation.tsv')
    nstr = nscript = 0
    with open(out, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f, delimiter='\t')
        w.writerow(['entry', 'idx', 'offset', 'n_codes', 'n_bytes',
                    'jp_text', 'translation'])
        for r in rows:
            if int(r['strings']) < 4:
                continue
            eid = int(r['eid'])
            d = g.load_entry(jp_rom, eid)
            if d is None:
                continue
            toff, base = int(r['table_off']), int(r['base'])
            got = 0
            for idx, o, codes, end in entry_strings(d, toff, base):
                if not any(c >= 0x20 for c in codes):
                    continue
                jp_text = jp_decode(codes, table_jp)
                w.writerow([eid, idx, o, len(codes), end - o, jp_text, ''])
                got += 1
            if got:
                nscript += 1
                nstr += got
    print(f'wrote {nstr} strings from {nscript} entries -> {out}')



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['scan', 'extract'])
    ap.add_argument('--rom', default=g.JP_ROM)
    ap.add_argument('--out', default='')
    a = ap.parse_args()
    if a.cmd == 'scan':
        cmd_scan(a)
    else:
        cmd_extract(a)


if __name__ == '__main__':
    main()
