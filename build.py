import argparse
import os
import sys
import subprocess
import shutil

def find_cmake():
    # 1. Check if 'cmake' is in PATH
    if shutil.which("cmake"):
        return "cmake"
        
    # 2. Check Python package fallback (if cmake was installed via pip)
    try:
        import cmake
        cmake_bin = os.path.join(cmake.CMAKE_BIN_DIR, "cmake")
        if os.path.exists(cmake_bin):
            return cmake_bin
        cmake_exe = os.path.join(cmake.CMAKE_BIN_DIR, "cmake.exe")
        if os.path.exists(cmake_exe):
            return cmake_exe
    except ImportError:
        pass

    # 3. Check Windows-specific Visual Studio CMake path
    if sys.platform == "win32":
        vs_cmake = r"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
        if os.path.exists(vs_cmake):
            return vs_cmake
        vs_ent_cmake = r"C:\Program Files\Microsoft Visual Studio\2022\Enterprise\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
        if os.path.exists(vs_ent_cmake):
            return vs_ent_cmake
        raise FileNotFoundError(
            "Could not find 'cmake.exe' in PATH, python packages, or Visual Studio.\n"
            "Please install CMake and ensure it is in your PATH."
        )
    else:
        raise FileNotFoundError(
            "Could not find 'cmake' in PATH or python packages.\n"
            "Please install CMake using your package manager (e.g., 'brew install cmake' on macOS, or 'sudo apt install cmake' on Linux)."
        )

