import os
import sys
import subprocess
import shutil

def find_cmake():
    if shutil.which("cmake"):
        return "cmake"
    vs_cmake = r"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
    if os.path.exists(vs_cmake):
        return vs_cmake
    raise FileNotFoundError("Could not find cmake.exe in PATH or Visual Studio BuildTools.")

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
        
    print("=== Build Completed Successfully ===")

if __name__ == "__main__":
    try:
        build_extension()
    except Exception as e:
        print(f"Build failed: {e}")
        sys.exit(1)
