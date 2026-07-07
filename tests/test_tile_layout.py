import unittest

from src.framecycler.core.tile_layout import compute_tile_layouts


class TestTileLayout(unittest.TestCase):
    def test_single_source_centered(self):
        layouts = compute_tile_layouts([(1920, 1080)], [1.0], 1920, 1080)
        self.assertEqual(len(layouts), 1)
        layout = layouts[0]
        self.assertAlmostEqual(layout.scale_x, 1.0, places=3)
        self.assertAlmostEqual(layout.scale_y, 1.0, places=3)
        self.assertAlmostEqual(layout.offset_x, 0.0, places=3)
        self.assertAlmostEqual(layout.offset_y, 0.0, places=3)

    def test_two_sources_preserve_distinct_aspects(self):
        layouts = compute_tile_layouts(
            [(1920, 1080), (1080, 1920)],
            [1.0, 1.0],
            1000,
            500,
        )
        self.assertEqual(len(layouts), 2)
        self.assertLess(layouts[0].scale_x, 1.0)
        self.assertLess(layouts[1].scale_x, 1.0)
        self.assertNotAlmostEqual(layouts[0].scale_x, layouts[1].scale_x, places=2)

    def test_grid_for_four_sources(self):
        layouts = compute_tile_layouts(
            [(100, 100)] * 4,
            [1.0] * 4,
            800,
            600,
        )
        self.assertEqual(len(layouts), 4)
        self.assertEqual({layout.source_index for layout in layouts}, {0, 1, 2, 3})


if __name__ == "__main__":
    unittest.main()
