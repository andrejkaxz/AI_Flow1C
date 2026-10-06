---
name: flow1c-wiki
description: Report project status from Git-tracked manifests or update the Markdown project wiki with approved decisions and delivered 1C behavior. Use for status questions, chronology, or wiki synchronization.
---

# FLOW1C wiki and status

For a status question, read manifests first and query Gitea only when open PR state is needed. Do not infer completion from draft files.

Run `scripts/flow1c.py status` for a read-only view and `scripts/flow1c.py status --write` when the user asks to update the repository.

Update wiki content only from merged or explicitly approved decisions. Each entry identifies the original user-supplied work reference, requirement IDs, functional area, key behavior, important constraints and approval/delivery state. Never invent a reference or require `G-xxx`. Avoid duplicating the full FS.
