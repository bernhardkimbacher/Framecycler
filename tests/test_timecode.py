import unittest
from src.framecycler.core.timecode import Timecode

class TestTimecode(unittest.TestCase):
    def test_frame_to_timecode_24fps(self):
        # 24 fps simple tests
        self.assertEqual(Timecode.frame_to_timecode(0, 24.0), "00:00:00:00")
        self.assertEqual(Timecode.frame_to_timecode(24, 24.0), "00:00:01:00")
        self.assertEqual(Timecode.frame_to_timecode(25, 24.0), "00:00:01:01")
        self.assertEqual(Timecode.frame_to_timecode(86400, 24.0), "01:00:00:00")

    def test_timecode_to_frame_24fps(self):
        self.assertEqual(Timecode.timecode_to_frame("00:00:00:00", 24.0), 0)
        self.assertEqual(Timecode.timecode_to_frame("00:00:01:00", 24.0), 24)
        self.assertEqual(Timecode.timecode_to_frame("00:00:01:01", 24.0), 25)
        self.assertEqual(Timecode.timecode_to_frame("01:00:00:00", 24.0), 86400)

    def test_start_frame_offset(self):
        self.assertEqual(Timecode.frame_to_timecode(0, 24.0, start_frame=86400), "01:00:00:00")
        self.assertEqual(Timecode.timecode_to_frame("01:00:00:00", 24.0, start_frame=86400), 0)

if __name__ == "__main__":
    unittest.main()
