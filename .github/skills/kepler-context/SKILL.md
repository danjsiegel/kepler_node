---
name: kepler-context
description: "Use when working on Kepler Node architecture, v1 handoff spec behavior, package boundaries, Raspberry Pi node orchestration, camera or mount adapters, imaging logic, storage layout, or deciding where code should live. Provides repo map, task playbooks, and project-specific gotchas."
---

# Kepler Context

Use this skill when the task needs more than the always-on repo instructions.

## What This Skill Covers
- Kepler Node architecture and package boundaries.
- The role of the private v1 handoff spec.
- Where to start for common repo tasks.
- Repo-specific constraints and gotchas.

## Load These Assets
- `architecture.md` for the current repo map and boundary expectations.
- `workflows.md` for task-oriented starting points.
- `safety-and-gotchas.md` for spec, git, and repo-specific traps.

## Default Working Assumptions
- Prefer `uv` commands already used by the repo.
- Preserve the current package boundaries unless the user explicitly asks for a restructure.
- Treat `lab/specs/V1_HANDOFF.md` as the controlling private v1 contract when implementation behavior is under discussion.