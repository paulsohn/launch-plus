# Getting Started

This guide walks through using roscope with your own ROS 2 project.

## Prerequisites

- **Python 3.10+** — used to evaluate Python launch files and `$(eval ...)`
  substitutions in XML launch files
- **Git** — for sparse-checkout operations
- **ROS 2** — required for `--rosdep` (system dependency resolution) and
  post-build resolution; the resolver itself does not depend on ROS 2 Python
  packages

## Installation

```bash
pip install git+https://github.com/paulsohn/roscope.git
```

Or for development:

```bash
git clone https://github.com/paulsohn/roscope.git
cd roscope
pip install -e .
```

## Quick start

Clone your workspace as usual, then point roscope at a launch file:

```bash
vcs import src < my_project.repos

roscope resolve -d my_bringup robot.launch.xml \
  robot_name:=my_robot \
  --preview \
  --visualize
```

This opens an interactive graph view in your browser showing the full launch
structure — every node, container, topic, remap, and include boundary — with no
build required.

To get the expanded XML instead:

```bash
roscope resolve -d my_bringup robot.launch.xml \
  robot_name:=my_robot \
  --preview \
  > resolved.launch.xml
```

To build only the packages the launch file needs:

```bash
roscope build -d my_bringup robot.launch.xml \
  robot_name:=my_robot \
  --rosdep
```

### Useful resolve flags

```bash
# Open an interactive graph view in your browser
--visualize

# Show launch arguments at include boundaries (auto-enabled with --visualize)
--show-args

# Install system deps via rosdep (requires sourced ROS 2)
--rosdep
```

## What is `-d`?

The `-d` flag (short for `--dirty`) tells roscope to use whatever is in `src/`
as-is.  roscope walks the directory looking for `package.xml` files and treats
each one as a package — no git operations, no version pinning.

Without `-d`, roscope requires a **lockfile** (`manifest.lock.repos`): a
snapshot of your workspace that pins every repository to a concrete commit SHA
and records which packages each repository contains.  The lockfile enables
on-demand sparse checkout (fetching only the packages a launch file actually
needs) and reproducible builds.

If you want that — read on.

## Lockfile workflow

The lockfile workflow is roscope's native mode: instead of importing all
repositories up front, roscope fetches only the packages a launch file
references, on demand, pinned to exact commit SHAs.

> **Note:** the default and `--clean` modes assume that all package repositories
> are described in one or more `.repos` files and will be checked out under a
> single source directory (default: `src/`).  The `.repos` files are also what
> `roscope index` reads to generate the lockfile in the first place.  This is
> the same convention Autoware and most large ROS 2 projects follow.  If your
> workspace is already structured this way, the lockfile workflow works out of
> the box.

### Step 1: Create a .repos manifest

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

### Step 2: Generate a lockfile

```bash
roscope index my_project.repos
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
roscope update              # re-resolve all refs
roscope update core/my_msgs # update a specific repo (uses lockfile key)
```

### Step 3: Resolve a launch file

```bash
roscope resolve my_bringup robot.launch.xml \
  robot_name:=my_robot \
  --preview \
  > resolved.launch.xml
```

This will:
1. Look up `my_bringup` in the lockfile
2. Sparse-checkout just the `my_bringup` package
3. Parse `robot.launch.xml`, following all `<include>` tags
4. Fetch additional packages as they're discovered in the launch graph
5. Output a single expanded XML with all includes inlined, variables
   substituted, and conditionals evaluated

Use `-c` (clean) in CI to guarantee reproducibility by resetting repos to
lockfile SHAs.

### Step 4: Build

```bash
source /opt/ros/humble/setup.bash

roscope build -c my_bringup robot.launch.xml \
  robot_name:=my_robot \
  --rosdep \
  --colcon-flagfile colcon-flags.txt
```

This resolves the launch file, computes the transitive build-dependency closure,
fetches any missing packages, installs system dependencies via rosdep, and runs
`colcon build` with only the needed packages.

#### Colcon flagfile

Extra colcon arguments are passed via a **flagfile** — one shell token per line:

```txt
# colcon-flags.txt
--symlink-install
--cmake-args
-DCMAKE_BUILD_TYPE=Release
--parallel-workers
4
```

### Step 5: Verify (optional)

After building, you can verify that the pre-build (preview) resolution matches
the post-build resolution:

```bash
# Source the built workspace (sets ROS_DISTRO, AMENT_PREFIX_PATH, etc.)
source /opt/ros/${ROS_DISTRO:-humble}/setup.bash
source install/setup.bash

# Preview resolve (pre-build, source paths)
roscope resolve --preview my_bringup robot.launch.xml \
  robot_name:=my_robot \
  > preview.launch.xml

# Post-build resolve (install paths)
roscope resolve my_bringup robot.launch.xml \
  robot_name:=my_robot \
  > postbuild.launch.xml

# Compare preview vs postbuild (strip the PREVIEW comment line)
grep -v '^<!-- PREVIEW:' preview.launch.xml > preview_clean.xml
diff preview_clean.xml postbuild.launch.xml
```

## Directory layout

After running the lockfile workflow, your workspace will look like:

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
  package resolution, and OpaqueFunction handling
- See [Architecture](architecture.md) for how the resolver works internally
- Check [Supported Environments](supported-environments.md) for platform
  compatibility and known limitations
