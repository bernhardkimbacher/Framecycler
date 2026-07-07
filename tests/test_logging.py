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
        setup_logging()

        logger = logging.getLogger()
        self.assertTrue(len(logger.handlers) >= 1)

        print("Stdout interception test message")

        log_file = get_log_file_path()
        self.assertTrue(os.path.exists(log_file))

        with open(log_file, "r", encoding="utf-8") as f:
            content = f.read()
            self.assertIn("NEW SESSION", content)
            self.assertIn("Stdout interception test message", content)

    def test_logging_setup_without_stdout(self):
        saved_stdout = sys.stdout
        saved_dunder_stdout = sys.__stdout__
        try:
            sys.__stdout__ = None
            sys.stdout = None
            setup_logging()
            logger = logging.getLogger()
            has_file_handler = any(
                isinstance(handler, logging.FileHandler) for handler in logger.handlers
            )
            self.assertTrue(has_file_handler)
            has_console_handler = any(
                isinstance(handler, logging.StreamHandler)
                and not isinstance(handler, logging.FileHandler)
                for handler in logger.handlers
            )
            self.assertFalse(has_console_handler)
            logging.getLogger().info("Logging without stdout smoke test")
        finally:
            sys.__stdout__ = saved_dunder_stdout
            sys.stdout = saved_stdout
