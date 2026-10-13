"""Autoplay must reject corrupt routes, wrong ROMs, stale or forged progress."""
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import autoplay
import export_autoplay
import scene_lookup
from harness_utils import expand_keys, HarnessError


class AutoplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.rom = self.root / 'test.gba'
        self.rom.write_bytes(b'rom')
        self.evidence = {key: key.encode() for key in ('ewram', 'iwram', 'vram', 'ppm')}
        self.segment = {'frames': 100, 'keys': ['A@5:4'],
                        'sha256': {k: autoplay.digest(v) for k, v in self.evidence.items()}}
        self.route = {'version': 1, 'status': 'complete',
                      'rom_sha256': autoplay.digest(b'rom'),
                      'segments': [copy.deepcopy(self.segment) for _ in range(2)],
                      'ending': {'description': 'synthetic ending',
                                 'ppm_sha256': autoplay.digest(b'ppm')}}

    def run_route(self, route=None, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return autoplay.replay(route or self.route, 'route-hash', self.rom, 'harness',
                                   self.root / 'out', **kwargs)

    def fake_harness(self, harness, rom, frames, keys, prefix, *, state, save, timeout):
        Path(save).write_bytes(b'checkpoint')
        for k, v in self.evidence.items():
            Path(str(prefix) + '.' + k).write_bytes(v)
        return self.evidence, ''

    def test_complete_requires_verified_terminal_image(self):
        path = self.root / 'route.json'
        path.write_text(json.dumps(self.route))
        autoplay.load_route(path)
        del self.route['ending']
        path.write_text(json.dumps(self.route))
        with self.assertRaises(ValueError):
            autoplay.load_route(path)

    def test_bad_keys_partial_evidence_and_boundary_crossing_rejected(self):
        for mutation in ({'keys': ['A@100:4']}, {'keys': ['A@99:4']},
                         {'keys': ['POKE@5:4']}, {'sha256': {'ppm': '0'*64}}):
            route = copy.deepcopy(self.route)
            route['segments'][0].update(mutation)
            path = self.root / 'route.json'
            path.write_text(json.dumps(route))
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                autoplay.load_route(path)

    def test_partial_cannot_report_clear(self):
        self.route['status'] = 'partial'
        del self.route['ending']
        with mock.patch.object(autoplay, 'run_harness', side_effect=self.fake_harness):
            self.assertEqual(self.run_route()['status'], 'PARTIAL')

    def test_failed_segment_resume_starts_from_last_verified_segment(self):
        calls = []
        def fail_second(*args, **kwargs):
            calls.append(kwargs['state'])
            if len(calls) == 2:
                raise HarnessError('simulated interruption')
            return self.fake_harness(*args, **kwargs)
        with mock.patch.object(autoplay, 'run_harness', side_effect=fail_second):
            with self.assertRaises(HarnessError):
                self.run_route()
        self.assertFalse((self.root / 'out/result.json').exists())
        with mock.patch.object(autoplay, 'run_harness', side_effect=self.fake_harness) as runner:
            self.assertEqual(self.run_route(resume=True)['status'], 'CLEAR')
            self.assertEqual(runner.call_count, 1)
            self.assertEqual(runner.call_args.kwargs['state'].name, 'segment-0001.ss1')

    def test_mismatch_does_not_advance_checkpoint(self):
        self.route['segments'][0]['sha256']['ewram'] = '0'*64
        with mock.patch.object(autoplay, 'run_harness', side_effect=self.fake_harness):
            with self.assertRaises(HarnessError):
                self.run_route()
        self.assertFalse((self.root / 'out/progress.json').exists())
        self.assertFalse((self.root / 'out/result.json').exists())

    def test_wrong_rom_and_modified_checkpoint_rejected(self):
        self.rom.write_bytes(b'changed')
        with self.assertRaises(ValueError):
            self.run_route()
        self.rom.write_bytes(b'rom')
        with mock.patch.object(autoplay, 'run_harness', side_effect=self.fake_harness):
            self.run_route()
        (self.root / 'out/segment-0002.ss1').write_bytes(b'tampered')
        with self.assertRaises(ValueError):
            self.run_route(resume=True)
        self.assertFalse((self.root / 'out/result.json').exists())

    def test_compaction_preserves_order_and_timing(self):
        events = [('A', 1, 4), ('A', 9, 4), ('A', 17, 4), ('B', 17, 2), ('A', 50, 4)]
        self.assertEqual(expand_keys(export_autoplay.compact(events)),
                         [f'{k}@{f}:{h}' for k, f, h in events])


class SceneOffsetTests(unittest.TestCase):
    def test_engine_index_does_not_alias_translation_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p / 'master.tsv').write_text(
                'entry\tidx\toffset\tn_codes\tn_bytes\tjp_text\ttranslation\n'
                '2\t3\t5951\t15\t32\tJP\tCN\tpool\n'
                '438\t0\t450\t5\t1964\tJP2\tCN2\n')
            (p / 'lines.tsv').write_text(
                'rid\tentry\tidx\tscene\tjp_text\ttranslation\n'
                '2:3\t2\t3\t2.0\tJP\tCN\n'
                '438:0\t438\t0\t438.0\tJP2\tCN2\n')
            lookup = scene_lookup.Lookup(lines_path=p/'lines.tsv', master_path=p/'master.tsv')
            self.assertEqual(lookup.at_offset(2, 3999)['rid'], '2:3')
            self.assertEqual(lookup.at_offset(438, 450)['rid'], '438:0')
            self.assertIsNone(lookup.at_offset(2, 4000))
            self.assertIsNone(lookup.scene_of(2, 4))
