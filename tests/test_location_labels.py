"""Synthetic resource tests: no game ROM, fonts, Pillow or emulator required."""
import json
from pathlib import Path
import random
import struct
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import gbtext
import lz77
import patch_location_labels as labels


class LocationLabelTests(unittest.TestCase):
    def setUp(self):
        self.base = 0x472850
        self.source = bytearray(self.base + 5000)
        struct.pack_into('<II', self.source, gbtext.FAT + 840 * 8,
                         self.base - gbtext.BASE, 5000)
        struct.pack_into('<II', self.source, self.base, 418 * 8, 8)
        struct.pack_into('<II', self.source, self.base + 8, 3456, 0)
        struct.pack_into('<II', self.source, self.base + 16, 4000, 16)
        self.before = b'scene-prefix' + b'japanese-bitmap!'
        encoded = lz77.compress(self.before)
        self.source[self.base + 3456:self.base + 3456 + len(encoded)] = encoded
        struct.pack_into('<I', self.source, self.base + 12, len(encoded))
        self.pixels = b'chinese--bitmap!'
        self.row = dict(kind='status', index=1, relative_pointer=3456, capacity=544,
                        decoded_size=len(self.before), pixel_offset=12,
                        prefix_sha256=labels.digest(self.before[:12]),
                        original_sha256=labels.digest(self.before[12:]),
                        pixels_hex=self.pixels.hex(), pixels_sha256=labels.digest(self.pixels))

    def test_preserves_prefix_addresses_and_every_unowned_byte(self):
        before = bytes(self.source)
        out = labels.patched_copy(before, [self.row])
        self.assertEqual(len(out), len(before))
        data, _, _, _ = labels.resource(out, self.row)
        self.assertEqual(data, self.before[:12] + self.pixels)
        spans = [(self.base + 12, self.base + 16), (self.base + 3456, self.base + 4000)]
        pos = 0
        for start, end in spans:
            self.assertEqual(before[pos:start], out[pos:start])
            pos = end
        self.assertEqual(before[pos:], out[pos:])
        self.assertEqual(labels.patched_copy(out, [self.row]), out)

    def test_check_rejects_untranslated_and_corrupt_background(self):
        with self.assertRaises(ValueError):
            labels.check(self.source, [self.row])
        damaged = dict(self.row, prefix_sha256='0' * 64)
        with self.assertRaises(ValueError):
            labels.patched_copy(self.source, [damaged])
        damaged = dict(self.row, original_sha256='0' * 64)
        with self.assertRaises(ValueError):
            labels.patched_copy(self.source, [damaged])

    def test_refuses_moving_parent_child_neighbor_or_truncated_rom(self):
        for offset in (gbtext.FAT + 840 * 8, self.base + 8, self.base + 16):
            damaged = bytearray(self.source)
            damaged[offset] ^= 4
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                labels.patched_copy(damaged, [self.row])
        with self.assertRaises(ValueError):
            labels.patched_copy(self.source[:self.base], [self.row])

    def test_overflow_never_changes_source(self):
        before = bytes(self.source)
        oversized = b'x' * 545
        with mock.patch.object(lz77, 'compress', return_value=oversized), \
             mock.patch.object(lz77, 'compress_fixed_slot', return_value=oversized), \
             mock.patch.object(gbtext, 'lzdec', side_effect=[self.before, self.before[:12] + self.pixels]):
            with self.assertRaisesRegex(ValueError, 'exceed slot'):
                labels.patched_copy(self.source, [self.row])
        self.assertEqual(self.source, before)

    def test_frozen_assets_and_duplicate_corruption_rejected(self):
        self.assertEqual(len(labels.load_assets()), 58)
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'assets.json'
            for rows in ([self.row, self.row], [dict(self.row, pixels_sha256='0'*64)]):
                p.write_text(json.dumps({'version': 1, 'patches': rows}))
                with self.assertRaises(ValueError):
                    labels.load_assets(p)

    def test_map_patch_preserves_header_and_neighboring_bytes(self):
        start = 0x45A5C0
        before = b'H' * 32 + b'old map pixels!!'
        struct.pack_into('<II', self.source, gbtext.FAT + 712 * 8,
                         start - gbtext.BASE, len(before))
        self.source[start:start + len(before)] = before
        pixels = b'new map pixels!!'
        row = dict(kind='map', index=712, file_start=start, decoded_size=len(before),
                   pixel_offset=32, prefix_sha256=labels.digest(before[:32]),
                   original_sha256=labels.digest(before[32:]),
                   pixels_hex=pixels.hex(), pixels_sha256=labels.digest(pixels))
        out = labels.patched_copy(self.source, [row])
        self.assertEqual(out[:start + 32], self.source[:start + 32])
        self.assertEqual(out[start + 32:start + len(before)], pixels)
        self.assertEqual(out[start + len(before):], self.source[start + len(before):])
        self.assertEqual(labels.patched_copy(out, [row]), out)

    def test_fixed_slot_compressor_roundtrips_including_flag_boundaries(self):
        rng = random.Random(123)
        samples = [bytes(rng.randrange(256) for _ in range(n)) for n in (1, 7, 8, 9, 50, 256)]
        samples += [b'ABCD' * 50, b'A' * 256, bytes(range(256)) * 20]
        for source in samples:
            encoded = lz77.compress_fixed_slot(source)
            self.assertEqual(gbtext.lzdec(encoded), source)
            self.assertLessEqual(len(encoded), len(lz77.compress(source)))
