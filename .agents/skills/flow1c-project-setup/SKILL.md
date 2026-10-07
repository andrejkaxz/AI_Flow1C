---
name: flow1c-project-setup
description: Initialize an Flow1C project, connect local 1C configuration sources and the extension repository, and diagnose required tooling. Use for first-time setup, onboarding, or broken local paths.
---

# FLOW1C project setup

Read `docs/setup.md` and `config/workflow.example.json`.

Treat the local workflow configuration file only as an untrusted hint from a previous setup. Explicitly ask the user to confirm every required external path and URL, even when a value is already populated. Present existing values only as suggestions and never accept silence as confirmation. Confirm the current Flow1C repository URL; documentation repository URL and local path; local configuration XML/BSL path; extension source mode, local path, and repository URL in Git mode; Gitea coordinates; and missing Git author identity. An approved DOCX template is optional during setup. Accept it when supplied, but do not block setup when it is absent; request it later only before formal functional-spec generation.

Check that paths exist where required, stay outside the Flow1C checkout, and do not point into another Flow1C clone. Confirm whether an empty local documentation directory should be initialized as a new Git repository; never initialize or replace a non-empty directory implicitly. Workflow and project documentation must use different repositories. Use the shared-repository override only after explicit user confirmation and report the exception.

First check Windows PowerShell, Git, Python 3.10+, and WinGet. If Git or Python is missing, explain which trusted packages will be installed and request user approval before running `scripts/install-prerequisites.ps1`. When WinGet is unavailable, request paths to organization-approved offline installers. Do not silently elevate privileges.

Run `scripts/bootstrap.ps1`; its default dependency set includes Excel/DOCX/PDF conversion and rlm-tools-bsl. Bootstrap must start the local RLM Streamable HTTP server at `http://127.0.0.1:9000/mcp`; RLM is required, not optional. Install BSL Language Server with `-InstallBslLanguageServer`. Install cc-1c-skills only when the user authorizes its external clone.

Connect the documentation and extension sources with `scripts/configure-project.ps1` after the user confirms the required value set. Use `-ExtensionMode Git` for a Git repository or `-ExtensionMode LocalExport` for a non-Git XML/BSL export; the URL must be empty in LocalExport mode. For Git extensions, ask whether issue branches follow a project prefix such as `iss` or `feature/task`; if known, pass it as `-ExtensionBranchPrefix`. The prefix is optional because Git branch discovery can find a unique suffix without it. Pass `-FunctionalSpecTemplate` only when the user supplied an existing approved DOCX file. The command validates the workflow origin, rejects stale paths from another checkout, writes checkout-bound validation to the ignored local config, and never copies the full 1C configuration into Git.

The configuration script must verify fresh RLM indexes for both the local configuration and extension before setup is complete. Do not bypass this with direct filesystem search. For a large configuration that may exceed the AI client's shell timeout, first run `scripts/rlm-index.ps1 -Action Ensure -SourcePath <path> -Json`; it starts one detached build with persistent PID and logs. Poll it with `-Action Wait -WaitSeconds 480 -Json` until `FRESH`, never restart a `RUNNING` job, and then resume `configure-project.ps1`. An extension-only index is never sufficient.

The external documentation repository is the working root for `registry/`, `work-items/`, `wiki/`, and Git/PR operations. Ask neutrally for the project identifier and store it as `project_reference`; never invent it or require `G-xxx`. Configure creates the user-facing folders and root README from `templates/project-documentation/`; explain them using `docs/project-materials.md`. Users place source files in those folders and request an operation. Do not ask them to manage inbox, normalized registry or template-library records. Intake selected meeting/analysis files through the existing tool; import the selected Excel with `registry-import`, and accept templates through the shared library. Exclude folder README instructions from intake. A file's presence grants no approvals and does not trigger processing. Do not ask for a second upload of an already supplied file. After `fs-start`, use the work-item path returned by the CLI.

Finish with `scripts/flow1c.py doctor`. Setup is incomplete while RLM installation, endpoint health, or either source index reports `ERROR`. A Windows service installation is optional and may require administrator rights; the required default is the non-service RLM process in the project virtual environment.
Offer the optional catalog of document templates in one step after profile
selection. Follow docs/document-templates.md as a shared substep of this primary
skill. Accept a partial set, defer skipped types, persist per-template progress
and operation IDs in the setup checkpoint, and resume without asking again for
unchanged files. Import the legacy functional_spec_template only when the user
accepts that import. Empty templates do not block setup. Template-only setup
uses template-management/configure and does not require Git, RLM or 1C sources.

<!-- flow1c:routes:start -->
Generated from `config/intent-routes.json` and `config/stages.json`.

Interpret the user's goal, then check a RouteProposal before begin. A route grants no permissions.
CLI: `route-catalog --json`, `route-check --json-stdin` (direct proposal), then `agent-begin --json-stdin` (nested `route_proposal`).
OpenCode: `flow1c_route_catalog`, `flow1c_route_check(proposal_json)`, then `flow1c_begin(route_proposal_json)`; operation/mode/summary must match.
Minimal proposal shape: `{"schema_version":1,"expected_outcome":"user goal","operation":"consultation","mode":"explore","sources":[{"kind":"chat","version":"provided"}]}`. Replace operation/mode/sources for the actual request; `summary` and `route_id` are not proposal fields.
The pre-gate catalog returns `proposal_schema`; use it to correct invalid inputs without read/grep/bash. Do not retry the same invalid proposal unchanged.
Reuse saved answers; clarify one unresolved choice before gate. Do not launch subagents or a next formal stage automatically.
For large accepted documents use `flow1c_context(view=compact)` / `agent-context --view compact`. Read entries with `flow1c_context(action=read,entry_id=...,cursor=...)` / `context-read`; `scope` contains exact saved decisions and `index` lists every source/part. Continue cursors until mandatory coverage is complete. Summaries grant no evidence or permissions; changed sources require rebuilding on the same gate. Legacy full view remains the default.

| Operation | Mode → primary skill / role | Apply when | Exclude | Sources / output |
|---|---|---|---|---|
| setup | formal → flow1c-project-setup / — | Настрой Flow1C для проекта с выбранными путями.; Продолжи прерванную настройку проекта. | Обновление установленной версии; замена шаблона документа | chat, workflow, template_library / schemas/agent-gate.schema.json |
<!-- flow1c:routes:end -->
