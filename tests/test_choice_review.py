"""Choice capture must preserve pages, deduplicate highlights and resume safely."""
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from choice_review import ChoiceReview, classify, glyph_sprites, png_from_ppm, PPM_HEADER
from harness_utils import HarnessError


class ChoiceReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        with mock.patch('choice_review.entry_id.RomIndex'):
            self.review = ChoiceReview(self.root / 'choice-review', b'rom', 'route')
        self.review.resolve = lambda _: 318

    def raw(self, name, frame, *, pattern=0x11, color=20, ui=0):
        directory = self.root / name
        directory.mkdir(exist_ok=True)
        data = bytearray(0x60400)
        struct.pack_into('<I', data, 0x15160, ui)
        for i in range(128):
            struct.pack_into('<H', data, 0x60000 + i*8, 0x200)
        for i, y in enumerate((28, 45, 114)):
            struct.pack_into('<4H', data, 0x60000+i*8, y, (1 << 14) | 26, i*4, 0)
            data[0x58000+i*128:0x58000+(i+1)*128] = bytes([pattern])*128
        prefix = directory / f'frame-{frame:06d}'
        prefix.with_suffix('.bin').write_bytes(data)
        prefix.with_suffix('.ppm').write_bytes(PPM_HEADER + bytes([color])*240*160*3)
        return directory

    def test_png_preserves_original_emulator_pixels(self):
        rgb = bytes(range(240))*480
        png = png_from_ppm(PPM_HEADER + rgb)
        cursor, compressed = 8, b''
        while cursor < len(png):
            size = struct.unpack_from('>I', png, cursor)[0]
            if png[cursor+4:cursor+8] == b'IDAT':
                compressed += png[cursor+8:cursor+8+size]
            cursor += size + 12
        scanlines = zlib.decompress(compressed)
        self.assertEqual(b''.join(scanlines[y*721+1:(y+1)*721] for y in range(160)), rgb)
        with self.assertRaises(HarnessError):
            png_from_ppm(PPM_HEADER + b'short')

    def test_inference_requires_visible_list_not_only_question(self):
        self.assertIsNone(classify(0, 318, [(20, 114, b'question')]))
        self.assertEqual(classify(0, 318, [(20, 28, b'a'), (20, 45, b'b')]), 'inference')
        self.assertIsNone(classify(0, 299, [(20, 28, b'a'), (20, 45, b'b')]))
        self.assertIsNone(classify(4, 3, [(20, 114, b'prompt')]))
        self.assertEqual(classify(4, 3, [(26, 130, b'yes')]), 'choice')

    def test_highlights_deduplicate_but_scrolled_pages_are_retained(self):
        self.review.consume(self.raw('one', 40), 1, 0)
        self.review.consume(self.raw('two', 60, pattern=0x77, color=30), 2, 100)
        self.assertEqual(len(self.review.data['screenshots']), 1)
        self.assertEqual(len(self.review.data['screenshots'][0]['occurrences']), 2)
        self.review.consume(self.raw('three', 70, pattern=0x10), 3, 200)
        self.assertEqual(len(self.review.data['screenshots']), 2)
        self.assertEqual(self.review.data['screenshots'][1]['occurrences'][0]['total_frame'], 270)
        self.assertIn('推理选项', (self.root / 'choice-review/index.html').read_text())

    def test_repeated_text_in_different_script_is_kept(self):
        self.review.consume(self.raw('one', 40), 1, 0)
        self.review.resolve = lambda _: 320
        self.review.consume(self.raw('two', 60), 2, 100)
        self.assertEqual(len(self.review.data['screenshots']), 2)

    def test_entry_cache_distinguishes_scripts_with_same_structural_header(self):
        del self.review.resolve
        first = bytearray(0x40000)
        struct.pack_into('<6I', first, 0x3F24, 0x02030AA0, 0, 0x02030300, 0, 1, 21)
        second = bytearray(first)
        second[0x30300 + 0x7A0] = 1
        first, second = bytes(first), bytes(second)
        matches = [SimpleNamespace(eid=e, structural_complete=True, ambiguous=[]) for e in (287, 292)]
        with mock.patch('choice_review.entry_id.resolve_from_ram', side_effect=[(x,) for x in matches]) as resolver:
            self.assertEqual(self.review.resolve(first), 287)
            self.assertEqual(self.review.resolve(second), 292)
            self.assertEqual(self.review.resolve(first), 287)
            self.assertEqual(resolver.call_count, 2)

    def test_resume_rolls_back_uncommitted_segment_and_rejects_tampering(self):
        self.review.consume(self.raw('one', 40), 1, 0)
        self.review.consume(self.raw('two', 50, pattern=0x10), 2, 100)
        with mock.patch('choice_review.entry_id.RomIndex'):
            resumed = ChoiceReview(self.root / 'choice-review', b'rom', 'route', completed=1, resume=True)
        self.assertEqual(len(resumed.data['screenshots']), 1)
        self.assertEqual(len(list((self.root/'choice-review').glob('*.png'))), 1)
        png = self.root / 'choice-review' / resumed.data['screenshots'][0]['file']
        png.write_bytes(b'corrupt')
        with mock.patch('choice_review.entry_id.RomIndex'), self.assertRaises(ValueError):
            ChoiceReview(self.root / 'choice-review', b'rom', 'route', completed=1, resume=True)

    def test_resume_rejects_wrong_route_and_missing_segment(self):
        self.review.consume(self.raw('one', 40), 1, 0)
        with mock.patch('choice_review.entry_id.RomIndex'):
            with self.assertRaises(ValueError):
                ChoiceReview(self.root / 'choice-review', b'rom', 'different', completed=1, resume=True)
            with self.assertRaises(ValueError):
                ChoiceReview(self.root / 'choice-review', b'rom', 'route', completed=2, resume=True)

    def test_empty_gallery_does_not_claim_unseen_screens(self):
        result = self.review.finish(1, 'PARTIAL')
        self.assertEqual(result['screenshots'], 0)
        self.assertEqual(json.loads((self.root/'choice-review/index.json').read_text())['status'], 'PARTIAL')


if __name__ == '__main__':
    unittest.main()
