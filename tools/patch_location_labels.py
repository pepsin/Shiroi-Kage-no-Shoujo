#!/usr/bin/env python3
"""Apply reviewed Chinese location bitmaps, preserving all other scene bytes.

The 4bpp assets are frozen in data/location_labels.json: rebuilding does not
depend on a locally installed font. e840's address is hardcoded in the game;
neither the parent nor its children may move. Reject unknown layouts or an
overflow instead of silently overwriting the next resource.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys

import gbtext
import lz77

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'data/location_labels.json'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def load_assets(path=ASSETS):
    manifest = json.loads(Path(path).read_text())
    if manifest.get('version') != 1 or not manifest.get('patches'):
        raise ValueError('invalid location asset manifest')
    seen = set()
    for r in manifest['patches']:
        key = (r['kind'], r['index'])
        if key in seen or r['kind'] not in ('status', 'map'):
            raise ValueError('duplicate or invalid location resource')
        seen.add(key)
        pixels = bytes.fromhex(r['pixels_hex'])
        if (len(pixels) != r['decoded_size'] - r['pixel_offset']
                or digest(pixels) != r['pixels_sha256']):
            raise ValueError(f'{key}: corrupt bitmap asset')
    return manifest['patches']


def fat_entry(rom, eid):
    if not 0 <= eid < gbtext.FAT_ENTRIES or len(rom) < gbtext.FAT + (eid + 1) * 8:
        raise ValueError('truncated or invalid FAT')
    ptr, size = struct.unpack_from('<II', rom, gbtext.FAT + eid * 8)
    start = gbtext.BASE + ptr
    if ptr % 4 or not size or start + size > len(rom):
        raise ValueError(f'invalid entry e{eid}')
    return start, size


def resource(rom, row):
    """Return decoded data and its owned compressed/file range."""
    index = row['index']
    if row['kind'] == 'map':
        start, size = fat_entry(rom, index)
        if start != row['file_start'] or size != row['decoded_size']:
            raise ValueError(f'e{index}: map resource moved or resized')
        return rom[start:start + size], start, size, None
    base, size = fat_entry(rom, 840)
    if base != 0x472850:
        raise ValueError('e840 must not move: engine has literal references')
    count = struct.unpack_from('<I', rom, base)[0] // 8
    if count != 418 or not 0 <= index < count:
        raise ValueError('unexpected e840 child table')
    ptr, length = struct.unpack_from('<II', rom, base + index * 8)
    capacity = row['capacity']
    if (ptr != row['relative_pointer'] or ptr % 4 or length > capacity
            or ptr < count * 8 or ptr + capacity > size):
        raise ValueError(f'e840/{index}: invalid compressed slot')
    records = [struct.unpack_from('<II', rom, base + i * 8) for i in range(count)]
    if min(p for p, n in records if p > ptr) != ptr + capacity:
        raise ValueError(f'e840/{index}: neighboring resource moved')
    data = gbtext.lzdec(rom[base + ptr:base + ptr + length])
    if data is None:
        raise ValueError(f'e840/{index}: invalid LZ77 stream')
    return data, base + ptr, capacity, base + index * 8 + 4


def validate_payload(data, row, *, translated=False):
    pos = row['pixel_offset']
    key = f"{row['kind']}/{row['index']}"
    if len(data) != row['decoded_size'] or digest(data[:pos]) != row['prefix_sha256']:
        raise ValueError(f'{key}: non-label scene bytes changed')
    pixels = bytes.fromhex(row['pixels_hex'])
    if data[pos:] != pixels and (translated or digest(data[pos:]) != row['original_sha256']):
        raise ValueError(f'{key}: unrecognized or untranslated label')
    return pixels


def check(rom, rows):
    for row in rows:
        data, _, _, _ = resource(rom, row)
        validate_payload(data, row, translated=True)


def patched_copy(source, rows):
    rom = bytearray(source)
    for row in rows:
        data, start, capacity, length_offset = resource(source, row)
        pixels = validate_payload(data, row)
        pos = row['pixel_offset']
        if data[pos:] == pixels:
            continue
        updated = data[:pos] + pixels
        if row['kind'] == 'map':
            rom[start + pos:start + capacity] = pixels
            continue
        encoded = lz77.compress(updated)
        if len(encoded) > capacity:
            encoded = lz77.compress_fixed_slot(updated)
        if gbtext.lzdec(encoded) != updated:
            raise ValueError('LZ77 roundtrip failed')
        if len(encoded) > capacity:
            raise ValueError(f"e840/{row['index']}: {len(encoded)} bytes exceed slot {capacity}")
        rom[start:start + capacity] = encoded + bytes(capacity - len(encoded))
        struct.pack_into('<I', rom, length_offset, len(encoded))
    if len(rom) != len(source):
        raise ValueError('ROM length changed')
    check(rom, rows)
    return bytes(rom)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('rom', type=Path)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--check', action='store_true')
    args = p.parse_args()
    try:
        rows = load_assets()
        source = args.rom.read_bytes()
        if args.apply:
            result = patched_copy(source, rows)
            temporary = args.rom.with_suffix('.location.tmp')
            temporary.write_bytes(result)
            temporary.replace(args.rom)
        else:
            check(source, rows)
    except (OSError, ValueError, KeyError, struct.error) as exc:
        print(f'FAIL: {exc}', file=sys.stderr)
        return 1
    print(f'location labels: {len(rows)} verified; non-label data preserved')
    return 0


if __name__ == '__main__':
    sys.exit(main())
