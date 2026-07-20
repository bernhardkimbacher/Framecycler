# Native dependency pins

Framecycler links **OpenImageIO** and **FFmpeg** from the system (macOS/Linux) or
**vcpkg** (Windows). Versions below are the CI-verified set; package CI fails if
installed packages drift outside the expected major.minor (see
`scripts/check_native_deps.py`).

| Platform | Package | Expected version (prefix) | Install |
| :--- | :--- | :--- | :--- |
| macOS (Homebrew) | `openimageio` | `3.1.` | `brew install openimageio` |
| macOS (Homebrew) | `ffmpeg` | `8.` | `brew install ffmpeg` |
| Ubuntu 22.04 apt | `libopenimageio-dev` | (distro) | `apt-get install libopenimageio-dev …` |
| Ubuntu 22.04 apt | `libavformat-dev` | (distro) | `apt-get install libavformat-dev …` |
| Windows vcpkg | `openimageio`, `ffmpeg` | manifest + baseline | `vcpkg install --x-manifest-root=.` |

## Windows

[`vcpkg.json`](../vcpkg.json) pins packages and a `builtin-baseline`. CI/package
workflows install from the manifest root instead of floating package names.
Windows runners ship a shallow/stale `C:\vcpkg`; workflows `git fetch` +
`git reset --hard` to the pinned baseline (then re-bootstrap) before
`vcpkg install` so the on-disk `ports/` and `versions/` match `baseline.json`.
Manifest mode installs into `./vcpkg_installed` (cached in CI along with
`C:\vcpkg\downloads`); classic `C:\vcpkg\installed` is unused.
`build.py` points CMake at that tree (`VCPKG_INSTALLED_DIR` +
`VCPKG_MANIFEST_INSTALL=OFF`) so configure does not reinstall into
`build/vcpkg_installed`.

## macOS / Linux

Brew and apt formula versions float with the runner image. We record **expected
version prefixes** (not exact bottle SHAs) so CI can detect major regressions
without breaking on every Homebrew bottle rebuild.

Verified locally / CI target (2026-07-20):

* Homebrew `openimageio` → `3.1.15.0`
* Homebrew `ffmpeg` → `8.1.2`

Update this table when intentionally bumping native deps.
