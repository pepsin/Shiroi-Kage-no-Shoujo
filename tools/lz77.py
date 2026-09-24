#!/usr/bin/env python3
"""LZ77 (GBA BIOS SWI 0x11/0x12 style) compressor for this game's FAT entries.

Entry format: 0x10, size_lo, size_mid, size_hi, then flag bytes (bit 7 first):
    flag bit = 1 -> compressed pair: n = (b1 >> 4) + 3, disp = ((b1 & 0xF) << 8 | b2) + 1
    flag bit = 0 -> literal byte
Round-trip against gbtext.lzdec is asserted by selftest().

Usage: lz77.py compress in.bin out.bin
       lz77.py roundtrip <rom> <eid> [...]
"""
import argparse
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import gbtext as g


def compress(data, max_window=0x1000):
    """GBA LZ77: match length 3..18, displacement 1..0x1000."""
    out = bytearray()
    out.append(0x10)
    n = len(data)
    out += bytes((n & 0xFF, (n >> 8) & 0xFF, (n >> 16) & 0xFF))
    MAXLEN = 18          # (0xF nibble) + 3
    i = 0
    while i < n:
        flag_pos = len(out)
        out.append(0)
        flags = 0
        for bit in range(8):
            if i >= n:
                break
            best_len = 0
            best_disp = 0
            limit = min(MAXLEN, n - i)
            start = max(0, i - max_window)
            j = start
            while j < i:
                l = 0
                while l < limit and data[j + l] == data[i + l]:
                    l += 1
                if l > best_len:
                    best_len = l
                    best_disp = i - j
                    if best_len == limit:
                        break
                j += 1
            if best_len >= 3 and 1 <= best_disp <= max_window:
                enc = ((best_len - 3) << 12) | (best_disp - 1)
                out.append((enc >> 8) & 0xFF)
                out.append(enc & 0xFF)
                flags |= 0x80 >> bit
                i += best_len
            else:
                out.append(data[i])
                i += 1
        out[flag_pos] = flags
    return bytes(out)


def selftest():
    import random
    random.seed(1)
    for trial in range(200):
        n = random.randint(1, 400)
        if trial % 3 == 0:
            data = bytes([random.choice(b'AB')] * n)
        elif trial % 3 == 1:
            data = bytes(random.randrange(256) for _ in range(n))
        else:
            base = bytes(random.randrange(256) for _ in range(random.randint(1, 20)))
            data = (base * (n // len(base) + 1))[:n]
        enc = compress(data)
        dec = g.lzdec(enc)
        if dec != data:
            print(f'FAIL trial {trial}: n={n} got={None if dec is None else len(dec)}')
            return False
    print('selftest passed: 200 random buffers round-trip through lzdec')
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['compress', 'selftest', 'roundtrip', 'sizecheck'])
    ap.add_argument('args', nargs='*')
    a = ap.parse_args()
    if a.cmd == 'selftest':
        sys.exit(0 if selftest() else 1)
    if a.cmd == 'compress':
        data = open(a.args[0], 'rb').read()
        enc = compress(data)
        open(a.args[1], 'wb').write(enc)
        print(f'{a.args[0]} {len(data)} -> {a.args[1]} {len(enc)} '
              f'({len(enc) / max(len(data), 1):.1%})')
        return
    if a.cmd == 'roundtrip':
        rom = open(a.args[0], 'rb').read()
        for eid in a.args[1:]:
            eid = int(eid)
            d = g.load_entry(rom, eid)
            enc = compress(d)
            dec = g.lzdec(enc)
            print(f'e{eid:04d}: raw={len(d)} compressed={len(enc)} '
                  f'roundtrip={"OK" if dec == d else "FAIL"}')
        return
    if a.cmd == 'sizecheck':
        print('unused')


if __name__ == '__main__':
    main()
