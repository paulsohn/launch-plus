# AI Agent Instructions

This file contains universal instructions for AI coding agents working on this project.

## Before Starting Any Task

1. Read `.agents/context.md` for architecture overview
2. Read `.agents/milestones.md` for goals and commit structure
3. Check your role-specific prompt in `.agents/prompts/`

**Important:** All agent-related files are under `.agents/` directory.

## General Principles

1. **Read before writing** - Always read and understand existing code before making modifications
2. **Minimal changes** - Make only the changes necessary to accomplish the task
3. **No over-engineering** - Avoid adding features, abstractions, or "improvements" beyond what was requested
4. **Security first** - Never introduce vulnerabilities (injection, XSS, etc.)
5. **Consistency** - Follow existing patterns and conventions in the codebase
6. **Root-cause over duct-tape** - Never dismiss a discrepancy as "cosmetic" or "benign" without concrete proof.
   If two outputs differ, assume the difference is a real bug until proven otherwise.
   Investigate relentlessly until the root cause is fully understood; only then decide
   whether to fix or accept.  A principled fix that eliminates the category of bug is
   always preferred over a workaround that papers over a single instance.
7. **Document what IS, not what ISN'T** - Describe the current approach directly.

   **Applies to design/implementation docs** (milestones.md, API docs):
   - Don't mention cancelled or unimplemented plans
   - Don't use language implying change from a previous state ("now", "instead of", "no longer") unless something was actually revised
   - Don't define by rejected alternatives ("X, not Y" where Y was a considered option)
   - Don't use "always X" when X is simply what the feature does — "always" implies a conditional
     version was considered and rejected; just describe the behavior directly
   - Bad: "1 repo = 1 package (no complex 'provides' tracking)"
   - Good: "Each repository in the lockfile maps to one or more packages"
   - Bad: "End comments are always emitted after each source section"
   - Good: "Each source section is bracketed by `<!-- source: -->` and `<!-- end: -->` comments"

   **Does NOT apply to problem documentation** (edge-cases.md, troubleshooting):
   - Describing real-world anti-patterns that exist is fine
   - Documenting what we don't support (and why) helps users understand scope
   - OK: "We do not support hardcoded relative paths in launch files; use $(find-pkg-share ...) instead"

   **Acceptable negations in any context:**
   - Technical characteristics: "no full clone" = sparse fetch
   - Scope limitations: "does not support Python launch files yet"
   - Explicit constraints: "this function does not modify the input"

## Commit Guidelines

- **Follow milestones.md**: Each milestone specifies the exact commits to make. Follow this structure as closely as possible.
- **Update milestones.md if plan changes**: If implementation differs from the planned commits, update milestones.md to reflect the actual commit structure.
- Follow conventional commits: `type(scope): description`
- Types: `feat`, `fix`, `test`, `docs`, `refactor`, `ci`
- Scopes: `indexer`, `resolver`, `fetcher`, `builder`, `launcher`, `cli`
- Keep commits PR-able size (one logical change)
- Run formatters/linters before commit

## Code Style

- Follow the project's existing code style and formatting
- Use meaningful variable and function names
- Keep functions focused and single-purpose
- Prefer explicit over implicit

## Communication

- Be concise and direct
- Ask clarifying questions when requirements are ambiguous
- Report blockers or issues immediately

## Project-Specific Context

See `context.md` for project architecture and `rules.md` for specific coding conventions.
