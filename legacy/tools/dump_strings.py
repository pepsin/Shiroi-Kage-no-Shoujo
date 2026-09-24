#!/usr/bin/env python3
"""Dump text strings from all scene entries + render the global glyph table.

Global glyph table: ROM file offset 0x800000, 1704 glyphs, 16x16 4bpp,
tile order TL,TR,BL,BR (GBA 4bpp 8x8 tiles), 0x80 bytes per glyph.
Text code N -> glyph N. Codes 0x00/0x0D/0x0E are string control codes.

Scene entry layout (after LZ77 decompression):
  u32 header[6]: [0]=0x680 (jump table off), [1]=0x118, [4]=0x7A0 (bytecode off)
  jump table @0x680, bytecode @0x7A0+, then a u16 offset table, then strings.
  Strings are u16 code streams terminated by 0x0000; 0x000D=line break,
  0x000E=page break. We locate the string area heuristically: the offset
  table is the last monotonically-increasing run of u16 before the text.

Outputs:
  <outdir>/strings.txt     - per entry: all strings as hex code streams
  <outdir>/glyphs_used.txt - sorted list of glyph codes used anywhere
  glyphs.png               - sheet of all 1704 glyphs (scaled x2)
"""
import struct, sys, os

def find_text_runs(d):
    """Locate the string pool via the u32 offset table.

    Layout: header(0x20) .. jump table @0x680 .. bytecode @0x7A0 ..
    u32 offset table (byte offsets relative to 0x7A0, monotonic increasing)
    .. string pool (u16 codes, 0000-terminated) .. FFFF padding.
    Returns list of (start, end) byte ranges of the pool (+fragments)."""
    n = len(d)
    u16 = lambda a: struct.unpack('<H', d[a:a + 2])[0]
    u32 = lambda a: struct.unpack('<I', d[a:a + 4])[0]
    BASE = 0x7A0
    best = None
    for align in (0, 2):
        p = BASE + align
        while p < n - 12:
            v0 = u32(p)
            if 0x100 <= v0 <= 0x8000:
                q = BASE + v0
                if q + 2 <= n and q > p and u16(q - 2) == 0x0000 and u16(q) != 0xFFFF:
                    # count monotonic entries
                    m = 1
                    while p + 4 * (m + 1) <= n - 4:
                        v = u32(p + 4 * m)
                        v2 = u32(p + 4 * (m + 1))
                        if v2 > v and v2 - v <= 0x400 and v2 <= 0x8000:
                            m += 1
                        else:
                            break
                    if m >= 3 and (best is None or m > best[0]):
                        best = (m, p, v0)
            p += 4
    if best is None:
        return []
    m, tabp, v0 = best
    pool_start = BASE + v0
    # pool = contiguous run of u16 < 0x6A8 from pool_start
    e = pool_start
    while e + 2 <= n and u16(e) < 0x6A8:
        e += 2
    runs = [(pool_start, e)]
    # collect later fragments (FFFF-gap separated), e.g. short trailing strings
    p = e
    while p < n - 2:
        if u16(p) == 0xFFFF:
            p += 2
            continue
        if u16(p) < 0x6A8:
            q = p
            while q < n - 1 and u16(q) < 0x6A8:
                q += 2
            if q - p >= 4:
                runs.append((p, q))
            p = q
        else:
            p += 2
    return runs

def main():
    resdir = sys.argv[1]
    outdir = sys.argv[2]
    rompath = sys.argv[3]
    os.makedirs(outdir, exist_ok=True)
    used = set()
    nstrings = 0
    with open(f'{outdir}/strings.txt', 'w') as out:
        import glob
        for path in sorted(glob.glob(f'{resdir}/e*.bin')):
            d = open(path, 'rb').read()
            if len(d) < 0x20:
                continue
            h = struct.unpack('<6I', d[:24])
            if not (h[0] == 0x680 and h[1] == 0x118 and h[4] == 0x7A0):
                continue
            eid = int(os.path.basename(path)[1:5])
            runs = find_text_runs(d)
            if not runs:
                continue
            out.write(f'=== entry {eid} ===\n')
            for ts, te in runs:
                if te - ts < 4:
                    continue
                vals = struct.unpack(f'<{(te - ts) // 2}H', d[ts:te])
                cur = []
                for v in vals:
                    if v == 0x0000:
                        if cur:
                            out.write('  ' + ' '.join(f'{c:04X}' for c in cur) + '\n')
                            nstrings += 1
                            for c in cur:
                                if c >= 0x20:
                                    used.add(c)
                            cur = []
                        else:
                            cur = []
                    else:
                        cur.append(v)
                if cur:
                    out.write('  ' + ' '.join(f'{c:04X}' for c in cur) + '\n')
                    nstrings += 1
                    for c in cur:
                        if c >= 0x20:
                            used.add(c)
    with open(f'{outdir}/glyphs_used.txt', 'w') as f:
        f.write(' '.join(f'{c:04X}' for c in sorted(used)) + '\n')
    print(f'strings: {nstrings}, distinct glyph codes used: {len(used)}, max: {max(used) if used else 0:#x}')

    # render global glyph table
    from PIL import Image
    rom = open(rompath, 'rb').read()
    GT = 0x800000
    NG = min(0x35400, len(rom) - GT) // 0x80
    def glyph_pixels(gid):
        b = rom[GT + gid * 0x80: GT + gid * 0x80 + 0x80]
        px = [[0] * 16 for _ in range(16)]
        for t in range(4):
            tx, ty = (t & 1) * 8, (t >> 1) * 8  # TL,TR,BL,BR
            for y in range(8):
                for x in range(4):
                    lo = b[t * 32 + y * 4 + x]
                    px[ty + y][tx + x * 2] = lo & 0xF
                    px[ty + y][tx + x * 2 + 1] = lo >> 4
        return px
    cols = 24
    rows = (NG + cols - 1) // cols
    S = 2
    img = Image.new('L', (cols * 16 * S, rows * 16 * S), 0)
    for gid in range(NG):
        px = glyph_pixels(gid)
        ox, oy = (gid % cols) * 16 * S, (gid // cols) * 16 * S
        for y in range(16):
            for x in range(16):
                v = (15 - px[y][x]) * 17  # inverted brightness
                for dy in range(S):
                    for dx in range(S):
                        img.putpixel((ox + x * S + dx, oy + y * S + dy), v)
    img.save(f'{outdir}/glyphs.png')
    print(f'wrote {outdir}/glyphs.png ({NG} glyphs)')

if __name__ == '__main__':
    main()
