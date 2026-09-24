#!/usr/bin/env python3
"""GBA BIOS Huffman (SWI 0x13) decoder.

Stream format (as implemented by the GBA BIOS / mGBA's _unHuffman):

  u32 header  = (decompressedSize << 8) | bitsPerCode
                (also byte 0 holds the signature 0x20/0x24/0x28/0x2C)
  u8  treeSizeByte  -> actual tree size = (treeSizeByte << 1) + 1 bytes
  ... tree data (treeSize bytes) ...
  ... bitstream ...

Tree nodes are bytes:  bit0-5 = offset, bit6 = R terminator, bit7 = L terminator.
"""
import struct


def decode(rom, off, max_out=None):
    """Decode a Huffman stream at `off`. Returns (data, bytes_consumed)."""
    header = struct.unpack_from('<I', rom, off)[0]
    signature = header & 0xFF
    if signature not in (0x20, 0x24, 0x28, 0x2C):
        raise ValueError(f"bad Huffman signature 0x{signature:02X}")
    remaining = header >> 8
    bits = header & 0xF
    if bits == 0:
        bits = 8
    if 32 % bits or bits == 1:
        raise ValueError(f"unsupported Huffman bits {bits}")
    if max_out is not None:
        remaining = min(remaining, max_out)

    tree_size = (rom[off + 4] << 1) + 1
    tree_base = off + 5
    src = off + 5 + tree_size

    out = bytearray()
    block = 0
    bits_seen = 0
    npointer = tree_base
    node = rom[npointer]

    def get8(a):
        return rom[a]

    while remaining > 0:
        bitstream = struct.unpack_from('<I', rom, src)[0]
        src += 4
        for _ in range(32):
            if remaining <= 0:
                break
            nxt = (npointer & ~1) + (node & 0x3F) * 2 + 2
            if bitstream & 0x80000000:
                if node & 0x40:              # R terminator
                    read_bits = get8(nxt + 1)
                else:
                    npointer = nxt + 1
                    node = get8(npointer)
                    bitstream = (bitstream << 1) & 0xFFFFFFFF
                    continue
            else:
                if node & 0x80:              # L terminator
                    read_bits = get8(nxt)
                else:
                    npointer = nxt
                    node = get8(npointer)
                    bitstream = (bitstream << 1) & 0xFFFFFFFF
                    continue

            block |= (read_bits & ((1 << bits) - 1)) << bits_seen
            bits_seen += bits
            npointer = tree_base
            node = get8(npointer)
            if bits_seen == 32:
                bits_seen = 0
                out += struct.pack('<I', block & 0xFFFFFFFF)
                remaining -= 4
                block = 0
            bitstream = (bitstream << 1) & 0xFFFFFFFF

    if remaining < 0:
        out = out[:remaining]
    return bytes(out), src - off


def find_streams(rom, limit=200):
    """Scan for plausible Huffman stream starts (signature 0x20/0x24/0x28/0x2C)."""
    hits = []
    for off in range(0, len(rom) - 6, 4):
        b0 = rom[off]
        if b0 not in (0x20, 0x24, 0x28, 0x2C):
            continue
        size = struct.unpack_from('<I', rom, off)[0] >> 8
        bits = rom[off] & 0xF
        if not (1 <= size <= 0x200000):
            continue
        tree_size = (rom[off + 4] << 1) + 1
        if tree_size < 3 or tree_size > 0x1FF:
            continue
        if off + 5 + tree_size >= len(rom):
            continue
        hits.append((off, size, bits, tree_size))
        if len(hits) >= limit:
            break
    return hits


if __name__ == '__main__':
    import sys
    rom = open("侦探神宫寺三郎 - 白影的少女[CGP](简)(JP)(65.56Mb).gba", 'rb').read()
    hits = find_streams(rom)
    print(f"candidate Huffman streams: {len(hits)}")
    for off, size, bits, tree in hits[:30]:
        print(f"  0x{off:06X} outSize={size:7d} bits={bits:2d} tree={tree:4d}")
