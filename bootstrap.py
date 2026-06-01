"""Bootstrap system for ProxyNet.

Handles on-first-launch environment setup:
- Python version check (>= 3.10)
- Auto-install missing pip packages (textual, aiohttp)
- Auto-download sing-box binary to local bin/ directory
- Platform detection (Windows / Linux / macOS)

All dependencies are kept local — no system PATH modification needed.
"""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

# ── Constants ────────────────────────────────────────────────────

MIN_PYTHON = (3, 10)
REQUIRED_PACKAGES = {
    "flask": "flask>=3.0",
    "textual": "textual>=0.52.0",
    "aiohttp": "aiohttp>=3.9.0",
}

# sing-box — fetch latest release dynamically
SING_BOX_REPO = "SagerNet/sing-box"
# Fallback version if API unreachable
SING_BOX_FALLBACK_VERSION = "1.13.12"

# Platform → URL pattern
_SING_BOX_URL_PATTERNS = {
    "Windows": "https://github.com/{repo}/releases/download/v{version}/sing-box-{version}-windows-amd64.zip",
    "Linux": "https://github.com/{repo}/releases/download/v{version}/sing-box-{version}-linux-amd64.tar.gz",
    "Darwin": "https://github.com/{repo}/releases/download/v{version}/sing-box-{version}-darwin-amd64.tar.gz",
}


# ── Helpers ──────────────────────────────────────────────────────

def _get_project_root() -> Path:
    """Get the project root directory."""
    return Path(os.path.dirname(os.path.abspath(__file__)))


def _get_bin_dir() -> Path:
    """Get the local bin directory for bundled binaries."""
    root = _get_project_root()
    bin_dir = root / "bin"
    bin_dir.mkdir(exist_ok=True)
    return bin_dir


def _get_singbox_path() -> Path:
    """Get the expected sing-box binary path."""
    bin_dir = _get_bin_dir()
    if sys.platform == "win32":
        return bin_dir / "sing-box.exe"
    return bin_dir / "sing-box"


def _get_python() -> str:
    """Get the Python executable path."""
    return sys.executable


# ── Version check ────────────────────────────────────────────────

def check_python_version() -> tuple[bool, str]:
    """Check if the Python version meets the minimum requirement.

    Returns (ok, message).
    """
    current = sys.version_info[:2]
    if current >= MIN_PYTHON:
        return True, f"Python {'.'.join(map(str, current))} [OK]"
    return False, (
        f"Python {'.'.join(map(str, current))} is too old. "
        f"Please install Python {'.'.join(map(str, MIN_PYTHON))}+"
    )


# ── Package auto-install ─────────────────────────────────────────

def _is_package_installed(package_name: str) -> bool:
    """Check if a Python package is installed."""
    try:
        __import__(package_name)
        return True
    except ImportError:
        return False


def _pip_install(package_spec: str) -> tuple[bool, str]:
    """Install a package via pip. Returns (ok, message)."""
    python = _get_python()
    try:
        result = subprocess.run(
            [python, "-m", "pip", "install", package_spec],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            return True, f"Installed {package_spec} [OK]"
        else:
            last_line = result.stderr.strip().split("\n")[-1] if result.stderr else "unknown error"
            return False, f"Failed to install {package_spec}: {last_line}"
    except subprocess.TimeoutExpired:
        return False, f"Timeout installing {package_spec}"
    except Exception as e:
        return False, f"Error installing {package_spec}: {e}"


def ensure_packages(verbose: bool = True) -> bool:
    """Check and install all required packages.

    Returns True if all packages are ready.
    """
    all_ok = True
    for pkg_name, pkg_spec in REQUIRED_PACKAGES.items():
        if _is_package_installed(pkg_name):
            if verbose:
                print(f"  [OK] {pkg_name}")
            continue

        print(f"  [INSTALLING] {pkg_spec} ...")
        ok, msg = _pip_install(pkg_spec)
        if verbose:
            print(f"  {'[OK]' if ok else '[FAIL]'} {msg}")
        if not ok:
            all_ok = False

    return all_ok


# ── sing-box bootstrap ───────────────────────────────────────────

def _get_singbox_download_url() -> Optional[str]:
    """Get the sing-box download URL for the current platform.

    Tries to fetch the latest version from GitHub API first,
    falls back to a known version if unreachable.
    """
    system = platform.system()
    pattern = _SING_BOX_URL_PATTERNS.get(system)
    if not pattern:
        return None

    version = _fetch_latest_version() or SING_BOX_FALLBACK_VERSION
    return pattern.format(repo=SING_BOX_REPO, version=version)


def _fetch_latest_version() -> Optional[str]:
    """Fetch latest sing-box release tag from GitHub API.

    Returns version string like '1.13.12', or None on failure.
    """
    api_url = f"https://api.github.com/repos/{SING_BOX_REPO}/releases/latest"
    try:
        import urllib.request
        import json

        req = urllib.request.Request(api_url)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "ProxyNet-Bootstrap/1.0")

        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            tag = data.get("tag_name", "")
            # Strip 'v' prefix if present
            if tag.startswith("v"):
                return tag[1:]
            return tag
    except Exception:
        return None


