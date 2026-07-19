"""Parity and cadence tests for the C++ transport clock (finding #3)."""

from __future__ import annotations

import time
import unittest

from src.framecycler import framecycler_engine as eng
from src.framecycler.core.playback_timing import advance_playback, realtime_steps


class TestTransportAdvanceParity(unittest.TestCase):
    def test_realtime_steps_match(self):
        for elapsed, fps in [
            (0.0, 24.0),
            (1.0 / 24.0, 24.0),
            (5.0 / 23.976, 23.976),
            (1.0, 30.0),
        ]:
            self.assertEqual(
                eng.transport_realtime_steps(elapsed, fps),
                realtime_steps(elapsed, fps),
            )

    def test_advance_matches_python(self):
        cases = [
            (10, 1, 1, 0, 100, "loop"),
            (10, 1, 0, 0, 100, "loop"),
            (10, 1, 1, 0, 10, "once"),
            (10, 1, 1, 0, 10, "loop"),
            (10, 1, 1, 0, 10, "bounce"),
            (0, 1, 5, 0, 100, "loop"),
            (5, -1, 3, 0, 10, "loop"),
            (1, -1, 1, 0, 10, "bounce"),
            (0, -1, 1, 0, 10, "once"),
        ]
        for current, direction, steps, in_pt, out_pt, mode in cases:
            with self.subTest(
                current=current, direction=direction, steps=steps, mode=mode
            ):
                py = advance_playback(current, direction, steps, in_pt, out_pt, mode)
                cpp = eng.transport_advance_playback(
                    current, direction, steps, in_pt, out_pt, mode
                )
                if py.frame is None:
                    self.assertLess(cpp.frame, 0)
                else:
                    self.assertEqual(cpp.frame, py.frame)
                self.assertEqual(cpp.direction, py.direction)
                self.assertEqual(cpp.stop, py.stop)


class TestTransportDecoderMapping(unittest.TestCase):
    def test_table_and_offset_mapping(self):
        prog = eng.TransportProgram()
        slot = eng.TransportSlotMapping()
        slot.source_index = 0
        slot.segment_global_start = 100
        slot.segment_global_end = 102
        slot.decoder_frames = [1000, 1001, 1002]
        prog.slots = [slot]
        self.assertEqual(eng.transport_decoder_frame_for_source(prog, 0, 100), 1000)
        self.assertEqual(eng.transport_decoder_frame_for_source(prog, 0, 102), 1002)
        self.assertEqual(eng.transport_decoder_frame_for_source(prog, 1, 100), -1)


class TestTransportClockTick(unittest.TestCase):
    def _program(self, **kwargs):
        prog = eng.TransportProgram()
        prog.playing = True
        prog.direction = 1
        prog.fps = 24.0
        prog.in_point = 0
        prog.out_point = 100
        prog.current_frame = 0
        prog.segment_global_start = 0
        prog.segment_global_end = 100
        prog.hold_at_segment_bounds = False
        prog.timing_mode = eng.TransportTimingMode.Realtime
        prog.loop_mode = eng.TransportLoopMode.Loop
        for key, value in kwargs.items():
            setattr(prog, key, value)
        return prog

    def test_realtime_catch_up(self):
        clock = eng.TransportClock()
        clock.set_program(self._program(current_frame=0, fps=48.0))
        clock.play()
        time.sleep(0.10)
        result = clock.tick_now()
        self.assertTrue(result.moved)
        # ~0.10s * 48fps ≈ 4–5 frames; allow wide CI jitter.
        self.assertGreaterEqual(result.frame, 2)
        self.assertLessEqual(result.frame, 12)

    def test_every_frame_and_boundary(self):
        clock = eng.TransportClock()
        prog = self._program(
            timing_mode=eng.TransportTimingMode.EveryFrame,
            fps=1000.0,
            current_frame=4,
            segment_global_start=0,
            segment_global_end=5,
            hold_at_segment_bounds=True,
            in_point=0,
            out_point=20,
        )
        clock.set_program(prog)
        clock.play()
        time.sleep(0.003)
        r1 = clock.tick_now()
        self.assertTrue(r1.moved)
        self.assertEqual(r1.frame, 5)
        self.assertFalse(r1.segment_boundary)

        # Re-arm on the segment edge; next step leaves the segment.
        prog.current_frame = clock.current_frame()
        prog.playing = True
        clock.set_program(prog)
        clock.play()
        time.sleep(0.003)
        r2 = clock.tick_now()
        self.assertTrue(r2.segment_boundary)
        self.assertFalse(clock.is_playing())

    def test_every_frame_gates_on_predicate(self):
        clock = eng.TransportClock()
        prog = self._program(
            timing_mode=eng.TransportTimingMode.EveryFrame,
            fps=1000.0,
            current_frame=0,
        )
        clock.set_program(prog)
        clock.play()
        time.sleep(0.003)
        blocked = clock.tick_now(lambda _frame: False)
        self.assertFalse(blocked.moved)
        self.assertEqual(clock.current_frame(), 0)

        allowed = clock.tick_now(lambda _frame: True)
        self.assertTrue(allowed.moved)
        self.assertEqual(allowed.frame, 1)


class TestPresentTimingRing(unittest.TestCase):
    def test_api_clear_drain_empty(self):
        renderer = eng.RhiRenderer()
        self.assertTrue(hasattr(renderer, "set_present_timing_enabled"))
        self.assertTrue(hasattr(renderer, "clear_present_timings"))
        self.assertTrue(hasattr(renderer, "drain_present_timings"))
        renderer.clear_present_timings()
        renderer.set_present_timing_enabled(True)
        samples = renderer.drain_present_timings()
        self.assertEqual(list(samples), [])
        renderer.set_present_timing_enabled(False)

    def test_null_rhi_present_samples_monotonic(self):
        """Null-RHI present loop in a subprocess (bare QWindow can SIGBUS on macOS)."""
        import os
        import pathlib
        import subprocess
        import sys

        repo = pathlib.Path(__file__).resolve().parents[1]
        smoke = pathlib.Path(__file__).with_name("_present_timing_null_smoke.py")
        env = dict(**os.environ)
        env["PYTHONPATH"] = str(repo) + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
        )
        proc = subprocess.run(
            [sys.executable, str(smoke)],
            cwd=str(repo),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        out = (proc.stdout or "").strip()
        if proc.returncode < 0 or proc.returncode == 139:
            self.skipTest("Null RHI present loop crashed (known bare-QWindow issue)")
        if out.startswith("SKIP:"):
            self.skipTest(out)
        if proc.returncode != 0:
            self.fail(f"present timing subprocess failed: {out}\n{proc.stderr}")
        self.assertTrue(out.startswith("OK:"), out)


if __name__ == "__main__":
    unittest.main()
