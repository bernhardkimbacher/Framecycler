try:
    import OpenImageIO
except ImportError:
    pass

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

    # Manifest-mode installs land in ./vcpkg_installed (or build/vcpkg_installed).
    # Classic installs used C:\vcpkg\installed — keep as fallback.
    _pkg_root = os.path.dirname(os.path.abspath(__file__))
    _repo_root = os.path.dirname(os.path.dirname(_pkg_root))
    _vcpkg_bin_candidates = [
        os.path.join(_repo_root, "vcpkg_installed", "x64-windows", "bin"),
        os.path.join(_repo_root, "build", "vcpkg_installed", "x64-windows", "bin"),
        os.path.join(_repo_root, "build", "Release"),
        _pkg_root,  # build.py copies deps next to the .pyd
    ]
    _vcpkg_root = os.environ.get("VCPKG_INSTALLATION_ROOT") or "C:\\vcpkg"
    _vcpkg_bin_candidates.append(
        os.path.join(_vcpkg_root, "installed", "x64-windows", "bin")
    )
    for _vcpkg_bin in _vcpkg_bin_candidates:
        if os.path.isdir(_vcpkg_bin):
            os.add_dll_directory(_vcpkg_bin)


# Set default shipped OCIO config if not specified by user
if "OCIO" not in os.environ:
    current_dir = os.path.dirname(os.path.abspath(__file__))
    bundled_config = os.path.join(current_dir, "color", "studio_config", "config.ocio")
    if os.path.exists(bundled_config):
        os.environ["OCIO"] = bundled_config
