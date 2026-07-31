"""Download and extract Chromium system deps without root — PURE PYTHON.

Parses .deb files (ar format) using only Python stdlib. No ar, dpkg, or apt needed.
Extracts .so files to /home/app/browser-libs/.

Usage:
    import install_deps
    install_deps.ensure_browser_libs()
"""

import io
import os
import sys
import tarfile
import urllib.request
from pathlib import Path

LIB_DIR = Path("/home/app/browser-libs")

BASE_URL = "http://archive.ubuntu.com/ubuntu/pool"

# Ubuntu 22.04 (jammy) amd64 — the libs Chromium headless shell needs
PACKAGES = [
    ("libglib2.0-0", "main/g/glib2.0/libglib2.0-0_2.72.4-0ubuntu2.3_amd64.deb"),
    ("libdbus-1-3", "main/d/dbus/libdbus-1-3_1.12.20-2ubuntu4.1_amd64.deb"),
    ("libasound2", "main/a/alsa-lib/libasound2_1.2.6.1-1ubuntu1_amd64.deb"),
    ("libx11-6", "main/libx/libx11/libx11-6_1.7.5-1ubuntu0.3_amd64.deb"),
    ("libx11-xcb1", "main/libx/libx11/libx11-xcb1_1.7.5-1ubuntu0.3_amd64.deb"),
    ("libxcb1", "main/libx/libxcb/libxcb1_1.14-3ubuntu3_amd64.deb"),
    ("libxcb-shm0", "main/libx/libxcb/libxcb-shm0_1.14-3ubuntu3_amd64.deb"),
    ("libxcb-render0", "main/libx/libxcb/libxcb-render0_1.14-3ubuntu3_amd64.deb"),
    ("libxrandr2", "main/libx/libxrandr/libxrandr2_1.5.2-1build1_amd64.deb"),
    ("libxcomposite1", "main/libx/libxcomposite/libxcomposite1_0.4.5-1build2_amd64.deb"),
    ("libxcursor1", "main/libx/libxcursor/libxcursor1_1.2.0-2build4_amd64.deb"),
    ("libxdamage1", "main/libx/libxdamage/libxdamage1_1.1.5-2build2_amd64.deb"),
    ("libxi6", "main/libx/libxi/libxi6_1.8-1build1_amd64.deb"),
    ("libxfixes3", "main/libx/libxfixes/libxfixes3_6.0.0-1_amd64.deb"),
    ("libxrender1", "main/libx/libxrender/libxrender1_0.9.10-1build4_amd64.deb"),
    ("libxext6", "main/libx/libxext/libxext6_1.3.4-1build1_amd64.deb"),
    ("libxau6", "main/libx/libxau/libxau6_1.0.9-1build5_amd64.deb"),
    ("libxdmcp6", "main/libx/libxdmcp/libxdmcp6_1.1.3-0ubuntu5_amd64.deb"),
    ("libatk1.0-0", "main/a/atk1.0/libatk1.0-0_2.36.0-3build1_amd64.deb"),
    ("libatk-bridge2.0-0", "main/a/at-spi2-core/libatk-bridge2.0-0_2.44.1-2_amd64.deb"),
    ("libatspi2.0-0", "main/a/at-spi2-core/libatspi2.0-0_2.44.1-2_amd64.deb"),
    ("libcairo2", "main/c/cairo/libcairo2_1.16.0-5ubuntu2_amd64.deb"),
    ("libcairo-gobject2", "main/c/cairo/libcairo-gobject2_1.16.0-5ubuntu2_amd64.deb"),
    ("libpango-1.0-0", "main/p/pango1.0/libpango-1.0-0_1.50.6+ds-2ubuntu1_amd64.deb"),
    ("libpangocairo-1.0-0", "main/p/pango1.0/libpangocairo-1.0-0_1.50.6+ds-2ubuntu1_amd64.deb"),
    ("libpangoft2-1.0-0", "main/p/pango1.0/libpangoft2-1.0-0_1.50.6+ds-2ubuntu1_amd64.deb"),
    ("libgdk-pixbuf-2.0-0", "main/g/gdk-pixbuf/libgdk-pixbuf-2.0-0_2.42.8+dfsg-1ubuntu0.3_amd64.deb"),
    ("libgtk-3-0", "main/g/gtk+3.0/libgtk-3-0_3.24.33-1ubuntu2.2_amd64.deb"),
    ("libepoxy0", "main/libe/libepoxy/libepoxy0_1.5.10-1_amd64.deb"),
    ("libpixman-1-0", "main/p/pixman/libpixman-1-0_0.40.0-1ubuntu0.22.04.1_amd64.deb"),
    ("libfontconfig1", "main/f/fontconfig/libfontconfig1_2.13.1-4.2ubuntu5_amd64.deb"),
    ("libfreetype6", "main/f/freetype/libfreetype6_2.11.1+dfsg-1ubuntu0.2_amd64.deb"),
    ("libharfbuzz0b", "main/h/harfbuzz/libharfbuzz0b_2.7.4-1ubuntu3.1_amd64.deb"),
    ("libfribidi0", "main/f/fribidi/libfribidi0_1.0.8-2ubuntu3.1_amd64.deb"),
    ("libthai0", "main/libt/libthai/libthai0_0.1.29-1build1_amd64.deb"),
    ("libffi8", "main/libf/libffi/libffi8ubuntu1_3.4.2-4_amd64.deb"),
    ("libpcre3", "main/p/pcre3/libpcre3_8.45-1build3_amd64.deb"),
    ("libnss3", "main/n/nss/libnss3_3.68.2-0ubuntu1.2_amd64.deb"),
    ("libnspr4", "main/n/nspr/libnspr4_4.32-3build1_amd64.deb"),
    ("libdrm2", "main/libd/libdrm/libdrm2_2.4.113-2~ubuntu0.22.04.1_amd64.deb"),
    ("libgbm1", "main/m/mesa/libgbm1_23.2.1-1ubuntu3.1~22.04.2_amd64.deb"),
    ("libxkbcommon0", "main/libx/libxkbcommon/libxkbcommon0_1.4.0-1_amd64.deb"),
]


