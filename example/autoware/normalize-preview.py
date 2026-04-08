#!/usr/bin/env python3
"""Normalize roscope preview output: replace source paths with colcon install paths.

Given a roscope lockfile, builds the path mapping:
  {workspace}/src/{repo}/{pkg_path}/ -> {workspace}/install/{pkg}/share/{pkg}/  (isolated)
  {workspace}/src/{repo}/{pkg_path}/ -> {workspace}/install/share/{pkg}/        (merge)

Reads from stdin (or a file), writes to stdout.

Usage:
  python3 normalize-preview.py [options] [input_file]

  -w, --workspace DIR    Workspace root (contains src/ and install/).
                         Defaults to the directory containing the lockfile.
  -l, --lockfile FILE    Path to manifest.lock.repos (default: manifest.lock.repos
                         in the current directory).
  --layout isolated|merge
                         Colcon install layout (default: isolated).
                         isolated: install/<pkg>/share/<pkg>/
                         merge:    install/share/<pkg>/
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def load_mappings(lockfile_path: Path, workspace: Path, layout: str) -> list[tuple[str, str]]:
    """Return sorted list of (src_prefix, install_prefix) pairs.

    Sorted longest-first so that more specific paths are replaced before
    shorter common prefixes.
    """
    try:
        import yaml
    except ImportError:
        sys.exit("ERROR: PyYAML is required. Install with: pip install pyyaml")

    with open(lockfile_path) as f:
        data = yaml.safe_load(f)

    packages: dict = data.get("packages", {})
    if not packages:
        sys.exit(f"ERROR: no 'packages' section found in {lockfile_path}")

    src_root = workspace / "src"
    install_root = workspace / "install"

    mappings: list[tuple[str, str]] = []
    for pkg_name, info in packages.items():
        if not isinstance(info, dict):
            continue
        repo = info.get("repo", "")
        pkg_path = info.get("path", "")
        if not repo:
            continue

        # Source path: workspace/src/<repo>/<pkg_path>/
        src = str(src_root / repo / pkg_path) + "/" if pkg_path else str(src_root / repo) + "/"

        # Install path depends on layout
        if layout == "merge":
            install = str(install_root / "share" / pkg_name) + "/"
        else:  # isolated (default)
            install = str(install_root / pkg_name / "share" / pkg_name) + "/"

        mappings.append((src, install))

    # Sort longest source path first to prevent shorter prefixes matching first
    mappings.sort(key=lambda x: len(x[0]), reverse=True)
    return mappings


_PREVIEW_HEADER = re.compile(r"^<!-- PREVIEW:[^\n]*\n", re.MULTILINE)


def normalize(text: str, mappings: list[tuple[str, str]]) -> str:
    # Remove the preview-mode header line so normalized output matches postbuild.
    text = _PREVIEW_HEADER.sub("", text, count=1)
    # Replace source paths with their install path equivalents.
    for src, install in mappings:
        text = text.replace(src, install)
    return text


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Normalize roscope preview output: replace source paths with install paths."
    )
    parser.add_argument(
        "input",
        nargs="?",
        help="Input file (default: stdin)",
    )
    parser.add_argument(
        "-w",
        "--workspace",
        help="Workspace root containing src/ and install/ (default: lockfile directory)",
    )
    parser.add_argument(
        "-l",
        "--lockfile",
        default="manifest.lock.repos",
        help="Path to manifest.lock.repos (default: manifest.lock.repos)",
    )
    parser.add_argument(
        "--layout",
        choices=["isolated", "merge"],
        default="isolated",
        help="Colcon install layout: isolated (default) or merge",
    )
    args = parser.parse_args()

    lockfile_path = Path(args.lockfile).resolve()
    if not lockfile_path.exists():
        sys.exit(f"ERROR: lockfile not found: {lockfile_path}")

    workspace = Path(args.workspace).resolve() if args.workspace else lockfile_path.parent

    mappings = load_mappings(lockfile_path, workspace, args.layout)
    if not mappings:
        sys.exit("ERROR: no package mappings found in lockfile")

    if args.input:
        with open(args.input) as f:
            text = f.read()
    else:
        text = sys.stdin.read()

    sys.stdout.write(normalize(text, mappings))


if __name__ == "__main__":
    main()