async def _download_file(url: str, dest: Path) -> bool:
    """Download a file with progress reporting."""
    try:
        import aiohttp
    except ImportError:
        # Fallback to urllib if aiohttp not yet installed
        import urllib.request

        def _sync_download():
            try:
                urllib.request.urlretrieve(url, str(dest))
                return True
            except Exception:
                return False

        return _sync_download()

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    print(f"  HTTP {resp.status} downloading {url}")
                    return False
                total = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                with open(dest, "wb") as f:
                    async for chunk in resp.content.iter_chunked(65536):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            pct = downloaded * 100 // total
                            print(f"\r  Downloading sing-box: {pct}% ({downloaded}/{total})", end="", flush=True)
                if total:
                    print()
                return True
    except Exception as e:
        print(f"\n  Download error: {e}")
        return False


def _extract_archive(archive_path: Path, dest_dir: Path) -> Optional[Path]:
    """Extract sing-box from archive. Returns path to the binary."""
    if archive_path.suffix == ".zip":
        with zipfile.ZipFile(archive_path, "r") as zf:
            # Find the sing-box binary in the archive
            for name in zf.namelist():
                if name.endswith("sing-box.exe") or (
                    name.rstrip("/").endswith("sing-box")
                    and not name.endswith("/")
                ):
                    # Extract just the binary
                    basename = os.path.basename(name)
                    target = dest_dir / basename
                    with zf.open(name) as src:
                        with open(target, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                    # Make executable on Unix
                    if sys.platform != "win32":
                        os.chmod(target, 0o755)
                    return target
        return None

    elif ".tar" in archive_path.suffix or archive_path.suffix == ".gz":
        import tarfile
        with tarfile.open(archive_path) as tf:
            for member in tf.getmembers():
                name = member.name
                if name.endswith("sing-box.exe") or (
                    name.rstrip("/").endswith("sing-box")
                    and not name.endswith("/")
                ):
                    basename = os.path.basename(name)
                    target = dest_dir / basename
                    member.name = basename  # Extract flat
                    tf.extract(member, dest_dir)
                    if sys.platform != "win32":
                        os.chmod(target, 0o755)
                    return target
        return None

    return None


async def ensure_singbox(verbose: bool = True) -> Optional[str]:
    """Ensure sing-box binary is available locally.

    Checks in order:
    1. Already in local bin/ directory
    2. Available on system PATH
    3. Download and extract to local bin/

    Returns path to sing-box binary, or None if unavailable.
    """
    bin_dir = _get_bin_dir()
    local_path = _get_singbox_path()

    # Check local bin first
    if local_path.exists():
        if verbose:
            print(f"  [OK] sing-box (bundled): {local_path}")
        return str(local_path)

    # Check system PATH
    system_path = shutil.which("sing-box")
    if system_path:
        if verbose:
            print(f"  [OK] sing-box (system): {system_path}")
        return system_path

    # Not found — offer to download
    if verbose:
        print(f"  [MISSING] sing-box not found")

    download_url = _get_singbox_download_url()
    if not download_url:
        if verbose:
            print(f"  [SKIP] No sing-box download for {platform.system()}")
        return None

    version = _fetch_latest_version() or SING_BOX_FALLBACK_VERSION
    print(f"  Downloading sing-box v{version} for {platform.system()}...")
    print(f"  URL: {download_url}")

    # Determine archive extension
    archive_ext = ".zip" if sys.platform == "win32" else ".tar.gz"
    archive_path = bin_dir / f"sing-box{archive_ext}"

    try:
        # Download
        ok = await _download_file(download_url, archive_path)
        if not ok:
            print("  [FAIL] Could not download sing-box.")
            print("  Please download manually from:")
            print(f"    https://github.com/SagerNet/sing-box/releases")
            print(f"  And place sing-box in: {bin_dir}")
            return None

        # Extract
        target = _extract_archive(archive_path, bin_dir)
        if not target:
            print("  [FAIL] Could not extract sing-box binary from archive.")
            return None

        # Cleanup archive
        try:
            os.remove(archive_path)
        except OSError:
            pass

        if verbose:
            print(f"  [OK] sing-box installed to: {target}")

        return str(target)

    except Exception as e:
        print(f"  [FAIL] Error setting up sing-box: {e}")
        return None


# ── Full bootstrap ───────────────────────────────────────────────

async def bootstrap(verbose: bool = True) -> dict:
    """Run the full bootstrap process.

    Returns a dict with bootstrap results:
    {
        "python_ok": bool,
        "packages_ok": bool,
        "singbox_path": str | None,
        "ready": bool,  # TUI can start
    }
    """
    if verbose:
        print("=" * 55)
        print("  ProxyNet Bootstrap")
        print("=" * 55)

    # 1. Check Python
    python_ok, python_msg = check_python_version()
    if verbose:
        print(f"  {python_msg}")
    if not python_ok:
        print(f"\n  Error: {python_msg}")
        return {"python_ok": False, "packages_ok": False, "singbox_path": None, "ready": False}

    # 2. Install packages
    packages_ok = ensure_packages(verbose=verbose)
    if not packages_ok and verbose:
        print("  [WARN] Some packages could not be installed.")
        print("  You can install manually: pip install textual aiohttp")

    # 3. Setup sing-box
    singbox_path = await ensure_singbox(verbose=verbose)

    if verbose:
        print()
        if packages_ok:
            print("  Bootstrap complete — starting ProxyNet TUI...")
        else:
            print("  Bootstrap partial — packages missing, TUI may not work.")

    return {
        "python_ok": python_ok,
        "packages_ok": packages_ok,
        "singbox_path": singbox_path,
        "ready": packages_ok,  # TUI needs packages, sing-box is optional
    }
