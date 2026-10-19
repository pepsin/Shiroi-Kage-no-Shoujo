"""Missed-instance coverage must not be hidden by another identical string."""
import csv
from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from find_missing_structured import master_slots, missing_strings, table_strings, unreviewed


def script(odd=False):
    data = bytearray(0x900)
    toff = 0x7C0 + int(odd)
    starts = [0x820 + int(odd) + 6*i for i in range(4)]
    for i, off in enumerate(starts):
        struct.pack_into('<I', data, toff + i*4, off - 0x7A0)
        struct.pack_into('<3H', data, off, 0x21, 0x22, 0)
    struct.pack_into('<I', data, toff+16, 0xFFFFFFFF)
    return bytes(data), starts


class ScriptCoverageTest(unittest.TestCase):
    def test_identical_text_at_another_offset_is_missing(self):
        data, starts = script()
        rows = list(missing_strings(115, data, {(115, starts[0])}, {0x20:'日',0x21:'文'}))
        self.assertEqual([r[2] for r in rows], starts[1:])

    def test_identical_text_in_another_entry_is_missing(self):
        data, starts = script()
        rows = list(missing_strings(115, data, {(182, x) for x in starts}, {0x20:'日',0x21:'文'}))
        self.assertEqual(len(rows), 4)

    def test_odd_table_and_strings_are_referenced(self):
        data, starts = script(odd=True)
        self.assertEqual([r[1] for r in table_strings(data)], starts)

    def test_header_numbers_are_not_a_text_table(self):
        data = bytearray(0x900)
        struct.pack_into('<5I', data, 0, 0, 10, 20, 30, 0xFFFFFFFF)
        self.assertEqual(list(table_strings(data)), [])

    def test_pool_and_table_offsets_share_absolute_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'master.tsv'
            path.write_text('entry\tidx\toffset\tn_codes\tn_bytes\tjp_text\ttranslation\n'
                            '1\t0\t128\t2\t6\t日文\t中文\n'
                            '2\t0\t2080\t2\t6\t日文\t中文\tpool\n')
            self.assertEqual(master_slots(path), {(1,2080),(2,2080)})

    def test_allowlist_does_not_hide_new_instance_or_changed_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'allow.tsv'
            path.write_text('entry\toffset\tjp_text\treason\n1\t2080\t洋子\t姓名\n')
            rows = [(1,0,2080,2,'洋子'),(1,1,2086,2,'洋子'),(2,0,2080,2,'洋子'),(1,0,2080,2,'番号')]
            self.assertEqual(unreviewed(rows,path),rows[1:])

    def test_recovered_full_line_must_replace_the_old_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'master.tsv'
            path.write_text('entry\tidx\toffset\tn_codes\tn_bytes\tjp_text\ttranslation\n'
                            '1\t0\t2080\t4\t10\t日本文字\t中文译文\tpool\n'
                            '1\t1\t2084\t2\t6\t文字\t译文\tpool\n')
            with self.assertRaisesRegex(ValueError, '翻译槽重叠'):
                master_slots(path)


if __name__ == '__main__':
    unittest.main()
