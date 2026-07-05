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

if __name__ == "__main__":
    unittest.main()
