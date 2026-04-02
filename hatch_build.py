"""Hatch build hook: build the frontend assets before packaging.

When building a wheel (``pip install .``), this hook runs ``pnpm install``
and ``pnpm run build`` in the ``launch_plus_viz/`` directory, producing
the compiled SPA in ``launch_plus/visualizer/static/``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict) -> None:
        vis_dir = Path(self.root) / "launch_plus_viz"
        if not (vis_dir / "package.json").exists():
            return

        self.app.display_info("Building frontend assets...")

        # Install dependencies
        subprocess.check_call(
            ["pnpm", "install", "--frozen-lockfile"],
            cwd=str(vis_dir),
        )

        # Build (output goes to ../launch_plus/visualizer/static/)
        subprocess.check_call(
            ["pnpm", "run", "build"],
            cwd=str(vis_dir),
        )

        self.app.display_info("Frontend build complete.")
