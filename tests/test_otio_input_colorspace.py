import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import opentimelineio as otio

from src.framecycler.core import otio_model
from src.framecycler.core.session import Session
from src.framecycler.core.settings import Settings


def _clip(path: str = "/tmp/plate.exr", frames: int = 10) -> otio.schema.Clip:
    return otio_model.clip_from_media(
        path,
        {
            "fps": 24.0,
            "frame_count": frames,
            "start_frame": 1001,
            "width": 64,
            "height": 32,
            "pixel_aspect_ratio": 1.0,
        },
    )


class TestOtioInputColorspaceSchema(unittest.TestCase):
    def test_get_set_clear_input_colorspace(self):
        clip = _clip()
        self.assertIsNone(otio_model.get_input_colorspace(clip))

        otio_model.set_input_colorspace(clip, "ACEScg")
        self.assertEqual(otio_model.get_input_colorspace(clip), "ACEScg")
        self.assertEqual(
            otio_model.get_fc_meta(clip)[otio_model.INPUT_COLORSPACE_KEY], "ACEScg"
        )

        otio_model.clear_input_colorspace(clip)
        self.assertIsNone(otio_model.get_input_colorspace(clip))

    def test_set_rejects_empty_name(self):
        clip = _clip()
        with self.assertRaises(ValueError):
            otio_model.set_input_colorspace(clip, "  ")


class TestSessionInputColorspace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(config_dir=os.path.join(self.tmp.name, "cfg"))
        self.mov = os.path.join(self.tmp.name, "plate.mov")
        self.exr = os.path.join(self.tmp.name, "beauty.exr")
        open(self.mov, "wb").close()
        open(self.exr, "wb").close()

    def tearDown(self):
        self.tmp.cleanup()

    def _mock_source(self, path: str, *, ext_hint: str | None = None):
        source = MagicMock()
        source.path = path
        source.fps = 24.0
        source.decoder_start_frame = 1
        source.frame_count = 5
        source.width = 8
        source.height = 8
        source.pixel_aspect_ratio = 1.0
        source.metadata = {"fps": 24.0, "frame_count": 5, "start_frame": 1, "width": 8, "height": 8}
        return source

    def test_add_media_stores_distinct_colorspaces(self):
        session = Session(self.settings)

        def detect(path, metadata=None):
            if path.endswith(".mov"):
                return "Rec.709 - Texture"
            if path.endswith(".exr"):
                return "ACEScg"
            return "Raw"

        session.set_input_colorspace_detector(detect)

        with patch.object(session.media_pool, "acquire", side_effect=self._mock_source):
            loaded = session.add_media([self.mov, self.exr], mode="sequence")

        self.assertEqual(loaded, 2)
        stacks = otio_model.shot_stacks(session.timeline)
        self.assertEqual(len(stacks), 2)
        clips = [otio_model.version_clips(s)[0] for s in stacks]
        self.assertEqual(otio_model.get_input_colorspace(clips[0]), "Rec.709 - Texture")
        self.assertEqual(otio_model.get_input_colorspace(clips[1]), "ACEScg")

    def test_ensure_keeps_existing_override(self):
        session = Session(self.settings)
        clip = _clip(self.exr)
        otio_model.set_input_colorspace(clip, "Rec.709 - Texture")
        session.set_input_colorspace_detector(lambda path, metadata=None: "ACEScg")

        result = session.ensure_clip_input_colorspace(clip, self.exr, {})
        self.assertEqual(result, "Rec.709 - Texture")
        self.assertEqual(otio_model.get_input_colorspace(clip), "Rec.709 - Texture")

    def test_acquire_detects_when_missing(self):
        session = Session(self.settings)
        clip = _clip(self.exr)
        self.assertIsNone(otio_model.get_input_colorspace(clip))
        session.set_input_colorspace_detector(lambda path, metadata=None: "ACEScg")

        with patch.object(session.media_pool, "acquire", return_value=self._mock_source(self.exr)):
            session.acquire_clip_media(clip)

        self.assertEqual(otio_model.get_input_colorspace(clip), "ACEScg")

    def test_export_import_preserves_per_clip_colorspace(self):
        session = Session(self.settings)
        clip_a = _clip(self.mov, frames=5)
        clip_b = _clip(self.exr, frames=5)
        otio_model.set_input_colorspace(clip_a, "Rec.709 - Texture")
        otio_model.set_input_colorspace(clip_b, "ACEScg")
        otio_model.append_shot(session.timeline, clip_a)
        otio_model.append_shot(session.timeline, clip_b)
        session._notify()

        otio_path = os.path.join(self.tmp.name, "session.otio")
        session.export_timeline(otio_path)

        loaded = Session(self.settings)
        with patch.object(loaded, "acquire_clip_media", return_value=None):
            loaded.import_timeline(otio_path)

        stacks = otio_model.shot_stacks(loaded.timeline)
        self.assertEqual(
            otio_model.get_input_colorspace(otio_model.version_clips(stacks[0])[0]),
            "Rec.709 - Texture",
        )
        self.assertEqual(
            otio_model.get_input_colorspace(otio_model.version_clips(stacks[1])[0]),
            "ACEScg",
        )

    def test_resolved_input_colorspace_follows_active_version(self):
        session = Session(self.settings)
        clip_a = _clip(self.mov, frames=5)
        clip_b = _clip(self.exr, frames=5)
        otio_model.set_input_colorspace(clip_a, "Rec.709 - Texture")
        otio_model.set_input_colorspace(clip_b, "ACEScg")
        stack = otio_model.append_shot(session.timeline, clip_a)
        otio_model.add_version(stack, clip_b, make_active=True)
        session._notify()

        resolved = session.resolved_input_colorspace_for_active(session.plan.global_start)
        self.assertEqual(resolved, "ACEScg")
        session.set_active_version(0, 0)
        resolved = session.resolved_input_colorspace_for_active(session.plan.global_start)
        self.assertEqual(resolved, "Rec.709 - Texture")


