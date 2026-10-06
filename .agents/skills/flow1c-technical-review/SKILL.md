---
name: flow1c-technical-review
description: Review 1C technical design or extension code against an approved user-referenced specification, local configuration evidence, project policy, ITS/v8std, and static diagnostics. Use for technical architect approval and Git diff review.
---

# FLOW1C technical review

If the user explicitly accepts missing document inputs, record only the displayed condition IDs through `flow1c_dialogue action=deviate`. A deviated review remains `UNVERIFIED_DRAFT`, is not approval, and cannot authorize extension mutation or publication.

For discussion, research or document drafting without formal prerequisites, use `flow1c-consultation` in explore/draft mode. Ask for an opaque user reference only when formal work needs it; never invent one or require `G-xxx`.

Read `standards/source-precedence.md`, then load only the relevant standard among `standards/bsl.md`, `standards/queries.md`, and `standards/customization.md`.

Build the `technical-architect` context. Review only the requested extension diff. Use rlm-tools-bsl to retrieve referenced configuration objects and procedures. Never infer metadata names from FS wording.

Evaluate implementation quality and functional conformity independently. When BSL Language Server is configured, run `flow1c_analyze_bsl` against the selected source and inspect its JSON or SARIF report before completing the review. This read-only diagnostic may run while formal prerequisites are blocked; it does not resolve those prerequisites. Cite findings from that report as `BSL-LS`; do not treat a clean static report as proof of functional conformity. Use mutating or build-oriented cc-1c-skills only for explicitly authorized XML/CFE artifact operations. Consultation-only query analysis is the narrow exception: route it to `flow1c-query-analysis`, which may use only read-only `meta-info` and `skd-info` through `flow1c_cc_inspect`. Record the source type for each finding and write the report under `reviews/technical-review.md` or `implementation/code-review.md`.

Do not change code during a review-only request. Do not approve a PR on behalf of the architect.
