#!/usr/bin/env bash
# Install acados following the Autoware convention.
#
# Mirrors the Ansible role at:
#   https://github.com/autowarefoundation/autoware/blob/811a7e75ba6d6752fd9d1caf4a9a26f09294d474/ansible/roles/acados/
#
# Usage (requires root):
#   sudo bash install-acados.sh
#
# After installation, set these environment variables:
#   export CMAKE_PREFIX_PATH="/opt/acados:${CMAKE_PREFIX_PATH}"
#   export ACADOS_SOURCE_DIR="/opt/acados"
#   export LD_LIBRARY_PATH="/opt/acados/lib:${LD_LIBRARY_PATH}"
#
# Note on the Python venv: autoware_path_optimizer's CMakeLists.txt hardcodes
#   ${ACADOS_SOURCE_DIR}/.venv/bin/python3
# to run acados_template code generation at build time, so the venv cannot be
# skipped — even if casadi/acados_template were installed system-wide.

set -euo pipefail

# ── Versions (match Autoware's ansible/roles/acados/defaults/main.yaml) ──────

ACADOS_VERSION="${ACADOS_VERSION:-v0.5.3}"
TERA_RENDERER_VERSION="${TERA_RENDERER_VERSION:-v0.2.0}"
ACADOS_DIR="/opt/acados"

# ── Detect architecture ──────────────────────────────────────────────────────

ARCH="$(uname -m)"
case "$ARCH" in
    x86_64)  TERA_ARCH="amd64" ;;
    aarch64) TERA_ARCH="arm64" ;;
    *)
        echo "ERROR: unsupported architecture: $ARCH"
        exit 1
        ;;
esac

# ── Skip if already installed at the correct version ─────────────────────────

if [[ -f "$ACADOS_DIR/lib/libacados.so" ]] && \
   [[ -f "$ACADOS_DIR/.version" ]] && \
   [[ "$(cat "$ACADOS_DIR/.version")" == "$ACADOS_VERSION" ]]; then
    echo "acados $ACADOS_VERSION already installed at $ACADOS_DIR — skipping"
    exit 0
fi

# ── Clone ────────────────────────────────────────────────────────────────────

echo "==> Cloning acados $ACADOS_VERSION"
rm -rf "$ACADOS_DIR"
git clone --depth 1 --branch "$ACADOS_VERSION" --recurse-submodules \
    https://github.com/acados/acados.git "$ACADOS_DIR"

# ── Build ────────────────────────────────────────────────────────────────────

echo "==> Building acados"
mkdir -p "$ACADOS_DIR/build"
cmake -S "$ACADOS_DIR" -B "$ACADOS_DIR/build" \
    -DACADOS_WITH_QPOASES=ON \
    -DCMAKE_POSITION_INDEPENDENT_CODE=ON
cmake --build "$ACADOS_DIR/build" --target install -j"$(nproc)"

# ── Tera renderer ────────────────────────────────────────────────────────────

echo "==> Downloading tera renderer ($TERA_ARCH)"
mkdir -p "$ACADOS_DIR/bin"
curl -fsSL \
    "https://github.com/acados/tera_renderer/releases/download/${TERA_RENDERER_VERSION}/t_renderer-${TERA_RENDERER_VERSION}-linux-${TERA_ARCH}" \
    -o "$ACADOS_DIR/bin/t_renderer"
chmod +x "$ACADOS_DIR/bin/t_renderer"

# ── Python venv ──────────────────────────────────────────────────────────────
#
# Required: autoware_path_optimizer's CMakeLists.txt invokes
#   ${ACADOS_SOURCE_DIR}/.venv/bin/python3
# to generate C code from acados_template at build time.

echo "==> Creating Python venv at $ACADOS_DIR/.venv"
python3 -m venv "$ACADOS_DIR/.venv"
"$ACADOS_DIR/.venv/bin/pip" install --upgrade pip
"$ACADOS_DIR/.venv/bin/pip" install casadi sympy
"$ACADOS_DIR/.venv/bin/pip" install -e "$ACADOS_DIR/interfaces/acados_template"

# ── Version marker ───────────────────────────────────────────────────────────

echo "$ACADOS_VERSION" > "$ACADOS_DIR/.version"

echo "==> acados $ACADOS_VERSION installed to $ACADOS_DIR"