def build_extension(*, clean: bool = False):
    print("=== Framecycler C++ Engine Build Script ===")

    version_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "generate_version.py")
    if os.path.exists(version_script):
        print("Generating build version metadata...")
        subprocess.run([sys.executable, version_script], check=True)
    
    cmake_path = find_cmake()
    
    print(f"Using CMake: {cmake_path}")
    
    current_dir = os.path.dirname(os.path.abspath(__file__))
    build_dir = os.path.join(current_dir, "build")
    
    if clean and os.path.exists(build_dir):
        print(f"Cleaning build directory (--clean): {build_dir}")
        shutil.rmtree(build_dir)
    os.makedirs(build_dir, exist_ok=True)
    
    python_exe = sys.executable
    print(f"Targeting Python: {python_exe}")
    
    # Resolve Qt SDK and PySide6 Qt lib paths
    sys.path.insert(0, current_dir)
    from scripts import qt_sdk
    try:
        qt_sdk_path_str = str(qt_sdk.ensure_qt_sdk(install=True))
        pyside_qt_lib_path_str = str(qt_sdk.pyside6_qt_lib())
        print(f"Using Qt SDK path: {qt_sdk_path_str}")
        print(f"Using PySide6 Qt lib path: {pyside_qt_lib_path_str}")
    except Exception as exc:
        print(f"Error: Failed to resolve Qt SDK or PySide6 Qt lib: {exc}", file=sys.stderr)
        sys.exit(1)
    
    vcpkg_toolchain = ""
    vcpkg_root = os.environ.get("VCPKG_INSTALLATION_ROOT")
    if vcpkg_root:
        toolchain_path = os.path.join(vcpkg_root, "scripts", "buildsystems", "vcpkg.cmake")
        if os.path.exists(toolchain_path):
            vcpkg_toolchain = toolchain_path

    # Standard platform-independent configuration and build flow
    configure_cmd = [
        cmake_path,
        "-S", current_dir,
        "-B", build_dir,
        f"-DPython_EXECUTABLE={python_exe}",
        "-DCMAKE_BUILD_TYPE=Release",
    ]
    if qt_sdk_path_str:
        configure_cmd.append(f"-DQT_SDK_PATH={qt_sdk_path_str}")
    if pyside_qt_lib_path_str:
        configure_cmd.append(f"-DPYSIDE6_QT_LIB={pyside_qt_lib_path_str}")
    if vcpkg_toolchain:
        configure_cmd.append(f"-DCMAKE_TOOLCHAIN_FILE={vcpkg_toolchain}")
        
    print(f"Configuring project: {configure_cmd}")
    subprocess.run(configure_cmd, check=True)
    
    build_cmd = [
        cmake_path,
        "--build", build_dir,
        "--config", "Release"
    ]
    print(f"Building project: {build_cmd}")
    subprocess.run(build_cmd, check=True)
        
    print("Locating compiled library module...")
    ext_suffix = ".pyd" if sys.platform == "win32" else ".so"
    
    dest_dir = os.path.join(current_dir, "src", "framecycler")
    os.makedirs(dest_dir, exist_ok=True)
    
    copied = False
    for root, dirs, files in os.walk(build_dir):
        for file in files:
            if file.endswith(ext_suffix) and "framecycler_engine" in file:
                src_path = os.path.join(root, file)
                dest_path = os.path.join(dest_dir, file)
                print(f"Copying {src_path} -> {dest_path}")
                shutil.copy2(src_path, dest_path)
                copied = True
                break
                
    if not copied:
        raise FileNotFoundError("Could not locate compiled framecycler_engine binary.")

    # On Windows, copy all dependency DLLs from vcpkg alongside the compiled .pyd module
    # so they are automatically resolved by the Windows dynamic loader.
    if sys.platform == "win32":
        # Prefer manifest-mode installs (./vcpkg_installed or build/vcpkg_installed),
        # then fall back to classic C:\vcpkg\installed.
        vcpkg_bin_candidates = [
            os.path.join(current_dir, "vcpkg_installed", "x64-windows", "bin"),
            os.path.join(build_dir, "vcpkg_installed", "x64-windows", "bin"),
            os.path.join(build_dir, "Release"),
        ]
        vcpkg_root = os.environ.get("VCPKG_INSTALLATION_ROOT") or "C:\\vcpkg"
        vcpkg_bin_candidates.append(
            os.path.join(vcpkg_root, "installed", "x64-windows", "bin")
        )
        copied_dll_names: set[str] = set()
        for vcpkg_bin in vcpkg_bin_candidates:
            if not os.path.isdir(vcpkg_bin):
                continue
            print(f"Copying dependency DLLs from {vcpkg_bin} to {dest_dir}...")
            for file in os.listdir(vcpkg_bin):
                if not file.endswith(".dll") or file in copied_dll_names:
                    continue
                shutil.copy2(os.path.join(vcpkg_bin, file), os.path.join(dest_dir, file))
                copied_dll_names.add(file)
        if not copied_dll_names:
            print(
                "Warning: no vcpkg dependency DLLs found to copy "
                f"(checked: {', '.join(vcpkg_bin_candidates)})"
            )

        # Copy Qt DLLs from the exact SDK used at link time
        qt_bin_dir = os.path.join(qt_sdk_path_str, "bin")
        if os.path.isdir(qt_bin_dir):
            print(f"Copying Qt DLLs from {qt_bin_dir} to {dest_dir}...")
            for file in os.listdir(qt_bin_dir):
                if file.endswith(".dll"):
                    shutil.copy2(os.path.join(qt_bin_dir, file), os.path.join(dest_dir, file))
    
    # On macOS, ad-hoc sign the compiled binary to satisfy Gatekeeper.
    # Without this, macOS provenance tracking quarantines freshly compiled .so files
    # and kills the process with SIGKILL (exit code 9) on import.
    if sys.platform == "darwin":
        so_path = os.path.join(dest_dir, next(
            f for f in os.listdir(dest_dir)
            if "framecycler_engine" in f and f.endswith(".so")
        ))
        print(f"Signing binary for macOS Gatekeeper: {so_path}")
        sign_result = subprocess.run(
            ["codesign", "--force", "--sign", "-", so_path],
            capture_output=True, text=True
        )
        if sign_result.returncode != 0:
            print(f"Warning: codesign failed (non-fatal): {sign_result.stderr.strip()}")
        else:
            print("Binary signed successfully.")
        
    print("=== Build Completed Successfully ===")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build framecycler_engine")
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Delete the CMake build/ directory before configuring (CI/package default)",
    )
    cli = parser.parse_args()
    try:
        build_extension(clean=cli.clean)
    except Exception as e:
        print(f"Build failed: {e}")
        sys.exit(1)
