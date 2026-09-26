#!/usr/bin/env python3
"""Summarise the DMA transfers a trace logged (see tools/trace_draw.sh).

Reads /tmp/gbarun_mem.txt (GBARUN_LOGSTORE on 0x040000B0-0x040000E0) and pairs
the three register writes of each transfer, printing the ones that touch OBJ
VRAM (0x06010000-0x06018000) - i.e. whoever fills the sprite tile area, and
where the bytes come from.
"""
import re
import sys

SRC, DST, CNT = 0x040000B0, 0x040000B4, 0x040000B8
pat = re.compile(r'ST32 a=0x([0-9A-F]+) v=0x([0-9A-F]+) pc=0x([0-9A-F]+).*? f=(\d+)')


def main():
    cur = {}
    out = []
    for line in open('/tmp/gbarun_mem.txt'):
        m = pat.search(line)
        if not m:
            continue
        addr, val, pc = (int(m.group(i), 16) for i in (1, 2, 3))
        fr = int(m.group(4))
        cur[addr] = (val, pc, fr)
        if addr == CNT:
            s = cur.get(SRC, (0, 0, 0))[0]
            d = cur.get(DST, (0, 0, 0))[0]
            c = cur[CNT][0]
            n = c & 0xFFFF
            words = n if not (c & 0x01000000) else 0
            if 0x06010000 <= d < 0x06018000 or d == 0:
                if 0x06010000 <= d < 0x06018000:
                    tile = (d - 0x06010000) // 32
                    out.append((fr, s, d, n, tile, cur[CNT][1]))
    print(f'{len(out)} DMA transfers into OBJ VRAM')
    seen = set()
    for fr, s, d, n, tile, pc in out:
        key = (s >> 12, tile)
        if key in seen:
            continue
        seen.add(key)
        print(f'  f={fr:5d} src=0x{s:08X} dst=0x{d:08X} n={n:5d} tile={tile:5d} pc=0x{pc:08X}'
              + ('   <- looks like a ROM source' if 0x08000000 <= s < 0x0A000000 else ''))


if __name__ == '__main__':
    sys.exit(main())
