---
description: Update Flow1C in place without re-entering local settings
agent: flow1c-update
---

Update this Flow1C checkout now while preserving its existing local configuration.

The authoritative update instructions are embedded in this request:

@.agents/skills/flow1c-project-update/SKILL.md
@docs/update.md

Do not run `/setup` and do not ask the user to reconfirm settings that pass validation.

Start `flow1c_begin` with `operation="update"` and `mode="formal"`, then run
`flow1c_action` with `action="update"` and `parameters_json='{"confirmed":true}'`.
The user's `/update` request authorizes this initial run. Use the JSON returned by
`flow1c_action` as the update result and call `flow1c_complete` without `output` only
after `READY`.
