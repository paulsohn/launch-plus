"""Virtual filesystem mount for preview mode.

In preview mode, source workspace directories are virtually mounted at
their predicted install paths using pyfakefs.  This makes the filesystem
look like a post-build environment, so all resolution uses real paths —
no portable path syntax needed.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from pathlib import Path

from pyfakefs.fake_filesystem_unittest import Patcher

from launch_plus.types import Lockfile

logger = logging.getLogger("launch_plus")


@contextmanager
def virtual_install_mount(
    lockfile: Lockfile,
    src_dir: Path,
    install_base: Path,
    *,
    merge_install: bool = False,
):
    """Context manager that mounts source packages at predicted install paths.

    Parameters
    ----------
    lockfile : Lockfile
        Package→repo mapping.
    src_dir : Path
        Absolute path to the source directory (contains cloned repos).
    install_base : Path
        Absolute path to the install directory.
    merge_install : bool
        If True, use merged layout (``install/share/<pkg>/``).
        If False (default), use isolated layout (``install/<pkg>/share/<pkg>/``).
    """
    src_dir = src_dir.resolve()
    install_base = install_base.resolve()

    # Capture real directory listing BEFORE pyfakefs patches os
    real_dirs = ["/" + d for d in os.listdir("/") if os.path.isdir("/" + d)]

    with Patcher() as patcher:
        fs = patcher.fs
        assert fs is not None

        # Mount the real filesystem (skip inaccessible dirs via exception)
        for d in real_dirs:
            try:
                fs.add_real_directory(d, lazy_read=True, read_only=False)
            except OSError:
                pass

        # Mount each lockfile package at its predicted install path
        for pkg_name, pkg_lock in lockfile.packages.items():
            pkg_src = src_dir / pkg_lock.repo / pkg_lock.path
            if not pkg_src.is_dir():
                logger.warning(
                    "source directory for package '%s' not found: %s",
                    pkg_name,
                    pkg_src,
                )
                continue

            if merge_install:
                target = install_base / "share" / pkg_name
            else:
                target = install_base / pkg_name / "share" / pkg_name

            fs.add_real_directory(
                str(pkg_src),
                target_path=str(target),
                read_only=True,
            )
            logger.debug("mounted %s → %s", pkg_src, target)

        yield
