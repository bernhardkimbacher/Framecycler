import unittest
import os
import sys
import logging
from src.framecycler.core.logging_config import get_log_file_path, setup_logging

class TestLogging(unittest.TestCase):
    def test_log_path_resolution(self):
        log_file = get_log_file_path()
        self.assertTrue(log_file.endswith("framecycler.log"))
        
        # Verify OS-specific folder paths
        if sys.platform == "darwin":
            self.assertIn("Library/Logs", log_file)
        elif sys.platform == "win32":
            self.assertIn("AppData", log_file)
        else:
            self.assertIn(".cache", log_file)

    def test_logging_setup(self):
        # Configure logging and verify handlers are attached
        setup_logging()
        
        logger = logging.getLogger()
        self.assertTrue(len(logger.handlers) >= 1)
        
        # Verify stdout interception works
        print("Stdout interception test message")
        
        log_file = get_log_file_path()
        self.assertTrue(os.path.exists(log_file))
        
        with open(log_file, "r", encoding="utf-8") as f:
            content = f.read()
            self.assertIn("NEW SESSION", content)
            self.assertIn("Stdout interception test message", content)
