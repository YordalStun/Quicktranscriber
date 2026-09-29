"""Hardware detection and GPU library setup."""

from __future__ import annotations

import functools
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0  # CREATE_NO_WINDOW


def run_quiet(cmd: list[str], timeout: float = 10) -> Optional[str]:
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, creationflags=_NO_WINDOW
        )
        return out.stdout if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _nvidia_smi() -> Optional[str]:
    exe = shutil.which("nvidia-smi")
    if not exe and IS_WINDOWS:
        candidate = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvidia-smi.exe"
        if candidate.exists():
            exe = str(candidate)
    return exe


@functools.lru_cache(maxsize=1)
def nvidia_gpus() -> list[dict[str, Any]]:
    exe = _nvidia_smi()
    if not exe:
        return []
    out = run_quiet(
        [exe, "--query-gpu=name,memory.total,memory.free,driver_version", "--format=csv,noheader,nounits"]
    )
    gpus = []
    if out:
        for line in out.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 4:
                try:
                    gpus.append(
                        {
                            "name": parts[0],
                            "vram_mb": int(float(parts[1])),
                            "free_mb": int(float(parts[2])),
                            "driver": parts[3],
                        }
                    )
                except ValueError:
                    continue
    return gpus


@functools.lru_cache(maxsize=1)
def cuda_driver_version() -> Optional[tuple[int, int]]:
    """Highest CUDA version the installed NVIDIA driver supports, e.g. (12, 8)."""
    exe = _nvidia_smi()
    if not exe:
        return None
    out = run_quiet([exe])
    if not out:
        return None
    m = re.search(r"CUDA Version:\s*(\d+)\.(\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else None


def free_vram_mb() -> int:
    exe = _nvidia_smi()
    if not exe:
        return 0
    out = run_quiet([exe, "--query-gpu=memory.free", "--format=csv,noheader,nounits"])
    try:
        return max(int(float(x)) for x in out.split()) if out else 0
    except ValueError:
        return 0


def _cpu_name() -> str:
    name = ""
    if IS_WINDOWS:
        try:
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
            )
            name = winreg.QueryValueEx(key, "ProcessorNameString")[0]
        except OSError:
            name = platform.processor()
    elif IS_MAC:
        name = run_quiet(["sysctl", "-n", "machdep.cpu.brand_string"]) or platform.processor()
    else:
        try:
            for line in Path("/proc/cpuinfo").read_text().splitlines():
                if line.lower().startswith("model name"):
                    name = line.split(":", 1)[1]
                    break
        except OSError:
            pass
    return (name or platform.machine()).strip()


@functools.lru_cache(maxsize=1)
def info() -> dict[str, Any]:
    import psutil

    ram = psutil.virtual_memory().total
    gpus = nvidia_gpus()
    apple_silicon = IS_MAC and platform.machine() == "arm64"
    cores = psutil.cpu_count(logical=False) or os.cpu_count() or 2
    threads = psutil.cpu_count(logical=True) or cores
    cuda_libs = cuda_libraries_installed()
    return {
        "os": platform.system(),
        "os_version": platform.release(),
        "arch": platform.machine(),
        "cpu": _cpu_name(),
        "cores": cores,
        "threads": threads,
        "ram_gb": round(ram / 1024**3, 1),
        "gpus": gpus,
        "nvidia": bool(gpus),
        "vram_gb": round(max((g["vram_mb"] for g in gpus), default=0) / 1024, 1),
        "cuda_driver": ".".join(map(str, cuda_driver_version() or ())) or None,
        "cuda_libs": cuda_libs,
        "apple_silicon": apple_silicon,
        "tier": _tier(ram / 1024**3, cores, gpus, apple_silicon),
    }


def _tier(ram_gb: float, cores: int, gpus: list, apple: bool) -> str:
    vram = max((g["vram_mb"] for g in gpus), default=0) / 1024
    if vram >= 10:
        return "gpu-high"
    if vram >= 5.5:
        return "gpu-mid"
    if vram >= 3.5:
        return "gpu-low"
    if apple and ram_gb >= 15:
        return "apple-high"
    if ram_gb >= 15 and cores >= 6:
        return "cpu-high"
    if ram_gb >= 7:
        return "cpu-mid"
    return "cpu-low"


def cpu_threads() -> int:
    """Threads to use for heavy CPU work: physical cores, leaving one for the UI."""
    info_ = info()
    return max(1, min(info_["cores"], info_["threads"] - 1))


# --- CUDA libraries for faster-whisper (CTranslate2 needs cuBLAS 12 + cuDNN 9) ----


def _nvidia_package_dirs() -> list[Path]:
    """bin/lib folders of the nvidia-* pip packages inside our venv."""
    dirs: list[Path] = []
    for base in map(Path, sys.path):
        nvidia = base / "nvidia"
        if not nvidia.is_dir():
            continue
        for pkg in ("cublas", "cudnn", "cuda_runtime", "cuda_nvrtc"):
            for sub in ("bin", "lib"):
                d = nvidia / pkg / sub
                if d.is_dir():
                    dirs.append(d)
    return dirs


def cuda_libraries_installed() -> bool:
    dirs = _nvidia_package_dirs()
    names = " ".join(p.name for d in dirs for p in d.iterdir()) if dirs else ""
    return "cublas" in names.lower() and "cudnn" in names.lower()


def prepare_cuda_libraries() -> None:
    """Make the pip-installed CUDA DLLs/SOs findable before CTranslate2 loads them."""
    dirs = _nvidia_package_dirs()
    if not dirs:
        return
    if IS_WINDOWS:
        os.environ["PATH"] = os.pathsep.join([str(d) for d in dirs] + [os.environ.get("PATH", "")])
        for d in dirs:
            try:
                os.add_dll_directory(str(d))
            except (OSError, AttributeError):
                pass
    elif IS_LINUX:
        # The dynamic loader reads LD_LIBRARY_PATH only at start-up, so preload
        # the libraries by absolute path instead.
        import ctypes

        for d in dirs:
            for lib in sorted(d.glob("lib*.so*")):
                try:
                    ctypes.CDLL(str(lib), mode=ctypes.RTLD_GLOBAL)
                except OSError:
                    pass


def linux_cuda_env() -> dict[str, str]:
    """Environment for child processes on Linux so cuDNN can find its sub-libraries."""
    env = dict(os.environ)
    if IS_LINUX:
        dirs = [str(d) for d in _nvidia_package_dirs()]
        if dirs:
            env["LD_LIBRARY_PATH"] = os.pathsep.join(dirs + [env.get("LD_LIBRARY_PATH", "")])
    return env
