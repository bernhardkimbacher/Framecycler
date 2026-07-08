"""Platform memory detection for Decode/Display cache settings."""
from __future__ import annotations

import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional


BYTES_PER_GB = 1024**3


@dataclass(frozen=True)
class PlatformCacheLimits:
    decode_max_gb: float
    display_max_gb: float
    coupled: bool
    combined_max_gb: float
    system_memory_gb: float
    vram_gb: float
    platform_label: str


def _bytes_to_gb(value: int) -> float:
    return max(0.0, value / BYTES_PER_GB)


def get_total_system_memory_gb() -> float:
    if sys.platform == "darwin":
        try:
            out = subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip()
            return _bytes_to_gb(int(out))
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    elif sys.platform == "win32":
        try:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return _bytes_to_gb(int(stat.ullTotalPhys))
        except (OSError, AttributeError):
            pass
    else:
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        return _bytes_to_gb(kb * 1024)
        except (OSError, ValueError, IndexError):
            pass

    return 8.0


def _linux_vram_from_sysfs_gb() -> Optional[float]:
    drm_root = "/sys/class/drm"
    if not os.path.isdir(drm_root):
        return None

    total = 0
    found = False
    for entry in os.listdir(drm_root):
        mem_path = os.path.join(drm_root, entry, "device", "mem_info_vram_total")
        if not os.path.isfile(mem_path):
            continue
        try:
            with open(mem_path, "r", encoding="utf-8") as handle:
                total += int(handle.read().strip())
                found = True
        except (OSError, ValueError):
            continue

    if found and total > 0:
        return _bytes_to_gb(total)
    return None


def _windows_vram_gb() -> Optional[float]:
    try:
        import ctypes
        from ctypes import wintypes

        class DXGI_ADAPTER_DESC(ctypes.Structure):
            _fields_ = [
                ("Description", wintypes.WCHAR * 128),
                ("VendorId", wintypes.UINT),
                ("DeviceId", wintypes.UINT),
                ("SubSysId", wintypes.UINT),
                ("Revision", wintypes.UINT),
                ("DedicatedVideoMemory", ctypes.c_size_t),
                ("DedicatedSystemMemory", ctypes.c_size_t),
                ("SharedSystemMemory", ctypes.c_size_t),
                ("AdapterLuid", wintypes.LARGE_INTEGER),
            ]

        class DXGI_ADAPTER_DESC1(DXGI_ADAPTER_DESC):
            _fields_ = [("Flags", wintypes.UINT)]

        dxgi = ctypes.windll.dxgi.CreateDXGIFactory1
        dxgi.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        dxgi.restype = ctypes.c_int

        factory = ctypes.c_void_p()
        if dxgi(ctypes.byref(factory)) != 0:
            return None

        vtbl = ctypes.cast(factory, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents

        enum_adapters = ctypes.WINFUNCTYPE(
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_void_p),
        )(vtbl[7])

        max_vram = 0
        index = 0
        while True:
            adapter = ctypes.c_void_p()
            hr = enum_adapters(factory, index, ctypes.byref(adapter))
            if hr != 0:
                break
            adapter_vtbl = ctypes.cast(adapter, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            get_desc = ctypes.WINFUNCTYPE(
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.POINTER(DXGI_ADAPTER_DESC1),
            )(adapter_vtbl[10])
            desc = DXGI_ADAPTER_DESC1()
            if get_desc(adapter, ctypes.byref(desc)) == 0:
                max_vram = max(max_vram, int(desc.DedicatedVideoMemory))
            index += 1

        if max_vram > 0:
            return _bytes_to_gb(max_vram)
    except (OSError, AttributeError, ImportError):
        pass
    return None


def get_total_vram_gb() -> float:
    if sys.platform == "darwin":
        return get_total_system_memory_gb()

    if sys.platform == "win32":
        vram = _windows_vram_gb()
        if vram is not None and vram > 0:
            return vram

    if sys.platform.startswith("linux"):
        vram = _linux_vram_from_sysfs_gb()
        if vram is not None and vram > 0:
            return vram

    # Fallback: conservative fraction of system RAM for integrated GPUs.
    return max(1.0, get_total_system_memory_gb() * 0.5)


def get_platform_cache_limits() -> PlatformCacheLimits:
    system_gb = get_total_system_memory_gb()
    vram_gb = get_total_vram_gb()
    label = platform.system()

    if sys.platform == "darwin":
        return PlatformCacheLimits(
            decode_max_gb=system_gb,
            display_max_gb=system_gb,
            coupled=True,
            combined_max_gb=system_gb,
            system_memory_gb=system_gb,
            vram_gb=vram_gb,
            platform_label=label,
        )

    return PlatformCacheLimits(
        decode_max_gb=system_gb,
        display_max_gb=vram_gb,
        coupled=False,
        combined_max_gb=system_gb + vram_gb,
        system_memory_gb=system_gb,
        vram_gb=vram_gb,
        platform_label=label,
    )


def gb_to_slider_ticks(gb: float) -> int:
    return max(0, int(round(gb * 10)))


def slider_ticks_to_gb(ticks: int) -> float:
    return max(0.0, ticks / 10.0)


def clamp_cache_limits(
    decode_gb: float,
    display_gb: float,
    limits: PlatformCacheLimits,
) -> tuple[float, float]:
    decode = max(0.0, min(decode_gb, limits.decode_max_gb))
    display = max(0.0, min(display_gb, limits.display_max_gb))

    if limits.coupled:
        combined = decode + display
        if combined > limits.combined_max_gb:
            overflow = combined - limits.combined_max_gb
            if display >= overflow:
                display -= overflow
            else:
                overflow -= display
                display = 0.0
                decode = max(0.0, decode - overflow)

    return round(decode, 1), round(display, 1)


def cache_warning_text(
    decode_gb: float,
    display_gb: float,
    limits: PlatformCacheLimits,
    threshold: float = 0.8,
) -> str:
    if limits.coupled:
        if limits.combined_max_gb <= 0:
            return ""
        ratio = (decode_gb + display_gb) / limits.combined_max_gb
        if ratio > threshold:
            return (
                f"Warning: combined cache ({decode_gb + display_gb:.1f} GB) exceeds "
                f"{int(threshold * 100)}% of system memory ({limits.combined_max_gb:.1f} GB). "
                "This may starve the system."
            )
        return ""

    warnings = []
    if limits.decode_max_gb > 0 and decode_gb / limits.decode_max_gb > threshold:
        warnings.append(
            f"Decode cache ({decode_gb:.1f} GB) exceeds {int(threshold * 100)}% of "
            f"system RAM ({limits.decode_max_gb:.1f} GB)."
        )
    if limits.display_max_gb > 0 and display_gb / limits.display_max_gb > threshold:
        warnings.append(
            f"Display cache ({display_gb:.1f} GB) exceeds {int(threshold * 100)}% of "
            f"available VRAM ({limits.display_max_gb:.1f} GB)."
        )
    if warnings:
        return "Warning: " + " ".join(warnings) + " This may starve the system."
    return ""
