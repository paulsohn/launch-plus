# Project Context: launch-plus

## Overview

launch-plus is a **Bazel-like build/run system for ROS 2** that enables lazy, on-demand package fetching and building based on actual launch-time dependencies.

**Problem**: Traditional ROS 2 workflow (vcs → rosdep → colcon → ros2 launch) requires cloning and building everything upfront.

**Solution**: Treat launch files as build targets. Parse them, resolve dependencies, fetch/build only what's needed.

## Design Philosophy: Bazel for ROS 2

```
# Bazel                          # launch-plus
bazel build //pkg:target    →    launch-plus build <pkg> <launcher>
bazel test //pkg:target     →    launch-plus test <pkg> <launcher>
bazel run //pkg:target      →    launch-plus run <pkg> <launcher>
bazel query //pkg:target    →    launch-plus dry-run <pkg> <launcher>
```

Key insight: **A launch file IS a build target** that declares its dependencies through:
- `<include file="$(find-pkg-share ...)"/>` - package dependencies
- `<node pkg="..."/>` - executable dependencies
- Embedded `launch-plus:` comment blocks - explicit dependency override (inline)

## Architecture

```
┌───────────────────────────────────────────────────────────────────┐
│  CLI Layer (Python — Click)                                       │
│  ├── launch-plus ...      (standalone)                            │
│  └── ros2 launch-plus ... (ros2 verb integration)                 │
├───────────────────────────────────────────────────────────────────┤
│  Core Library (launch_plus — pure Python)                         │
│  ├── indexer      — .repos → lockfile (pkg→repo+path+SHA map)     │
│  ├── resolver     — launch file → resolved structure + dep graph  │
│  │   ├── entities/substitutions/ — typed substitution objects     │
│  │   ├── entities/actions/       — @expose_action handlers        │
│  │   ├── parsers/                — XML/YAML → Entity object model │
│  │   ├── shim modules            — launch/launch_ros interception │
│  │   └── OpaqueFunction execution with patched I/O                │
│  ├── orchestrator — coordinates resolve + fetch retry loop        │
│  ├── renderer     — resolved IR → XML output                      │
│  ├── fetcher      — git sparse-checkout on demand                 │
│  ├── builder      — colcon build orchestration                    │
│  ├── rosdep       — system dependency resolution                  │
│  └── locator      — package lookup (lockfile / AMENT / overlay)   │
└───────────────────────────────────────────────────────────────────┘
```

## Data Flow

```
1. INDEX PHASE

   launch-plus index [file.repos...]           # Generate from scratch
   launch-plus index --append [file.repos...]  # Append to existing
        ↓
   [indexer] for each repo in .repos:
     - git ls-remote → resolve version to SHA
     - git archive → fetch package.xml files (no full clone)
     - parse package.xml → extract dependencies
        ↓
   manifest.lock.repos
     - repositories: {repo_key → url, sha, packages[]}
     - packages: {pkg_name → repo, path, deps}  ← O(1) lookup

2. UPDATE PHASE (optional, on-demand)

   launch-plus update [--branch]
        ↓
   [updater] for each repo in lockfile:
     - compare current SHA vs remote
     - if changed: re-fetch package.xml, update lockfile
        ↓
   manifest.lock.repos (updated)

3. EXECUTION PHASE (resolve/build/test/run)

   launch-plus <mode> <package> <launch_file>
        ↓
   [resolver] lockfile.packages[package] → quick lookup
        ↓
   [resolver] parse launch file → dependency graph
        ↓
   [fetcher] for each needed package:
     - lockfile.packages[pkg].repo → lockfile.repos[repo]
     - sparse-checkout only needed directories
        ↓
   [builder] colcon build --packages-select <needed>  (skip if mode=resolve)
        ↓
   [launcher] ros2 launch <package> <launch_file>     (only if mode=run)
```

## Key Files

### Input: .repos files (vcs format)

**Universal tool** - works with any .repos files, not Autoware-specific.

**Default**: `manifest.repos` → `manifest.lock.repos`

```bash
launch-plus index                        # Uses manifest.repos
launch-plus index my.repos               # Appends my.repos → manifest.lock.repos
launch-plus index a.repos b.repos        # Appends multiple → manifest.lock.repos
```

### Output: manifest.lock.repos

Dual-indexed lockfile for efficient operations:
- `repositories`: grouped by repository (for fetching)
- `packages`: indexed by package name (for O(1) lookup)

