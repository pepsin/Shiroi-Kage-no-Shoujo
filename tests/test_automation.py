"""Regression tests for failures that used to produce false green results.

All ROMs and emulator outputs here are synthetic; no game assets are needed.
"""
import argparse
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1] / 'tools'
sys.path.insert(0, str(TOOLS))
import assemble_subset as subset
import check_append_guard as guard
import crash_bisect as bisect
import dyn_check as dyn
import entry_id
import gbtext as g
import harness_utils as harness
import state_inspect as si
import romdbg


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.script = self.root / 'emulator.py'
        self.prefix = self.root / 'check.cn'

    def write_emulator(self, before='', after=''):
        self.script.write_text('''import os, sys
from pathlib import Path
''' + before + '''
p = os.environ['GBARUN_RAMSNAP'].split(':', 1)[1]
for ext, size in [('ewram', 262144), ('iwram', 32768), ('vram', 98304)]:
    Path(p + '.' + ext).write_bytes(bytes(size))
Path(os.environ['GBARUN_SHOT']).write_bytes(b'P6\\n240 160\\n255\\n' + bytes(240 * 160 * 3))
''' + after, encoding='utf-8')

    def run_emulator(self, **kwargs):
        return harness.run_harness(sys.executable, self.script, 10, ['A@2:4'],
                                   self.prefix, **kwargs)

    def test_clean_environment_and_complete_evidence(self):
        self.write_emulator(before="assert 'GBARUN_POKE' not in os.environ\n"
                            "assert 'GBARUN_KEYS' not in os.environ\n"
                            "assert 'GBARUN_BIOS' not in os.environ\n"
                            "assert os.environ['GBARUN_NO_AUTOSAVE'] == '1'\n")
        with mock.patch.dict(os.environ, GBARUN_POKE='0:0:00', GBARUN_KEYS='A@0:99',
                             GBARUN_BIOS='old.bin'):
            evidence, _ = self.run_emulator()
        self.assertEqual(len(evidence['ewram']), 0x40000)
        self.assertTrue(Path(str(self.prefix) + '.log').exists())

    def test_failed_exit_cannot_reuse_old_snapshots(self):
        Path(str(self.prefix) + '.ewram').write_bytes(bytes(0x40000))
        self.write_emulator(before='sys.exit(7)')
        with self.assertRaisesRegex(harness.HarnessError, '退出码 7'):
            self.run_emulator()
        self.assertFalse(Path(str(self.prefix) + '.ewram').exists())

    def test_missing_or_short_output_rejected(self):
        for mutation in ("Path(p + '.ewram').unlink()",
                         "Path(p + '.iwram').write_bytes(b'bad')",
                         "Path(os.environ['GBARUN_SHOT']).unlink()"):
            with self.subTest(mutation=mutation):
                self.write_emulator(after=mutation)
                with self.assertRaises(harness.HarnessError):
                    self.run_emulator()

    def test_timeout_is_a_failure(self):
        self.write_emulator(before='import time\ntime.sleep(2)')
        with self.assertRaisesRegex(harness.HarnessError, '超时'):
            self.run_emulator(timeout=0.1)

    def test_state_load_failure_even_with_exit_zero(self):
        self.write_emulator()
        state = self.root / 'start.ss1'
        state.write_bytes(b'not a state')
        with self.assertRaisesRegex(harness.HarnessError, '未能加载 state'):
            self.run_emulator(state=state)

    def test_failed_checkpoint_preserves_previous_save(self):
        self.write_emulator()
        save = self.root / 'checkpoint.ss1'
        save.write_bytes(b'previous')
        with self.assertRaisesRegex(harness.HarnessError, '未能保存'):
            self.run_emulator(save=save)
        self.assertEqual(save.read_bytes(), b'previous')

    def test_checkpoint_written_only_after_successful_run(self):
        self.write_emulator(after="Path(os.environ['GBARUN_SAVE']).write_bytes(b'new checkpoint')\n"
                            "print('[state] wrote checkpoint')\n")
        save = self.root / 'checkpoint.ss1'
        self.run_emulator(save=save)
        self.assertEqual(save.read_bytes(), b'new checkpoint')

    def test_choice_capture_requires_harness_support(self):
        self.write_emulator()
        with self.assertRaisesRegex(harness.HarnessError, '不支持选项截图'):
            self.run_emulator(choice_capture=self.root/'choices')
        self.assertFalse((self.root/'choices').exists())

    def test_choice_capture_validates_fresh_images_and_state(self):
        self.write_emulator(after="d = Path(os.environ['GBARUN_CHOICE_DIR'])\n"
                            "(d/'frame-000001.bin').write_bytes(bytes(0x60400))\n"
                            "(d/'frame-000001.ppm').write_bytes(Path(os.environ['GBARUN_SHOT']).read_bytes())\n"
                            "print('[choices] captured 1')\n")
        target = self.root/'choices'
        self.run_emulator(choice_capture=target)
        self.assertEqual((target/'frame-000001.bin').stat().st_size, 0x60400)
        self.assertTrue((target/'frame-000001.ppm').exists())
        with self.assertRaises(FileExistsError):
            self.run_emulator(choice_capture=target)

    def test_choice_capture_incomplete_output_is_not_success(self):
        for contents in ("print('[choices] captured 1')",
                         "d = Path(os.environ['GBARUN_CHOICE_DIR'])\n"
                         "(d/'frame-000001.bin').write_bytes(b'broken')\n"
                         "(d/'frame-000001.ppm').write_bytes(b'broken')\n"
                         "print('[choices] captured 1')"):
            with self.subTest(contents=contents):
                self.write_emulator(after=contents)
                with self.assertRaises(harness.HarnessError):
                    self.run_emulator(choice_capture=self.root/'choices')
                self.assertFalse((self.root/'choices').exists())

    def test_string_keys_expand_as_tokens(self):
        self.assertEqual(harness.expand_keys('START@0:4 A@10-30:2:10'),
                         ['START@0:4', 'A@10:2', 'A@20:2', 'A@30:2'])
        self.assertEqual(len(harness.expand_keys('A@60-1400:4:20', 1401)), 68)
        self.assertEqual(len(harness.expand_keys('A@60-1399:4:20', 1400)), 67)

    def test_malformed_keys_do_not_hang_or_silently_disappear(self):
        for key in ('X@10:4', 'A@4-8:4:0', 'A@8-4:4', 'A@10:0', 'A@-1:4',
                    'A@10:4:40', 'A@0-5000:4:1', 'typo', 'A@100:4'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                harness.expand_keys([key], 100)


class DynamicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.spec = {'version': 1, 'checks': [
            {'name': name, 'rom': 'cn', 'frames': 100, 'keys': ['A@10:4'],
             'invariants': {'live': True}} for name in ('first', 'second')]}
        self.specpath = self.root / 'checks.json'
        self.specpath.write_text(json.dumps(self.spec))
        self.baseline = self.root / 'baseline.json'
        for name, value in {'SPEC': str(self.specpath), 'BASELINE': str(self.baseline),
                            'OUTDIR': str(self.root / 'out'), 'HARNESS': sys.executable,
                            'ROMS': {'cn': sys.executable, 'jp': sys.executable}}.items():
            patch = mock.patch.object(dyn, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.obs = {'returncode': 0, 'live': True, 'entry': 438, 'rid': '438:0'}
        self.hashes = dict.fromkeys(('ewram', 'iwram', 'vram', 'shot', 'samples'), 'a' * 64)
        runner_patch = mock.patch.object(dyn, 'run_once',
                                         return_value=(self.obs, self.hashes, ''))
        self.runner = runner_patch.start()
        self.addCleanup(runner_patch.stop)

    def run_checks(self, **kwargs):
        args = dict(only='', update=False, strict=False, json=True)
        args.update(kwargs)
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = dyn.cmd_run(argparse.Namespace(**args))
        return code, json.loads(stream.getvalue())

    def test_successful_update_emits_only_json(self):
        code, report = self.run_checks(update=True)
        self.assertEqual(code, 0)
        self.assertTrue(report['baseline_updated'])
        self.assertEqual(self.run_checks(strict=True)[0], 0)

    def test_failed_update_preserves_entire_baseline(self):
        self.run_checks(update=True)
        original = self.baseline.read_bytes()
        self.runner.return_value = ({'error': 'emulator failed'}, {}, '')
        code, report = self.run_checks(update=True)
        self.assertEqual(code, 1)
        self.assertFalse(report['baseline_updated'])
        self.assertEqual(self.baseline.read_bytes(), original)

    def test_partial_update_preserves_unselected_checks(self):
        self.run_checks(update=True)
        old = json.loads(self.baseline.read_text())['checks']['second']
        self.runner.return_value = (self.obs, dict(self.hashes, shot='b' * 64), '')
        code, _ = self.run_checks(update=True, only='first')
        base = json.loads(self.baseline.read_text())
        self.assertEqual(code, 0)
        self.assertEqual(base['checks']['second'], old)
        self.assertEqual(base['checks']['first']['hashes']['cn']['shot'], 'b' * 64)

    def test_unmatched_filter_is_error(self):
        with self.assertRaisesRegex(ValueError, '没有匹配'):
            self.run_checks(only='misspelled')
        self.runner.assert_not_called()

    def test_missing_baseline_fails_in_strict_mode(self):
        code, report = self.run_checks(strict=True)
        self.assertEqual(code, 1)
        self.assertTrue(all(not check['pass'] for check in report['checks']))

    def test_drift_json_matches_exit_code(self):
        self.run_checks(update=True)
        self.runner.return_value = (self.obs, dict(self.hashes, shot='b' * 64), '')
        code, report = self.run_checks(strict=True)
        self.assertEqual(code, 1)
        self.assertEqual(report['failed'], 2)
        self.assertFalse(report['checks'][0]['pass'])
        code, report = self.run_checks()
        self.assertEqual(code, 0)
        self.assertTrue(report['checks'][0]['pass'])

    def test_changed_route_is_detected_even_if_evidence_matches(self):
        self.run_checks(update=True)
        self.spec['checks'][0]['keys'] = ['B@10:4']
        self.specpath.write_text(json.dumps(self.spec))
        self.assertEqual(self.run_checks(strict=True)[1]['failed'], 1)

    def test_missing_rom_fails_independently_of_invariants(self):
        with mock.patch.object(dyn, 'ROMS', {'cn': str(self.root / 'missing.gba')}):
            code, report = self.run_checks(update=True)
        self.assertEqual(code, 1)
        self.assertFalse(self.baseline.exists())
        self.assertIn('不存在', report['checks'][0]['invariants'][0]['detail'])

    def test_missing_differential_values_do_not_compare_equal(self):
        chk = {'invariants': {'same_entry': True}}
        runs = {'jp': {'returncode': 0}, 'cn': {'returncode': 0}}
        self.assertFalse(all(ok for ok, _ in dyn.check_invariants(chk, runs)))
        chk['invariants'] = {'same_typo': True}
        self.assertFalse(all(ok for ok, _ in dyn.check_invariants(chk, runs)))

    def test_true_no_stall_requires_two_changes(self):
        self.assertFalse(dyn._one_invariant('no_stall', True, {'cursor_changes': 1})[0])

    def test_nonzero_exit_is_failure_without_error_field(self):
        self.assertFalse(all(ok for ok, _ in dyn.check_invariants(
            {'invariants': {'live': True}}, {'cn': {'returncode': 7, 'live': True}})))

    def test_invalid_specs_rejected_before_running(self):
        for change in ({'frames': 0}, {'rom': 'typo'}, {'name': '../escape'},
                       {'keys': ['A@0-10:4:0']}, {'sample_every': -1},
                       {'state': 'unversioned.ss1'}, {'invariants': {}},
                       {'invariants': {'same_entry': True}},
                       {'invariants': {'no_stall': True}},
                       {'invariants': {'lve': True}}):
            spec = copy.deepcopy(self.spec)
            spec['checks'][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                dyn.validate_spec(spec)
        spec = copy.deepcopy(self.spec)
        spec['checks'].append(spec['checks'][0])
        with self.assertRaises(ValueError):
            dyn.validate_spec(spec)

    def test_sampling_always_keeps_final_frame(self):
        for frames, every in ((9000, 400), (10_000_000, 1), (1, 1), (20, 50)):
            fs = dyn.sample_frames(frames, every)
            self.assertEqual(fs[-1], frames - 1)
            self.assertLessEqual(len(fs), dyn.MAX_SAMPLES)
            self.assertEqual(fs, sorted(set(fs)))

    def test_missing_samples_fail_even_when_ram_is_present(self):
        # Exercise real run_once parsing, bypassing only emulator execution.
        with mock.patch.object(dyn, 'run_harness', return_value=(
                {ext: bytes(size) for ext, size in harness.RAM_SIZES.items()}, '')):
            obs, hashes, _ = ORIGINAL_RUN_ONCE('case', 'cn', 'unused',
                                              {'frames': 100, 'sample_every': 20}, str(self.root))
        self.assertIn('采样数据缺失', obs['error'])
        self.assertEqual(hashes, {})

    def test_list_and_import_do_not_require_rom(self):
        isolated = self.root / 'isolated'
        (isolated / 'tools').mkdir(parents=True)
        (isolated / 'tests' / 'dyn').mkdir(parents=True)
        for module in ('dyn_check', 'entry_id', 'gbtext', 'state_inspect', 'scene_lookup', 'harness_utils'):
            shutil.copy(TOOLS / (module + '.py'), isolated / 'tools')
        (isolated / 'tests' / 'dyn' / 'checks.json').write_text(json.dumps(self.spec))
        p = subprocess.run([sys.executable, str(isolated / 'tools' / 'dyn_check.py'), 'list'],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('first', p.stdout)


ORIGINAL_RUN_ONCE = dyn.run_once


def tiny_rom():
    rom = bytearray(g.BASE + 128)
    struct.pack_into('<2I', rom, g.FAT + 8, 32, 4)
    rom[g.BASE + 32:g.BASE + 36] = b'base'
    return rom


class RomAndBisectTests(unittest.TestCase):
    def test_original_zero_length_placeholders_are_valid(self):
        base = tiny_rom()
        struct.pack_into('<2I', base, g.FAT + 841 * 8, 40, 0)
        self.assertEqual(guard.scan(base, base)[0], [])
        self.assertEqual(subset.differing_entries(base, base), [])

    def test_same_size_in_place_write_is_candidate(self):
        base = tiny_rom()
        full = bytearray(base)
        full[g.BASE + 32:g.BASE + 36] = b'edit'
        self.assertEqual(subset.differing_entries(base, full), [1])
        result = subset.assemble(base, full, [1])
        self.assertEqual(result, full)
        self.assertEqual(subset.assemble(base, full, []), base)

    def test_relocated_entries_are_aligned_and_guarded(self):
        base = tiny_rom() + b'x'
        full = bytearray(base)
        off = len(full) - g.BASE
        full += b'new text'
        struct.pack_into('<2I', full, g.FAT + 8, off, 8)
        result = subset.assemble(base, full, [1])
        dest, size = subset.entry_record(result, 1)
        self.assertEqual((g.BASE + dest) % 4, 0)
        self.assertEqual(result[g.BASE + dest:g.BASE + dest + size], b'new text')
        self.assertEqual(guard.scan(result, base)[0], [])

    def test_out_of_fat_entry_cannot_overwrite_dispatch_table(self):
        base = tiny_rom()
        for eid in (-1, 1024, 1499):
            with self.subTest(eid=eid), self.assertRaises(ValueError):
                subset.assemble(base, base, [eid])

    def test_truncated_fat_and_entries_fail(self):
        with self.assertRaises(ValueError):
            guard.scan(b'bad', tiny_rom())
        base = tiny_rom()
        struct.pack_into('<2I', base, g.FAT + 8, 999999, 4)
        self.assertTrue(guard.scan(base, tiny_rom())[0])
        with self.assertRaises(ValueError):
            subset.assemble(tiny_rom(), base, [1])

    def test_deleted_entry_does_not_pass_guard_check(self):
        base = tiny_rom()
        full = bytearray(base)
        struct.pack_into('<2I', full, g.FAT + 8, 0, 0)
        self.assertTrue(guard.scan(full, base)[0])

    def test_bad_alignment_and_nonzero_guard_detected(self):
        base = tiny_rom()
        full = bytearray(base)
        struct.pack_into('<2I', full, g.FAT + 8, 65, 4)
        full[g.BASE + 69] = 1
        self.assertEqual({b[1] for b in guard.scan(full, base)[0]}, {'align', 'guard'})

    def test_ddmin_cross_partition_interaction_with_budget_and_cache(self):
        seen = set()
        def fails(items):
            key = tuple(items)
            self.assertNotIn(key, seen)
            seen.add(key)
            return {3, 399} <= set(items)
        result, runs = bisect.ddmin(list(range(454)), fails, max_runs=120, log=lambda _: None)
        self.assertEqual(result, [3, 399])
        self.assertEqual(runs, len(seen))

    def test_ddmin_distinguishes_nonreproducible_and_empty_failure(self):
        self.assertIsNone(bisect.ddmin([1, 2], lambda _: False, log=lambda _: None)[0])
        self.assertEqual(bisect.ddmin([1, 2], lambda _: True, log=lambda _: None)[0], [])

    def test_ddmin_honors_budget(self):
        with self.assertRaises(bisect.Budget):
            bisect.ddmin([1, 2], lambda s: 2 in s, max_runs=1, log=lambda _: None)

    def test_empty_ram_cannot_resolve_an_entry(self):
        idx = mock.Mock()
        idx.candidates.return_value = ([438], False)
        idx.entry.return_value = bytes(2000)
        self.assertIsNone(entry_id.resolve(idx, b'', 30, 1).eid)

    def test_short_prefix_is_not_complete_structural_evidence(self):
        m = entry_id.Match(438, 32, 32, 32, 32, None, [438], False)
        self.assertFalse(m.structural_complete)

    def test_dispatch_key_count_is_not_dialogue_line_count(self):
        ew, iw = bytearray(0x40000), bytearray(0x8000)
        pool = 0x02030300
        struct.pack_into('<6I', ew, 0x3F24, pool, 0, pool - 0x7A0, 0, 1, 1)
        struct.pack_into('<3I', ew, 0x30300 + 16, 100, 120, 140)
        struct.pack_into('<2I', iw, 0x7C40, 26, 140)
        e = si.engine(dyn.Snap(ew, iw, bytes(0x18000)))
        self.assertTrue(e['live'])
        self.assertGreater(e['idx'], e['rows'])

    def test_menu_garbage_line_pointers_are_safe_to_inspect(self):
        for reader in (si.read_row, romdbg.read_row):
            for off in (-100, -1, 4, 0x08000000):
                with self.subTest(reader=reader, off=off):
                    self.assertEqual(reader(bytes(4), off), [])


if __name__ == '__main__':
    unittest.main()
