# Lockfile Schema & Validation Rules

This document defines the schema and validation rules for `manifest.lock.repos`.

## Schema Overview

```yaml
version: 1

repos:
  <repo_key>:
    url: <git_url>
    sha: <40_hex_chars>
    version: <original_version_string>
    branch: <branch_name>              # Optional
    packages:
      - <package_name>

packages:
  <package_name>:
    repo: <repo_key>
    path: <relative_path>
```

Dependencies are resolved from on-disk `package.xml` at build time, not stored
in the lockfile.  The lockfile is an index only: it maps package names to their
repository and path.

## Validation Rules

### 1. packages ↔ repos Consistency

| ID | Rule | Severity |
|----|------|----------|
| P1 | Every `packages[name].repo` must reference existing `repos` key | Error |
| P2 | Package names must be unique (enforced by map keys) | Error |
| P3 | All packages in `repo.packages` list must exist in `packages` | Error |
| P4 | All packages pointing to a repo must be in that repo's `packages` list | Error |

### 2. Field Format Validation

| ID | Rule | Severity |
|----|------|----------|
| F1 | `version` must be present and positive integer | Error |
| F2 | `repo.url` must be valid git URL (https://, git@, or file://) | Error |
| F3 | `repo.sha` must be 40 hexadecimal characters | Error |
| F4 | `repo.branch` if present, must be non-empty string | Warning |
| F5 | `package.path` must be valid relative path (no leading `..`) | Error |

### 3. Completeness Checks

| ID | Rule | Severity |
|----|------|----------|
| C1 | Every repo must have ≥1 package in its `packages` list | Error |
| C2 | No orphan packages (every package's repo must exist) | Error |

## Example: Valid Lockfile

```yaml
version: 1

repos:
  autoware_msgs:
    url: https://github.com/autowarefoundation/autoware_msgs.git
    sha: abc123def456abc123def456abc123def456abc1
    version: main
    packages:
      - autoware_planning_msgs
      - autoware_control_msgs

packages:
  autoware_planning_msgs:
    repo: autoware_msgs
    path: autoware_planning_msgs

  autoware_control_msgs:
    repo: autoware_msgs
    path: autoware_control_msgs
```

## Validation Implementation

The validation should be implemented as part of:
1. **Lockfile loading**: Validate on parse, fail fast on errors
2. **`launch-plus index --verify`**: Full validation for CI checks

### Suggested API

```rust
pub struct LockfileValidator;

impl LockfileValidator {
    /// Validate entire lockfile, return all errors
    pub fn validate(lockfile: &Lockfile) -> Vec<ValidationError>;
}

pub struct ValidationError {
    pub rule_id: String,      // e.g., "P1", "F3"
    pub severity: Severity,   // Error or Warning
    pub message: String,
    pub context: Option<String>,  // e.g., "repo: autoware_msgs"
}
```
