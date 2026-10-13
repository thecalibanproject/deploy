"""Unit tests of stitch.py's idle cuts (python3 -m unittest discover -s e2e/demo -p 'test_*.py')."""
import unittest

import stitch


class IdleCuts(unittest.TestCase):
    def test_nested_spans_are_cut_once(self):
        keep = stitch.KEEP
        tl = {"idle": [{"start": 10, "end": 100}, {"start": 20, "end": 50}]}
        cuts = stitch.idle_cuts(tl)
        self.assertEqual(cuts, [(10 + keep, 100 - keep)])
        # A chapter inside the nested span keeps its order after the cut.
        self.assertLessEqual(stitch.shift(30, cuts), stitch.shift(99, cuts))

    def test_short_spans_are_kept(self):
        self.assertEqual(stitch.idle_cuts({"idle": [{"start": 0, "end": 1}]}), [])


if __name__ == "__main__":
    unittest.main()
