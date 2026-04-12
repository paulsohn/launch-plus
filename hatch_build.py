"""Hatch build hook: build the frontend assets before packaging.

When building a wheel (``pip install .``), this hook runs ``pnpm install``
and ``pnpm run build`` in the ``roscope_viz/`` directory, producing
the compiled SPA in ``roscope/visualizer/static/``.

``pnpm`` must be available on PATH. If it is not found the build fails with
a clear error — a missing frontend would produce a broken install.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict) -> None:
        vis_dir = Path(self.root) / "roscope_viz"
        if not (vis_dir / "package.json").exists():
            return

        # Skip rebuild if assets are already present (e.g. second target in
        # `python -m build` which builds both sdist and wheel).
        static_index = Path(self.root) / "roscope" / "visualizer" / "static" / "index.html"
        if static_index.is_file():
            self.app.display_info("Frontend assets already built, skipping.")
            return

        if shutil.which("pnpm") is None:
            raise RuntimeError(
                "pnpm not found — cannot build frontend assets. "
                "Install pnpm (https://pnpm.io/installation) and try again."
            )

        self.app.display_info("Building frontend assets...")

        # Install dependencies
        subprocess.check_call(
            ["pnpm", "install", "--frozen-lockfile"],
            cwd=str(vis_dir),
        )

        # Build (output goes to ../roscope/visualizer/static/)
        subprocess.check_call(
            ["pnpm", "run", "build"],
            cwd=str(vis_dir),
        )

        self.app.display_info("Frontend build complete.")
