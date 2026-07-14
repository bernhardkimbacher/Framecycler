import tempfile
import unittest
from pathlib import Path

import numpy as np

from tests.oiio_fixtures import (
    require_oiio,
    write_float_exr,
    write_layered_exr,
    write_uint16_dpx,
)
from src.framecycler.decoders.exr_decoder import EXRDecoder
from src.framecycler.decoders.dpx_decoder import DPXDecoder


class TestOIIODecoders(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_oiio()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="framecycler_decoders_")
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_exr_decoder_reads_frame(self):
        path = self.tmp_path / "shot.1001.exr"
        write_float_exr(path, value=0.33)
        decoder = EXRDecoder(str(path))
        meta = decoder.get_metadata()
        self.assertEqual(meta["width"], 32)
        self.assertEqual(meta["height"], 16)
        self.assertIn("channels", meta)
        self.assertIn("layers", meta)
        frame = decoder.read_frame(1001)
        self.assertEqual(frame["data"].shape, (16, 32, 3))
        self.assertEqual(frame["data"].dtype, np.float16)
        self.assertAlmostEqual(float(frame["data"].mean()), 0.33, places=3)

    def test_exr_decoder_resolution_scale(self):
        path = self.tmp_path / "shot.1001.exr"
        write_float_exr(path, value=0.33)
        decoder = EXRDecoder(str(path))
        meta = decoder.get_metadata()
        self.assertEqual(meta["width"], 32)
        self.assertEqual(meta["height"], 16)
        frame = decoder.read_frame(1001, resolution_scale=0.5)
        self.assertEqual(frame["data"].shape, (8, 16, 3))
        self.assertEqual(frame["data"].dtype, np.float16)

    def test_exr_decoder_layer_switch(self):
        path = self.tmp_path / "shot.1001.exr"
        write_layered_exr(path)
        decoder = EXRDecoder(str(path))
        decoder.active_layer = "beauty"
        beauty = decoder.read_frame(1001)["data"]
        decoder.active_layer = "depth"
        depth = decoder.read_frame(1001)["data"]
        self.assertAlmostEqual(float(beauty.mean()), 0.25, places=3)
        self.assertAlmostEqual(float(depth.mean()), 0.75, places=3)

    def test_exr_decoder_missing_frames_timeline(self):
        write_float_exr(self.tmp_path / "shot.1001.exr")
        write_float_exr(self.tmp_path / "shot.1002.exr")
        write_float_exr(self.tmp_path / "shot.1004.exr")
        write_float_exr(self.tmp_path / "shot.1005.exr")
        
        decoder = EXRDecoder(str(self.tmp_path / "shot.####.exr"))
        meta = decoder.get_metadata()
        
        self.assertEqual(meta["frame_count"], 5)
        self.assertEqual(decoder.frame_numbers, [1001, 1002, 1003, 1004, 1005])
        self.assertEqual(decoder.existing_frame_numbers, [1001, 1002, 1004, 1005])
        
        self.assertEqual(decoder.get_file_path(1003, fallback_nearest=False), None)
        nearest = decoder.get_file_path(1003, fallback_nearest=True)
        self.assertIn(Path(nearest).name, ["shot.1002.exr", "shot.1004.exr"])

    def test_dpx_decoder_reads_frame(self):
        path = self.tmp_path / "shot.1001.dpx"
        write_uint16_dpx(path)
        decoder = DPXDecoder(str(path))
        meta = decoder.get_metadata()
        self.assertEqual(meta["width"], 32)
        self.assertEqual(meta["height"], 16)
        self.assertIn("transfer_characteristic", meta)
        self.assertIn("colorimetric_specification", meta)
        frame = decoder.read_frame(1001)
        self.assertEqual(frame["data"].shape, (16, 32, 3))
        self.assertEqual(frame["data"].dtype, np.float16)


if __name__ == "__main__":
    unittest.main()
