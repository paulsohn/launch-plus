#!/usr/bin/env bash
# Integration test for launch-plus against the Autoware example.
#
# Usage:
#   bash test-autoware.sh -c   # clean: reset repos to lockfile SHAs
#   bash test-autoware.sh -d   # dirty: use whatever is on disk
#
# Requires:
#   - launch-plus binary on PATH (or built at ../../target/release/launch-plus)
#   - ROS 2 sourced (for rosdep + ament)
#   - colcon installed (for the build step)

set -euo pipefail

# ── Parse arguments ──────────────────────────────────────────────────────────

if [[ $# -ne 1 ]] || [[ "$1" != "-c" && "$1" != "-d" ]]; then
    echo "Usage: $0 -c | -d"
    echo "  -c  clean (reset repos to lockfile SHAs)"
    echo "  -d  dirty (use whatever is on disk)"
    exit 1
fi

MODE_FLAG="$1"
if [[ "$MODE_FLAG" == "-c" ]]; then
    MODE="--clean"
else
    MODE="--dirty"
fi

# ── Locate launch-plus ───────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if command -v launch-plus &>/dev/null; then
    LP=launch-plus
elif [[ -x ../../target/release/launch-plus ]]; then
    LP=../../target/release/launch-plus
else
    echo "ERROR: launch-plus not found. Build with: cargo build --bin launch-plus --release"
    exit 1
fi

echo "Using: $LP"
echo "Mode:  $MODE"
echo

# ── Common flags ─────────────────────────────────────────────────────────────

# Launcher target and arguments (shared between resolve and build)
LAUNCH_ARGS=(
    autoware_launch autoware.launch.xml
    sensor_model:=sample_sensor_kit
    vehicle_model:=sample_vehicle
    "map_path:=[map_path]"
)

# Resolver behavior flags (shared between resolve and build)
COMMON_FLAGS=(
    --allow-global-arg-cascade
    --apply-launch-arg-defaults
    --apply-opaque-file-access
    --allow-including-unportable-path
    --rosdep
)

# Resolve-only display flags (not accepted by build)
RESOLVE_DISPLAY=(
    --inline-params
    --flatten-namespaces
    --show-args
)

# ── Acados environment (if installed) ────────────────────────────────────────

if [[ -d /opt/acados/lib ]]; then
    export CMAKE_PREFIX_PATH="/opt/acados:${CMAKE_PREFIX_PATH:-}"
    export ACADOS_SOURCE_DIR="/opt/acados"
    export LD_LIBRARY_PATH="/opt/acados/lib:${LD_LIBRARY_PATH:-}"
fi

# ── Clean workspace if requested ─────────────────────────────────────────────

if [[ "$MODE_FLAG" == "-c" ]]; then
    echo "==> Cleaning workspace"
    rm -rf src/ build/ install/ log/
    echo
fi

# ── Step 1: Preview resolve (portable paths, no expand) ──────────────────────

echo "==> Step 1: Preview resolve"
$LP resolve "$MODE" "${LAUNCH_ARGS[@]}" "${COMMON_FLAGS[@]}" "${RESOLVE_DISPLAY[@]}" --preview > preview_raw.xml
echo "    OK (preview_raw.xml)"
echo

# ── Step 2: Build ────────────────────────────────────────────────────────────

echo "==> Step 2: Build"
$LP build "$MODE" "${LAUNCH_ARGS[@]}" "${COMMON_FLAGS[@]}" --colcon-flagfile colcon-flags.txt
echo "    OK"
echo

# ── Step 3: Source install ───────────────────────────────────────────────────

echo "==> Step 3: Source install/setup.bash"
# shellcheck disable=SC1091
set +u  # colcon's setup.bash references unset variables (COLCON_TRACE, etc.)
source install/setup.bash
set -u
echo "    OK (AMENT_PREFIX_PATH set)"
echo

# ── Step 4: Preview resolve + expand paths ───────────────────────────────────

echo "==> Step 4: Preview resolve (expand paths)"
$LP resolve "$MODE" "${LAUNCH_ARGS[@]}" "${COMMON_FLAGS[@]}" "${RESOLVE_DISPLAY[@]}" --preview --expand-paths > preview.xml
echo "    OK (preview.xml)"
echo

# ── Step 5: Post-build resolve (real paths) ──────────────────────────────────

echo "==> Step 5: Post-build resolve"
$LP resolve "$MODE" "${LAUNCH_ARGS[@]}" "${COMMON_FLAGS[@]}" "${RESOLVE_DISPLAY[@]}" > postbuild.xml
echo "    OK (postbuild.xml)"
echo

# ── Step 6: Compare ─────────────────────────────────────────────────────────

echo "==> Step 6: Diff preview.xml vs postbuild.xml"
if diff preview.xml postbuild.xml > /dev/null 2>&1; then
    echo "    OK (identical)"
else
    echo "    FAIL: preview.xml and postbuild.xml differ!"
    diff preview.xml postbuild.xml | head -40
    exit 1
fi

echo
echo "All steps passed."
