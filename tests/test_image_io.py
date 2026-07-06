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
from src.framecycler.decoders import image_io


class TestImageIO(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_oiio()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="framecycler_image_io_")
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_metadata_flat_exr(self):
        path = self.tmp_path / "flat.exr"
        write_float_exr(path, value=0.42)
        meta = image_io.read_metadata(str(path))
        self.assertEqual(meta.width, 32)
        self.assertEqual(meta.height, 16)
        self.assertIn("R", meta.channel_names)
        self.assertIn("beauty", meta.layers)
        self.assertAlmostEqual(meta.pixel_aspect_ratio, 1.0)

    def test_read_metadata_exr_pixel_aspect_ratio(self):
        path = self.tmp_path / "anamorphic.exr"
        write_float_exr(path, value=0.42, pixel_aspect=2.0)
        meta = image_io.read_metadata(str(path))
        self.assertAlmostEqual(meta.pixel_aspect_ratio, 2.0)

    def test_read_pixels_flat_exr(self):
        path = self.tmp_path / "flat.exr"
        write_float_exr(path, value=0.42)
        arr = image_io.read_pixels(str(path))
        self.assertEqual(arr.shape, (16, 32, 3))
        self.assertEqual(arr.dtype, np.float32)
        self.assertAlmostEqual(float(arr[0, 0, 0]), 0.42, places=4)

    def test_list_layers_and_layer_read(self):
        path = self.tmp_path / "layered.exr"
        write_layered_exr(path)
        layers = image_io.list_layers(str(path))
        self.assertEqual(layers, ["beauty", "depth"])
        beauty = image_io.read_pixels(str(path), layer="beauty")
        depth = image_io.read_pixels(str(path), layer="depth")
        self.assertAlmostEqual(float(beauty.mean()), 0.25, places=3)
        self.assertAlmostEqual(float(depth.mean()), 0.75, places=3)

    def test_read_pixels_dpx(self):
        path = self.tmp_path / "test.dpx"
        write_uint16_dpx(path)
        arr = image_io.read_pixels(str(path))
        self.assertEqual(arr.shape, (16, 32, 3))
        self.assertEqual(arr.dtype, np.float32)
        self.assertGreater(arr.max(), 0.4)


if __name__ == "__main__":
    unittest.main()
