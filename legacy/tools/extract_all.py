#!/usr/bin/env python3
"""Extract all FAT resources from the CGP ROM; decompress LZ77 entries.

FAT: ROM 0x15A000, 8-byte entries {offset, size}, base = 0x15C000.
Entry 850 = global glyph table (1704 glyphs, 16x16 4bpp, 0x80 bytes each).
Script entries contain u16 codes: glyph indices into the global table;
control codes: 0x0000=end of string, 0x000D=line break, 0x000E=page break.

Usage: extract_all.py <rom> <outdir>
"""
import struct, sys, os

FAT = 0x15A000
BASE = 0x15C000
NENT = 1735  # last plausible entry index + 1

def lzdec(src):
    if len(src) < 4 or src[0] != 0x10:
        return None
    size = src[1] | (src[2] << 8) | (src[3] << 16)
    if size == 0 or size > 0x200000:
        return None
    out = bytearray(); p = 4
    try:
        while len(out) < size:
            flags = src[p]; p += 1
            for k in range(8):
                if len(out) >= size:
                    break
                if flags & (0x80 >> k):
                    b1, b2 = src[p], src[p + 1]; p += 2
                    n = (b1 >> 4) + 3
                    d = (((b1 & 0xF) << 8) | b2) + 1
                    if d > len(out):
                        return None
                    for _ in range(n):
                        out.append(out[-d])
                else:
                    out.append(src[p]); p += 1
    except IndexError:
        return None
    return bytes(out)

def looks_like_text_region(vals, i):
    """Check if u16 array at i has a run of plausible text codes."""
    n = 0
    while i + n < len(vals) and n < 400:
        v = vals[i + n]
        if v == 0x0000 or v == 0x000D or v == 0x000E:
            n += 1
            continue
        if 0x20 <= v < 0x6A8:  # glyph index into the 1704-entry table
            n += 1
            continue
        break
    return n

def main():
    rom = open(sys.argv[1], 'rb').read()
    outdir = sys.argv[2]
    os.makedirs(outdir, exist_ok=True)
    stats = []
    for i in range(NENT):
        off, size = struct.unpack('<2I', rom[FAT + i * 8:FAT + i * 8 + 8])
        if off == 0 and size == 0:
            continue
        raw = rom[BASE + off:BASE + off + size]
        dec = lzdec(raw)
        data = dec if dec is not None else raw
        with open(f'{outdir}/e{i:04d}.bin', 'wb') as f:
            f.write(data)
        # text detection: longest run of plausible text codes
        vals = struct.unpack(f'<{len(data) // 2}H', data[:len(data) // 2 * 2])
        best = 0; bestat = 0; j = 0
        while j < len(vals):
            n = looks_like_text_region(vals, j)
            if n > best:
                best, bestat = n, j
            j += max(n, 1)
        stats.append((i, bool(dec), len(data), best, bestat * 2))
    with open(f'{outdir}/index.txt', 'w') as f:
        f.write('# id lz size longest_text_run run_offset\n')
        for i, lz, ln, best, bestat in stats:
            f.write(f'{i} {int(lz)} {ln} {best} {bestat:#x}\n')
    texty = [s for s in stats if s[3] >= 12]
    print(f'entries: {len(stats)}, text-like (run>=12): {len(texty)}')
    for i, lz, ln, best, bestat in texty[:60]:
        print(f'  e{i:04d} lz={int(lz)} size={ln} run={best} @{bestat:#x}')

if __name__ == '__main__':
    main()
