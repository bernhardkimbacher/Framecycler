"""Python-path missing_frame_mode for EXR/DPX gap frames."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

import numpy as np

from tests.oiio_fixtures import require_oiio, write_float_exr


class TestMissingFrameModePython(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_oiio()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fc_missing_frame_")
        # Gap at 1002 between 1001 and 1003
        write_float_exr(os.path.join(self.tmp, "gap.1001.exr"), width=32, height=16, value=0.4)
        write_float_exr(os.path.join(self.tmp, "gap.1003.exr"), width=32, height=16, value=0.6)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _decoder(self):
        from src.framecycler.decoders.exr_decoder import EXRDecoder

        return EXRDecoder(os.path.join(self.tmp, "gap.1001.exr"))

    def test_nearest_fills_gap(self):
        dec = self._decoder()
        self.assertIn(1002, dec.frame_numbers)
        frame = dec.read_frame(1002, missing_frame_mode="Nearest Frame")
        self.assertTrue(np.allclose(frame["data"][..., 0].astype(np.float32), 0.4, atol=0.02))

    def test_flat_gray_gap(self):
        dec = self._decoder()
        frame = dec.read_frame(1002, missing_frame_mode="Flat Gray")
        self.assertTrue(np.allclose(frame["data"][..., 0].astype(np.float32), 0.05, atol=0.01))

    def test_red_x_gap(self):
        dec = self._decoder()
        frame = dec.read_frame(1002, missing_frame_mode="Red X")
        h, w, _ = frame["data"].shape
        center = frame["data"][h // 2, w // 2].astype(np.float32)
        self.assertAlmostEqual(float(center[0]), 1.0, places=2)
        self.assertAlmostEqual(float(center[1]), 0.0, places=2)


if __name__ == "__main__":
    unittest.main()
