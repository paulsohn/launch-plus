# Getting Started

This guide walks through using launch-plus with your own ROS 2 project.

## Prerequisites

- **Rust toolchain** (edition 2024, MSRV 1.85) — install via [rustup.rs](https://rustup.rs)
- **Python 3.10+** — used to evaluate Python launch files and `$(eval ...)`
  substitutions in XML launch files
- **Git** — for sparse-checkout operations
- **ROS 2** — required for `--rosdep` (system dependency resolution) and
  post-build resolution; the resolver itself does not depend on ROS 2 Python
  packages

## Installation

```bash
git clone https://github.com/paulsohn/launch-plus.git
cd launch-plus
cargo build --bin launch-plus --release

# Add to PATH (optional)
export PATH="$PWD/target/release:$PATH"
```

## Step 1: Create a .repos manifest

Write a `.repos` file listing the git repositories your project uses.  This is
the standard [vcstool](https://github.com/dirk-thomas/vcstool) format:

```yaml
# my_project.repos
repositories:
  core/my_msgs:
    type: git
    url: https://github.com/my-org/my_msgs.git
    version: v1.0.0              # tag, branch, or SHA

  drivers/my_driver:
    type: git
    url: https://github.com/my-org/my_driver.git
    version: main

  launch/my_bringup:
    type: git
    url: https://github.com/my-org/my_bringup.git
    version: v2.1.0
```

If you already have a `.repos` file (e.g. from an existing vcstool workflow),
you can use it directly.

## Step 2: Generate a lockfile

```bash
launch-plus index my_project.repos
```

This resolves every version reference to a concrete commit SHA, scans each
repository for ROS packages, and writes `manifest.lock.repos`.

The lockfile records:
- Exact commit SHAs for every repository
- Package names and their locations within each repository

**Commit the lockfile** alongside your `.repos` manifest.  This ensures
reproducible builds — anyone with the same lockfile will get exactly the same
source code.

To update the lockfile when upstream repos change:

```bash
launch-plus update              # re-resolve all refs
launch-plus update core/my_msgs # update a specific repo (uses lockfile key)
```

## Step 3: Resolve a launch file

```bash
launch-plus resolve -d my_bringup robot.launch.xml \
  robot_name:=my_robot \
  --preview \
  > resolved.launch.xml
```

This will:
1. Look up `my_bringup` in the lockfile
2. Sparse-checkout just the `my_bringup` package
3. Parse `robot.launch.xml`, following all `<include>` tags
4. Fetch additional packages as they're discovered in the launch graph
5. Output a single flattened XML with all includes inlined, variables
   substituted, and conditionals evaluated

The `-d` (dirty) flag tells launch-plus to use whatever is on disk and only
fetch what's missing.  Use `-c` (clean) for CI to ensure reproducibility.

### Useful resolve flags

```bash
# Show launch arguments at include boundaries
--show-args

# Expand <param from="file.yaml"/> entries inline
--inline-params

# Fold namespace stacks onto each <node> element
--flatten-namespaces

# Allow OpaqueFunction bodies to read parameter files
--apply-opaque-file-access

# Fill unset args from their declared defaults
--apply-launch-arg-defaults

# Propagate parent args into included files (legacy launch files)
--allow-global-arg-cascade

# Install system deps via rosdep (requires sourced ROS 2)
--rosdep
```

## Step 4: Build

```bash
source /opt/ros/humble/setup.bash

launch-plus build -c my_bringup robot.launch.xml \
  robot_name:=my_robot \
  --rosdep \
  --colcon-flagfile colcon-flags.txt
```

This resolves the launch file, computes the transitive build-dependency closure,
fetches any missing packages, installs system dependencies via rosdep, and runs
`colcon build` with only the needed packages.

### Colcon flagfile

Extra colcon arguments are passed via a **flagfile** — one shell token per line:

```txt
# colcon-flags.txt
--symlink-install
--cmake-args
-DCMAKE_BUILD_TYPE=Release
--parallel-workers
4
```

## Step 5: Verify (optional)

After building, you can verify that the pre-build (preview) resolution matches
the post-build resolution:

```bash
# Source the built workspace
source install/setup.bash

# Preview resolve (pre-build, portable paths)
launch-plus resolve --preview -d my_bringup robot.launch.xml \
  robot_name:=my_robot \
  > preview.launch.xml

# Post-build resolve (real install paths)
launch-plus resolve -d my_bringup robot.launch.xml \
  robot_name:=my_robot \
  > postbuild.launch.xml

# Compare preview vs postbuild (normalize paths first)
INSTALL_DIR="$(pwd)/install"
sed -E "s|${INSTALL_DIR}/[^/]+/share/([^/]+)|\$(find-pkg-share \1)|g" \
  postbuild.launch.xml > postbuild_normalized.xml
grep -v '^<!-- PREVIEW:' preview.launch.xml > preview_clean.xml
diff preview_clean.xml postbuild_normalized.xml
```

## Directory layout

After running launch-plus, your workspace will look like:

```
my_workspace/
├── my_project.repos          # your manifest
├── manifest.lock.repos       # generated lockfile (commit this)
├── colcon-flags.txt          # colcon arguments
├── src/                      # sparse-checked out packages
│   ├── core/my_msgs/
│   ├── drivers/my_driver/
│   └── launch/my_bringup/
├── build/                    # colcon build output
├── install/                  # colcon install output
└── log/                      # colcon log output
```

## Next steps

- Read [Core Concepts](concepts.md) for deeper understanding of lockfiles,
  portable paths, and OpaqueFunction handling
- See [Architecture](architecture.md) for how the resolver works internally
- Check [Supported Environments](supported-environments.md) for platform
  compatibility and known limitations
