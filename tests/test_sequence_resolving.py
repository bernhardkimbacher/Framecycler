import os
import shutil
import tempfile
import unittest
from src.framecycler.decoders.base import BaseDecoder

class DummyDecoder(BaseDecoder):
    def get_metadata(self):
        return {}
    def read_frame(self, index):
        return {}
    def close(self):
        pass

class TestSequenceResolving(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_find_sequence_from_single_file(self):
        # Create dummy sequence files
        files = [
            "MOC_CAS_0010.0993.exr",
            "MOC_CAS_0010.0994.exr",
            "MOC_CAS_0010.0995.exr",
            "unrelated.exr"
        ]
        for f in files:
            with open(os.path.join(self.test_dir, f), "w") as fh:
                fh.write("")

        decoder = DummyDecoder()
        
        # Resolve from one of the sequence files
        target = os.path.join(self.test_dir, "MOC_CAS_0010.0994.exr")
        resolved = decoder._find_sequence_from_single_file(target)
        
        self.assertEqual(len(resolved), 3)
        self.assertEqual(resolved[0][0], 993)
        self.assertEqual(resolved[0][1], os.path.abspath(os.path.join(self.test_dir, "MOC_CAS_0010.0993.exr")))
        self.assertEqual(resolved[1][0], 994)
        self.assertEqual(resolved[2][0], 995)
        
        # Resolve from unrelated.exr (no digits/sequence patterns)
        unrelated_target = os.path.join(self.test_dir, "unrelated.exr")
        resolved_unrelated = decoder._find_sequence_from_single_file(unrelated_target)
        self.assertEqual(len(resolved_unrelated), 0)

    def test_version_digits_are_not_a_sequence(self):
        for f in ("plate_v001.exr", "plate_v002.exr"):
            with open(os.path.join(self.test_dir, f), "w") as fh:
                fh.write("")
        decoder = DummyDecoder()
        target = os.path.join(self.test_dir, "plate_v001.exr")
        self.assertEqual(decoder._find_sequence_from_single_file(target), [])

    def test_version_plus_frame_sequence(self):
        files = [
            "shot_v001.1001.exr",
            "shot_v001.1002.exr",
            "shot_v001.1003.exr",
        ]
        for f in files:
            with open(os.path.join(self.test_dir, f), "w") as fh:
                fh.write("")
        decoder = DummyDecoder()
        target = os.path.join(self.test_dir, "shot_v001.1002.exr")
        resolved = decoder._find_sequence_from_single_file(target)
        self.assertEqual([f for f, _ in resolved], [1001, 1002, 1003])

    def test_mixed_padding_rejected(self):
        for f in ("shot.1.exr", "shot.0001.exr", "shot.0002.exr"):
            with open(os.path.join(self.test_dir, f), "w") as fh:
                fh.write("")
        decoder = DummyDecoder()
        # Seed with 4-digit padding — must not pull in shot.1.exr
        target = os.path.join(self.test_dir, "shot.0001.exr")
        resolved = decoder._find_sequence_from_single_file(target)
        self.assertEqual(len(resolved), 2)
        self.assertEqual([f for f, _ in resolved], [1, 2])
        for _, path in resolved:
            self.assertIn("000", os.path.basename(path))

    def test_hash_pattern_fixed_width(self):
        from src.framecycler.decoders.base import pattern_frame_regex
        import re
        rx = re.compile(pattern_frame_regex("shot.####.exr"))
        self.assertTrue(rx.match("shot.0001.exr"))
        self.assertFalse(rx.match("shot.1.exr"))
        self.assertFalse(rx.match("shot.00001.exr"))


if __name__ == "__main__":
    unittest.main()