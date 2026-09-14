#!/usr/bin/env python3
#this script was made by bkbenken123
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from functools import lru_cache
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Iterable, Optional

SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))
APP_VERSION = "2026.08.25.5"
FFMPEG_BUILD_PROFILE = "all-vendors-win-linux-v5-required-codecs"
AMD_PCI_VENDOR_ID = "0x1002"
INTEL_PCI_VENDOR_ID = "0x8086"
NVIDIA_PCI_VENDOR_ID = "0x10de"
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
BINARIES_DIR = os.path.join(SCRIPT_DIR, "binaries")
FFMPEG_SOURCE_DIR = os.path.join(BINARIES_DIR, "ffmpeg")
FFMPEG_GIT_URLS = (
    "https://git.ffmpeg.org/ffmpeg.git",
    "https://github.com/FFmpeg/FFmpeg.git",
)
FFMPEG_OUTPUT_DIR = os.path.join(BINARIES_DIR, "ffmpeg-build")
FFMPEG_OUTPUT_BIN_DIR = os.path.join(FFMPEG_OUTPUT_DIR, "bin")
YTDLP_SOURCE_DIR = os.path.join(BINARIES_DIR, "yt-dlp")
DEPENDENCIES_DIR = os.path.join(BINARIES_DIR, "dependencies")
AMF_SOURCE_DIR = os.path.join(DEPENDENCIES_DIR, "AMF")
NV_CODEC_HEADERS_DIR = os.path.join(DEPENDENCIES_DIR, "nv-codec-headers")
LIBVPL_SOURCE_DIR = os.path.join(DEPENDENCIES_DIR, "libvpl")
LIBVPL_BUILD_DIR = os.path.join(DEPENDENCIES_DIR, "libvpl-build")
DEPENDENCY_PREFIX_DIR = os.path.join(DEPENDENCIES_DIR, "local")
FFMPEG_BUILD_MARKER = os.path.join(FFMPEG_OUTPUT_DIR, ".downloader-build-profile.json")
FFMPEG_CONFIGURE_LOG = os.path.join(FFMPEG_OUTPUT_DIR, "configure.log")
ENCODER_BACKENDS = ("AMD", "INTEL", "NVIDIA", "CPU")

DEFAULT_CONFIG = {
    "encoder_backend": "CPU",
    "default_download_dir": os.path.expanduser("~/Downloads"),
    "download_playlist": True,
    "use_ytdlp_audio_conversion": True,
}



# ---------------------------------------------------------------------------
# Codec configuration
# ---------------------------------------------------------------------------

AUDIO_FFMPEG_CODEC = {
    "aac": "aac",
    "opus": "libopus",
    "flac": "flac",
    "lpcm": "pcm_s16le",
    "mpeg-1": "libmp3lame",
    "mpeg-2": "mp2",
    "copy": None,
}

# Exact audio-codec list requested from the reference script.
AUDIO_CODECS = [
    "aac",
    "opus",
    "flac",
    "lpcm",
    "mpeg-1",
    "copy",
]

CPU_VIDEO_ENCODER_ARGS = {
    "copy": ["-c:v", "copy"],
    "h264": ["-c:v", "libx264"],
    "h265": ["-c:v", "libx265"],
    "vp9": ["-c:v", "libvpx-vp9"],
    "av1": ["-c:v", "libaom-av1"],
    "prores_422": ["-c:v", "prores_ks", "-profile:v", "3"],
    "dnxhr_sq": ["-c:v", "dnxhd", "-profile:v", "dnxhr_sq"],
    "dnxhr_hq": ["-c:v", "dnxhd", "-profile:v", "dnxhr_hq"],
}

# Exact video-codec list requested from the reference script.
VIDEO_CODECS = [
    "copy",
    "h264",
    "h265",
    "vp9",
    "av1",
    "prores_422",
    "dnxhr_sq",
    "dnxhr_hq",
]

# Exact container lists requested from the reference script.
VIDEO_CONTAINERS = ["mp4", "mkv", "webm", "mov"]
AUDIO_CONTAINERS = ["mp3", "m4a", "wav", "flac", "opus"]
THUMBNAIL_CONTAINERS = ["jpg", "png", "webp"]

GPU_ENCODER_CANDIDATES = {
    # Windows mappings. Linux adds VAAPI vendor-specific alternatives at runtime.
    "AMD": {
        "h264": ["h264_amf"],
        "h265": ["hevc_amf"],
        "av1": ["av1_amf"],
    },
    "INTEL": {
        "h264": ["h264_qsv"],
        "h265": ["hevc_qsv"],
        "vp9": ["vp9_qsv"],
        "av1": ["av1_qsv"],
    },
    "NVIDIA": {
        "h264": ["h264_nvenc"],
        "h265": ["hevc_nvenc"],
        "av1": ["av1_nvenc"],
    },
}

GPU_ENCODER_LABELS = {
    "h264_amf": "AMD AMF H.264",
    "hevc_amf": "AMD AMF HEVC",
    "av1_amf": "AMD AMF AV1",
    "h264_qsv": "Intel QSV H.264",
    "hevc_qsv": "Intel QSV HEVC",
    "vp9_qsv": "Intel QSV VP9",
    "av1_qsv": "Intel QSV AV1",
    "h264_nvenc": "NVIDIA NVENC H.264",
    "hevc_nvenc": "NVIDIA NVENC HEVC",
    "av1_nvenc": "NVIDIA NVENC AV1",
    "h264_vaapi": "VAAPI H.264",
    "hevc_vaapi": "VAAPI HEVC",
    "vp9_vaapi": "VAAPI VP9",
    "av1_vaapi": "VAAPI AV1",
}

# These are intermediate/download remnants that can be removed after a final
# output has been successfully created.
CLEANUP_EXTS = {
    ".part",
    ".ytdl",
    ".webm",
    ".m4a",
    ".mkv",
    ".mp4",
    ".mka",
    ".opus",
    ".temp",
    ".tmp",
    ".mp2",
}


# ---------------------------------------------------------------------------
# Configuration and yt-dlp startup update
# ---------------------------------------------------------------------------


def _validated_config(raw: object) -> dict:
    config = dict(DEFAULT_CONFIG)
    if not isinstance(raw, dict):
        return config

    backend = raw.get("encoder_backend")
    if isinstance(backend, str) and backend.upper() in ENCODER_BACKENDS:
        config["encoder_backend"] = backend.upper()

    directory = raw.get("default_download_dir")
    if isinstance(directory, str) and directory.strip():
        config["default_download_dir"] = os.path.abspath(os.path.expanduser(directory.strip()))

    playlist = raw.get("download_playlist")
    if isinstance(playlist, bool):
        config["download_playlist"] = playlist

    ytdlp_audio = raw.get("use_ytdlp_audio_conversion")
    if isinstance(ytdlp_audio, bool):
        config["use_ytdlp_audio_conversion"] = ytdlp_audio

    return config


def load_config() -> tuple[dict, Optional[str]]:
    if not os.path.isfile(CONFIG_PATH):
        return dict(DEFAULT_CONFIG), None

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            return _validated_config(json.load(handle)), None
    except Exception as exc:
        return dict(DEFAULT_CONFIG), f"Could not read config.json; defaults were loaded: {exc}"


