import os
import threading
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from src.framecycler.ui.main_window import MainWindow


def _get_app():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class TestFrameReadyThreading(unittest.TestCase):
    """Regression test: CacheEngine notifies frame-ready from a worker thread.

    Previously this used QTimer.singleShot() called directly from that worker
    thread, which never fires because the worker has no Qt event loop. The
    fix routes the notification through a Signal, which Qt automatically
    queues onto the GUI thread regardless of which thread emits it.
    """

    def setUp(self):
        self.app = _get_app()

    def test_frame_ready_signal_runs_handler_on_main_thread(self):
        window = MainWindow()
        self.addCleanup(window.close)

        main_thread_name = threading.current_thread().name
        observed_threads = []
        done = threading.Event()

        def on_ready(media_path, frame_index):
            observed_threads.append(threading.current_thread().name)
            done.set()

        window._frame_ready_signal.connect(on_ready)

        def emit_from_worker():
            window._frame_ready_signal.emit("/tmp/fake.exr", 42)

        worker = threading.Thread(target=emit_from_worker)
        worker.start()
        worker.join()

        # The signal must be delivered via the GUI thread's event loop.
        deadline = time.time() + 2.0
        while not done.is_set() and time.time() < deadline:
            self.app.processEvents()

        self.assertTrue(done.is_set(), "frame-ready signal was never delivered")
        self.assertEqual(observed_threads, [main_thread_name])

    def test_on_cache_frame_ready_applies_frame_once_notified(self):
        window = MainWindow()
        self.addCleanup(window.close)

        calls = []
        orig_apply = window._apply_cached_frames

        def traced_apply(frame):
            calls.append(threading.current_thread().name)
            return orig_apply(frame)

        window._apply_cached_frames = traced_apply

        # Build a minimal one-shot session so the path-based ready handler
        # can match the playhead version.
        from src.framecycler.core import otio_model
        from src.framecycler.core.media_source import MediaSource
        from src.framecycler.core.playback_plan import PlaybackPlan, Segment, VersionSlot

        class _FakeCache:
            native_cache = None

            def get_frame(self, frame_index):
                return None

            def set_playhead(self, frame_index, direction=1):
                pass

            def get_cached_frames(self):
                return set()

            def close(self):
                pass

            def add_frame_ready_callback(self, callback):
                pass

            def set_playback_range(self, local_in, local_out):
                pass

        fake_path = os.path.abspath("fake.exr")
        source = MediaSource(
            path=fake_path,
            decoder=type("D", (), {"frame_numbers": []})(),
            cache=_FakeCache(),
            frame_count=1,
            fps=24.0,
            decoder_start_frame=0,
        )
        clip = otio_model.clip_from_media(
            fake_path, {"fps": 24.0, "frame_count": 1, "start_frame": 0}
        )
        stack = otio_model.wrap_shot_stack(clip)
        version = VersionSlot(clip=clip, source=source, is_active=True, is_compare=True)
        segment = Segment(
            index=0,
            global_start=0,
            global_end=0,
            stack=stack,
            versions=[version],
            rate=24.0,
        )
        window.session.plan = PlaybackPlan(segments=[segment], global_start=0, global_end=0)
        window.session.media_pool._entries[fake_path] = (source, 1)
        window.current_frame = 0
        window.start_frame = 0
        window.end_frame = 0

        worker = threading.Thread(
            target=lambda: window._frame_ready_signal.emit(fake_path, 0)
        )
        worker.start()
        worker.join()

        deadline = time.time() + 2.0
        while not calls and time.time() < deadline:
            self.app.processEvents()

        self.assertTrue(calls, "_apply_cached_frames was never invoked after frame-ready")
        self.assertEqual(calls, [threading.current_thread().name])


if __name__ == "__main__":
    unittest.main()
