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


class TestOtioCdlSchema(unittest.TestCase):
    def test_get_set_clear_cdl_on_clip_stack_timeline(self):
        timeline = otio_model.new_timeline()
        clip = _clip()
        stack = otio_model.append_shot(timeline, clip)

        self.assertIsNone(otio_model.get_cdl(clip))
        self.assertIsNone(otio_model.get_cdl(stack))
        self.assertIsNone(otio_model.get_cdl(timeline))

        otio_model.set_cdl(clip, slope=(1.1, 1.0, 0.9), saturation=1.2)
        otio_model.set_cdl(stack, offset=(0.01, 0.0, -0.01))
        otio_model.set_cdl(timeline, power=(1.05, 1.05, 1.05), style="asc")

        self.assertEqual(otio_model.get_cdl(clip)["slope"], [1.1, 1.0, 0.9])
        self.assertEqual(otio_model.get_cdl(clip)["saturation"], 1.2)
        self.assertEqual(otio_model.get_cdl(stack)["offset"], [0.01, 0.0, -0.01])
        self.assertEqual(otio_model.get_cdl(timeline)["style"], "asc")

        otio_model.clear_cdl(clip)
        self.assertIsNone(otio_model.get_cdl(clip))
        self.assertIsNotNone(otio_model.get_cdl(stack))

    def test_resolve_inheritance_clip_stack_timeline(self):
        timeline = otio_model.new_timeline()
        clip = _clip()
        stack = otio_model.append_shot(timeline, clip)

        otio_model.set_cdl(timeline, slope=(1.5, 1.5, 1.5))
        resolved = otio_model.resolve_cdl(clip, stack, timeline)
        self.assertEqual(resolved["slope"], [1.5, 1.5, 1.5])

        otio_model.set_cdl(stack, slope=(1.2, 1.2, 1.2))
        resolved = otio_model.resolve_cdl(clip, stack, timeline)
        self.assertEqual(resolved["slope"], [1.2, 1.2, 1.2])

        otio_model.set_cdl(clip, slope=(0.8, 0.9, 1.0))
        resolved = otio_model.resolve_cdl(clip, stack, timeline)
        self.assertEqual(resolved["slope"], [0.8, 0.9, 1.0])

        otio_model.clear_cdl(clip)
        resolved = otio_model.resolve_cdl(clip, stack, timeline)
        self.assertEqual(resolved["slope"], [1.2, 1.2, 1.2])

    def test_resolve_identity_when_absent(self):
        resolved = otio_model.resolve_cdl(None, None, None)
        self.assertTrue(otio_model.cdl_is_identity(resolved))

    def test_wrap_shot_stack_preserves_preexisting_cdl(self):
        clip = _clip("/tmp/v1.exr")
        stack = otio.schema.Stack(name="shot")
        otio_model.set_cdl(stack, slope=(1.25, 1.0, 0.95), saturation=0.9)
        stack.append(clip)
        # Same merge-safe index init used by wrap_shot_stack
        meta = otio_model.get_fc_meta(stack)
        meta.setdefault("active", 0)
        meta.setdefault("compare", 0)
        otio_model.update_fc_meta(stack, active=meta["active"], compare=meta["compare"])

        cdl = otio_model.get_cdl(stack)
        self.assertIsNotNone(cdl)
        self.assertEqual(cdl["slope"], [1.25, 1.0, 0.95])
        self.assertEqual(cdl["saturation"], 0.9)
        self.assertEqual(otio_model.active_index(stack), 0)

    def test_wrap_then_set_cdl_survives_set_active(self):
        clip_a = _clip("/tmp/a.exr")
        clip_b = _clip("/tmp/b.exr")
        stack = otio_model.wrap_shot_stack(clip_a)
        otio_model.add_version(stack, clip_b, make_active=True)
        otio_model.set_cdl(stack, slope=(1.3, 1.0, 1.0))
        otio_model.set_active_version(stack, 0)
        self.assertEqual(otio_model.get_cdl(stack)["slope"], [1.3, 1.0, 1.0])


