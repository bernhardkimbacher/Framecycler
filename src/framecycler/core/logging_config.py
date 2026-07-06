import os
import sys
import logging
from datetime import datetime

from .version import get_application_version

def get_log_file_path():
    app_name = "Framecycler"
    if sys.platform == "darwin":
        # macOS standard log folder: ~/Library/Logs/Framecycler/framecycler.log
        return os.path.expanduser(f"~/Library/Logs/{app_name}/framecycler.log")
    elif sys.platform == "win32":
        # Windows standard log folder: %LOCALAPPDATA%\Framecycler\logs\framecycler.log
        local_app_data = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/AppData/Local")
        return os.path.join(local_app_data, app_name, "logs", "framecycler.log")
    else:
        # Linux standard: ~/.cache/framecycler/log/framecycler.log
        cache_dir = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
        return os.path.join(cache_dir, app_name.lower(), "log", "framecycler.log")

from PySide6.QtCore import qInstallMessageHandler, QtMsgType

class StreamToLogger:
    def __init__(self, logger, log_level):
        self.logger = logger
        self.log_level = log_level

    def write(self, buf):
        for line in buf.rstrip().splitlines():
            l = line.rstrip()
            if l:
                self.logger.log(self.log_level, l)

    def flush(self):
        pass

def qt_message_handler(mode, context, message):
    log_msg = f"Qt: {message}"
    if mode == QtMsgType.QtDebugMsg:
        logging.debug(log_msg)
    elif mode == QtMsgType.QtWarningMsg:
        logging.warning(log_msg)
    elif mode == QtMsgType.QtCriticalMsg:
        logging.critical(log_msg)
    elif mode == QtMsgType.QtFatalMsg:
        logging.fatal(log_msg)
    else:
        logging.info(log_msg)

def log_uncaught_exception(exctype, value, tb):
    import traceback
    err_msg = "".join(traceback.format_exception(exctype, value, tb))
    logging.critical(f"Uncaught Exception:\n{err_msg}")
    sys.__excepthook__(exctype, value, tb)

_SESSION_SEPARATOR_WIDTH = 80

def _write_session_separator(log_file: str) -> None:
    """Append a highly visible divider before each app launch."""
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    version = get_application_version()
    banner = (
        f"  NEW SESSION  {started_at}  |  {version}  |  PID {os.getpid()}"
    )
    rule = "=" * _SESSION_SEPARATOR_WIDTH
    block = f"\n{rule}\n{banner}\n{rule}\n\n"
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(block)
    except OSError as e:
        print(f"Failed to write session separator to '{log_file}': {e}")

def setup_logging():
    log_file = get_log_file_path()
    log_dir = os.path.dirname(log_file)
    try:
        os.makedirs(log_dir, exist_ok=True)
    except Exception as e:
        print(f"Failed to create log directory '{log_dir}': {e}")
        # Fall back to user home directory
        log_file = os.path.expanduser("~/framecycler.log")

    # Configure root logger
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    # Formatter
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s:%(lineno)d] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Save original streams before redirecting to prevent infinite logging loops
    original_stdout = sys.__stdout__
    original_stderr = sys.__stderr__

    # Console Handler (use original stdout to prevent recursion loops)
    console_handler = logging.StreamHandler(original_stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # File Handler
    try:
        _write_session_separator(log_file)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        logging.info(f"Logging initialized. Writing to: {log_file}")
    except Exception as e:
        print(f"Failed to initialize file logging to '{log_file}': {e}")

    # Set up global exception hook
    sys.excepthook = log_uncaught_exception

    # Redirect sys.stdout and sys.stderr to standard logging
    sys.stdout = StreamToLogger(logging.getLogger("STDOUT"), logging.INFO)
    sys.stderr = StreamToLogger(logging.getLogger("STDERR"), logging.ERROR)

    # Redirect Qt internal messages/warnings to standard logging
    qInstallMessageHandler(qt_message_handler)