def _extract_ar_member(data: bytes, member_prefix: str) -> bytes | None:
    """Parse .deb (ar archive) in pure Python. Extract member matching prefix."""
    if not data.startswith(b"!<arch>\n"):
        return None
    pos = 8  # skip magic
    while pos + 60 <= len(data):
        header = data[pos:pos + 60]
        name = header[0:16].decode("ascii", errors="replace").strip().rstrip("/")
        try:
            size = int(header[48:58].decode("ascii").strip())
        except ValueError:
            break
        pos += 60
        if name.startswith(member_prefix):
            return data[pos:pos + size]
        pos += size
        if pos % 2 == 1:
            pos += 1
    return None


def _download_and_extract(name: str, url_path: str) -> bool:
    """Download .deb and extract .so files using pure Python."""
    url = f"{BASE_URL}/{url_path}"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            deb_data = resp.read()
    except Exception as e:
        print(f"  SKIP {name}: {e}")
        return False

    try:
        data_tar = _extract_ar_member(deb_data, "data.tar")
        if not data_tar:
            print(f"  SKIP {name}: no data.tar in .deb")
            return False

        with tarfile.open(fileobj=io.BytesIO(data_tar)) as tf:
            for member in tf.getmembers():
                if ".so" in member.name and (member.isfile() or member.issym()):
                    basename = os.path.basename(member.name)
                    if member.issym():
                        # Recreate symlink
                        link_path = LIB_DIR / basename
                        if not link_path.exists():
                            link_path.symlink_to(member.linkname if "/" not in member.linkname else os.path.basename(member.linkname))
                    else:
                        member.name = basename
                        tf.extract(member, str(LIB_DIR))
        return True
    except Exception as e:
        print(f"  SKIP {name}: {e}")
        return False


def _create_symlinks():
    """Create missing .so symlinks for versioned files."""
    for f in LIB_DIR.iterdir():
        if not f.is_file() or ".so." not in f.name:
            continue
        parts = f.name.split(".so.")
        # Create .so.N symlink (e.g. libglib-2.0.so.0 -> libglib-2.0.so.0.7200.4)
        version_parts = parts[1].split(".")
        if len(version_parts) > 1:
            short = f"{parts[0]}.so.{version_parts[0]}"
            link = LIB_DIR / short
            if not link.exists():
                link.symlink_to(f.name)


def ensure_browser_libs() -> dict:
    """Main entry point. Returns status dict."""
    LIB_DIR.mkdir(parents=True, exist_ok=True)

    # Check if already done
    existing = [f.name for f in LIB_DIR.iterdir() if ".so" in f.name] if LIB_DIR.exists() else []
    if any("libglib-2.0.so" in f for f in existing):
        return {"status": "already_installed", "lib_count": len(existing), "dir": str(LIB_DIR)}

    print(f"Downloading browser libs to {LIB_DIR}...")
    success = 0
    failed = 0
    for name, url_path in PACKAGES:
        if _download_and_extract(name, url_path):
            success += 1
        else:
            failed += 1

    _create_symlinks()
    final_count = len([f for f in LIB_DIR.iterdir() if ".so" in f.name]) if LIB_DIR.exists() else 0
    print(f"Done: {success}/{len(PACKAGES)} packages, {final_count} .so files")
    return {"status": "installed", "packages_ok": success, "packages_failed": failed, "lib_count": final_count, "dir": str(LIB_DIR)}


if __name__ == "__main__":
    result = ensure_browser_libs()
    print(result)
    sys.exit(0 if result.get("packages_failed", 0) < len(PACKAGES) else 1)