class TestMainWindowInputColorspaceApply(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        import sys

        cls._app = QApplication.instance() or QApplication(sys.argv)

    def test_seek_and_menu_override_use_per_clip_colorspace(self):
        from src.framecycler.ui.main_window import MainWindow

        with tempfile.TemporaryDirectory() as tmp:
            settings_dir = os.path.join(tmp, "cfg")
            mov = os.path.join(tmp, "a.mov")
            exr = os.path.join(tmp, "b.exr")
            open(mov, "wb").close()
            open(exr, "wb").close()

            with patch("src.framecycler.ui.main_window.Settings") as settings_cls:
                settings_cls.return_value = Settings(config_dir=settings_dir)
                with patch("src.framecycler.ui.main_window.PackageManager") as pm_cls:
                    pm = MagicMock()
                    pm.menu_actions = []
                    pm_cls.return_value = pm
                    window = MainWindow()

            session = window.session
            clip_a = otio_model.clip_from_media(
                mov,
                {"fps": 24.0, "frame_count": 5, "start_frame": 1, "width": 8, "height": 8},
            )
            clip_b = otio_model.clip_from_media(
                exr,
                {"fps": 24.0, "frame_count": 5, "start_frame": 1, "width": 8, "height": 8},
            )
            otio_model.set_input_colorspace(clip_a, "Rec.709 - Texture")
            otio_model.set_input_colorspace(clip_b, "ACEScg")
            otio_model.append_shot(session.timeline, clip_a)
            otio_model.append_shot(session.timeline, clip_b)
            session._notify()

            window.start_frame = session.plan.global_start
            window.end_frame = session.plan.global_end
            window.current_frame = session.plan.global_start
            window._apply_resolved_input_colorspace(force=True)
            self.assertEqual(window.ocio_manager.input_colorspace, "Rec.709 - Texture")

            window.current_frame = session.plan.segments[1].global_start
            window._apply_resolved_input_colorspace(force=True)
            self.assertEqual(window.ocio_manager.input_colorspace, "ACEScg")

            window._set_input_colorspace("sRGB - Texture")
            stored = otio_model.get_input_colorspace(
                otio_model.version_clips(otio_model.shot_stacks(session.timeline)[1])[0]
            )
            self.assertEqual(stored, "sRGB - Texture")
            self.assertEqual(window.ocio_manager.input_colorspace, "sRGB - Texture")

            # Other clip unchanged
            other = otio_model.get_input_colorspace(
                otio_model.version_clips(otio_model.shot_stacks(session.timeline)[0])[0]
            )
            self.assertEqual(other, "Rec.709 - Texture")

            window.close()


if __name__ == "__main__":
    unittest.main()