```yaml
repositories:
  core/autoware_msgs:
    type: git
    url: https://github.com/autowarefoundation/autoware_msgs.git
    version: 588f00df3acca009ea709a74acd6538897eef424  # pinned SHA
    ref: 1.11.0                                        # original tag/branch
    packages:
      - autoware_common_msgs
      - autoware_msgs
      - autoware_planning_msgs

packages:
  autoware_msgs:
    repo: core/autoware_msgs
    path: autoware_msgs
```

### Automatic Dependency Tracking (launch.xml/launch.yaml)

The XML/YAML resolver automatically tracks dependencies during parsing — no manual declaration needed for most cases.

**Tracked patterns:**
| Pattern | Tracked Package |
|---------|-----------------|
| `$(find-pkg-share pkg)` | `pkg` |
| `$(find-pkg-prefix pkg)` | `pkg` |
| `<node pkg="pkg" .../>` | `pkg` |
| `<include file="$(find-pkg-share pkg)/..."/>` | `pkg` |
| `<composable_node pkg="pkg" .../>` | `pkg` |

### launch.py Support

Python launch files are resolved by:
1. Installing a `MetaPathFinder` that intercepts imports of `launch`, `launch_ros`, and `ament_index_python`
2. Replacing them with shim modules that record constructor arguments
3. Loading the target launch file via `importlib`
4. Calling `generate_launch_description()`
5. Walking the resulting `LaunchDescription` tree
6. Executing `OpaqueFunction` bodies with patched filesystem access

The shims require no ROS 2 Python packages to be installed.

## Directory Structure

```
launch-plus/
├── pyproject.toml                    # Python package configuration
├── launch_plus/
│   ├── __init__.py
│   ├── __main__.py                   # Entry point
│   ├── cli.py                        # Click CLI
│   ├── indexer.py                    # .repos → lockfile
│   ├── fetcher.py                    # git sparse-checkout
│   ├── resolver.py                   # Launch file resolution
│   ├── orchestrator.py               # Resolve + fetch coordination
│   ├── renderer.py                   # Resolved IR → XML output
│   ├── builder.py                    # colcon build orchestration
│   ├── rosdep.py                     # System dependency resolution
│   ├── locator.py                    # Package location
│   ├── types.py                      # Shared dataclasses
│   ├── exceptions.py                 # Exception hierarchy
│   ├── entities/
│   │   ├── expose.py                 # @expose_action / @expose_substitution registries
│   │   ├── substitution.py           # Substitution ABC
│   │   ├── substitutions/            # Typed substitution objects (arg, var, env, eval, ...)
│   │   └── actions/                  # @expose_action handlers (arg, node, include, ...)
│   ├── parsers/
│   │   ├── entity.py                 # Entity ABC (XmlEntity, YamlEntity)
│   │   ├── xml_parser.py             # XML → Entity tree
│   │   ├── yaml_parser.py            # YAML → Entity tree
│   │   ├── parse_substitution.py     # Lark grammar → Substitution objects
│   │   └── grammar.lark              # Substitution grammar
│   └── verb/                         # ros2 CLI verb integration
├── tests/
│   ├── conftest.py
│   ├── test_resolver.py
│   └── ...
├── example/
│   └── autoware/                     # Bundled Autoware integration test
└── .agents/                          # AI agent instructions
```

## Technology Stack

- **Python 3.10+**: All modules (CLI, resolver, indexer, fetcher, builder)
- **Click**: CLI framework
- **Lark**: Substitution expression grammar parsing
- **Git sparse-checkout**: Partial cloning
- **colcon**: Build orchestration (called as subprocess)

## ROS 2 Integration

- **Target: ROS 2 Humble / Jazzy**
- Integrates as `ros2 launch-plus` verb
- Respects `AMENT_PREFIX_PATH`, `COLCON_PREFIX_PATH`
- Uses standard `package.xml` for dependency info

## Execution Modes

| Mode | Fetch | Build | Test | Launch | Output |
|------|-------|-------|------|--------|--------|
| `resolve` | Yes* | No | No | No | Resolved XML/YAML |
| `build` | Yes | Yes | No | No | Build artifacts |
| `test` | Yes | Yes | Yes | No | Test results |
| `run` | Yes | Yes | No | Yes | Running system |

*Fetch required if lockfile not available or launch file not yet fetched.

## Terminology

### Portable Path

A path string in `$(find-pkg-share <pkg>)/...` format — an *unresolved substitution* that
references a package resource without binding to any specific filesystem layout.  Portable
paths are valid inputs to the ROS 2 launch substitution engine and are the **canonical output
format of launch-plus in all modes**.

### Verbose Flag

Flags that enable non-default resolver behaviour that would otherwise discourage users from
leaving upstream launch files as-is.  Examples: `--allow-global-arg-cascade`,
`--apply-launch-arg-defaults`.
