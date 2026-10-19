import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from audit_context import audit_groups, display_runs, flags_for, shared_combinations


class ContextAuditTests(unittest.TestCase):
    def test_jump_cannot_glue_two_display_runs(self):
        events = [('row', 21, 4, 0), ('op', 25, 0x9E, b''),
                  ('row', 30, 9, 0), ('page', 34)]
        self.assertEqual([r for r, _ in display_runs(events)], [[4], [9]])

    def test_unknown_width_rejects_affected_run(self):
        events = [('row', 21, 4, 0), ('unknown', 25, 0x41),
                  ('row', 26, 9, 0), ('page', 30), ('row', 31, 6, 0)]
        self.assertEqual([r for r, _ in display_runs(events)], [[6]])

    def test_moved_negation_is_checked_across_lines(self):
        self.assertEqual(flags_for(['」洋子君は安田徳子を', '、知らないかもしれんが'],
                                   ['」洋子也许不认识', '、安田徳子这个人']), [])
        self.assertIn('否定可能丢失:认知', flags_for(
            ['」洋子君は安田徳子を', '、知らないかもしれんが'],
            ['」洋子知道安田徳子', '、安田徳子']))

    def test_negative_question_can_be_positive_in_chinese(self):
        self.assertEqual(flags_for(['」何か知らないか！『'], ['」你知道些什么吗！『']), [])

    def test_source_repetition_is_not_silently_rewritten(self):
        self.assertEqual(flags_for(['可能性はある，', 'その可能性に賭けよう'],
                                   ['还是有可能，', '就赌这个可能性']), [])

    def test_names_are_compared_over_the_whole_group(self):
        self.assertEqual(flags_for(['とくちゃんを', '探している'], ['现在去找', '小徳']), [])
        self.assertIn('人物指代:徳子→悠', flags_for(['とくちゃんを探している'], ['正在找小悠']))

    def test_search_subject_and_object_are_distinguished(self):
        jp = ['：』ゆうちゃん＋を探している']
        self.assertIn('主客体疑似颠倒:寻找', flags_for(jp, ['：』小悠＋正在寻找']))
        self.assertEqual(flags_for(jp, ['：正在寻找』小悠＋']), [])

    def test_shared_suffix_keeps_all_predecessors_and_entries(self):
        groups = [dict(entry=1, rows=[4, 9]), dict(entry=1, rows=[6, 9]),
                  dict(entry=2, rows=[4, 9])]
        self.assertEqual(shared_combinations(groups), {(1, 9): groups[:2]})

    def test_unmapped_slot_only_gets_complete_clause_role_check(self):
        master = {(315, 100): dict(idx=50, jp='：』ゆうちゃん＋を探している', cn='：』小悠＋正在寻找'),
                  (1, 200): dict(idx=2, jp='知らない', cn='那个名字')}
        findings = audit_groups([], master)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]['basis'], 'single-complete-clause')
        self.assertEqual(findings[0]['entry'], 315)


if __name__ == '__main__':
    unittest.main()
