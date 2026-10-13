"""Fixed output locations must not destroy inputs or collide across runs."""
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import autoplay
import record_video
import revalidate_autoplay
import work_paths


class FixedOutputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name) / 'work'
        patch = mock.patch.object(work_paths, 'WORK', self.work)
        patch.start()
        self.addCleanup(patch.stop)
        self.out = self.work / 'autoplay'

    def test_fresh_run_replaces_only_its_own_artifacts_and_logs(self):
        sibling = self.work / 'scenes/manual.tsv'
        sibling.parent.mkdir(parents=True)
        sibling.write_text('manual edits')
        with contextlib.redirect_stdout(io.StringIO()):
            with work_paths.fixed_run(self.out):
                (self.out / 'obsolete.png').write_bytes(b'old')
                print('old log')
            with work_paths.fixed_run(self.out):
                print('new log')
        self.assertFalse((self.out / 'obsolete.png').exists())
        self.assertEqual((self.out / 'run.log').read_text(), 'new log\n')
        self.assertEqual(sibling.read_text(), 'manual edits')
        self.assertIn('autoplay/inference-review/index.html', (self.work / 'index.html').read_text())

    def test_failure_preserves_evidence_and_resume_appends_log(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(ValueError, 'interrupted'):
                with work_paths.fixed_run(self.out):
                    (self.out / 'checkpoint').write_bytes(b'keep')
                    raise ValueError('interrupted')
            with work_paths.fixed_run(self.out, resume=True):
                print('resumed')
        self.assertEqual((self.out / 'checkpoint').read_bytes(), b'keep')
        log = (self.out / 'run.log').read_text()
        self.assertIn('interrupted', log)
        self.assertIn('resumed', log)

    def test_concurrent_run_cannot_reset_first_run(self):
        with work_paths.fixed_run(self.out):
            (self.out / 'checkpoint').write_bytes(b'keep')
            with self.assertRaisesRegex(ValueError, '正在运行'):
                with work_paths.fixed_run(self.out):
                    self.fail('second writer entered')
            self.assertEqual((self.out / 'checkpoint').read_bytes(), b'keep')

    def test_input_inside_output_is_rejected_before_cleanup(self):
        self.out.mkdir(parents=True)
        source = self.out / 'route.json'
        source.write_text('keep route')
        with self.assertRaisesRegex(ValueError, '输入位于'):
            with work_paths.fixed_run(self.out, inputs=(source,)):
                self.fail('input would be removed')
        self.assertEqual(source.read_text(), 'keep route')

    def test_symlink_and_work_root_cannot_be_reset(self):
        self.work.mkdir()
        target = self.work.parent / 'manual'
        target.mkdir()
        self.out.symlink_to(target, target_is_directory=True)
        for path in (self.out, self.work, self.work / '..' / 'manual'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                with work_paths.fixed_run(path):
                    self.fail('unsafe directory accepted')
        self.assertTrue(target.is_dir())

    def test_public_runners_reject_custom_output_arguments(self):
        for module in (autoplay, revalidate_autoplay, record_video):
            with self.subTest(module=module.__name__), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    module.main(['--out', str(self.work / 'custom')])
                self.assertEqual(error.exception.code, 2)
        self.assertFalse(self.work.exists())


if __name__ == '__main__':
    unittest.main()
