---
name: flow1c-functional-review
description: Review a functional specification against registered business requirements, meeting evidence, traceability, and related project decisions. Use for functional architect review or cross-specification conflict checks.
---

# FLOW1C functional review

A document-only formal review may use `READY_WITH_DEVIATIONS` only after an explicit, scoped user decision over currently shown waivable conditions. Preserve `UNVERIFIED_DRAFT` and report every limitation; this state is not functional approval and does not authorize publication.

For discussion, research or document drafting without formal prerequisites, use `flow1c-consultation` in explore/draft mode. Ask for an opaque user reference only when formal work needs it; never invent one or require `G-xxx`.

Build the context with role `functional-architect`. Review the clean specification, requirements snapshot, traceability and only related wiki decisions.

Check that every requirement is represented without changing its meaning; actors, states, validations, exceptions, permissions and acceptance criteria are explicit; unsupported behavior is marked as a question or proposal; rules do not conflict with approved work items; and the FS is implementable and testable without guessing.

Write findings to `reviews/functional-review.md`, ordered by business impact. Cite the requirement and FS section for each finding. Edit the analyst's files only when the user asks to apply the review. Human approval remains a Gitea review action.