def save_config(config: dict) -> None:
    normalized = _validated_config(config)
    changed = {
        key: value
        for key, value in normalized.items()
        if value != DEFAULT_CONFIG[key]
    }

    if not changed:
        try:
            if os.path.isfile(CONFIG_PATH):
                os.remove(CONFIG_PATH)
        except OSError as exc:
            raise OSError(f"Could not remove config.json: {exc}") from exc
        return

    temporary_path = CONFIG_PATH + ".tmp"
    try:
        with open(temporary_path, "w", encoding="utf-8") as handle:
            json.dump(changed, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(temporary_path, CONFIG_PATH)
    except Exception:
        try:
            if os.path.isfile(temporary_path):
                os.remove(temporary_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# General utilities
# ---------------------------------------------------------------------------


def find_local_executable(candidates: Iterable[str]) -> Optional[str]:
    """Find an executable in binaries/, beside the script, or on PATH."""
    candidates = list(candidates)

    for directory in (BINARIES_DIR, SCRIPT_DIR):
        for candidate in candidates:
            path = os.path.join(directory, candidate)
            if os.path.isfile(path):
                return os.path.abspath(path)

    for candidate in candidates:
        path = shutil.which(candidate)
        if path:
            return os.path.abspath(path)

    return None


def _ffmpeg_built_tool(name: str) -> Optional[str]:
    """Return an FFmpeg tool from the dedicated build output, never the source repo."""
    executable_name = f"{name}.exe" if sys.platform == "win32" else name
    path = os.path.join(FFMPEG_OUTPUT_BIN_DIR, executable_name)
    return os.path.abspath(path) if os.path.isfile(path) else None


def _ffmpeg_source_checkout_exists() -> bool:
    return os.path.isfile(os.path.join(FFMPEG_SOURCE_DIR, "configure"))


def _ffmpeg_build_profile_ready() -> bool:
    try:
        with open(FFMPEG_BUILD_MARKER, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return (
            isinstance(data, dict)
            and data.get("profile") == FFMPEG_BUILD_PROFILE
            and data.get("platform") == sys.platform
        )
    except Exception:
        return False


def _write_ffmpeg_build_profile() -> None:
    os.makedirs(FFMPEG_OUTPUT_DIR, exist_ok=True)
    temporary = FFMPEG_BUILD_MARKER + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump({"profile": FFMPEG_BUILD_PROFILE, "platform": sys.platform}, handle, indent=2)
        handle.write("\n")
    os.replace(temporary, FFMPEG_BUILD_MARKER)


FFMPEG_SHARED_DLL_PREFIXES = (
    "avcodec-",
    "avdevice-",
    "avfilter-",
    "avformat-",
    "avutil-",
    "swresample-",
    "swscale-",
)


def _ffmpeg_runtime_dlls() -> list[str]:
    """Return versioned FFmpeg shared-library DLLs from the dedicated build output."""
    if sys.platform != "win32" or not os.path.isdir(FFMPEG_OUTPUT_BIN_DIR):
        return []

    dlls: list[str] = []
    try:
        names = os.listdir(FFMPEG_OUTPUT_BIN_DIR)
    except OSError:
        return []

    for name in names:
        lower = name.lower()
        if not lower.endswith(".dll"):
            continue
        if lower.startswith(FFMPEG_SHARED_DLL_PREFIXES):
            path = os.path.join(FFMPEG_OUTPUT_BIN_DIR, name)
            if os.path.isfile(path):
                dlls.append(os.path.abspath(path))
    return sorted(dlls)


def _ffmpeg_shared_runtime_ready() -> bool:
    """Require all core FFmpeg DLL families on Windows before skipping a build."""
    if sys.platform != "win32":
        return True

    present = {
        next((prefix for prefix in FFMPEG_SHARED_DLL_PREFIXES if os.path.basename(path).lower().startswith(prefix)), None)
        for path in _ffmpeg_runtime_dlls()
    }
    return all(prefix in present for prefix in FFMPEG_SHARED_DLL_PREFIXES)


def _verify_ffmpeg_runtime(ffmpeg: str, ffprobe: str) -> tuple[bool, str]:
    """Actually launch both tools so missing DLL dependencies are caught at startup."""
    for tool in (ffmpeg, ffprobe):
        try:
            proc = subprocess.run(
                [tool, "-version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=20,
                check=False,
            )
        except Exception as exc:
            return False, f"{os.path.basename(tool)} could not start: {exc}"
        if proc.returncode != 0:
            output = (proc.stdout or "").strip()
            if len(output) > 1200:
                output = output[-1200:]
            return False, f"{os.path.basename(tool)} runtime check failed ({proc.returncode}): {output}"
    return True, ""


def _verify_ffmpeg_required_encoders(ffmpeg: str) -> tuple[bool, str]:
    """Require encoders that the downloader itself depends on for normal output modes."""
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=20,
            check=False,
        )
    except Exception as exc:
        return False, f"could not query FFmpeg encoders: {exc}"

    if proc.returncode != 0:
        output = (proc.stdout or "").strip()
        if len(output) > 1200:
            output = output[-1200:]
        return False, f"FFmpeg encoder query failed ({proc.returncode}): {output}"

    output = (proc.stdout or "").lower()
    required = ("libmp3lame",)
    missing = [name for name in required if not re.search(rf"\b{re.escape(name)}\b", output)]
    if missing:
        return False, "missing required encoder(s): " + ", ".join(missing)
    return True, ""


def find_ffmpeg_executable(name: str) -> Optional[str]:
    """Resolve FFmpeg, preferring the downloader's dedicated build on every OS."""
    built = _ffmpeg_built_tool(name)
    if built:
        return built

    if sys.platform.startswith("linux"):
        path = shutil.which(name)
        return os.path.abspath(path) if path else None

    candidates = [f"{name}.exe", name] if sys.platform == "win32" else [name]
    return find_local_executable(candidates)


def _ytdlp_source_main() -> Optional[str]:
    path = os.path.join(YTDLP_SOURCE_DIR, "yt_dlp", "__main__.py")
    return os.path.abspath(path) if os.path.isfile(path) else None


def find_ytdlp_executable() -> Optional[str]:
    """Resolve a prebuilt yt-dlp executable when one is available."""
    if sys.platform.startswith("linux"):
        path = os.path.join(BINARIES_DIR, "yt-dlp_linux")
        if not os.path.isfile(path):
            return None

        if not os.access(path, os.X_OK):
            try:
                current_mode = os.stat(path).st_mode
                os.chmod(path, current_mode | 0o100)
            except OSError:
                return None

        return os.path.abspath(path)

    # Also accept a locally built PyInstaller executable inside the cloned repo.
    repo_candidates = [
        os.path.join(YTDLP_SOURCE_DIR, "yt-dlp.exe"),
        os.path.join(YTDLP_SOURCE_DIR, "dist", "yt-dlp.exe"),
    ]
    for path in repo_candidates:
        if os.path.isfile(path):
            return os.path.abspath(path)

    return find_local_executable(["yt-dlp.exe", "yt-dlp", "yt_dlp.exe", "yt_dlp"])


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", name).strip().rstrip(".")
    return cleaned or "download"


def is_playlist_info(info: object) -> bool:
    return isinstance(info, dict) and bool(info.get("entries"))


def command_text(cmd: list[str]) -> str:
    """Format a command for logs without changing what is executed."""
    return subprocess.list2cmdline([str(part) for part in cmd])


def get_ytdlp_command() -> Optional[list[str]]:
    """Use the cloned yt-dlp source directly when it exists."""
    source_main = _ytdlp_source_main()
    if source_main:
        return [sys.executable, source_main]

    executable = find_ytdlp_executable()
    if executable:
        return [executable]

    # Linux keeps its historical bundled-binary/source-checkout rule.
    if sys.platform.startswith("linux"):
        return None

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode == 0:
            return [sys.executable, "-m", "yt_dlp"]
    except Exception:
        pass

    return None


def _run_update_command(
    cmd: list[str],
    timeout: int = 180,
    cwd: Optional[str] = None,
) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        output = "\n".join(
            part.strip() for part in (proc.stdout, proc.stderr) if part and part.strip()
        )
        return int(proc.returncode), output
    except subprocess.TimeoutExpired:
        return -1, "Update command timed out."
    except Exception as exc:
        return -1, str(exc)


def _run_streamed_process(
    cmd: list[str],
    log,
    cwd: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
) -> int:
    """Run a long build command while forwarding each output line to the GUI."""
    log(f"Running: {command_text(cmd)}")
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            stdin=subprocess.DEVNULL,
        )
    except Exception as exc:
        log(f"Could not start build command: {exc}")
        return -1

    if proc.stdout is not None:
        for line in iter(proc.stdout.readline, ""):
            if line == "":
                break
            text = line.rstrip()
            if text:
                log(f"[ffmpeg-build] {text}")

    proc.wait()
    return int(proc.returncode or 0)


def _find_windows_msys2() -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Return (bash.exe, toolchain bin directory, MSYSTEM) for an MSYS2 install."""
    roots: list[str] = []
    for value in (
        os.environ.get("MSYS2_ROOT"),
        r"C:\msys64",
        r"C:\tools\msys64",
        r"C:\msys32",
    ):
        if value and value not in roots:
            roots.append(value)

    path_bash = shutil.which("bash")
    if path_bash:
        normalized = os.path.normpath(path_bash)
        # Typical MSYS2 bash path is <root>\usr\bin\bash.exe.
        root_guess = os.path.dirname(os.path.dirname(os.path.dirname(normalized)))
        if os.path.isfile(os.path.join(root_guess, "usr", "bin", "bash.exe")):
            roots.insert(0, root_guess)

    toolchains = (
        ("ucrt64", "UCRT64", ("gcc.exe", "clang.exe")),
        ("mingw64", "MINGW64", ("gcc.exe", "clang.exe")),
        ("clang64", "CLANG64", ("clang.exe", "gcc.exe")),
    )

    for root in roots:
        bash = os.path.join(root, "usr", "bin", "bash.exe")
        if not os.path.isfile(bash):
            continue
        for subdir, msystem, compiler_names in toolchains:
            toolchain_bin = os.path.join(root, subdir, "bin")
            if any(os.path.isfile(os.path.join(toolchain_bin, item)) for item in compiler_names):
                return os.path.abspath(bash), os.path.abspath(toolchain_bin), msystem

    # Last chance: a shell already configured by the user. The preflight inside
    # the build command will reject Git Bash if no compiler/make is available.
    if path_bash:
        return os.path.abspath(path_bash), None, None

    return None, None, None




def _git_checkout_ready(path: str, required_relpath: str) -> bool:
    return os.path.isfile(os.path.join(path, *required_relpath.split("/")))


def _prepare_git_dependency(path: str, url: str, required_relpath: str, label: str, log) -> bool:
    """Clone a build-time dependency once; later runs update it with fast-forward-only Git."""
    git = shutil.which("git")
    if _git_checkout_ready(path, required_relpath):
        if git and os.path.isdir(os.path.join(path, ".git")):
            code, output = _run_update_command([git, "pull", "--ff-only"], timeout=180, cwd=path)
            if code == 0:
                log(f"{label}: source checkout is up to date.")
            else:
                log(f"WARNING: {label} could not be updated; using the existing checkout.")
                if output:
                    log(output.splitlines()[-1])
        return True

    if not git:
        log(f"WARNING: Git is required to fetch {label}.")
        return False
    if os.path.exists(path):
        log(f"WARNING: {path} exists but is not a valid {label} checkout.")
        return False

    os.makedirs(os.path.dirname(path), exist_ok=True)
    log(f"Cloning {label} into {path}...")
    code = _run_streamed_process([git, "clone", "--depth", "1", url, path], log)
    return code == 0 and _git_checkout_ready(path, required_relpath)


def _msys2_package_prefix(msystem: Optional[str]) -> Optional[str]:
    return {
        "UCRT64": "mingw-w64-ucrt-x86_64",
        "MINGW64": "mingw-w64-x86_64",
        "CLANG64": "mingw-w64-clang-x86_64",
    }.get(msystem or "")


def _prepare_windows_hardware_build_dependencies(
    bash: str,
    toolchain_bin: Optional[str],
    msystem: Optional[str],
    env: dict[str, str],
    log,
) -> None:
    """Install MSYS2 build-time dependencies for NVIDIA NVENC and Intel QSV."""
    package_prefix = _msys2_package_prefix(msystem)
    if not package_prefix or not toolchain_bin:
        log(
            "WARNING: A recognized MSYS2 MinGW/UCRT64 environment was not detected; "
            "automatic NVIDIA/Intel MSYS2 build dependency installation is unavailable."
        )
        return

    msys_root = os.path.dirname(os.path.dirname(toolchain_bin))
    pacman = os.path.join(msys_root, "usr", "bin", "pacman.exe")
    if not os.path.isfile(pacman):
        log("WARNING: MSYS2 pacman was not found; using whatever hardware SDK headers are already installed.")
        return

    packages = [
        # Build tooling / hardware SDKs.
        f"{package_prefix}-pkgconf",
        f"{package_prefix}-ffnvcodec-headers",
        f"{package_prefix}-libvpl",
        # External codec libraries used by this downloader's codec/container UI.
        # In particular, FFmpeg has no built-in MP3 encoder; yt-dlp --audio-format
        # mp3 requires libmp3lame to be compiled into FFmpeg.
        f"{package_prefix}-lame",
        f"{package_prefix}-opus",
        f"{package_prefix}-libx264",
        f"{package_prefix}-x265",
        f"{package_prefix}-libvpx",
        f"{package_prefix}-aom",
    ]
    log("Ensuring FFmpeg hardware and external codec build dependencies are installed in MSYS2...")
    code = _run_streamed_process(
        [bash, "-lc", "pacman -S --needed --noconfirm " + " ".join(packages)],
        log,
        env=env,
    )
    if code != 0:
        log(
            "WARNING: Some MSYS2 GPU build dependencies could not be installed. "
            "FFmpeg will still configure with every dependency that is available."
        )


def _windows_ffmpeg_build_workspace(log) -> Optional[str]:
    """Choose a writable build path with no spaces (FFmpeg builds dislike them)."""
    candidates = []
    for base in (
        os.environ.get("LOCALAPPDATA"),
        os.environ.get("TEMP"),
        os.environ.get("PUBLIC"),
        r"C:\Users\Public",
    ):
        if not base:
            continue
        candidate = os.path.abspath(os.path.join(base, "DownloaderFFmpegBuild"))
        if " " in candidate:
            continue
        if candidate not in candidates:
            candidates.append(candidate)

    for candidate in candidates:
        try:
            os.makedirs(os.path.dirname(candidate), exist_ok=True)
            return candidate
        except OSError:
            continue

    log(
        "ERROR: Could not find a writable Windows build path without spaces. "
        "Set LOCALAPPDATA, TEMP, or PUBLIC to a writable no-space path and run again."
    )
    return None


def _prepare_windows_ffmpeg_workspace(log) -> Optional[str]:
    workspace = _windows_ffmpeg_build_workspace(log)
    if not workspace:
        return None

    configure = os.path.join(workspace, "configure")
    configured = os.path.join(workspace, "ffbuild", "config.mak")

    # Keep a configured partial build so a failed compile can resume next run.
    # If configure never completed, refresh the workspace from the user's clone.
    if not os.path.isfile(configured):
        try:
            if os.path.isdir(workspace):
                shutil.rmtree(workspace)
            elif os.path.exists(workspace):
                os.remove(workspace)

            log(f"Copying FFmpeg source to no-space build workspace: {workspace}")
            shutil.copytree(
                FFMPEG_SOURCE_DIR,
                workspace,
                ignore=shutil.ignore_patterns(
                    ".git",
                    "*.exe",
                    "*.o",
                    "*.a",
                    "*.d",
                    "config.h",
                    "config_components.h",
                ),
            )

            # Never inherit a configure result generated in the original path.
            stale_config = os.path.join(workspace, "ffbuild", "config.mak")
            if os.path.isfile(stale_config):
                os.remove(stale_config)
        except Exception as exc:
            log(f"ERROR: Could not prepare FFmpeg build workspace: {exc}")
            return None

    if not os.path.isfile(configure):
        log(f"ERROR: FFmpeg configure script is missing from build workspace: {workspace}")
        return None

    return workspace



def _parse_amf_header_version(version_header: str) -> Optional[tuple[int, int, int, int]]:
    """Read AMF_VERSION_* macros from an AMF core/Version.h header."""
    try:
        text = Path(version_header).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    values: list[int] = []
    for macro in (
        "AMF_VERSION_MAJOR",
        "AMF_VERSION_MINOR",
        "AMF_VERSION_RELEASE",
        "AMF_VERSION_BUILD_NUM",
    ):
        match = re.search(rf"^\s*#\s*define\s+{macro}\s+(\d+)", text, re.MULTILINE)
        if not match:
            return None
        values.append(int(match.group(1)))
    return tuple(values)  # type: ignore[return-value]


def _ffmpeg_required_amf_version(configure_path: str) -> Optional[tuple[int, int, int, int]]:
    """Extract FFmpeg's current minimum AMF SDK version from its configure script."""
    try:
        text = Path(configure_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    # Current FFmpeg uses a packed hexadecimal comparison:
    # major<<48 | minor<<32 | release<<16 | build.
    match = re.search(
        r'check_cpp_condition\s+amf\s+"AMF/core/Version\.h"[\s\\]+.*?>=\s*(0x[0-9A-Fa-f]+)',
        text,
        re.DOTALL,
    )
    if not match:
        return None
    packed = int(match.group(1), 16)
    return (
        (packed >> 48) & 0xFFFF,
        (packed >> 32) & 0xFFFF,
        (packed >> 16) & 0xFFFF,
        packed & 0xFFFF,
    )


def _format_version(version: Optional[tuple[int, int, int, int]]) -> str:
    return ".".join(str(part) for part in version) if version else "unknown"


def _prepare_windows_amf_headers(workspace: str, log) -> Optional[str]:
    """Fetch current AMD AMF headers and stage them inside the no-space build workspace."""
    os.makedirs(DEPENDENCIES_DIR, exist_ok=True)

    if not _prepare_git_dependency(
        AMF_SOURCE_DIR,
        "https://github.com/GPUOpen-LibrariesAndSDKs/AMF.git",
        "amf/public/include/core/Version.h",
        "AMD AMF SDK headers",
        log,
    ):
        log(
            "WARNING: Current AMD AMF headers could not be prepared. "
            "FFmpeg will continue without forcing AMF instead of aborting the whole build."
        )
        return None

    source_headers = os.path.join(AMF_SOURCE_DIR, "amf", "public", "include")
    source_version_header = os.path.join(source_headers, "core", "Version.h")
    found_version = _parse_amf_header_version(source_version_header)
    required_version = _ffmpeg_required_amf_version(os.path.join(FFMPEG_SOURCE_DIR, "configure"))

    if found_version:
        log(f"AMD AMF SDK header version: {_format_version(found_version)}")
    if required_version:
        log(f"FFmpeg minimum AMF SDK version: {_format_version(required_version)}")
    if found_version and required_version and found_version < required_version:
        log(
            "WARNING: The checked-out AMD AMF headers are older than this FFmpeg checkout requires. "
            "AMF will be left to FFmpeg autodetection so the other GPU backends can still build."
        )
        return None

    # FFmpeg eventually tokenizes --extra-cflags itself.  An absolute include
    # path such as ".../youtube stuff/..." therefore gets split even if Python
    # or bash originally passed it as one argument.  Stage the headers inside
    # the already no-space FFmpeg workspace so the compiler never sees a path
    # containing spaces.
    staged_include = os.path.join(workspace, "_downloader_deps", "include")
    staged_amf = os.path.join(staged_include, "AMF")
    try:
        if os.path.isdir(staged_amf):
            shutil.rmtree(staged_amf)
        os.makedirs(staged_include, exist_ok=True)
        # source_headers contains core/, components/, etc.  FFmpeg expects them as
        # <include-root>/AMF/core/Version.h, so copy that tree under AMF/.
        shutil.copytree(source_headers, staged_amf)
    except OSError as exc:
        log(f"WARNING: Could not stage current AMD AMF headers: {exc}")
        return None

    staged_header = os.path.join(staged_amf, "core", "Version.h")
    if not os.path.isfile(staged_header):
        log("WARNING: Staged AMD AMF Version.h is missing; AMF will not be forced.")
        return None

    log(f"Staged current AMD AMF headers in no-space FFmpeg workspace: {staged_include}")
    return staged_include


def _copy_msys_runtime_dependencies(staged_bin: str, toolchain_bin: Optional[str], log) -> None:
    """Copy non-system MinGW runtime DLL dependencies needed by the staged FFmpeg build."""
    if not toolchain_bin or not os.path.isdir(toolchain_bin):
        return

    objdump_candidates = [
        os.path.join(toolchain_bin, "objdump.exe"),
        shutil.which("objdump"),
    ]
    objdump = next((item for item in objdump_candidates if item and os.path.isfile(item)), None)
    if not objdump:
        log("WARNING: objdump was not found; external MinGW DLL dependencies could not be auto-collected.")
        return

    try:
        queue_paths = [
            os.path.join(staged_bin, name)
            for name in os.listdir(staged_bin)
            if name.lower().endswith((".exe", ".dll"))
            and os.path.isfile(os.path.join(staged_bin, name))
        ]
    except OSError:
        return

    seen: set[str] = set()
    copied: list[str] = []
    while queue_paths:
        binary = queue_paths.pop(0)
        key = os.path.normcase(os.path.abspath(binary))
        if key in seen:
            continue
        seen.add(key)
        try:
            proc = subprocess.run(
                [objdump, "-p", binary],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except Exception:
            continue

        for raw_line in (proc.stdout or "").splitlines():
            match = re.search(r"DLL Name:\s*(.+?)\s*$", raw_line, re.IGNORECASE)
            if not match:
                continue
            dll_name = match.group(1).strip()
            if not dll_name.lower().endswith(".dll"):
                continue
            staged = os.path.join(staged_bin, dll_name)
            if os.path.isfile(staged):
                queue_paths.append(staged)
                continue
            source = os.path.join(toolchain_bin, dll_name)
            if not os.path.isfile(source):
                continue
            try:
                shutil.copy2(source, staged)
                copied.append(dll_name)
                queue_paths.append(staged)
            except OSError as exc:
                log(f"WARNING: Could not copy runtime dependency {dll_name}: {exc}")

    if copied:
        log("Copied MinGW runtime dependency DLL(s): " + ", ".join(sorted(set(copied), key=str.lower)))


def _preserve_windows_configure_log(workspace: str, log) -> Optional[str]:
    """Copy FFmpeg's external-workspace config.log into binaries/ffmpeg-build/."""
    source = os.path.join(workspace, "ffbuild", "config.log")
    if not os.path.isfile(source):
        log(f"WARNING: FFmpeg configure log was not found in the build workspace: {source}")
        return None

    try:
        os.makedirs(FFMPEG_OUTPUT_DIR, exist_ok=True)
        shutil.copy2(source, FFMPEG_CONFIGURE_LOG)
        log(f"Saved FFmpeg configure log: {FFMPEG_CONFIGURE_LOG}")
        return FFMPEG_CONFIGURE_LOG
    except OSError as exc:
        log(f"WARNING: Could not save FFmpeg configure log: {exc}")
        return None


def _log_configure_failure_tail(path: Optional[str], log, max_lines: int = 80) -> None:
    """Put the useful tail of config.log directly in the GUI after a configure failure."""
    if not path or not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError as exc:
        log(f"WARNING: Could not read saved configure log: {exc}")
        return

    if not lines:
        return
    log(f"--- FFmpeg configure.log tail (last {min(max_lines, len(lines))} lines) ---")
    for line in lines[-max_lines:]:
        text = line.rstrip()
        if text:
            log(f"[configure.log] {text}")
    log("--- end configure.log tail ---")


def _build_ffmpeg_windows(log) -> bool:
    bash, toolchain_bin, msystem = _find_windows_msys2()
    if not bash:
        log(
            "ERROR: FFmpeg source is present, but an MSYS2 build shell was not found. "
            "Install MSYS2 with make + a MinGW/UCRT64 compiler, then run the downloader again."
        )
        return False

    workspace = _prepare_windows_ffmpeg_workspace(log)
    if not workspace:
        return False

    staged_install = os.path.join(workspace, "_shared_install")

    env = os.environ.copy()
    env["FFMPEG_BUILD_WINDOWS"] = workspace
    env["CHERE_INVOKING"] = "1"
    env["MSYS2_PATH_TYPE"] = "inherit"
    if msystem:
        env["MSYSTEM"] = msystem
    if toolchain_bin:
        msys_root = os.path.dirname(os.path.dirname(toolchain_bin))
        usr_bin = os.path.join(msys_root, "usr", "bin")
        env["PATH"] = os.pathsep.join([toolchain_bin, usr_bin, env.get("PATH", "")])

    _prepare_windows_hardware_build_dependencies(bash, toolchain_bin, msystem, env, log)
    amf_include = _prepare_windows_amf_headers(workspace, log)
    if amf_include:
        env["DOWNLOADER_AMF_INCLUDE_WINDOWS"] = amf_include

    build_script = r"""
set -o pipefail
BUILDROOT="$(cygpath -u "$FFMPEG_BUILD_WINDOWS" 2>/dev/null || printf '%s' "$FFMPEG_BUILD_WINDOWS")"
INSTALLROOT="$BUILDROOT/_shared_install"
cd "$BUILDROOT" || exit 90

if ! command -v make >/dev/null 2>&1; then
    echo "ERROR: GNU make is missing from the MSYS2 environment. Install package: make"
    exit 91
fi
if ! command -v gcc >/dev/null 2>&1 && ! command -v clang >/dev/null 2>&1; then
    echo "ERROR: No MinGW/UCRT64 C compiler was found. Install an MSYS2 MinGW/UCRT64 GCC or Clang toolchain."
    exit 92
fi

EXTRA_FLAGS=()
EXTRA_CFLAGS=()

# Windows hardware backends. NVIDIA/Intel come from MSYS2 packages; AMD uses
# current upstream AMF headers staged inside the no-space build workspace.
# AMF is AUTODETECTED by current FFmpeg.  Do not pass --enable-amf here: if a
# future/header mismatch occurs, explicitly requesting an autodetect library makes
# FFmpeg abort the entire configure step.  Supplying a valid include root is enough.
AMF_INCLUDE_WINDOWS="${DOWNLOADER_AMF_INCLUDE_WINDOWS:-}"
if [ -n "$AMF_INCLUDE_WINDOWS" ]; then
    AMF_INCLUDE="$(cygpath -u "$AMF_INCLUDE_WINDOWS" 2>/dev/null || printf '%s' "$AMF_INCLUDE_WINDOWS")"
    AMF_HEADER="$AMF_INCLUDE/AMF/core/Version.h"
    if [ -f "$AMF_HEADER" ]; then
        AMF_CC="$(command -v gcc || command -v clang || true)"
        if [ -n "$AMF_CC" ] && printf '%s\n' \
            '#include <AMF/core/Version.h>' \
            'int main(void) { return 0; }' \
            | "$AMF_CC" -x c -I"$AMF_INCLUDE" -c -o "$BUILDROOT/.downloader-amf-test.o" - >/dev/null 2>&1; then
            rm -f "$BUILDROOT/.downloader-amf-test.o"
            EXTRA_CFLAGS+=("-I./_downloader_deps/include")
            echo "AMD AMF headers compile successfully from: $AMF_INCLUDE"
            echo "AMD AMF build support supplied to FFmpeg autodetection."
        else
            rm -f "$BUILDROOT/.downloader-amf-test.o"
            echo "WARNING: Current AMD AMF headers exist but this compiler cannot include them."
            echo "WARNING: Continuing the multi-vendor build without forcing AMF."
        fi
    else
        echo "WARNING: Staged AMD AMF Version.h was not found at $AMF_HEADER."
    fi
else
    echo "WARNING: Current AMD AMF headers were not staged; continuing without forcing AMF."
fi
if command -v pkg-config >/dev/null 2>&1 && pkg-config --exists ffnvcodec; then
    EXTRA_FLAGS+=(--enable-ffnvcodec --enable-nvenc --enable-nvdec)
    echo "NVIDIA NVENC/NVDEC build support enabled."
else
    echo "WARNING: NVIDIA ffnvcodec headers were not found."
fi
if command -v pkg-config >/dev/null 2>&1 && pkg-config --exists vpl; then
    EXTRA_FLAGS+=(--enable-libvpl)
    echo "Intel oneVPL/QSV build support enabled."
else
    echo "WARNING: Intel oneVPL/libvpl was not found."
fi
EXTRA_FLAGS+=(--enable-d3d11va --enable-dxva2)

if ! command -v nasm >/dev/null 2>&1; then
    echo "NASM was not found; compiling with --disable-x86asm so the first build can still complete."
    EXTRA_FLAGS+=(--disable-x86asm)
fi

if command -v pkg-config >/dev/null 2>&1; then
    if pkg-config --exists x264; then EXTRA_FLAGS+=(--enable-libx264); fi
    if pkg-config --exists x265; then EXTRA_FLAGS+=(--enable-libx265); fi
    if pkg-config --exists vpx; then EXTRA_FLAGS+=(--enable-libvpx); fi
    if pkg-config --exists aom; then EXTRA_FLAGS+=(--enable-libaom); fi
    if pkg-config --exists opus; then EXTRA_FLAGS+=(--enable-libopus); fi
    if pkg-config --exists lame; then EXTRA_FLAGS+=(--enable-libmp3lame); fi
else
    echo "pkg-config was not found; optional external codec libraries will not be auto-enabled."
fi

# The old workspace may be configured static-only. A DLL build needs a full
# clean reconfigure so --enable-shared actually takes effect.
if [ -f ffbuild/config.mak ]; then
    echo "Removing the previous static/partial FFmpeg configuration..."
    make distclean || exit $?
fi
rm -rf "$INSTALLROOT"
mkdir -p "$INSTALLROOT"

echo "Configuring FFmpeg with shared DLL libraries enabled..."
CONFIGURE_ARGS=(
    --prefix="$INSTALLROOT"
    --disable-doc
    --disable-ffplay
    --enable-gpl
    --enable-version3
    --enable-shared
    --disable-static
)
if [ "${#EXTRA_CFLAGS[@]}" -gt 0 ]; then
    CONFIGURE_ARGS+=("--extra-cflags=${EXTRA_CFLAGS[*]}")
fi
CONFIGURE_ARGS+=("${EXTRA_FLAGS[@]}")

echo "FFmpeg configure extra C flags: ${EXTRA_CFLAGS[*]:-(none)}"
echo "FFmpeg configure hardware flags: ${EXTRA_FLAGS[*]:-(autodetect only)}"
bash ./configure "${CONFIGURE_ARGS[@]}"
CONFIGURE_STATUS=$?
if [ "$CONFIGURE_STATUS" -ne 0 ]; then
    echo "ERROR: FFmpeg configure failed with code $CONFIGURE_STATUS."
    if [ -f ffbuild/config.log ]; then
        echo "Configure log in build workspace: $BUILDROOT/ffbuild/config.log"
    fi
    exit "$CONFIGURE_STATUS"
fi

JOBS="${NUMBER_OF_PROCESSORS:-4}"
echo "Compiling FFmpeg shared build with $JOBS parallel job(s)..."
make -j"$JOBS" || exit $?
echo "Installing ffmpeg.exe, ffprobe.exe and FFmpeg DLLs into the staging folder..."
make install || exit $?
"""

    log(f"FFmpeg source checkout (read-only build input): {FFMPEG_SOURCE_DIR}")
    log(f"FFmpeg compile workspace (separate from repository): {workspace}")
    log(f"FFmpeg final build output: {FFMPEG_OUTPUT_BIN_DIR}")
    log(f"FFmpeg build shell: {bash}")
    log("FFmpeg build type: shared DLLs (--enable-shared --disable-static)")
    if msystem:
        log(f"MSYS2 environment: {msystem}")

    build_code = _run_streamed_process([bash, "-lc", build_script], log, env=env)
    saved_config_log = _preserve_windows_configure_log(workspace, log)
    if build_code != 0:
        _log_configure_failure_tail(saved_config_log, log)
        return False

    staged_bin = os.path.join(staged_install, "bin")
    built_ffmpeg = os.path.join(staged_bin, "ffmpeg.exe")
    built_ffprobe = os.path.join(staged_bin, "ffprobe.exe")
    if not (os.path.isfile(built_ffmpeg) and os.path.isfile(built_ffprobe)):
        log("ERROR: Shared build finished but staged ffmpeg.exe/ffprobe.exe were not produced.")
        return False

    try:
        staged_names = os.listdir(staged_bin)
    except OSError as exc:
        log(f"ERROR: Could not inspect staged FFmpeg build output: {exc}")
        return False

    ffmpeg_dlls = [
        name
        for name in staged_names
        if name.lower().endswith(".dll")
        and name.lower().startswith(FFMPEG_SHARED_DLL_PREFIXES)
        and os.path.isfile(os.path.join(staged_bin, name))
    ]
    present_prefixes = {
        next((prefix for prefix in FFMPEG_SHARED_DLL_PREFIXES if name.lower().startswith(prefix)), None)
        for name in ffmpeg_dlls
    }
    missing_prefixes = [prefix for prefix in FFMPEG_SHARED_DLL_PREFIXES if prefix not in present_prefixes]
    if missing_prefixes:
        log(
            "ERROR: Shared FFmpeg build did not produce all required DLL families. Missing: "
            + ", ".join(prefix.rstrip("-") for prefix in missing_prefixes)
        )
        return False

    _copy_msys_runtime_dependencies(staged_bin, toolchain_bin, log)

    try:
        # Runtime output is deliberately separate from binaries\ffmpeg, which remains
        # a source-only Git checkout. Replace the output bin atomically enough that
        # stale versioned DLLs from an older FFmpeg build cannot be mixed in.
        os.makedirs(FFMPEG_OUTPUT_DIR, exist_ok=True)
        replacement_bin = os.path.join(FFMPEG_OUTPUT_DIR, "bin.new")
        old_bin = os.path.join(FFMPEG_OUTPUT_DIR, "bin.old")
        for path in (replacement_bin, old_bin):
            if os.path.isdir(path):
                shutil.rmtree(path)
            elif os.path.exists(path):
                os.remove(path)
        os.makedirs(replacement_bin, exist_ok=True)

        files_to_copy = ["ffmpeg.exe", "ffprobe.exe"] + [
            name for name in os.listdir(staged_bin) if name.lower().endswith(".dll")
        ]
        copied_dlls: list[str] = []
        for name in files_to_copy:
            src = os.path.join(staged_bin, name)
            if not os.path.isfile(src):
                continue
            shutil.copy2(src, os.path.join(replacement_bin, name))
            if name.lower().endswith(".dll"):
                copied_dlls.append(name)

        # Verify the freshly copied set before making it the active runtime.
        candidate_ffmpeg = os.path.join(replacement_bin, "ffmpeg.exe")
        candidate_ffprobe = os.path.join(replacement_bin, "ffprobe.exe")
        runtime_ok, runtime_error = _verify_ffmpeg_runtime(candidate_ffmpeg, candidate_ffprobe)
        if not runtime_ok:
            log(f"ERROR: Fresh FFmpeg build cannot run from the separate output folder: {runtime_error}")
            shutil.rmtree(replacement_bin, ignore_errors=True)
            return False

        if os.path.isdir(FFMPEG_OUTPUT_BIN_DIR):
            os.replace(FFMPEG_OUTPUT_BIN_DIR, old_bin)
        os.replace(replacement_bin, FFMPEG_OUTPUT_BIN_DIR)
        if os.path.isdir(old_bin):
            shutil.rmtree(old_bin, ignore_errors=True)

        log(f"Installed compiled FFmpeg into separate build folder: {FFMPEG_OUTPUT_BIN_DIR}")
        log(r"The binaries\ffmpeg repository was not used as an output directory.")
        log(f"Copied {len(copied_dlls)} runtime DLL(s) beside FFmpeg.")
        for name in sorted(copied_dlls, key=str.lower):
            log(f"FFmpeg runtime DLL: {name}")
    except OSError as exc:
        log(f"ERROR: Could not install compiled FFmpeg into the separate build folder: {exc}")
        return False

    installed_ffmpeg = os.path.join(FFMPEG_OUTPUT_BIN_DIR, "ffmpeg.exe")
    installed_ffprobe = os.path.join(FFMPEG_OUTPUT_BIN_DIR, "ffprobe.exe")
    runtime_ok, runtime_error = _verify_ffmpeg_runtime(installed_ffmpeg, installed_ffprobe)
    if not runtime_ok:
        log(f"ERROR: Compiled FFmpeg cannot run with the copied DLL set: {runtime_error}")
        return False

    return True

def _prepare_posix_hardware_dependencies(log) -> tuple[Optional[str], Optional[str]]:
    """Prepare header-only AMD/NVIDIA dependencies under binaries/dependencies/."""
    os.makedirs(DEPENDENCIES_DIR, exist_ok=True)

    amf_ok = _prepare_git_dependency(
        AMF_SOURCE_DIR,
        "https://github.com/GPUOpen-LibrariesAndSDKs/AMF.git",
        "amf/public/include/core/Factory.h",
        "AMD AMF headers",
        log,
    )
    nv_ok = _prepare_git_dependency(
        NV_CODEC_HEADERS_DIR,
        "https://github.com/FFmpeg/nv-codec-headers.git",
        "include/ffnvcodec/nvEncodeAPI.h",
        "NVIDIA nv-codec-headers",
        log,
    )

    staged_include = os.path.join(DEPENDENCY_PREFIX_DIR, "include")
    staged_amf = os.path.join(staged_include, "AMF")
    if amf_ok:
        source_headers = os.path.join(AMF_SOURCE_DIR, "amf", "public", "include")
        try:
            if os.path.isdir(staged_amf):
                shutil.rmtree(staged_amf)
            os.makedirs(staged_include, exist_ok=True)
            shutil.copytree(source_headers, staged_amf)
            log("AMD AMF headers staged for the Linux FFmpeg build.")
        except OSError as exc:
            log(f"WARNING: Could not stage AMD AMF headers: {exc}")
            amf_ok = False

    pkgconfig_dir: Optional[str] = None
    if nv_ok:
        make = shutil.which("make")
        if make:
            os.makedirs(DEPENDENCY_PREFIX_DIR, exist_ok=True)
            code = _run_streamed_process(
                [make, "install", f"PREFIX={DEPENDENCY_PREFIX_DIR}"],
                log,
                cwd=NV_CODEC_HEADERS_DIR,
            )
            if code == 0:
                for candidate in (
                    os.path.join(DEPENDENCY_PREFIX_DIR, "lib", "pkgconfig"),
                    os.path.join(DEPENDENCY_PREFIX_DIR, "lib64", "pkgconfig"),
                ):
                    if os.path.isfile(os.path.join(candidate, "ffnvcodec.pc")):
                        pkgconfig_dir = candidate
                        break
                log("NVIDIA nv-codec-headers installed into the local dependency prefix.")
            else:
                log("WARNING: nv-codec-headers could not be installed into the local prefix.")
        else:
            log("WARNING: GNU make is required to stage nv-codec-headers.")

    return (staged_include if amf_ok else None), pkgconfig_dir


def _pkg_config_exists(env: dict[str, str], *packages: str) -> bool:
    pkg_config = shutil.which("pkg-config") or shutil.which("pkgconf")
    if not pkg_config:
        return False
    try:
        proc = subprocess.run(
            [pkg_config, "--exists", *packages],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
        )
        return proc.returncode == 0
    except Exception:
        return False


def _find_local_pkgconfig_file(name: str) -> Optional[str]:
    for libdir in ("lib", "lib64"):
        directory = os.path.join(DEPENDENCY_PREFIX_DIR, libdir, "pkgconfig")
        if os.path.isfile(os.path.join(directory, name)):
            return directory
    return None


def _prepare_posix_libvpl(log) -> tuple[Optional[str], Optional[str]]:
    """Build Intel's oneVPL dispatcher locally when the distro does not provide libvpl-dev."""
    cmake = shutil.which("cmake")
    if not cmake:
        log("WARNING: CMake is not installed, so a missing Intel oneVPL dispatcher cannot be built locally.")
        return None, None

    if not _prepare_git_dependency(
        LIBVPL_SOURCE_DIR,
        "https://github.com/intel/libvpl.git",
        "CMakeLists.txt",
        "Intel oneVPL dispatcher",
        log,
    ):
        return None, None

    try:
        if os.path.isdir(LIBVPL_BUILD_DIR):
            shutil.rmtree(LIBVPL_BUILD_DIR)
        os.makedirs(LIBVPL_BUILD_DIR, exist_ok=True)
        os.makedirs(DEPENDENCY_PREFIX_DIR, exist_ok=True)
    except OSError as exc:
        log(f"WARNING: Could not prepare the local oneVPL build directory: {exc}")
        return None, None

    configure = [
        cmake,
        "-S", LIBVPL_SOURCE_DIR,
        "-B", LIBVPL_BUILD_DIR,
        f"-DCMAKE_INSTALL_PREFIX={DEPENDENCY_PREFIX_DIR}",
        "-DCMAKE_BUILD_TYPE=Release",
    ]
    if _run_streamed_process(configure, log) != 0:
        log("WARNING: Intel oneVPL dispatcher configuration failed.")
        return None, None
    if _run_streamed_process([cmake, "--build", LIBVPL_BUILD_DIR, "--parallel", str(os.cpu_count() or 4)], log) != 0:
        log("WARNING: Intel oneVPL dispatcher build failed.")
        return None, None
    if _run_streamed_process([cmake, "--install", LIBVPL_BUILD_DIR], log) != 0:
        log("WARNING: Intel oneVPL dispatcher installation failed.")
        return None, None

    pkg_dir = _find_local_pkgconfig_file("vpl.pc")
    lib_dir = None
    for candidate in (
        os.path.join(DEPENDENCY_PREFIX_DIR, "lib"),
        os.path.join(DEPENDENCY_PREFIX_DIR, "lib64"),
    ):
        if os.path.isdir(candidate) and any(name.startswith("libvpl.so") for name in os.listdir(candidate)):
            lib_dir = candidate
            break
    if pkg_dir:
        log("Intel oneVPL dispatcher built into the local dependency prefix.")
    return pkg_dir, lib_dir


def _build_ffmpeg_posix(log) -> bool:
    """Build a Linux/POSIX FFmpeg with AMD, Intel and NVIDIA acceleration where available."""
    configure = os.path.join(FFMPEG_SOURCE_DIR, "configure")
    make = shutil.which("make")
    shell = shutil.which("bash") or shutil.which("sh")
    if not shell or not make:
        log("ERROR: FFmpeg source exists, but bash/sh and GNU make are required to compile it.")
        return False

    build_dir = os.path.join(FFMPEG_OUTPUT_DIR, "obj")
    install_dir = os.path.join(FFMPEG_OUTPUT_DIR, "install")
    try:
        if os.path.isdir(build_dir):
            shutil.rmtree(build_dir)
        if os.path.isdir(install_dir):
            shutil.rmtree(install_dir)
        os.makedirs(build_dir, exist_ok=True)
        os.makedirs(install_dir, exist_ok=True)
    except OSError as exc:
        log(f"ERROR: Could not prepare separate FFmpeg build directories: {exc}")
        return False

    amf_include, nv_pkgconfig = _prepare_posix_hardware_dependencies(log)
    env = os.environ.copy()
    pkg_paths: list[str] = []
    if nv_pkgconfig:
        pkg_paths.append(nv_pkgconfig)
    existing_pkg_path = env.get("PKG_CONFIG_PATH", "")
    if pkg_paths:
        env["PKG_CONFIG_PATH"] = os.pathsep.join(pkg_paths + ([existing_pkg_path] if existing_pkg_path else []))

    local_vpl_lib: Optional[str] = None
    if not _pkg_config_exists(env, "vpl"):
        vpl_pkgconfig, local_vpl_lib = _prepare_posix_libvpl(log)
        if vpl_pkgconfig:
            pkg_paths.append(vpl_pkgconfig)
            env["PKG_CONFIG_PATH"] = os.pathsep.join(pkg_paths + ([existing_pkg_path] if existing_pkg_path else []))
            existing_ld = env.get("LD_LIBRARY_PATH", "")
            if local_vpl_lib:
                env["LD_LIBRARY_PATH"] = os.pathsep.join([local_vpl_lib, existing_ld]) if existing_ld else local_vpl_lib

    configure_args = [
        shell,
        configure,
        f"--prefix={install_dir}",
        "--disable-doc",
        "--disable-ffplay",
        "--enable-gpl",
        "--enable-version3",
    ]

    extra_cflags: list[str] = []
    extra_ldflags: list[str] = []
    if local_vpl_lib:
        extra_ldflags += [f"-L{local_vpl_lib}", f"-Wl,-rpath,{local_vpl_lib}"]

    if amf_include:
        # FFmpeg tokenizes --extra-cflags internally, so an absolute path with
        # spaces can be split into multiple compiler arguments.  Copy AMF into
        # the build tree and use a relative include path instead.  This is safe
        # regardless of where the downloader/source repository is stored.
        build_local_include = os.path.join(build_dir, "_downloader_deps", "include")
        build_local_amf = os.path.join(build_local_include, "AMF")
        try:
            if os.path.isdir(build_local_amf):
                shutil.rmtree(build_local_amf)
            os.makedirs(build_local_include, exist_ok=True)
            shutil.copytree(os.path.join(amf_include, "AMF"), build_local_amf)
            extra_cflags.append("-I./_downloader_deps/include")
            log(
                "Linux AMD AMF headers staged inside the FFmpeg build tree for path-safe "
                "autodetection (runtime still requires AMD AMF drivers)."
            )
        except OSError as exc:
            log(f"WARNING: Could not stage Linux AMF headers into the build tree: {exc}")

    if _pkg_config_exists(env, "ffnvcodec"):
        configure_args += ["--enable-ffnvcodec", "--enable-nvenc", "--enable-nvdec"]
        log("Linux NVIDIA NVENC/NVDEC build support enabled.")
    else:
        log("WARNING: NVIDIA ffnvcodec headers are unavailable; NVENC/NVDEC will not be compiled.")

    if _pkg_config_exists(env, "vpl"):
        configure_args.append("--enable-libvpl")
        log("Linux Intel oneVPL/QSV build support enabled.")
    elif _pkg_config_exists(env, "libmfx"):
        configure_args.append("--enable-libmfx")
        log("Linux Intel Media SDK/QSV build support enabled through libmfx.")
    else:
        log("Intel oneVPL/libmfx development files were not found; Intel VAAPI will still be used when available.")

    if _pkg_config_exists(env, "libva", "libva-drm"):
        configure_args.append("--enable-vaapi")
        log("Linux VAAPI build support enabled for AMD/Intel GPUs.")
    else:
        log(
            "WARNING: libva + libva-drm development files were not found. "
            "Install your distro's libva development packages for AMD/Intel VAAPI support."
        )

    if _pkg_config_exists(env, "vulkan"):
        configure_args.append("--enable-vulkan")

    for package, flag in (
        ("x264", "--enable-libx264"),
        ("x265", "--enable-libx265"),
        ("vpx", "--enable-libvpx"),
        ("aom", "--enable-libaom"),
        ("opus", "--enable-libopus"),
        ("lame", "--enable-libmp3lame"),
    ):
        if _pkg_config_exists(env, package):
            configure_args.append(flag)

    if extra_cflags:
        configure_args.append("--extra-cflags=" + " ".join(extra_cflags))
    if extra_ldflags:
        configure_args.append("--extra-ldflags=" + " ".join(extra_ldflags))

    log(f"FFmpeg source checkout (build input only): {FFMPEG_SOURCE_DIR}")
    log(f"FFmpeg object/build directory: {build_dir}")
    log(f"FFmpeg final build output: {FFMPEG_OUTPUT_BIN_DIR}")

    if _run_streamed_process(configure_args, log, cwd=build_dir, env=env) != 0:
        return False

    jobs = str(os.cpu_count() or 4)
    if _run_streamed_process([make, f"-j{jobs}"], log, cwd=build_dir, env=env) != 0:
        return False
    if _run_streamed_process([make, "install"], log, cwd=build_dir, env=env) != 0:
        return False

    installed_bin = os.path.join(install_dir, "bin")
    try:
        if os.path.isdir(FFMPEG_OUTPUT_BIN_DIR):
            shutil.rmtree(FFMPEG_OUTPUT_BIN_DIR)
        shutil.copytree(installed_bin, FFMPEG_OUTPUT_BIN_DIR)

        runtime_lib_dir = os.path.join(FFMPEG_OUTPUT_DIR, "lib")
        if os.path.isdir(runtime_lib_dir):
            shutil.rmtree(runtime_lib_dir)
        if local_vpl_lib:
            os.makedirs(runtime_lib_dir, exist_ok=True)
            for name in os.listdir(local_vpl_lib):
                if name.startswith("libvpl.so"):
                    src = os.path.join(local_vpl_lib, name)
                    dst = os.path.join(runtime_lib_dir, name)
                    if os.path.islink(src):
                        target = os.readlink(src)
                        os.symlink(target, dst)
                    elif os.path.isfile(src):
                        shutil.copy2(src, dst)
            log(f"Packaged local Intel oneVPL dispatcher into: {runtime_lib_dir}")
    except OSError as exc:
        log(f"ERROR: Could not install FFmpeg into separate build output: {exc}")
        return False

    ffmpeg = _ffmpeg_built_tool("ffmpeg")
    ffprobe = _ffmpeg_built_tool("ffprobe")
    if not ffmpeg or not ffprobe:
        log("ERROR: Linux FFmpeg build did not produce ffmpeg and ffprobe.")
        return False
    runtime_ok, runtime_error = _verify_ffmpeg_runtime(ffmpeg, ffprobe)
    if not runtime_ok:
        log(f"ERROR: Linux FFmpeg build could not run: {runtime_error}")
        return False
    return True



def check_ffmpeg_update_available(log) -> tuple[bool, bool, str]:
    """Check whether the FFmpeg Git checkout has a newer upstream commit.

    This is intentionally read-only with respect to the checked-out source: it
    fetches remote metadata but never pulls, checks out, replaces, or rebuilds
    anything. Returns (success, update_available, summary).
    """
    if not _ffmpeg_source_checkout_exists():
        message = f"FFmpeg source checkout was not found at {FFMPEG_SOURCE_DIR}."
        log(f"WARNING: {message}")
        return False, False, message

    git = shutil.which("git")
    if not git:
        message = "Git was not found on PATH, so FFmpeg update availability could not be checked."
        log(f"WARNING: {message}")
        return False, False, message

    probe_code, probe_output = _run_update_command(
        [git, "rev-parse", "--is-inside-work-tree"],
        timeout=30,
        cwd=FFMPEG_SOURCE_DIR,
    )
    is_git_checkout = probe_code == 0 and probe_output.strip().lower().endswith("true")
    if not is_git_checkout:
        message = (
            "FFmpeg update availability cannot be checked yet because binaries/ffmpeg is a plain "
            "source tree without Git metadata. Use Settings -> Check / Update FFmpeg once to "
            "convert it to a Git checkout."
        )
        log(f"WARNING: {message}")
        return False, False, message

    head_code, head_output = _run_update_command(
        [git, "rev-parse", "HEAD"], timeout=30, cwd=FFMPEG_SOURCE_DIR
    )
    if head_code != 0 or not head_output.strip():
        message = "Could not read the current FFmpeg Git revision while checking for updates."
        log(f"WARNING: {message}")
        return False, False, message
    head_revision = head_output.strip().splitlines()[-1]

    log("Checking FFmpeg Git checkout for available updates...")
    fetch_code, fetch_output = _run_update_command(
        [git, "fetch", "--quiet", "--prune", "origin"],
        timeout=300,
        cwd=FFMPEG_SOURCE_DIR,
    )
    if fetch_code != 0:
        message = "Could not check FFmpeg for updates from origin; the existing source/build are unchanged."
        log(f"WARNING: {message}")
        if fetch_output:
            log(f"[ffmpeg-git] {fetch_output.splitlines()[-1]}")
        return False, False, message

    upstream_code, upstream_output = _run_update_command(
        [git, "rev-parse", "@{u}"], timeout=30, cwd=FFMPEG_SOURCE_DIR
    )
    if upstream_code != 0 or not upstream_output.strip():
        # A checkout without an upstream can still normally compare against
        # origin/<current-branch>.
        branch_code, branch_output = _run_update_command(
            [git, "rev-parse", "--abbrev-ref", "HEAD"], timeout=30, cwd=FFMPEG_SOURCE_DIR
        )
        branch = branch_output.strip().splitlines()[-1] if branch_code == 0 and branch_output.strip() else ""
        if branch and branch != "HEAD":
            upstream_code, upstream_output = _run_update_command(
                [git, "rev-parse", f"origin/{branch}"], timeout=30, cwd=FFMPEG_SOURCE_DIR
            )

    if upstream_code != 0 or not upstream_output.strip():
        message = "FFmpeg remote metadata was fetched, but the upstream revision could not be determined."
        log(f"WARNING: {message}")
        return False, False, message

    upstream_revision = upstream_output.strip().splitlines()[-1]
    if head_revision == upstream_revision:
        message = f"No FFmpeg update is available. Source is up to date at {head_revision[:12]}."
        log(message)
        return True, False, message

    # HEAD differing from upstream can also mean local commits are ahead or the
    # histories diverged, so use merge-base --is-ancestor to distinguish a real
    # fast-forward update from those cases.
    ancestor_code, _ = _run_update_command(
        [git, "merge-base", "--is-ancestor", head_revision, upstream_revision],
        timeout=30,
        cwd=FFMPEG_SOURCE_DIR,
    )
    if ancestor_code == 0:
        message = (
            f"FFmpeg update is available: {head_revision[:12]} -> {upstream_revision[:12]}. "
            "Open Settings and click Check / Update FFmpeg to install it."
        )
        log(message)
        return True, True, message

    reverse_ancestor_code, _ = _run_update_command(
        [git, "merge-base", "--is-ancestor", upstream_revision, head_revision],
        timeout=30,
        cwd=FFMPEG_SOURCE_DIR,
    )
    if reverse_ancestor_code == 0:
        message = (
            f"No FFmpeg update is available from origin; the local checkout is ahead "
            f"({head_revision[:12]} vs {upstream_revision[:12]})."
        )
        log(message)
        return True, False, message

    message = (
        "FFmpeg local and upstream Git histories have diverged, so update availability cannot be "
        "reported as a normal fast-forward update. The manual updater will leave this checkout unchanged."
    )
    log(f"WARNING: {message}")
    return False, False, message

def update_ffmpeg_source_checkout(log) -> tuple[bool, bool, str]:
    """Check/update FFmpeg source and convert a legacy source snapshot into a Git checkout.

    Returns (success, changed, summary). A successful no-op means a real Git
    checkout was already current and no rebuild is required.
    """
    if not _ffmpeg_source_checkout_exists():
        message = f"FFmpeg source checkout was not found at {FFMPEG_SOURCE_DIR}."
        log(f"ERROR: {message}")
        return False, False, message

    git = shutil.which("git")
    if not git:
        message = "Git was not found on PATH, so FFmpeg updates cannot be checked."
        log(f"ERROR: {message}")
        return False, False, message

    # Older versions of this downloader could leave binaries/ffmpeg as a plain
    # source snapshot/copy. Such a directory has valid FFmpeg source files but no
    # .git metadata, so `git pull` cannot work. Convert it once by cloning the
    # official FFmpeg repository beside it and atomically swapping the directories.
    git_probe_code, git_probe_output = _run_update_command(
        [git, "rev-parse", "--is-inside-work-tree"],
        timeout=30,
        cwd=FFMPEG_SOURCE_DIR,
    )
    is_git_checkout = git_probe_code == 0 and git_probe_output.strip().lower().endswith("true")

    if not is_git_checkout:
        log(
            "FFmpeg source folder is a plain source tree without Git metadata. "
            "Converting it to a proper FFmpeg Git checkout..."
        )
        parent_dir = os.path.dirname(FFMPEG_SOURCE_DIR)
        staged_dir = os.path.join(parent_dir, "ffmpeg.git-new")
        backup_dir = os.path.join(parent_dir, "ffmpeg.snapshot-backup")

        try:
            for stale in (staged_dir, backup_dir):
                if os.path.isdir(stale):
                    shutil.rmtree(stale)
                elif os.path.exists(stale):
                    os.remove(stale)
        except OSError as exc:
            message = f"Could not clear a stale FFmpeg update staging folder: {exc}"
            log(f"ERROR: {message}")
            return False, False, message

        cloned = False
        last_output = ""
        for repo_url in FFMPEG_GIT_URLS:
            log(f"Cloning FFmpeg Git source from: {repo_url}")
            clone_code, clone_output = _run_update_command(
                [git, "-c", "core.autocrlf=false", "clone", "--depth", "1", repo_url, staged_dir],
                timeout=900,
                cwd=parent_dir,
            )
            last_output = clone_output
            if clone_output:
                for line in clone_output.splitlines():
                    if line.strip():
                        log(f"[ffmpeg-git] {line}")
            if clone_code == 0 and os.path.isfile(os.path.join(staged_dir, "configure")):
                cloned = True
                break

            try:
                if os.path.isdir(staged_dir):
                    shutil.rmtree(staged_dir)
                elif os.path.exists(staged_dir):
                    os.remove(staged_dir)
            except OSError:
                pass

        if not cloned:
            message = "Could not clone the FFmpeg Git repository. The existing source tree and compiled FFmpeg were left unchanged."
            log(f"ERROR: {message}")
            if last_output:
                log(last_output.splitlines()[-1])
            return False, False, message

        revision_code, revision_output = _run_update_command(
            [git, "rev-parse", "HEAD"], timeout=30, cwd=staged_dir
        )
        new_revision = (
            revision_output.strip().splitlines()[-1]
            if revision_code == 0 and revision_output.strip()
            else "unknown"
        )

        try:
            os.replace(FFMPEG_SOURCE_DIR, backup_dir)
            try:
                os.replace(staged_dir, FFMPEG_SOURCE_DIR)
            except Exception:
                os.replace(backup_dir, FFMPEG_SOURCE_DIR)
                raise
        except Exception as exc:
            try:
                if os.path.isdir(staged_dir):
                    shutil.rmtree(staged_dir)
            except OSError:
                pass
            message = f"Could not replace the legacy FFmpeg source tree with the Git checkout: {exc}"
            log(f"ERROR: {message}")
            return False, False, message

        try:
            shutil.rmtree(backup_dir)
        except OSError as exc:
            log(f"WARNING: The old FFmpeg source snapshot could not be removed: {backup_dir} ({exc})")

        log(f"FFmpeg source is now a real Git checkout at revision: {new_revision[:12]}")
        message = "FFmpeg source tree was converted to the latest Git checkout. Rebuilding the compiled FFmpeg output now."
        log(message)
        return True, True, message

    before_code, before_output = _run_update_command(
        [git, "rev-parse", "HEAD"], timeout=30, cwd=FFMPEG_SOURCE_DIR
    )
    if before_code != 0 or not before_output.strip():
        message = "Could not read the current FFmpeg Git revision."
        log(f"ERROR: {message}")
        if before_output:
            log(before_output.splitlines()[-1])
        return False, False, message

    before_revision = before_output.strip().splitlines()[-1]
    log(f"Checking FFmpeg Git checkout for updates: {FFMPEG_SOURCE_DIR}")
    log("FFmpeg source update: git pull --ff-only")
    pull_code, pull_output = _run_update_command(
        [git, "pull", "--ff-only"], timeout=300, cwd=FFMPEG_SOURCE_DIR
    )
    if pull_output:
        for line in pull_output.splitlines():
            if line.strip():
                log(f"[ffmpeg-git] {line}")
    if pull_code != 0:
        message = "FFmpeg Git update failed; the existing source checkout and compiled build were left in place."
        log(f"ERROR: {message}")
        return False, False, message

    after_code, after_output = _run_update_command(
        [git, "rev-parse", "HEAD"], timeout=30, cwd=FFMPEG_SOURCE_DIR
    )
    if after_code != 0 or not after_output.strip():
        message = "FFmpeg was pulled, but the new Git revision could not be read."
        log(f"ERROR: {message}")
        return False, False, message

    after_revision = after_output.strip().splitlines()[-1]
    changed = before_revision != after_revision
    if not changed:
        message = f"FFmpeg source is already up to date at {after_revision[:12]}. No rebuild is needed."
        log(message)
        return True, False, message

    log(f"FFmpeg source updated: {before_revision[:12]} -> {after_revision[:12]}")
    message = "FFmpeg source was updated. Rebuilding the compiled FFmpeg output now."
    log(message)
    return True, True, message


def ensure_ffmpeg_ready(log, force_rebuild: bool = False) -> bool:
    """Compile the cloned FFmpeg when needed; otherwise use the completed dedicated build."""
    if _ffmpeg_source_checkout_exists():
        ffmpeg = _ffmpeg_built_tool("ffmpeg")
        ffprobe = _ffmpeg_built_tool("ffprobe")
        dll_ready = _ffmpeg_shared_runtime_ready()
        profile_ready = _ffmpeg_build_profile_ready()

        if ffmpeg and ffprobe and dll_ready and profile_ready and not force_rebuild:
            runtime_ok, runtime_error = _verify_ffmpeg_runtime(ffmpeg, ffprobe)
            encoders_ok, encoders_error = _verify_ffmpeg_required_encoders(ffmpeg)
            if runtime_ok and encoders_ok:
                log("Separate FFmpeg multi-vendor build already exists and is complete; skipping compilation.")
                log(f"FFmpeg: {ffmpeg}")
                log(f"FFprobe: {ffprobe}")
                return True
            if not runtime_ok:
                log(f"Existing FFmpeg runtime is incomplete or broken: {runtime_error}")
            if not encoders_ok:
                log(f"Existing FFmpeg build is missing a required codec: {encoders_error}")

        if force_rebuild:
            log("FFmpeg source changed; forcing a fresh rebuild of the separate compiled output...")
            try:
                if os.path.isfile(FFMPEG_BUILD_MARKER):
                    os.remove(FFMPEG_BUILD_MARKER)
            except OSError as exc:
                log(f"WARNING: Could not invalidate the FFmpeg build marker: {exc}")

            if sys.platform == "win32":
                workspace = _windows_ffmpeg_build_workspace(log)
                if workspace:
                    try:
                        if os.path.isdir(workspace):
                            shutil.rmtree(workspace)
                            log("Cleared the old Windows FFmpeg compile workspace so the updated Git source is recopied.")
                    except OSError as exc:
                        log(f"ERROR: Could not refresh the Windows FFmpeg build workspace: {exc}")
                        return False
        elif not profile_ready and (ffmpeg or ffprobe):
            log("FFmpeg build profile changed; rebuilding once for AMD + Intel + NVIDIA and Linux support...")
        elif sys.platform == "win32" and ffmpeg and ffprobe and not dll_ready:
            log("Existing Windows FFmpeg build is missing shared DLLs; rebuilding...")
        else:
            log("Separate FFmpeg build output was not found or is incomplete. Starting compilation...")

        success = _build_ffmpeg_windows(log) if sys.platform == "win32" else _build_ffmpeg_posix(log)
        if not success:
            return False

        try:
            detect_ffmpeg_features.cache_clear()
        except NameError:
            pass

        ffmpeg = _ffmpeg_built_tool("ffmpeg")
        ffprobe = _ffmpeg_built_tool("ffprobe")
        if ffmpeg and ffprobe and _ffmpeg_shared_runtime_ready():
            runtime_ok, runtime_error = _verify_ffmpeg_runtime(ffmpeg, ffprobe)
            encoders_ok, encoders_error = _verify_ffmpeg_required_encoders(ffmpeg)
            if runtime_ok and encoders_ok:
                try:
                    _write_ffmpeg_build_profile()
                except OSError as exc:
                    log(f"WARNING: FFmpeg was built, but the build-profile marker could not be written: {exc}")
                log("FFmpeg multi-vendor build finished successfully in the separate build folder.")
                log("Required FFmpeg audio encoder available: libmp3lame")
                log(f"FFmpeg: {ffmpeg}")
                log(f"FFprobe: {ffprobe}")
                return True
            if not runtime_ok:
                log(f"ERROR: FFmpeg was built but cannot start: {runtime_error}")
            if not encoders_ok:
                log(f"ERROR: FFmpeg was built without a required codec: {encoders_error}")
            return False

        log("ERROR: FFmpeg build finished, but the executable/runtime set is incomplete.")
        return False

    # If there is no cloned source checkout, Linux can still use distro FFmpeg.
    ffmpeg = find_ffmpeg_executable("ffmpeg")
    ffprobe = find_ffmpeg_executable("ffprobe")
    if ffmpeg and ffprobe:
        runtime_ok, runtime_error = _verify_ffmpeg_runtime(ffmpeg, ffprobe)
        if runtime_ok:
            log(f"Using existing FFmpeg installation: {ffmpeg}")
            return True
        log(f"ERROR: Existing FFmpeg installation could not start: {runtime_error}")
        return False

    log(
        f"ERROR: No FFmpeg source checkout was found at {FFMPEG_SOURCE_DIR}, "
        "and no usable FFmpeg installation was found."
    )
    return False


def update_ytdlp_installation() -> tuple[bool, list[str]]:
    messages: list[str] = []
    source_main = _ytdlp_source_main()

    if source_main:
        messages.append(f"Using cloned yt-dlp source checkout: {YTDLP_SOURCE_DIR}")
        git = shutil.which("git")
        git_dir = os.path.join(YTDLP_SOURCE_DIR, ".git")
        if git and os.path.isdir(git_dir):
            code, output = _run_update_command(
                [git, "pull", "--ff-only"],
                timeout=180,
                cwd=YTDLP_SOURCE_DIR,
            )
            messages.append("yt-dlp source update: git pull --ff-only")
            if output:
                messages.extend(line for line in output.splitlines() if line.strip())
            if code != 0:
                messages.append(
                    "WARNING: The yt-dlp source checkout could not be updated; "
                    "the existing checkout will still be tested and used."
                )
        elif not git:
            messages.append(
                "WARNING: Git was not found on PATH, so the cloned yt-dlp checkout "
                "could not be updated automatically."
            )

        verify_code, version_output = _run_update_command(
            [sys.executable, source_main, "--version"], timeout=30
        )
        if version_output:
            messages.append(f"yt-dlp source version: {version_output.splitlines()[-1]}")
        if verify_code == 0:
            return True, messages

        messages.append("ERROR: The cloned yt-dlp source checkout could not be executed.")
        return False, messages

    executable = find_ytdlp_executable()
    if executable:
        code, output = _run_update_command([executable, "-U"])
        messages.append(f"yt-dlp standalone updater: {executable}")
        if output:
            messages.extend(line for line in output.splitlines() if line.strip())
        if code == 0:
            command = get_ytdlp_command()
            return command is not None, messages

        if sys.platform.startswith("linux"):
            messages.append(
                "Bundled yt-dlp_linux could not update itself; the existing binary will be used."
            )
            return get_ytdlp_command() is not None, messages

        messages.append("Standalone update did not succeed; trying pip.")

    if sys.platform.startswith("linux"):
        messages.append(
            "Missing binaries/yt-dlp source checkout or binaries/yt-dlp_linux."
        )
        return False, messages

    pip_base = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "yt-dlp",
        "--disable-pip-version-check",
    ]
    attempts = [pip_base]
    if sys.prefix == getattr(sys, "base_prefix", sys.prefix):
        attempts.append([*pip_base, "--user"])

    for command in attempts:
        code, output = _run_update_command(command)
        messages.append(f"yt-dlp pip updater: {command_text(command)}")
        if output:
            messages.extend(line for line in output.splitlines() if line.strip())
        if code == 0:
            break
    else:
        return get_ytdlp_command() is not None, messages

    verify_code, version_output = _run_update_command(
        [sys.executable, "-m", "yt_dlp", "--version"], timeout=20
    )
    if version_output:
        messages.append(f"Installed yt-dlp version: {version_output.splitlines()[-1]}")
    return verify_code == 0 or get_ytdlp_command() is not None, messages


def parse_ffmpeg_component_names(output: str) -> set[str]:
    """
    Parse names from `ffmpeg -encoders` or `ffmpeg -decoders`.

    A normal row resembles:
        V....D h264_amf            AMD AMF H.264 encoder

    The old script incorrectly selected the final word ("encoder") instead of
    the second column ("h264_amf").
    """
    names: set[str] = set()
    for raw_line in output.splitlines():
        parts = raw_line.strip().split()
        if len(parts) < 2:
            continue

        flags = parts[0]
        name = parts[1]
        if re.fullmatch(r"[A-Z\.]{6,8}", flags, re.IGNORECASE):
            names.add(name.lower())

    return names


def build_final_path(src: str, target_extension: str) -> str:
    """Turn `Title.temp.mkv.webm` into `Title.<target_extension>`."""
    extension = target_extension.lower().lstrip(".")
    filename = os.path.basename(src)

    # Use the final marker inserted by the output template. A video title may
    # itself contain the text `.temp`, so matching the first occurrence would
    # incorrectly truncate the title.
    marker_index = filename.lower().rfind(".temp.")
    if marker_index >= 0:
        root = filename[:marker_index]
    else:
        root = os.path.splitext(filename)[0]

    return os.path.join(os.path.dirname(src), f"{root}.{extension}")


def unique_existing_paths(paths: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for path in paths:
        absolute = os.path.abspath(path)
        key = os.path.normcase(absolute)
        if key in seen or not os.path.isfile(absolute):
            continue
        seen.add(key)
        result.append(absolute)
    return result



def snapshot_temp_files(folder: str) -> dict[str, tuple[int, int]]:
    """Record temp-file mtimes/sizes so stale files are never reconverted."""
    snapshot: dict[str, tuple[int, int]] = {}
    try:
        names = os.listdir(folder)
    except OSError:
        return snapshot
    for name in names:
        if ".temp." not in name.lower():
            continue
        path = os.path.abspath(os.path.join(folder, name))
        if not os.path.isfile(path):
            continue
        try:
            stat = os.stat(path)
            snapshot[os.path.normcase(path)] = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            continue
    return snapshot


# ---------------------------------------------------------------------------
# FFmpeg capability detection
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def detect_ffmpeg_features() -> dict:
    result = {
        "ffmpeg": None,
        "ffprobe": None,
        "hwaccels": set(),
        "encoders": set(),
        "decoders": set(),
    }

    ffmpeg = find_ffmpeg_executable("ffmpeg")
    ffprobe = find_ffmpeg_executable("ffprobe")
    result["ffmpeg"] = ffmpeg
    result["ffprobe"] = ffprobe

    if not ffmpeg:
        return result

    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-hwaccels"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        for line in (proc.stdout + proc.stderr).splitlines():
            value = line.strip().lower()
            if not value or value.startswith("hardware acceleration"):
                continue
            if re.fullmatch(r"[a-z0-9_+-]+", value):
                result["hwaccels"].add(value)
    except Exception:
        pass

    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=12,
            check=False,
        )
        result["encoders"] = parse_ffmpeg_component_names(proc.stdout + proc.stderr)
    except Exception:
        pass

    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-decoders"],
            capture_output=True,
            text=True,
            timeout=12,
            check=False,
        )
        result["decoders"] = parse_ffmpeg_component_names(proc.stdout + proc.stderr)
    except Exception:
        pass

    return result


def gpu_encoder_candidates(video_codec: str, backend: str) -> list[str]:
    """Return encoder candidates for the selected vendor on the current operating system."""
    backend = backend.upper()
    if sys.platform.startswith("linux"):
        linux = {
            "AMD": {
                "h264": ["h264_vaapi", "h264_amf"],
                "h265": ["hevc_vaapi", "hevc_amf"],
                "vp9": ["vp9_vaapi"],
                "av1": ["av1_vaapi", "av1_amf"],
            },
            "INTEL": {
                "h264": ["h264_qsv", "h264_vaapi"],
                "h265": ["hevc_qsv", "hevc_vaapi"],
                "vp9": ["vp9_qsv", "vp9_vaapi"],
                "av1": ["av1_qsv", "av1_vaapi"],
            },
            "NVIDIA": GPU_ENCODER_CANDIDATES["NVIDIA"],
        }
        return list(linux.get(backend, {}).get(video_codec, []))
    return list(GPU_ENCODER_CANDIDATES.get(backend, {}).get(video_codec, []))


def hardware_supported_codecs(backend: str) -> list[str]:
    return [codec for codec in VIDEO_CODECS if codec != "copy" and gpu_encoder_candidates(codec, backend)]


def _linux_render_node_for_vendor(backend: str) -> Optional[str]:
    if not sys.platform.startswith("linux"):
        return None
    vendor_id = {"AMD": AMD_PCI_VENDOR_ID, "INTEL": INTEL_PCI_VENDOR_ID}.get(backend.upper())
    if not vendor_id:
        return None
    sys_drm = "/sys/class/drm"
    try:
        names = sorted(name for name in os.listdir(sys_drm) if re.fullmatch(r"renderD\d+", name))
    except OSError:
        return None
    for name in names:
        vendor_file = os.path.join(sys_drm, name, "device", "vendor")
        try:
            value = Path(vendor_file).read_text(encoding="ascii").strip().lower()
        except Exception:
            continue
        if value == vendor_id.lower():
            device = os.path.join("/dev/dri", name)
            if os.path.exists(device):
                return device
    return None


def _amd_device_init_args() -> list[str]:
    """Create a vendor-bound AMD device on Windows; Linux normally uses AMD VAAPI."""
    if sys.platform == "win32":
        return [
            "-init_hw_device", f"d3d11va=amd_d3d11:,vendor_id={AMD_PCI_VENDOR_ID}",
            "-init_hw_device", "amf=amd_amf@amd_d3d11",
            "-filter_hw_device", "amd_amf",
        ]
    return []


def _intel_qsv_device_init_args() -> list[str]:
    if sys.platform == "win32":
        return [
            "-init_hw_device", f"d3d11va=intel_d3d11:,vendor_id={INTEL_PCI_VENDOR_ID}",
            "-init_hw_device", "qsv=intel_qsv@intel_d3d11",
            "-filter_hw_device", "intel_qsv",
        ]
    if sys.platform.startswith("linux"):
        node = _linux_render_node_for_vendor("INTEL")
        if node:
            return [
                "-init_hw_device", f"vaapi=intel_va:{node}",
                "-init_hw_device", "qsv=intel_qsv@intel_va",
                "-filter_hw_device", "intel_qsv",
            ]
    return []


def _vaapi_device_init_args(backend: str) -> list[str]:
    node = _linux_render_node_for_vendor(backend)
    if not node:
        return []
    name = "amd_va" if backend.upper() == "AMD" else "intel_va"
    return [
        "-init_hw_device", f"vaapi={name}:{node}",
        "-filter_hw_device", name,
    ]


def _encoder_output_args(encoder: str) -> list[str]:
    args = ["-c:v", encoder]
    if encoder.endswith("_amf"):
        args += ["-usage", "transcoding"]
    return args


def usable_gpu_encoders(video_codec: str, backend: str) -> list[str]:
    features = detect_ffmpeg_features()
    return [encoder for encoder in gpu_encoder_candidates(video_codec, backend) if encoder in features["encoders"]]


def gpu_decode_strategies(
    encoder: str, backend: str, hwaccels: set[str]
) -> list[tuple[str, list[str]]]:
    """Return hardware-decode prefixes that remain bound to the selected vendor."""
    backend = backend.upper()

    if sys.platform.startswith("linux") and encoder.endswith("_vaapi") and "vaapi" in hwaccels:
        node = _linux_render_node_for_vendor(backend)
        if node:
            name = "amd_va" if backend == "AMD" else "intel_va"
            return [(
                f"{backend} VAAPI decode",
                [
                    "-init_hw_device", f"vaapi={name}:{node}",
                    "-filter_hw_device", name,
                    "-hwaccel", "vaapi",
                    "-hwaccel_device", name,
                    "-hwaccel_output_format", "vaapi",
                    "-extra_hw_frames", "32",
                ],
            )]
        return []

    if backend == "AMD":
        if sys.platform == "win32" and "d3d11va" in hwaccels:
            return [(
                "AMD D3D11VA decode",
                [
                    *_amd_device_init_args(),
                    "-hwaccel", "d3d11va",
                    "-hwaccel_device", "amd_d3d11",
                    "-hwaccel_output_format", "d3d11",
                    "-extra_hw_frames", "32",
                ],
            )]
        # Linux AMF encoding is kept as a CPU-decode fallback because AMF runtime
        # initialization there is driver/Vulkan dependent; VAAPI is tried first.
        return []

    if backend == "INTEL" and encoder.endswith("_qsv") and "qsv" in hwaccels:
        init = _intel_qsv_device_init_args()
        if init:
            return [(
                "Intel QSV decode",
                [*init, "-hwaccel", "qsv", "-hwaccel_device", "intel_qsv", "-hwaccel_output_format", "qsv", "-extra_hw_frames", "32"],
            )]
        return [(
            "Intel QSV decode",
            ["-hwaccel", "qsv", "-hwaccel_output_format", "qsv", "-extra_hw_frames", "32"],
        )]

    if backend == "NVIDIA" and "cuda" in hwaccels:
        return [(
            "NVIDIA CUDA decode",
            ["-hwaccel", "cuda", "-hwaccel_device", "0", "-hwaccel_output_format", "cuda", "-extra_hw_frames", "32"],
        )]

    return []


def cpu_decode_gpu_encode_strategies(encoder: str, backend: str) -> list[tuple[str, list[str], Optional[str]]]:
    """Return CPU-decode + same-vendor GPU-encode attempts."""
    backend = backend.upper()
    if encoder.endswith("_vaapi"):
        init = _vaapi_device_init_args(backend)
        if not init:
            return []
        return [(f"vendor-bound {backend} VAAPI", init, "format=nv12,hwupload")]

    if encoder.endswith("_qsv"):
        attempts: list[tuple[str, list[str], Optional[str]]] = [("Intel QSV", [], "format=nv12")]
        init = _intel_qsv_device_init_args()
        if init:
            attempts.append(("vendor-bound Intel QSV", init, "format=nv12,hwupload"))
        return attempts

    if encoder.endswith("_amf"):
        attempts = [("AMD AMF", [], "format=nv12")]
        if sys.platform == "win32":
            attempts.append(("vendor-bound AMD AMF", _amd_device_init_args(), "format=nv12,hwupload"))
        return attempts

    if encoder.endswith("_nvenc"):
        return [("NVIDIA NVENC", [], "format=nv12")]

    return [(backend, [], "format=nv12")]


def probe_input_video(src: str) -> dict:
    features = detect_ffmpeg_features()
    ffprobe = features.get("ffprobe")
    if not ffprobe:
        return {}

    cmd = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name,profile,pix_fmt,width,height",
        "-of",
        "json",
        src,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=12,
            check=False,
        )
        if proc.returncode != 0:
            return {}
        data = json.loads(proc.stdout)
        streams = data.get("streams") or []
        return streams[0] if streams else {}
    except Exception:
        return {}


def probe_input_audio(src: str) -> dict:
    """Return basic information about the first audio stream in *src*."""
    features = detect_ffmpeg_features()
    ffprobe = features.get("ffprobe")
    if not ffprobe:
        return {}

    cmd = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_name,profile,sample_rate,channels",
        "-of",
        "json",
        src,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=12,
            check=False,
        )
        if proc.returncode != 0:
            return {}
        data = json.loads(proc.stdout)
        streams = data.get("streams") or []
        return streams[0] if streams else {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Download/conversion worker
# ---------------------------------------------------------------------------


class Worker(threading.Thread):
    def __init__(self, urls: list[str], opts: dict, output_queue: queue.Queue):
        super().__init__(daemon=True)
        self.urls = urls
        self.url = urls[0] if urls else ""
        self.opts = opts
        self.q = output_queue
        self.proc: Optional[subprocess.Popen] = None
        self.downloaded_files: list[str] = []

    def _log_capabilities(self) -> None:
        features = detect_ffmpeg_features()
        ffmpeg = features.get("ffmpeg")
        if not ffmpeg:
            if sys.platform.startswith("linux"):
                self.q.put(("error", "ffmpeg was not found in the dedicated build output or on the Linux system PATH."))
            else:
                self.q.put(("error", "ffmpeg was not found in binaries/, beside the script, or on PATH."))
            return

        self.q.put(("log", f"Build: {APP_VERSION}"))
        self.q.put(("log", f"FFmpeg: {ffmpeg}"))
        if features["hwaccels"]:
            self.q.put(("log", f"FFmpeg hwaccels: {', '.join(sorted(features['hwaccels']))}"))
        else:
            self.q.put(("log", "FFmpeg reports no hardware acceleration methods."))

        selected_codec = self.opts.get("video_codec", "copy")
        backend = self.opts.get("encoder_backend", "CPU")
        self.q.put(("log", f"Forced encoder backend: {backend}"))
        self.q.put((
            "log",
            "yt-dlp audio conversion: "
            + ("enabled" if self.opts.get("use_ytdlp_audio_conversion", True) else "disabled"),
        ))

        if backend == "CPU" or selected_codec in ("copy", None):
            return

        mapped = gpu_encoder_candidates(selected_codec, backend)
        compiled = [encoder for encoder in mapped if encoder in features["encoders"]]
        if compiled:
            labels = [GPU_ENCODER_LABELS.get(item, item) for item in compiled]
            self.q.put(("log", f"Compiled {backend} encoder(s) for {selected_codec}: {', '.join(labels)}"))
        elif mapped:
            self.q.put(("log", f"No compiled {backend} encoder was found for {selected_codec}."))
        else:
            self.q.put(("log", f"{selected_codec} has no {backend} hardware encoder mapping."))

    def _get_playlist_title(self, url: str) -> Optional[str]:
        if not self.opts.get("download_playlist", True):
            return None

        command = get_ytdlp_command()
        if not command:
            return None

        try:
            proc = subprocess.run(
                [
                    *command,
                    url,
                    "--dump-single-json",
                    "--flat-playlist",
                    "--no-warnings",
                    "--yes-playlist",
                ],
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
            )
            if proc.returncode == 0:
                data = json.loads(proc.stdout)
                if is_playlist_info(data):
                    return data.get("title") or "playlist"
            elif proc.stderr.strip():
                self.q.put(("log", f"Playlist title lookup failed: {proc.stderr.strip()}"))
        except Exception as exc:
            self.q.put(("log", f"Playlist title lookup failed: {exc}"))
        return None

    def _build_outtmpl(self, outdir: str, mode: str) -> tuple[str, str]:
        del mode
        playlist_title = self._get_playlist_title(self.url)
        if playlist_title:
            folder = os.path.join(outdir, sanitize_filename(playlist_title))
        else:
            folder = outdir
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, "%(title)s.temp.%(ext)s"), folder

    def _stream_process(self, proc: subprocess.Popen, prefix: Optional[str] = None) -> int:
        self.proc = proc
        try:
            if proc.stdout is not None:
                for line in iter(proc.stdout.readline, ""):
                    if line == "":
                        break
                    text = line.rstrip()
                    if not text:
                        continue
                    if prefix:
                        self.q.put(("log", f"{prefix} {text}"))
                    else:
                        self.q.put(("log", text))
            proc.wait()
            return int(proc.returncode or 0)
        except Exception as exc:
            self.q.put(("log", f"Process streaming error: {exc}"))
            return -1
        finally:
            self.proc = None

    def _use_ytdlp_video_native(self) -> bool:
        """yt-dlp can natively download/merge to mp4 when nothing needs transcoding."""
        return (
            str(self.opts.get("container") or "").lower().lstrip(".") == "mp4"
            and self.opts.get("video_codec") == "copy"
            and self.opts.get("audio_codec") == "copy"
        )

    def _run_yt_dlp_subprocess(self, outtmpl: str, mode: str) -> bool:
        command = get_ytdlp_command()
        if not command:
            self.q.put(("error", "yt-dlp was not found after the startup update."))
            return False

        cmd = [
            *command,
            self.url,
            "-o",
            outtmpl,
            "--no-warnings",
            "--no-color",
            "--newline",
            "--force-overwrites",
            "--yes-playlist" if self.opts.get("download_playlist", True) else "--no-playlist",
        ]

        if mode == "thumbnail":
            cmd += ["--skip-download", "--write-thumbnail"]
        elif mode == "audio":
            cmd += ["-f", "bestaudio/best"]
            audio_format = str(self.opts.get("container") or "best").lower()
            if (
                self.opts.get("use_ytdlp_audio_conversion", True)
                and audio_format == "mp3"
            ):
                cmd += ["-x", "--audio-format", audio_format]

                # Explicitly point yt-dlp at the same FFmpeg installation used
                # by the application. This is especially useful for the bundled
                # Windows binaries while still working with PATH FFmpeg on Linux.
                ffmpeg = find_ffmpeg_executable("ffmpeg")
                if ffmpeg:
                    cmd += ["--ffmpeg-location", os.path.dirname(ffmpeg)]

                self.q.put((
                    "log",
                    f"yt-dlp audio post-processing enabled: --audio-format {audio_format}",
                ))
        else:
            if self._use_ytdlp_video_native():
                merge_format = str(self.opts.get("container") or "mp4").lower().lstrip(".")
                cmd += ["-f", "bestvideo+bestaudio/best", "--merge-output-format", merge_format]
                self.q.put((
                    "log",
                    f"yt-dlp native video processing enabled: "
                    f"--merge-output-format {merge_format} (copy/copy, no FFmpeg pass)",
                ))
            else:
                cmd += ["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mkv"]

        self.q.put(("log", f"Starting yt-dlp: {command_text(cmd)}"))

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                stdin=subprocess.DEVNULL,
            )
        except Exception as exc:
            self.q.put(("error", f"Failed to start yt-dlp: {exc}"))
            return False

        reported_paths: set[str] = set()
        destination_re = re.compile(r"Destination:\s+(.*)")
        merging_re = re.compile(r'Merging formats into\s*(?:"([^"]+)"|(.+))')
        thumbnail_re = re.compile(r"Writing video thumbnail\s+\d+\s+to:\s+(.*)")

        if proc.stdout is not None:
            for line in iter(proc.stdout.readline, ""):
                if line == "":
                    break
                text = line.rstrip()
                if text:
                    self.q.put(("log", text))

                match = destination_re.search(text)
                if match:
                    reported_paths.add(os.path.abspath(match.group(1).strip().strip('"')))

                match = merging_re.search(text)
                if match:
                    value = (match.group(1) or match.group(2) or "").strip().strip('"')
                    if value:
                        reported_paths.add(os.path.abspath(value))

                match = thumbnail_re.search(text)
                if match:
                    reported_paths.add(os.path.abspath(match.group(1).strip().strip('"')))

        proc.wait()
        if proc.returncode != 0:
            self.q.put(("error", f"yt-dlp exited with code {proc.returncode}"))
            return False

        self.downloaded_files.extend(sorted(reported_paths))
        self.q.put(("log", "yt-dlp finished."))
        return True

    def _find_changed_intermediate_files(
        self,
        out_folder: str,
        before: dict[str, tuple[int, int]],
    ) -> list[str]:
        candidates: list[str] = []
        after = snapshot_temp_files(out_folder)
        for normalized, signature in after.items():
            if before.get(normalized) == signature:
                continue
            # Recover the actual-cased path from the directory scan.
            for name in os.listdir(out_folder):
                path = os.path.abspath(os.path.join(out_folder, name))
                if os.path.normcase(path) == normalized and os.path.isfile(path):
                    candidates.append(path)
                    break
        return unique_existing_paths(candidates)

    def _audio_args(self, audio_codec: Optional[str], mode: str) -> list[str]:
        if mode == "thumbnail":
            return []

        if audio_codec in (None, "copy"):
            return ["-c:a", "copy"]

        ffmpeg_codec = AUDIO_FFMPEG_CODEC.get(audio_codec)
        if not ffmpeg_codec:
            return ["-c:a", "copy"]

        args = ["-c:a", ffmpeg_codec]
        if audio_codec == "aac":
            args += ["-b:a", "192k"]
        elif audio_codec == "opus":
            args += ["-b:a", "160k"]
        elif audio_codec == "mpeg-1":
            # MPEG-1 Audio Layer III, suitable for the .mp3 container.
            args += ["-q:a", "2"]
        elif audio_codec == "mpeg-2":
            args += ["-b:a", "192k"]

        return args

    def _copy_audio_compatible(self, source_codec: str, dst: str) -> bool:
        """Return whether an existing audio stream can be safely copied to dst."""
        codec = (source_codec or "").lower()
        extension = os.path.splitext(dst)[1].lower()

        if extension == ".mkv":
            return True
        if extension == ".webm":
            return codec in {"opus", "vorbis"}
        if extension in {".mp4", ".mov", ".m4a"}:
            return codec in {"aac", "alac", "mp3", "mp2", "ac3", "eac3", "flac"}
        if extension == ".mp3":
            return codec == "mp3"
        if extension == ".wav":
            return codec.startswith("pcm_")
        if extension == ".flac":
            return codec == "flac"
        if extension == ".opus":
            return codec == "opus"

        return False

    def _fallback_audio_codec_for_container(self, dst: str) -> Optional[str]:
        """Pick a sane encoder when stream-copy is invalid for the destination."""
        extension = os.path.splitext(dst)[1].lower()
        return {
            ".mp4": "aac",
            ".mov": "aac",
            ".m4a": "aac",
            ".mp3": "mpeg-1",
            ".wav": "lpcm",
            ".flac": "flac",
            ".opus": "opus",
            ".webm": "opus",
        }.get(extension)

    def _resolved_audio_args(
        self,
        src: str,
        dst: str,
        audio_codec: Optional[str],
        mode: str,
    ) -> list[str]:
        """Resolve 'copy' against the actual source codec and destination container."""
        if audio_codec not in (None, "copy"):
            return self._audio_args(audio_codec, mode)

        info = probe_input_audio(src)
        source_codec = str(info.get("codec_name") or "").lower()
        if source_codec and self._copy_audio_compatible(source_codec, dst):
            self.q.put(("log", f"Audio stream copy is compatible: {source_codec} -> {os.path.splitext(dst)[1].lower()}"))
            return ["-c:a", "copy"]

        fallback = self._fallback_audio_codec_for_container(dst)
        if fallback:
            shown_source = source_codec or "unknown codec"
            self.q.put((
                "log",
                f"Audio stream copy is not compatible: {shown_source} -> "
                f"{os.path.splitext(dst)[1].lower()}. Transcoding audio as {fallback} instead.",
            ))
            return self._audio_args(fallback, mode)

        # Unknown destination: preserve the user's explicit copy request and let
        # FFmpeg report any muxer limitation.
        return ["-c:a", "copy"]

    def _muxer_args(self, dst: str, video_codec: Optional[str]) -> list[str]:
        extension = os.path.splitext(dst)[1].lower()
        args: list[str] = []

        if extension in (".mp4", ".mov"):
            args += ["-movflags", "+faststart"]
        if video_codec == "h265" and extension in (".mp4", ".mov"):
            args += ["-tag:v", "hvc1"]

        return args

    def _run_ffmpeg_attempt(self, label: str, cmd: list[str], dst: str) -> bool:
        try:
            if os.path.isfile(dst):
                os.remove(dst)
        except OSError:
            pass

        self.q.put(("log", f"{label}: {command_text(cmd)}"))
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                stdin=subprocess.DEVNULL,
            )
        except Exception as exc:
            self.q.put(("log", f"{label} could not start: {exc}"))
            return False

        return_code = self._stream_process(proc, prefix="[ffmpeg]")
        if return_code == 0 and os.path.isfile(dst) and os.path.getsize(dst) > 0:
            self.q.put(("log", f"{label} succeeded."))
            return True

        self.q.put(("log", f"{label} failed with code {return_code}."))
        try:
            if os.path.isfile(dst):
                os.remove(dst)
        except OSError:
            pass
        return False

    def _base_video_output_args(
        self,
        dst: str,
        video_args: list[str],
        audio_args: list[str],
        video_codec: str,
        video_filter: Optional[str],
    ) -> list[str]:
        args = [
            "-map",
            "0:v:0?",
            "-map",
            "0:a:0?",
            "-map_metadata",
            "0",
            "-sn",
        ]

        if video_filter:
            args += ["-vf", video_filter]

        args += video_args
        args += audio_args
        args += self._muxer_args(dst, video_codec)
        args += [dst]
        return args

    def _convert_video(
        self,
        ffmpeg: str,
        src: str,
        dst: str,
        video_codec: str,
        audio_codec: str,
    ) -> bool:
        features = detect_ffmpeg_features()
        audio_args = self._resolved_audio_args(src, dst, audio_codec, "video")
        backend = self.opts.get("encoder_backend", "CPU")

        input_info = probe_input_video(src)
        if input_info:
            self.q.put(
                (
                    "log",
                    "Input video: "
                    f"codec={input_info.get('codec_name', 'unknown')}, "
                    f"pixel_format={input_info.get('pix_fmt', 'unknown')}, "
                    f"size={input_info.get('width', '?')}x{input_info.get('height', '?')}",
                )
            )

        if video_codec == "copy":
            cmd = [ffmpeg, "-y", "-nostdin", "-i", src]
            cmd += self._base_video_output_args(
                dst=dst,
                video_args=["-c:v", "copy"],
                audio_args=audio_args,
                video_codec=video_codec,
                video_filter=None,
            )
            return self._run_ffmpeg_attempt("Stream-copy/remux", cmd, dst)

        cpu_video_args = CPU_VIDEO_ENCODER_ARGS.get(video_codec)
        if not cpu_video_args:
            self.q.put(("error", f"No encoder mapping exists for {video_codec}."))
            return False

        if backend == "CPU":
            self.q.put(("log", f"Forced CPU encoder for {video_codec}."))
            cpu_cmd = [ffmpeg, "-y", "-nostdin", "-i", src]
            cpu_cmd += self._base_video_output_args(
                dst=dst,
                video_args=cpu_video_args,
                audio_args=audio_args,
                video_codec=video_codec,
                video_filter=None,
            )
            return self._run_ffmpeg_attempt("CPU decode + CPU encode", cpu_cmd, dst)

        mapped_encoders = gpu_encoder_candidates(video_codec, backend)
        if not mapped_encoders:
            self.q.put(
                (
                    "error",
                    f"The selected {backend} backend is forced, but {video_codec} has no "
                    f"{backend} hardware encoder mapping. Choose CPU or a codec supported "
                    f"by {backend}. No other backend was used.",
                )
            )
            return False

        gpu_encoders = usable_gpu_encoders(video_codec, backend)
        if not gpu_encoders:
            expected = ", ".join(mapped_encoders)
            self.q.put(
                (
                    "error",
                    f"The selected {backend} backend is forced, but FFmpeg does not contain "
                    f"a usable encoder for {video_codec}. Expected: {expected}. "
                    "No other GPU backend and no CPU fallback were used.",
                )
            )
            return False

        for encoder in gpu_encoders:
            label = GPU_ENCODER_LABELS.get(encoder, encoder)
            self.q.put(("log", f"Forced {backend} encoder: {label}"))

            for decode_label, decode_args in gpu_decode_strategies(
                encoder, backend, features["hwaccels"]
            ):
                gpu_decode_cmd = [ffmpeg, "-y", "-nostdin", *decode_args, "-i", src]
                gpu_decode_cmd += self._base_video_output_args(
                    dst=dst,
                    video_args=_encoder_output_args(encoder),
                    audio_args=audio_args,
                    video_codec=video_codec,
                    video_filter=None,
                )
                if self._run_ffmpeg_attempt(
                    f"{decode_label} + {backend} encode ({label})",
                    gpu_decode_cmd,
                    dst,
                ):
                    return True

            self.q.put((
                "log",
                f"Hardware decoding failed or was unavailable; retrying CPU decode "
                f"while keeping {label} encoding on the selected {backend} backend.",
            ))
            for strategy_label, prefix_args, video_filter in cpu_decode_gpu_encode_strategies(encoder, backend):
                direct_cmd = [ffmpeg, "-y", "-nostdin", *prefix_args, "-i", src]
                direct_cmd += self._base_video_output_args(
                    dst=dst,
                    video_args=_encoder_output_args(encoder),
                    audio_args=audio_args,
                    video_codec=video_codec,
                    video_filter=video_filter,
                )
                if self._run_ffmpeg_attempt(
                    f"CPU decode + {strategy_label} encode ({label})", direct_cmd, dst
                ):
                    return True

        self.q.put(
            (
                "error",
                f"Forced {backend} encoding failed for {video_codec}. "
                "No other GPU backend and no CPU fallback were used.",
            )
        )
        return False

    def _finalize_ytdlp_output(self, src: str, dst: str, label: str = "output") -> bool:
        """Move a yt-dlp-produced file (audio or video) to its final name."""
        src_abs = os.path.abspath(src)
        dst_abs = os.path.abspath(dst)

        if os.path.normcase(src_abs) == os.path.normcase(dst_abs):
            return os.path.isfile(dst_abs) and os.path.getsize(dst_abs) > 0

        try:
            if os.path.isfile(dst_abs):
                os.remove(dst_abs)
            os.replace(src_abs, dst_abs)
        except OSError as exc:
            self.q.put(("error", f"Could not finalize yt-dlp {label}: {exc}"))
            return False

        if not os.path.isfile(dst_abs) or os.path.getsize(dst_abs) <= 0:
            self.q.put(("error", f"yt-dlp {label} is missing or empty: {dst_abs}"))
            return False

        self.q.put((
            "log",
            f"yt-dlp {label} finalized without a second FFmpeg conversion: {dst_abs}",
        ))
        return True

    def _convert_audio(
        self,
        ffmpeg: str,
        src: str,
        dst: str,
        audio_codec: str,
    ) -> bool:
        cmd = [
            ffmpeg,
            "-y",
            "-nostdin",
            "-i",
            src,
            "-map",
            "0:a:0?",
            "-vn",
            "-map_metadata",
            "0",
        ]
        cmd += self._resolved_audio_args(src, dst, audio_codec, "audio")
        cmd += [dst]
        return self._run_ffmpeg_attempt("Audio conversion", cmd, dst)

    def _convert_thumbnail(self, ffmpeg: str, src: str, dst: str) -> bool:
        if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(dst)):
            return True

        cmd = [
            ffmpeg,
            "-y",
            "-nostdin",
            "-i",
            src,
            "-frames:v",
            "1",
            dst,
        ]
        return self._run_ffmpeg_attempt("Thumbnail conversion", cmd, dst)

    def _ffmpeg_convert(
        self,
        src: str,
        dst: str,
        mode: str,
        video_codec: Optional[str],
        audio_codec: Optional[str],
    ) -> bool:
        features = detect_ffmpeg_features()
        ffmpeg = features.get("ffmpeg")
        if not ffmpeg:
            self.q.put(("error", "ffmpeg is required for conversion but was not found."))
            return False

        if mode == "video":
            return self._convert_video(
                ffmpeg,
                src,
                dst,
                video_codec or "copy",
                audio_codec or "copy",
            )
        if mode == "audio":
            return self._convert_audio(ffmpeg, src, dst, audio_codec or "copy")
        if mode == "thumbnail":
            return self._convert_thumbnail(ffmpeg, src, dst)

        self.q.put(("error", f"Unknown conversion mode: {mode}"))
        return False

    def _cleanup_after_success(self, source: str, final: str) -> None:
        final_abs = os.path.abspath(final)
        source_abs = os.path.abspath(source)
        directory = os.path.dirname(final_abs)
        final_stem = os.path.splitext(os.path.basename(final_abs))[0]
        removed: list[str] = []

        try:
            filenames = os.listdir(directory)
        except OSError as exc:
            self.q.put(("log", f"Cleanup scan failed: {exc}"))
            return

        for filename in filenames:
            path = os.path.abspath(os.path.join(directory, filename))
            if os.path.normcase(path) == os.path.normcase(final_abs):
                continue

            # Only remove the exact converted source or files carrying the
            # explicit `.temp.` marker inserted by this script. Do not remove
            # arbitrary similarly named media files.
            is_exact_source = os.path.normcase(path) == os.path.normcase(source_abs)
            is_our_temp_file = (
                filename.startswith(final_stem) and ".temp." in filename.lower()
            )
            if not (is_exact_source or is_our_temp_file):
                continue

            try:
                os.remove(path)
                removed.append(path)
            except OSError as exc:
                self.q.put(("log", f"Cleanup failed for {path}: {exc}"))

        if removed:
            self.q.put(("log", f"Removed intermediate files: {', '.join(removed)}"))

    def _run_single_url(self, url: str, index: int, total: int) -> tuple[bool, str]:
        self.url = url
        self.downloaded_files = []
        out_folder = self.opts.get("outdir", os.path.expanduser("~/Downloads"))

        self.q.put(("log", f"=== Link {index}/{total}: {url} ==="))
        mode = self.opts["mode"]
        outtmpl, out_folder = self._build_outtmpl(out_folder, mode)
        self.q.put(("log", f"Output template: {outtmpl}"))

        before_download = snapshot_temp_files(out_folder)
        success = self._run_yt_dlp_subprocess(outtmpl, mode)
        if not success:
            self.q.put(("error", f"Link {index}/{total} failed; conversion was not started."))
            return False, out_folder

        candidates = unique_existing_paths(self.downloaded_files)
        snapshot_candidates = self._find_changed_intermediate_files(
            out_folder, before_download
        )
        candidates = unique_existing_paths([*candidates, *snapshot_candidates])
        candidates.sort(key=lambda path: os.path.getmtime(path))

        use_ytdlp_audio = (
            mode == "audio"
            and self.opts.get("use_ytdlp_audio_conversion", True)
            and str(self.opts.get("container") or "").lower().lstrip(".") == "mp3"
        )
        use_ytdlp_video = (mode == "video" and self._use_ytdlp_video_native())
        use_ytdlp_native = use_ytdlp_audio or use_ytdlp_video

        if use_ytdlp_native:
            target_extension = "." + str(self.opts["container"]).lower().lstrip(".")
            candidates = [
                path
                for path in candidates
                if os.path.splitext(path)[1].lower() == target_extension
            ]

        if not candidates:
            if use_ytdlp_audio:
                self.q.put((
                    "error",
                    f"Link {index}/{total}: yt-dlp did not create the requested "
                    f"{self.opts['container']} audio file.",
                ))
            elif use_ytdlp_video:
                self.q.put((
                    "error",
                    f"Link {index}/{total}: yt-dlp did not create the requested "
                    f"{self.opts['container']} video file.",
                ))
            else:
                self.q.put(("error", f"Link {index}/{total}: no newly downloaded intermediate file was found."))
            return False, out_folder

        successful_outputs: list[str] = []
        successful_pairs: list[tuple[str, str]] = []
        for src in candidates:
            final = build_final_path(src, self.opts["container"])
            if use_ytdlp_native:
                kind = "audio" if use_ytdlp_audio else "video"
                self.q.put(("log", f"Finalizing yt-dlp {kind}: {src} -> {final}"))
                converted = self._finalize_ytdlp_output(src, final, kind)
            else:
                self.q.put(("log", f"Converting: {src} -> {final}"))
                converted = self._ffmpeg_convert(
                    src=src,
                    dst=final,
                    mode=mode,
                    video_codec=self.opts.get("video_codec"),
                    audio_codec=self.opts.get("audio_codec"),
                )
            if converted:
                successful_outputs.append(final)
                successful_pairs.append((src, final))
            else:
                self.q.put(("error", f"Conversion failed for {src}"))

        if not successful_outputs:
            self.q.put(("error", f"Link {index}/{total}: no output files were created successfully."))
            return False, out_folder

        for src, final in successful_pairs:
            if os.path.isfile(final):
                self._cleanup_after_success(src, final)

        self.q.put(("log", f"Link {index}/{total}: created {len(successful_outputs)} output file(s)."))
        return True, out_folder

    def run(self) -> None:
        out_folder = self.opts.get("outdir", os.path.expanduser("~/Downloads"))
        try:
            self._log_capabilities()
            total = len(self.urls)
            successful_links = 0

            for index, url in enumerate(self.urls, start=1):
                try:
                    success, out_folder = self._run_single_url(url, index, total)
                    if success:
                        successful_links += 1
                except Exception as exc:
                    self.q.put(("error", f"Link {index}/{total} failed with an exception: {exc}"))

            failed_links = total - successful_links
            self.q.put((
                "log",
                f"Batch complete: {successful_links} succeeded, {failed_links} failed, {total} total.",
            ))
        except Exception as exc:
            self.q.put(("error", f"Worker exception: {exc}"))
        finally:
            self.q.put(("done", out_folder))


