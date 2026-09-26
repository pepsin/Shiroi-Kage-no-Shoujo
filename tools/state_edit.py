#!/usr/bin/env python3
"""Read/modify the `gbAs` chunk of an mGBA PNG savestate.

The state payload is a zlib stream wrapping a 0x61000-byte GBASerializedState.
mGBA refuses to load a state whose stored BIOS checksum differs from the
loaded BIOS, so tests against a real BIOS need that field patched.

Usage:
  state_edit.py info <state.ssN>
  state_edit.py setbios <in.ssN> <out.ssN> <crc32hex>
  state_edit.py patch <in.ssN> <out.ssN> <offset_hex>:<hexbytes> ...
"""
import binascii
import struct
import sys
import zlib


def parse(path):
    d = open(path, 'rb').read()
    out = []
    i = 8
    while i + 8 <= len(d):
        ln = struct.unpack('>I', d[i:i + 4])[0]
        typ = d[i + 4:i + 8]
        out.append((typ, d[i + 8:i + 8 + ln]))
        i += 12 + ln
    return out


def write(path, chunks):
    buf = bytearray(b'\x89PNG\r\n\x1a\n')
    for typ, data in chunks:
        buf += struct.pack('>I', len(data)) + typ + data
        buf += struct.pack('>I', binascii.crc32(typ + data) & 0xFFFFFFFF)
    open(path, 'wb').write(bytes(buf))


def main():
    cmd = sys.argv[1]
    chunks = parse(sys.argv[2])
    if cmd == 'info':
        for typ, data in chunks:
            if typ == b'gbAs':
                raw = zlib.decompress(data)
                print('gbAs payload', len(raw))
                print('  versionMagic 0x%08X' % struct.unpack_from('<I', raw, 0)[0])
                print('  biosChecksum 0x%08X' % struct.unpack_from('<I', raw, 4)[0])
                print('  romCrc32     0x%08X' % struct.unpack_from('<I', raw, 8)[0])
                print('  title        ', raw[0x10:0x1C])
                print('  PC           0x%08X' % struct.unpack_from('<I', raw, 0x20 + 15 * 4)[0])
            else:
                print(typ.decode('latin1'), len(data))
        return
    out = []
    for typ, data in chunks:
        if typ == b'gbAs':
            raw = bytearray(zlib.decompress(data))
            if cmd == 'setbios':
                struct.pack_into('<I', raw, 4, int(sys.argv[4], 16))
            elif cmd == 'patch':
                for spec in sys.argv[4:]:
                    off, hexs = spec.split(':')
                    o = int(off, 16)
                    b = bytes.fromhex(hexs)
                    raw[o:o + len(b)] = b
            data = zlib.compress(bytes(raw), 9)
        out.append((typ, data))
    write(sys.argv[3], out)
    print('wrote', sys.argv[3])


if __name__ == '__main__':
    main()
