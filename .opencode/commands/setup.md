---
description: Deterministically configure and verify the Flow1C environment
agent: flow1c-setup
---

Execute the Flow1C setup state machine now.

The authoritative setup instructions are embedded in this request, not left to optional skill routing:

@.agents/skills/flow1c-project-setup/SKILL.md
@docs/setup.md

Begin with the mandatory JSON audit from `scripts\setup-state.ps1`. Ask once for the target profile, recommend `analysis`, and never select `full` implicitly. Do not create a work item or invent a project/task reference. Do not return a list of commands for the user to run.

This audit was executed by the command runner before the model received the request:

!`PowerShell -ExecutionPolicy Bypass -File .\scripts\setup-state.ps1 -Json -Profile analysis`
After profile selection offer optional document templates from the configured
catalog in one step. Accept partial uploads, preserve operation IDs and skipped
decisions, and use flow1c_template per docs/document-templates.md. Do not block setup
on an empty library. Offer legacy DOCX import without asking for its file again.