# ---------------------------------------------------------------------------
# Tkinter UI
# ---------------------------------------------------------------------------


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"DOWNLOADER")
        self.root.minsize(900, 520)

        self.q: queue.Queue = queue.Queue()
        self.worker: Optional[Worker] = None
        self.settings, config_warning = load_config()
        self.settings_window: Optional[tk.Toplevel] = None
        self.ffmpeg_update_button: Optional[ttk.Button] = None
        self.ffmpeg_update_status_var: Optional[tk.StringVar] = None
        self.ffmpeg_update_in_progress = False
        self.startup_prepare_in_progress = True
        self.startup_tools_ready = False

        top = ttk.Frame(root, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="URL(s):").pack(side="left")
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(top, textvariable=self.url_var, width=80)
        self.url_entry.pack(side="left", padx=6, fill="x", expand=True)
        ttk.Button(top, text="Paste", command=self.paste_clipboard).pack(side="left", padx=4)

        options_row = ttk.Frame(root, padding=6)
        options_row.pack(fill="x")

        ttk.Label(options_row, text="Mode:").pack(side="left", padx=(0, 6))
        self.mode_var = tk.StringVar(value="video")
        self.mode_dd = ttk.OptionMenu(
            options_row,
            self.mode_var,
            "video",
            "video",
            "audio",
            "thumbnail",
            command=lambda _value: self.on_mode_change(),
        )
        self.mode_dd.pack(side="left", padx=(0, 12))

        ttk.Label(options_row, text="Container:").pack(side="left", padx=(0, 6))
        self.container_var = tk.StringVar(value="mp4")
        self.container_dd = ttk.OptionMenu(
            options_row,
            self.container_var,
            "mp4",
            "mp4",
            "mkv",
            "webm",
            "mov",
            command=lambda _value: self.on_container_change(),
        )
        self.container_dd.pack(side="left", padx=(0, 12))

        ttk.Label(options_row, text="Video codec:").pack(side="left", padx=(0, 6))
        self.video_codec_var = tk.StringVar(value="copy")
        self.video_codec_dd = ttk.OptionMenu(
            options_row,
            self.video_codec_var,
            "copy",
            *VIDEO_CODECS,
        )
        self.video_codec_dd.pack(side="left", padx=(0, 12))

        ttk.Label(options_row, text="Audio codec:").pack(side="left", padx=(0, 6))
        self.audio_codec_var = tk.StringVar(value="copy")
        self.audio_codec_dd = ttk.OptionMenu(
            options_row,
            self.audio_codec_var,
            "copy",
            *AUDIO_CODECS,
        )
        self.audio_codec_dd.pack(side="left", padx=(0, 12))

        output_row = ttk.Frame(root, padding=6)
        output_row.pack(fill="x")
        ttk.Label(output_row, text="Output folder:").pack(side="left")
        self.outdir_var = tk.StringVar(value=self.settings["default_download_dir"])
        ttk.Entry(output_row, textvariable=self.outdir_var, width=60).pack(
            side="left", padx=6, fill="x", expand=True
        )
        ttk.Button(output_row, text="Browse", command=self.browse_outdir).pack(side="left", padx=4)

        controls = ttk.Frame(root, padding=6)
        controls.pack(fill="x")
        self.start_btn = ttk.Button(controls, text="Start", command=self.start, state="disabled")
        self.start_btn.pack(side="left")
        ttk.Button(controls, text="Open Output Folder", command=self.open_outdir).pack(
            side="left", padx=6
        )
        ttk.Button(controls, text="Settings", command=self.open_settings).pack(side="right")

        status_frame = ttk.Frame(root, padding=6)
        status_frame.pack(fill="x")
        self.status_var = tk.StringVar(value="Preparing FFmpeg / yt-dlp")
        ttk.Label(status_frame, textvariable=self.status_var).pack(side="left")

        log_frame = ttk.LabelFrame(root, text="Log", padding=6)
        log_frame.pack(fill="both", expand=True, padx=6, pady=6)
        self.log_text = tk.Text(log_frame, height=22, state="disabled", wrap="word")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.log(f"App started. Build: {APP_VERSION}")
        self.log(f"Encoder backend: {self.settings['encoder_backend']}")
        self.log(
            "Playlist downloads: "
            + ("enabled" if self.settings["download_playlist"] else "disabled")
        )
        self.log(
            "yt-dlp audio conversion: "
            + ("enabled" if self.settings["use_ytdlp_audio_conversion"] else "disabled")
        )
        if config_warning:
            self.log(f"WARNING: {config_warning}")
        self.on_mode_change()
        self.root.after(100, self._start_tool_preparation)
        self.root.after(150, self._process_queue)

    def _start_tool_preparation(self) -> None:
        threading.Thread(target=self._startup_prepare_thread, daemon=True).start()

    def _startup_prepare_thread(self) -> None:
        def startup_log(message: str) -> None:
            self.q.put(("log", message))

        startup_log("Preparing local FFmpeg and yt-dlp tools...")
        ffmpeg_ready = ensure_ffmpeg_ready(startup_log)

        if ffmpeg_ready:
            # Only report FFmpeg update availability at startup. Do not pull or
            # rebuild automatically; the Settings button remains the explicit
            # update action.
            check_ffmpeg_update_available(startup_log)

        # yt-dlp is independent of the FFmpeg source/build check. Always attempt
        # its updater at app startup, even if FFmpeg preparation failed.
        startup_log("Attempting automatic yt-dlp update...")
        ytdlp_success, messages = update_ytdlp_installation()
        for message in messages:
            startup_log(message)
        ytdlp_ready = ytdlp_success and get_ytdlp_command() is not None

        self.q.put(("startup_tools_done", ffmpeg_ready, ytdlp_ready))

    def _start_ffmpeg_update(self) -> None:
        if self.ffmpeg_update_in_progress:
            return
        if self.startup_prepare_in_progress:
            parent = self.settings_window if self.settings_window is not None else self.root
            messagebox.showinfo(
                "FFmpeg update",
                "Startup tool preparation is still running. Try the FFmpeg update again after it finishes.",
                parent=parent,
            )
            return
        if self.worker is not None and self.worker.is_alive():
            parent = self.settings_window if self.settings_window is not None else self.root
            messagebox.showwarning(
                "FFmpeg update",
                "A download/conversion is currently running. Update FFmpeg after it finishes.",
                parent=parent,
            )
            return

        self.ffmpeg_update_in_progress = True
        if self.ffmpeg_update_button is not None:
            self.ffmpeg_update_button.configure(state="disabled")
        if self.ffmpeg_update_status_var is not None:
            self.ffmpeg_update_status_var.set("Checking for updates...")
        self.status_var.set("Updating FFmpeg")
        self.start_btn.configure(state="disabled")
        self.log("Manual FFmpeg update requested from Settings.")
        threading.Thread(target=self._ffmpeg_update_thread, daemon=True).start()

    def _ffmpeg_update_thread(self) -> None:
        def update_log(message: str) -> None:
            self.q.put(("log", message))

        success, changed, summary = update_ffmpeg_source_checkout(update_log)
        rebuilt = False
        if success and changed:
            rebuilt = ensure_ffmpeg_ready(update_log, force_rebuild=True)
            if rebuilt:
                summary = "FFmpeg source was updated and the compiled FFmpeg build was rebuilt successfully."
            else:
                summary = (
                    "FFmpeg source was updated, but the rebuild did not complete successfully. "
                    "The log contains the build error; the app will retry the rebuild on the next startup."
                )
                success = False

        self.q.put(("ffmpeg_update_done", success, changed, rebuilt, summary))

    def open_settings(self) -> None:
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.lift()
            self.settings_window.focus_force()
            return

        window = tk.Toplevel(self.root)
        self.settings_window = window
        window.title("Settings")
        window.transient(self.root)
        window.resizable(False, False)
        window.protocol("WM_DELETE_WINDOW", self._close_settings)

        content = ttk.Frame(window, padding=14)
        content.pack(fill="both", expand=True)

        backend_var = tk.StringVar(value=self.settings["encoder_backend"])
        directory_var = tk.StringVar(value=self.settings["default_download_dir"])
        playlist_var = tk.StringVar(
            value="Yes" if self.settings["download_playlist"] else "No"
        )
        ytdlp_audio_var = tk.BooleanVar(
            value=self.settings["use_ytdlp_audio_conversion"]
        )

        ttk.Label(content, text="Encoder backend:").grid(
            row=0, column=0, sticky="w", padx=(0, 12), pady=(0, 10)
        )
        backend_box = ttk.Combobox(
            content,
            textvariable=backend_var,
            values=ENCODER_BACKENDS,
            state="readonly",
            width=18,
        )
        backend_box.grid(row=0, column=1, sticky="ew", pady=(0, 10))

        ttk.Label(content, text="Default download directory:").grid(
            row=1, column=0, sticky="w", padx=(0, 12), pady=(0, 10)
        )
        directory_entry = ttk.Entry(content, textvariable=directory_var, width=52)
        directory_entry.grid(row=1, column=1, sticky="ew", pady=(0, 10))

        def browse_default_directory() -> None:
            initial = directory_var.get().strip() or DEFAULT_CONFIG["default_download_dir"]
            selected = filedialog.askdirectory(parent=window, initialdir=initial)
            if selected:
                directory_var.set(selected)

        ttk.Button(content, text="Browse", command=browse_default_directory).grid(
            row=1, column=2, padx=(8, 0), pady=(0, 10)
        )

        ttk.Label(content, text="Download full playlist:").grid(
            row=2, column=0, sticky="w", padx=(0, 12), pady=(0, 14)
        )
        playlist_box = ttk.Combobox(
            content,
            textvariable=playlist_var,
            values=("Yes", "No"),
            state="readonly",
            width=18,
        )
        playlist_box.grid(row=2, column=1, sticky="w", pady=(0, 10))

        ttk.Checkbutton(
            content,
            text="Use yt-dlp for format conversion (if supported)",
            variable=ytdlp_audio_var,
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(0, 14))

        ttk.Separator(content, orient="horizontal").grid(
            row=4, column=0, columnspan=3, sticky="ew", pady=(2, 12)
        )
        ttk.Label(content, text="FFmpeg update:").grid(
            row=5, column=0, sticky="w", padx=(0, 12), pady=(0, 4)
        )
        self.ffmpeg_update_status_var = tk.StringVar(value="")
        self.ffmpeg_update_button = ttk.Button(
            content,
            text="Check / Update FFmpeg",
            command=self._start_ffmpeg_update,
            state="disabled" if (self.startup_prepare_in_progress or self.ffmpeg_update_in_progress) else "normal",
        )
        self.ffmpeg_update_button.grid(row=5, column=1, sticky="w", pady=(0, 4))
        ttk.Label(content, textvariable=self.ffmpeg_update_status_var).grid(
            row=6, column=0, columnspan=3, sticky="w", pady=(0, 10)
        )

        content.columnconfigure(1, weight=1)

        buttons = ttk.Frame(content)
        buttons.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(12, 0))

        def save_settings() -> None:
            directory = directory_var.get().strip()
            if not directory:
                messagebox.showerror(
                    "Invalid directory",
                    "Enter a default download directory.",
                    parent=window,
                )
                return

            new_settings = {
                "encoder_backend": backend_var.get(),
                "default_download_dir": os.path.abspath(os.path.expanduser(directory)),
                "download_playlist": playlist_var.get() == "Yes",
                "use_ytdlp_audio_conversion": bool(ytdlp_audio_var.get()),
            }
            try:
                save_config(new_settings)
            except Exception as exc:
                messagebox.showerror(
                    "Settings error",
                    f"Could not save config.json:\n{exc}",
                    parent=window,
                )
                return

            self.settings = _validated_config(new_settings)
            self.outdir_var.set(self.settings["default_download_dir"])
            self.log(f"Settings saved. Encoder backend: {self.settings['encoder_backend']}")
            self.log(
                "Playlist downloads: "
                + ("enabled" if self.settings["download_playlist"] else "disabled")
            )
            self.log(
                "yt-dlp audio conversion: "
                + ("enabled" if self.settings["use_ytdlp_audio_conversion"] else "disabled")
            )
            self.on_mode_change()
            self._close_settings()

        def reset_fields() -> None:
            backend_var.set(DEFAULT_CONFIG["encoder_backend"])
            directory_var.set(DEFAULT_CONFIG["default_download_dir"])
            playlist_var.set("Yes" if DEFAULT_CONFIG["download_playlist"] else "No")
            ytdlp_audio_var.set(DEFAULT_CONFIG["use_ytdlp_audio_conversion"])

        ttk.Button(buttons, text="Save", command=save_settings).pack(side="left")
        ttk.Button(buttons, text="Reset to Defaults", command=reset_fields).pack(
            side="right"
        )

        window.update_idletasks()
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - window.winfo_width()) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - window.winfo_height()) // 2)
        window.geometry(f"+{x}+{y}")
        window.grab_set()
        backend_box.focus_set()

    def _close_settings(self) -> None:
        if self.settings_window is not None:
            try:
                self.settings_window.grab_release()
            except tk.TclError:
                pass
            try:
                self.settings_window.destroy()
            except tk.TclError:
                pass
            self.settings_window = None
            self.ffmpeg_update_button = None
            self.ffmpeg_update_status_var = None

    def log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _set_option_menu(
        self,
        widget: ttk.OptionMenu,
        variable: tk.StringVar,
        values: list[str],
        callback=None,
    ) -> None:
        menu = widget["menu"]
        menu.delete(0, "end")

        def choose(value: str) -> None:
            variable.set(value)
            if callback is not None:
                callback()

        for value in values:
            menu.add_command(label=value, command=lambda item=value: choose(item))

    def _allowed_video_codecs(self, container: str) -> list[str]:
        # Deliberately show the complete list. Compatibility is validated when
        # Start is pressed instead of silently removing codecs from the menu.
        return list(VIDEO_CODECS)

    def _allowed_audio_codecs(self, mode: str, container: str) -> list[str]:
        if mode in ("video", "audio"):
            return list(AUDIO_CODECS)
        return []

    def _ytdlp_supports_audio_container(self, container: str) -> bool:
        """Return whether yt-dlp should own conversion for this output format.

        In this UI, yt-dlp conversion is intentionally limited to MP3. Other
        audio formats are handled by the normal FFmpeg codec/container path so
        the user can choose the appropriate codec.
        """
        return container.lower().lstrip(".") == "mp3"

    def _update_audio_codec_state(self) -> None:
        """Disable codec selection only when yt-dlp owns the selected format."""
        if self.mode_var.get() != "audio":
            return

        use_ytdlp = self.settings.get("use_ytdlp_audio_conversion", True)
        container = self.container_var.get().lower().lstrip(".")
        ytdlp_handles_format = use_ytdlp and self._ytdlp_supports_audio_container(container)
        self.audio_codec_dd.configure(
            state="disabled" if ytdlp_handles_format else "normal"
        )

    def on_mode_change(self) -> None:
        mode = self.mode_var.get()

        if mode == "video":
            containers = list(VIDEO_CONTAINERS)
            self._set_option_menu(
                self.container_dd,
                self.container_var,
                containers,
                self.on_container_change,
            )
            if self.container_var.get() not in containers:
                self.container_var.set("mp4")
            self.video_codec_dd.configure(state="normal")
            self.audio_codec_dd.configure(state="normal")
        elif mode == "audio":
            containers = list(AUDIO_CONTAINERS)
            self._set_option_menu(
                self.container_dd,
                self.container_var,
                containers,
                self.on_container_change,
            )
            if self.container_var.get() not in containers:
                self.container_var.set("mp3")
            self.video_codec_dd.configure(state="disabled")
            self.audio_codec_dd.configure(state="normal")
        else:
            containers = list(THUMBNAIL_CONTAINERS)
            self._set_option_menu(
                self.container_dd,
                self.container_var,
                containers,
                self.on_container_change,
            )
            if self.container_var.get() not in containers:
                self.container_var.set("jpg")
            self.video_codec_dd.configure(state="disabled")
            self.audio_codec_dd.configure(state="disabled")

        self.on_container_change()

    def on_container_change(self) -> None:
        mode = self.mode_var.get()
        if mode == "video":
            self._set_option_menu(
                self.video_codec_dd,
                self.video_codec_var,
                list(VIDEO_CODECS),
            )
            if self.video_codec_var.get() not in VIDEO_CODECS:
                self.video_codec_var.set("copy")

        audio_values = self._allowed_audio_codecs(mode, self.container_var.get().lower())
        if audio_values:
            self._set_option_menu(
                self.audio_codec_dd,
                self.audio_codec_var,
                audio_values,
            )
            if self.audio_codec_var.get() not in audio_values:
                self.audio_codec_var.set("copy")
        else:
            self.audio_codec_var.set("N/A")

        self._update_audio_codec_state()

    def _validate_selection(self) -> Optional[str]:
        mode = self.mode_var.get()
        container = self.container_var.get().lower()
        video = self.video_codec_var.get()
        audio = self.audio_codec_var.get()

        if mode == "video":
            if container not in VIDEO_CONTAINERS:
                return f"Unsupported video container: {container}."
            if video not in VIDEO_CODECS:
                return f"Unsupported video codec: {video}."
            if audio not in AUDIO_CODECS:
                return f"Unsupported audio codec: {audio}."

            video_ok = {
                "mp4": {"copy", "h264", "h265", "vp9", "av1"},
                "mkv": set(VIDEO_CODECS),
                "webm": {"copy", "vp9", "av1"},
                "mov": {"copy", "h264", "h265", "av1", "prores_422", "dnxhr_sq", "dnxhr_hq"},
            }
            audio_ok = {
                "mp4": {"copy", "aac", "flac", "lpcm", "mpeg-1", "mpeg-2"},
                "mkv": set(AUDIO_CODECS),
                "webm": {"copy", "opus"},
                "mov": {"copy", "aac", "flac", "lpcm", "mpeg-1", "mpeg-2"},
            }
            if video not in video_ok[container]:
                return (
                    f"Video codec '{video}' is not compatible with {container}. "
                    "Choose MKV or a compatible video codec."
                )
            if audio not in audio_ok[container]:
                return (
                    f"Audio codec '{audio}' is not compatible with {container}. "
                    "Choose MKV or a compatible audio codec."
                )

            backend = self.settings["encoder_backend"]
            if (
                video != "copy"
                and backend != "CPU"
                and not gpu_encoder_candidates(video, backend)
            ):
                supported = ", ".join(hardware_supported_codecs(backend))
                return (
                    f"Video codec '{video}' is not supported by the forced {backend} "
                    f"encoder backend. Supported hardware codecs: {supported}. "
                    "Choose CPU in Settings to use the software encoder."
                )

        elif mode == "audio":
            if container not in AUDIO_CONTAINERS:
                return f"Unsupported audio container: {container}."

            # yt-dlp only owns conversion for formats it supports in this UI.
            # For other containers, fall through to the normal FFmpeg codec
            # validation so the user can select a compatible codec.
            if (
                self.settings.get("use_ytdlp_audio_conversion", True)
                and self._ytdlp_supports_audio_container(container)
            ):
                return None

            if audio not in AUDIO_CODECS:
                return f"Unsupported audio codec: {audio}."

            audio_ok = {
                "mp3": {"copy", "mpeg-1"},
                "m4a": {"copy", "aac"},
                "wav": {"copy", "lpcm"},
                "flac": {"copy", "flac"},
                "opus": {"copy", "opus"},
            }
            if audio not in audio_ok[container]:
                recommended = {
                    "mp3": "mpeg-1",
                    "m4a": "aac",
                    "wav": "lpcm",
                    "flac": "flac",
                    "opus": "opus",
                }[container]
                return (
                    f"Audio codec '{audio}' is not compatible with {container}. "
                    f"Use '{recommended}' or 'copy'."
                )

        elif mode == "thumbnail":
            if container not in THUMBNAIL_CONTAINERS:
                return f"Unsupported thumbnail format: {container}."

        return None

    def paste_clipboard(self) -> None:
        try:
            self.url_var.set(self.root.clipboard_get().strip())
        except Exception:
            self.log("Clipboard is empty or unavailable.")

    def browse_outdir(self) -> None:
        directory = filedialog.askdirectory(initialdir=self.outdir_var.get())
        if directory:
            self.outdir_var.set(directory)

    def open_outdir(self) -> None:
        directory = self.outdir_var.get()
        if not os.path.isdir(directory):
            messagebox.showerror("Error", "Output folder does not exist.")
            return

        if sys.platform == "win32":
            os.startfile(directory)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", directory])
        else:
            subprocess.Popen(["xdg-open", directory])

    def start(self) -> None:
        if self.startup_prepare_in_progress:
            messagebox.showinfo(
                "Preparing tools",
                "FFmpeg / yt-dlp startup preparation is still running.",
            )
            return

        if not self.startup_tools_ready:
            # Re-check in case the user fixed the local toolchain after startup.
            self.startup_tools_ready = bool(
                find_ffmpeg_executable("ffmpeg")
                and find_ffmpeg_executable("ffprobe")
                and get_ytdlp_command()
            )
            if not self.startup_tools_ready:
                messagebox.showerror(
                    "Tools not ready",
                    "FFmpeg or yt-dlp is not ready. Check the startup log, fix the reported toolchain issue, and restart the app.",
                )
                return

        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Already running", "A download is already running.")
            return

        raw_urls = self.url_var.get().strip()
        urls = [item.strip() for item in raw_urls.split(";") if item.strip()]
        if not urls:
            messagebox.showwarning("No URL", "Enter at least one URL.")
            return

        unusual_urls = [
            url
            for url in urls
            if not (
                url.startswith("http://")
                or url.startswith("https://")
                or url.startswith("ytsearch:")
            )
        ]
        if unusual_urls:
            preview = "\n".join(unusual_urls[:5])
            if len(unusual_urls) > 5:
                preview += f"\n...and {len(unusual_urls) - 5} more"
            proceed = messagebox.askyesno(
                "Validate URLs",
                "Some entries do not look like HTTP(S) URLs:\n\n"
                f"{preview}\n\nProceed anyway?",
            )
            if not proceed:
                return

        outdir = self.outdir_var.get().strip() or os.path.expanduser("~/Downloads")
        try:
            os.makedirs(outdir, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("Output folder error", str(exc))
            return

        if not (find_ffmpeg_executable("ffmpeg") and find_ffmpeg_executable("ffprobe")):
            messagebox.showerror(
                "ffmpeg missing",
                "The compiled FFmpeg tools are missing. Restart the app to run first-run compilation and check the build log.",
            )
            return

        if not get_ytdlp_command():
            messagebox.showerror(
                "yt-dlp missing",
                "The cloned binaries/yt-dlp checkout could not be started. Check the startup log.",
            )
            return

        mode = self.mode_var.get()
        validation_error = self._validate_selection()
        if validation_error:
            messagebox.showerror("Incompatible codec/container", validation_error)
            return

        options = {
            "mode": mode,
            "container": self.container_var.get(),
            "video_codec": self.video_codec_var.get() if mode == "video" else None,
            "audio_codec": self.audio_codec_var.get() if mode in ("video", "audio") else None,
            "outdir": outdir,
            "encoder_backend": self.settings["encoder_backend"],
            "download_playlist": self.settings["download_playlist"],
            "use_ytdlp_audio_conversion": self.settings["use_ytdlp_audio_conversion"],
        }

        self.status_var.set("Running")
        self.start_btn.configure(state="disabled")
        self.worker = Worker(urls, options, self.q)
        self.worker.start()
        self.log(f"Worker started with {len(urls)} link(s).")

    def _process_queue(self) -> None:
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]

                if kind == "log":
                    self.log(item[1])
                elif kind == "error":
                    self.log(f"ERROR: {item[1]}")
                elif kind == "done":
                    folder = item[1]
                    self.log(f"Finished. Output folder: {folder}")
                    self.status_var.set("Idle")
                    self.start_btn.configure(state="normal")
                elif kind == "startup_tools_done":
                    ffmpeg_ready, ytdlp_ready = bool(item[1]), bool(item[2])
                    self.startup_prepare_in_progress = False
                    self.startup_tools_ready = ffmpeg_ready and ytdlp_ready
                    if self.ffmpeg_update_button is not None and not self.ffmpeg_update_in_progress:
                        self.ffmpeg_update_button.configure(state="normal")
                    if self.startup_tools_ready:
                        self.start_btn.configure(state="normal")
                        self.status_var.set("Idle")
                        self.log("Startup tool preparation finished. FFmpeg and yt-dlp are ready.")
                    else:
                        self.start_btn.configure(state="disabled")
                        self.status_var.set("Tool setup failed")
                        if not ffmpeg_ready:
                            self.log("ERROR: FFmpeg first-run setup did not complete successfully.")
                        if not ytdlp_ready:
                            self.log("ERROR: yt-dlp source setup did not complete successfully.")
                elif kind == "ffmpeg_update_done":
                    success, changed, rebuilt, summary = bool(item[1]), bool(item[2]), bool(item[3]), str(item[4])
                    self.ffmpeg_update_in_progress = False
                    if self.ffmpeg_update_button is not None:
                        self.ffmpeg_update_button.configure(state="normal")
                    if self.ffmpeg_update_status_var is not None:
                        if success and changed and rebuilt:
                            self.ffmpeg_update_status_var.set("Updated and rebuilt successfully")
                        elif success and not changed:
                            self.ffmpeg_update_status_var.set("Already up to date")
                        else:
                            self.ffmpeg_update_status_var.set("Update failed - see log")

                    worker_running = self.worker is not None and self.worker.is_alive()
                    if self.startup_tools_ready and not worker_running:
                        self.start_btn.configure(state="normal")
                        self.status_var.set("Idle")
                    elif not worker_running:
                        self.status_var.set("Tool setup failed")

                    parent = self.settings_window if self.settings_window is not None else self.root
                    if success:
                        messagebox.showinfo("FFmpeg update", summary, parent=parent)
                    else:
                        messagebox.showerror("FFmpeg update", summary, parent=parent)
                else:
                    self.log(f"Unknown queue message: {item}")
        except queue.Empty:
            pass

        self.root.after(150, self._process_queue)


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()