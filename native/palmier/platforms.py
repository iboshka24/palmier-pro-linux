import os
import platform
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path


def desktop_name():
    return "Palmier Pro Linux" if os.name != "nt" else "Palmier Pro Windows"


def _font_families():
    if os.name == "nt":
        return set()
    try:
        out = subprocess.run(["fc-list", ":family"], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return set()
    families = set()
    for line in out.stdout.splitlines():
        for part in line.split(","):
            name = part.strip().lower()
            if name:
                families.add(name)
    return families


@lru_cache(maxsize=1)
def ui_font():
    if os.name == "nt":
        return "Segoe UI"
    available = _font_families()
    for family in ("Inter", "DejaVu Sans", "Liberation Sans", "Noto Sans", "Ubuntu", "Cantarell"):
        if family.lower() in available:
            return family
    return "DejaVu Sans"


def session_type():
    return os.environ.get("XDG_SESSION_TYPE", "unknown")


def hw_hint():
    ffmpeg = shutil.which("ffmpeg")
    encoders = []
    if ffmpeg:
        try:
            from .media import hw_encoders
            encoders = list(hw_encoders())
        except Exception:
            encoders = []
    return dict(session=session_type(), ffmpeg=bool(ffmpeg),
                vaapi=Path("/dev/dri").exists(), encoders=encoders)


def text_font(system=None):
    override = os.environ.get("PALMIER_FONT_PATH")
    if override:
        result = Path(override)
        if not result.is_absolute() or not result.is_file():
            raise ValueError("PALMIER_FONT_PATH must identify an existing absolute font path")
        return result
    system = system or platform.system()
    if system == "Windows":
        candidates = [Path(os.environ.get("WINDIR", "C:/Windows"))/"Fonts"/"arial.ttf"]
    else:
        candidates = [Path(name) for name in (
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/TTF/DejaVuSans.ttf",
        )]
    result = next((file for file in candidates if file.is_file()), None)
    if result is None:
        raise ValueError("No text font found. Install fonts-liberation/fonts-dejavu-core or set PALMIER_FONT_PATH")
    return result
