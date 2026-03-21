#!/usr/bin/env bash
# Integration test for launch-plus against the Autoware example.
#
# Usage:
#   bash test-autoware.sh        # default: verify SHA + clean tree, error if wrong
#   bash test-autoware.sh -c     # test --clean: reset repos to lockfile SHAs (stash dirty)
#   bash test-autoware.sh -d     # test --dirty: use whatever is on disk
#
# The -c/-d flags only select which launch-plus workspace mode to test;
# they do NOT perform any extra cleanup themselves.
#
# Requires:
#   - launch-plus binary on PATH (or built at ../../target/release/launch-plus)
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
    --show-args
)

# ── Acados environment (if installed) ────────────────────────────────────────

if [[ -d /opt/acados/lib ]]; then
    export CMAKE_PREFIX_PATH="/opt/acados:${CMAKE_PREFIX_PATH:-}"
    export ACADOS_SOURCE_DIR="/opt/acados"
    export LD_LIBRARY_PATH="/opt/acados/lib:${LD_LIBRARY_PATH:-}"
fi

# ── Agnocast environment ────────────────────────────────────────────────────

export ENABLE_AGNOCAST="${ENABLE_AGNOCAST:-1}"

# ── Step 1: Preview resolve (portable paths, no expand) ──────────────────────

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

# ── Step 5: Normalize and compare ────────────────────────────────────────────
# Replace real install paths with portable $(find-pkg-share ...) tokens so we
# can diff preview (portable) vs postbuild (real paths).

echo "==> Step 5: Normalize postbuild paths and diff against preview"

INSTALL_DIR="$(pwd)/install"

if [[ -z "${ROS_DISTRO:-}" ]]; then
    echo "ERROR: ROS_DISTRO is not set. Source your ROS 2 environment first." >&2
    exit 1
fi
ROS_SHARE="/opt/ros/${ROS_DISTRO}/share"

# Normalize colcon install paths:  <install>/<pkg>/share/<pkg> → $(find-pkg-share <pkg>)
# Normalize ROS system paths:      /opt/ros/<distro>/share/<pkg> → $(find-pkg-share <pkg>)
# Also strip the preview marker line from preview_raw.xml.
sed -E \
    -e "s|${INSTALL_DIR}/[^/]+/share/([^/]+)|\$(find-pkg-share \1)|g" \
    -e "s|${ROS_SHARE}/([^/]+)|\$(find-pkg-share \1)|g" \
    postbuild.xml > postbuild_normalized.xml

grep -v '^<!-- PREVIEW:' preview_raw.xml > preview.xml

if diff preview.xml postbuild_normalized.xml > /dev/null 2>&1; then
    echo "    OK (identical after normalization)"
else
    echo "    FAIL: preview.xml and postbuild_normalized.xml differ!"
    diff preview.xml postbuild_normalized.xml | head -60
    exit 1
fi

echo
echo "All steps passed."
