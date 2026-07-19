"""Unit tests for annotation geometry and hit-testing."""

from __future__ import annotations

import unittest

from PySide6.QtCore import QRectF

from src.framecycler.ui.annotations.geometry import (
    hit_test_shape,
    hit_test_topmost,
    image_rect_for_viewport,
    translate_shape,
    uv_to_widget,
    widget_to_uv,
)
from src.framecycler.ui.annotations.models import AnnotationKind, AnnotationShape
from src.framecycler.core.session import Session
from src.framecycler.core.settings import Settings


class TestAnnotationGeometry(unittest.TestCase):
    def test_widget_uv_roundtrip_center(self):
        rect = QRectF(100, 50, 200, 100)
        uv = widget_to_uv(200, 100, rect)
        self.assertIsNotNone(uv)
        self.assertAlmostEqual(uv[0], 0.5, places=5)
        self.assertAlmostEqual(uv[1], 0.5, places=5)
        pt = uv_to_widget(uv[0], uv[1], rect)
        self.assertAlmostEqual(pt.x(), 200.0, places=5)
        self.assertAlmostEqual(pt.y(), 100.0, places=5)

    def test_widget_uv_outside(self):
        rect = QRectF(100, 50, 200, 100)
        self.assertIsNone(widget_to_uv(10, 10, rect))

    def test_image_rect_for_viewport_full_bleed(self):
        # scale 1, zoom 1, no pan → full widget
        rect = image_rect_for_viewport(200, 100, 1.0, 1.0, 1.0, 0.0, 0.0)
        self.assertAlmostEqual(rect.width(), 200.0, places=3)
        self.assertAlmostEqual(rect.height(), 100.0, places=3)

    def test_hit_line(self):
        shape = AnnotationShape(
            kind=AnnotationKind.LINE,
            points=[(0.1, 0.1), (0.9, 0.1)],
            thickness=0.01,
        )
        rect = QRectF(0, 0, 1000, 1000)
        self.assertTrue(hit_test_shape(shape, 0.5, 0.1, image_rect=rect, tol_px=10))
        self.assertFalse(hit_test_shape(shape, 0.5, 0.5, image_rect=rect, tol_px=5))

    def test_hit_rect_interior(self):
        shape = AnnotationShape(
            kind=AnnotationKind.RECT,
            points=[(0.2, 0.2), (0.6, 0.6)],
        )
        rect = QRectF(0, 0, 500, 500)
        self.assertTrue(hit_test_shape(shape, 0.4, 0.4, image_rect=rect))
        self.assertFalse(hit_test_shape(shape, 0.9, 0.9, image_rect=rect, tol_px=2))

    def test_hit_test_topmost(self):
        a = AnnotationShape(kind=AnnotationKind.RECT, points=[(0.1, 0.1), (0.5, 0.5)])
        b = AnnotationShape(kind=AnnotationKind.RECT, points=[(0.2, 0.2), (0.4, 0.4)])
        rect = QRectF(0, 0, 400, 400)
        idx = hit_test_topmost([a, b], 0.3, 0.3, image_rect=rect)
        self.assertEqual(idx, 1)

    def test_translate_clamps(self):
        shape = AnnotationShape(kind=AnnotationKind.LINE, points=[(0.95, 0.05), (0.9, 0.1)])
        translate_shape(shape, 0.2, -0.2)
        for u, v in shape.points:
            self.assertGreaterEqual(u, 0.0)
            self.assertLessEqual(u, 1.0)
            self.assertGreaterEqual(v, 0.0)
            self.assertLessEqual(v, 1.0)


class TestSessionAnnotations(unittest.TestCase):
    def test_per_shot_storage(self):
        session = Session(Settings())
        shapes = [
            AnnotationShape(kind=AnnotationKind.ARROW, points=[(0.1, 0.1), (0.5, 0.5)])
        ]
        session.set_annotations_for_shot(0, shapes)
        session.set_annotations_for_shot(1, [])
        got = session.annotations_for_shot(0)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].kind, AnnotationKind.ARROW)
        self.assertEqual(session.annotations_for_shot(1), [])
        session.clear_annotations_for_shot(0)
        self.assertEqual(session.annotations_for_shot(0), [])

    def test_clear_session_clears_annotations(self):
        session = Session(Settings())
        session.set_annotations_for_shot(
            2, [AnnotationShape(kind=AnnotationKind.TEXT, points=[(0.5, 0.5)], text="Hi")]
        )
        session.clear()
        self.assertEqual(session.annotations_for_shot(2), [])


if __name__ == "__main__":
    unittest.main()
