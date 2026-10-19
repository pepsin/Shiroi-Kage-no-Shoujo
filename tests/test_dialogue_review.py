import hashlib
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from review_dialogue import check_checkpoint, numbers


class DialogueReviewTests(unittest.TestCase):
    def test_selected_segments_preserve_order_and_deduplicate(self):
        self.assertEqual(numbers('53,39-41,40'), [39,40,41,53])
        for value in ('0','4-2','1-2-3',''):
            with self.assertRaises(ValueError):
                numbers(value)

    def test_checkpoint_from_another_build_is_rejected(self):
        state = {'wram':b'new script','iwram':b'cursor','vram':b'pixels'}
        expected = {k:hashlib.sha256(state[r]).hexdigest()
                    for r,k in [('wram','ewram'),('iwram','iwram'),('vram','vram')]}
        with patch('review_dialogue.read_state',return_value=state):
            check_checkpoint('test.ss1',expected)
            expected['ewram']=hashlib.sha256(b'old script').hexdigest()
            with self.assertRaisesRegex(ValueError,'断点不属于该路线'):
                check_checkpoint('test.ss1',expected)


if __name__ == '__main__':
    unittest.main()
