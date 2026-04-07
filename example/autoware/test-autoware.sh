#!/usr/bin/env bash
# Integration test for roscope against the Autoware example.
#
# Usage:
#   bash test-autoware.sh        # default: verify SHA + clean tree, error if wrong
#   bash test-autoware.sh -c     # test --clean: reset repos to lockfile SHAs (stash dirty)
#   bash test-autoware.sh -d     # test --dirty: use whatever is on disk
#
# The -c/-d flags only select which roscope workspace mode to test;
# they do NOT perform any extra cleanup themselves.
#
# Requires:
#   - roscope on PATH (pip install -e . from repo root)
#   - ROS 2 sourced (for rosdep + ament)
#   - colcon installed (for the build step)

set -euo pipefail

# ── Parse arguments ──────────────────────────────────────────────────────────

MODE_FLAG="${1:-}"  # empty string if no argument given

if [[ $# -gt 1 ]] || [[ -n "$MODE_FLAG" && "$MODE_FLAG" != "-c" && "$MODE_FLAG" != "-d" ]]; then
    echo "Usage: $0 [-c | -d]"
    echo "  (none)  default (verify SHA + clean tree, error if mismatch)"
    echo "  -c      test --clean mode (reset repos to lockfile SHAs)"
    echo "  -d      test --dirty mode (use whatever is on disk)"
    exit 1
fi

if [[ "$MODE_FLAG" == "-c" ]]; then
    MODE="--clean"
elif [[ "$MODE_FLAG" == "-d" ]]; then
    MODE="--dirty"
else
    MODE=""  # default mode: no flag
fi

# ── Locate roscope ───────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if command -v roscope &>/dev/null; then
    LP=roscope
else
    echo "ERROR: roscope not found. Install with: pip install -e . (from repo root)"
    exit 1
fi

echo "Using: $LP"
echo "Mode:  ${MODE:-default (verify)}"
echo

# Build the mode argument array (empty in default mode).
# Using the ${arr[@]+"${arr[@]}"} pattern in command lines for compatibility
# with bash <4.4 where empty array expansion under set -u is an error.
if [[ -n "$MODE" ]]; then
    MODE_ARGS=("$MODE")
else
    MODE_ARGS=()
fi

# ── Common flags ─────────────────────────────────────────────────────────────

# Launcher target and arguments (shared between resolve and build)
LAUNCH_ARGS=(
    autoware_launch autoware.launch.xml
    sensor_model:=sample_sensor_kit
    vehicle_model:=sample_vehicle
    "map_path:={map_path}"
)

# Resolver behavior flags (shared between resolve and build)
COMMON_FLAGS=(
    --apply-launch-arg-defaults
    --rosdep
)

# Resolve-only display flags (not accepted by build)
RESOLVE_DISPLAY=(
    --inline-params
)

# ── Acados environment (if installed) ────────────────────────────────────────

if [[ -d /opt/acados/lib ]]; then
    export CMAKE_PREFIX_PATH="/opt/acados:${CMAKE_PREFIX_PATH:-}"
    export ACADOS_SOURCE_DIR="/opt/acados"
    export LD_LIBRARY_PATH="/opt/acados/lib:${LD_LIBRARY_PATH:-}"
fi

# ── Agnocast environment ────────────────────────────────────────────────────

export ENABLE_AGNOCAST="${ENABLE_AGNOCAST:-1}"

# ── Step 1: Preview resolve (source paths) ───────────────────────────────────

echo "==> Step 1: Preview resolve"
$LP resolve ${MODE_ARGS[@]+"${MODE_ARGS[@]}"} "${LAUNCH_ARGS[@]}" "${COMMON_FLAGS[@]}" "${RESOLVE_DISPLAY[@]}" --preview > preview_raw.xml
echo "    OK (preview_raw.xml)"
echo

# ── Step 2: Build ────────────────────────────────────────────────────────────

echo "==> Step 2: Build"
$LP build ${MODE_ARGS[@]+"${MODE_ARGS[@]}"} "${LAUNCH_ARGS[@]}" "${COMMON_FLAGS[@]}" --colcon-flagfile colcon-flags.txt
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

# ── Step 4: Post-build resolve (real paths) ──────────────────────────────────

echo "==> Step 4: Post-build resolve"
$LP resolve ${MODE_ARGS[@]+"${MODE_ARGS[@]}"} "${LAUNCH_ARGS[@]}" "${COMMON_FLAGS[@]}" "${RESOLVE_DISPLAY[@]}" > postbuild.xml
echo "    OK (postbuild.xml)"
echo

# ── Step 5: Compare preview vs postbuild ─────────────────────────────────────
# Diff preview (source paths) vs postbuild (install paths).
# Lines containing "PREVIEW:" or "xacro" are expected to differ (path-dependent).

echo "==> Step 5: Compare preview vs postbuild"

UNEXPECTED=$(diff preview_raw.xml postbuild.xml \
    | grep "^[<>]" \
    | grep -v "PREVIEW:" \
    | grep -v "xacro" \
    | grep -v "<!-- source:" \
    | grep -v "<!-- params from:" \
    | grep -v "<!-- end params from:" \
    || true)

if [[ -z "$UNEXPECTED" ]]; then
    echo "    OK (no unexpected differences)"
else
    echo "    FAIL: unexpected differences between preview and postbuild:"
    echo "$UNEXPECTED" | head -20
    exit 1
fi

echo
echo "All steps passed."
