---
name: flow1c-testing
description: Create a 1C test plan, record a test protocol, or design Vanessa Automation scenarios for one user-referenced work item from requirements, the approved specification, and the implemented Git diff.
---

# FLOW1C testing

Testing documentation may continue in formal mode with a scoped user deviation for waivable conditions. Keep `UNVERIFIED_DRAFT` visible; `COMPLETE_WITH_DEVIATIONS` is not test approval or publication evidence, and output integrity remains non-waivable.

For discussion, research or document drafting without formal prerequisites, use `flow1c-consultation` in explore/draft mode. Ask for an opaque user reference only when formal work needs it; never invent one or require `G-xxx`.

Build the `tester` context. When implementation exists, inspect the bounded extension diff to identify changed branches, validations, permissions and error paths. Code may add test cases but cannot replace approved expected behavior.

Maintain traceability between requirement IDs, acceptance criteria and test IDs. Include positive, negative, boundary, permission, migration and regression scenarios when relevant. Put planned cases in `testing/test-plan.md` and actual evidence in `testing/test-protocol.md`; never mark a case passed without execution evidence.

Generate Vanessa feature files under `testing/vanessa/` only when requested. Prefer existing steps verified from the project's Vanessa step library and connected Vanessa_for_AI guidance. Do not invent step phrases.
For a user test-protocol template, use template list/resolve and document tools
as a shared substep in this gate per docs/document-templates.md. Preserve the
existing work item/evidence requirements. Example test results, signatures and
approvals never become actual outcomes. Record missing execution evidence as
unknown and continue the same request after user answers.
