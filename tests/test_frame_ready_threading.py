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

        def on_ready(source_index, frame_index):
            observed_threads.append(threading.current_thread().name)
            done.set()

        window._frame_ready_signal.connect(on_ready)

        def emit_from_worker():
            window._frame_ready_signal.emit(0, 42)

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

        # Simulate a source at frame 0 with decoder_start_frame 0, then notify
        # "frame 0 ready" from a background thread exactly like CacheEngine does.
        class _FakeCache:
            def get_frame(self, frame_index):
                return None

            def set_playhead(self, frame_index, direction=1):
                pass

            def get_cached_frames(self):
                return set()

            def close(self):
                pass

        class _FakeSource:
            path = "fake.exr"
            frame_count = 1
            timeline_offset = 0
            decoder_start_frame = 0
            cache = _FakeCache()
            decoder = type("D", (), {"frame_numbers": []})()

        window.sources = [_FakeSource()]
        window.current_frame = 0

        worker = threading.Thread(target=lambda: window._frame_ready_signal.emit(0, 0))
        worker.start()
        worker.join()

        deadline = time.time() + 2.0
        while not calls and time.time() < deadline:
            self.app.processEvents()

        self.assertTrue(calls, "_apply_cached_frames was never invoked after frame-ready")
        self.assertEqual(calls, [threading.current_thread().name])


if __name__ == "__main__":
    unittest.main()
