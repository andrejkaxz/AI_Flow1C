---
name: flow1c-git-gitea
description: Safely create Flow1C branches and commits and publish pull requests to Gitea for one user-referenced work item. Use when the user asks to start a branch, commit, push, publish, or open a PR.
---

# FLOW1C Git and Gitea

Read `docs/lifecycle.md`. Before mutation, identify the repository, branch, opaque user-supplied work reference, changed files and target branch. Never invent a reference or require `G-xxx`. Preserve unrelated changes. Use `scripts/flow1c.py git-commit` to stage only the safely resolved work-item directory, status page and explicitly requested registry changes.

Branch names:

- `fs/<safe-reference-slug>/specification` for initial documentation;
- `feature/<safe-reference-slug>` in the extension repository;
- `fs/<safe-reference-slug>/post-development` after implementation;
- `fs/<safe-reference-slug>/acceptance` for testing and delivery.

Create Gitea PRs with `scripts/flow1c.py pr-create`. The token comes from the configured environment variable and must never be printed or committed. Include requirement IDs, phase, validation results and open questions. Assign human reviewers. Never merge or approve unless the user explicitly requests that distinct action and has authority.