class TestSessionCdlRoundTrip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(config_dir=os.path.join(self.tmp.name, "cfg"))
        self.exr_a = os.path.join(self.tmp.name, "a.exr")
        self.exr_b = os.path.join(self.tmp.name, "b.exr")
        # Touch placeholder files so acquire paths exist; mock acquire to avoid decoder.
        open(self.exr_a, "wb").close()
        open(self.exr_b, "wb").close()

    def tearDown(self):
        self.tmp.cleanup()

    def _session_with_two_versions(self) -> Session:
        session = Session(self.settings)
        clip_a = otio_model.clip_from_media(
            self.exr_a,
            {"fps": 24.0, "frame_count": 5, "start_frame": 1, "width": 8, "height": 8},
        )
        clip_b = otio_model.clip_from_media(
            self.exr_b,
            {"fps": 24.0, "frame_count": 5, "start_frame": 1, "width": 8, "height": 8},
        )
        stack = otio_model.append_shot(session.timeline, clip_a)
        otio_model.add_version(stack, clip_b, make_active=True)
        session._notify()
        return session

    def test_export_import_preserves_all_levels(self):
        session = self._session_with_two_versions()
        session.set_timeline_cdl(slope=(1.4, 1.4, 1.4))
        session.set_stack_cdl(0, offset=(0.02, 0.0, 0.0))
        session.set_clip_cdl(0, 0, slope=(0.7, 0.8, 0.9), saturation=1.3)
        session.set_clip_cdl(0, 1, slope=(1.1, 1.0, 0.95))

        otio_path = os.path.join(self.tmp.name, "session.otio")
        session.export_timeline(otio_path)

        loaded = Session(self.settings)
        with patch.object(loaded, "acquire_clip_media", return_value=None):
            loaded.import_timeline(otio_path)

        stacks = otio_model.shot_stacks(loaded.timeline)
        clips = otio_model.version_clips(stacks[0])
        self.assertEqual(otio_model.get_cdl(loaded.timeline)["slope"], [1.4, 1.4, 1.4])
        self.assertEqual(otio_model.get_cdl(stacks[0])["offset"], [0.02, 0.0, 0.0])
        self.assertEqual(otio_model.get_cdl(clips[0])["slope"], [0.7, 0.8, 0.9])
        self.assertEqual(otio_model.get_cdl(clips[1])["slope"], [1.1, 1.0, 0.95])

    def test_resolved_cdl_for_active_follows_active_version(self):
        session = self._session_with_two_versions()
        session.set_clip_cdl(0, 0, slope=(0.5, 0.5, 0.5))
        session.set_clip_cdl(0, 1, slope=(1.5, 1.5, 1.5))
        session.set_active_version(0, 1)
        resolved = session.resolved_cdl_for_active(session.plan.global_start)
        self.assertEqual(resolved["slope"], [1.5, 1.5, 1.5])
        session.set_active_version(0, 0)
        resolved = session.resolved_cdl_for_active(session.plan.global_start)
        self.assertEqual(resolved["slope"], [0.5, 0.5, 0.5])


class TestMainWindowCdlApply(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        import sys

        cls._app = QApplication.instance() or QApplication(sys.argv)

    def test_apply_resolved_cdl_and_reset_does_not_wipe_otio(self):
        from src.framecycler.ui.main_window import MainWindow

        with tempfile.TemporaryDirectory() as tmp:
            settings_dir = os.path.join(tmp, "cfg")
            with patch("src.framecycler.ui.main_window.Settings") as settings_cls:
                settings_cls.return_value = Settings(config_dir=settings_dir)
                # Avoid package discovery noise / native renderer issues where possible
                with patch("src.framecycler.ui.main_window.PackageManager") as pm_cls:
                    pm = MagicMock()
                    pm.menu_actions = []
                    pm_cls.return_value = pm
                    window = MainWindow()

            session = window.session
            clip = otio_model.clip_from_media(
                os.path.join(tmp, "p.exr"),
                {"fps": 24.0, "frame_count": 3, "start_frame": 1, "width": 8, "height": 8},
            )
            open(os.path.join(tmp, "p.exr"), "wb").close()
            otio_model.append_shot(session.timeline, clip)
            session.set_clip_cdl(0, 0, slope=(1.2, 1.0, 0.8), saturation=1.1)
            session._notify()

            window.current_frame = session.plan.global_start
            window._apply_resolved_cdl(force=True)
            self.assertEqual(window.ocio_manager.cdl_slope, (1.2, 1.0, 0.8))
            self.assertAlmostEqual(window.ocio_manager.cdl_saturation, 1.1)

            window._reset_grade()
            self.assertTrue(window.ocio_manager._cdl_is_identity())
            # OTIO persistence intact
            stored = otio_model.get_cdl(otio_model.version_clips(otio_model.shot_stacks(session.timeline)[0])[0])
            self.assertEqual(stored["slope"], [1.2, 1.0, 0.8])

            window._apply_resolved_cdl(force=True)
            self.assertEqual(window.ocio_manager.cdl_slope, (1.2, 1.0, 0.8))

            window.close()


if __name__ == "__main__":
    unittest.main()
