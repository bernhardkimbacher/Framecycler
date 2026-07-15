import os
import sys

if sys.platform == "win32":
    try:
        import PySide6
        qt_bin_dir = os.path.join(os.path.dirname(PySide6.__file__), "Qt", "bin")
        if os.path.isdir(qt_bin_dir):
            os.add_dll_directory(qt_bin_dir)
    except ImportError:
        pass


# Set default shipped OCIO config if not specified by user
if "OCIO" not in os.environ:
    current_dir = os.path.dirname(os.path.abspath(__file__))
    bundled_config = os.path.join(current_dir, "color", "studio_config", "config.ocio")
    if os.path.exists(bundled_config):
        os.environ["OCIO"] = bundled_config
