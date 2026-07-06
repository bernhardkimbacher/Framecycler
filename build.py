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
        raise FileNotFoundError(
            "Could not find 'cmake.exe' in PATH, python packages, or Visual Studio BuildTools.\n"
            "Please install CMake and ensure it is in your PATH."
        )
    else:
        raise FileNotFoundError(
            "Could not find 'cmake' in PATH or python packages.\n"
            "Please install CMake using your package manager (e.g., 'brew install cmake' on macOS, or 'sudo apt install cmake' on Linux)."
        )

def find_vcvarsall():
    # Look for vcvars64.bat or vcvarsall.bat
    vcvars = r"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
    if os.path.exists(vcvars):
        return vcvars
    vcvars_all = r"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvarsall.bat"
    if os.path.exists(vcvars_all):
        return vcvars_all
    return None

def build_extension():
    print("=== Framecycler C++ Engine Build Script ===")
    
    cmake_path = find_cmake()
    vcvars_path = find_vcvarsall()
    
    print(f"Using CMake: {cmake_path}")
    print(f"Using VCVars: {vcvars_path}")
    
    current_dir = os.path.dirname(os.path.abspath(__file__))
    build_dir = os.path.join(current_dir, "build")
    
    # Recreate build directory to clean previous failures
    if os.path.exists(build_dir):
        shutil.rmtree(build_dir)
    os.makedirs(build_dir, exist_ok=True)
    
    python_exe = sys.executable
    print(f"Targeting Python: {python_exe}")
    
    # On Windows, compile with NMake inside the vcvars64.bat shell
    if sys.platform == "win32" and vcvars_path:
        configure_cmd = (
            f'"{vcvars_path}" amd64 && '
            f'"{cmake_path}" -G "NMake Makefiles" -S "{current_dir}" -B "{build_dir}" '
            f'-DPython_EXECUTABLE="{python_exe}" -DCMAKE_BUILD_TYPE=Release'
        )
        build_cmd = (
            f'"{vcvars_path}" amd64 && '
            f'"{cmake_path}" --build "{build_dir}" --config Release'
        )
        
        print("Configuring with NMake inside VC Environment...")
        subprocess.run(f'cmd.exe /c "{configure_cmd}"', shell=True, check=True)
        
        print("Building with NMake inside VC Environment...")
        subprocess.run(f'cmd.exe /c "{build_cmd}"', shell=True, check=True)
    else:
        # Standard fallback for POSIX or standard path setups
        configure_cmd = [
            cmake_path,
            "-S", current_dir,
            "-B", build_dir,
            f"-DPython_EXECUTABLE={python_exe}",
            "-DCMAKE_BUILD_TYPE=Release"
        ]
        subprocess.run(configure_cmd, check=True)
        
        build_cmd = [
            cmake_path,
            "--build", build_dir,
            "--config", "Release"
        ]
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
    try:
        build_extension()
    except Exception as e:
        print(f"Build failed: {e}")
        sys.exit(1)
