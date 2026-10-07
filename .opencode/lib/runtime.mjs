import { createHash } from "node:crypto"
import { existsSync, readFileSync } from "node:fs"
import path from "node:path"

// These files define the tools and permissions cached by an OpenCode instance.
const runtimeFiles = [
  ".opencode/tools/flow1c.ts", ".opencode/plugins/flow1c-guard.js",
  ".opencode/lib/runtime.mjs", ".opencode/agents/flow1c-controller.md",
  ".opencode/agents/flow1c-setup.md", ".opencode/agents/flow1c-update.md", "opencode.json",
  "config/intent-routes.json", "schemas/route-proposal.schema.json", "schemas/route-decision.schema.json",
  "schemas/agent-handoff.schema.json",
  "config/context.json", "schemas/context-manifest.schema.json",
]

function snapshot(root) {
  return new Map(runtimeFiles.map(file => {
    const target = path.join(root, file)
    return [file, existsSync(target) ? createHash("sha256").update(readFileSync(target)).digest("hex") : null]
  }))
}

export function isSavedUpdateContinuation(root, input) {
  // A cached adapter may finish only the already-authorized indexing/doctor
  // checkpoint. Other operations and changed external-update choices reload.
  try {
    const args = JSON.parse(input ?? "{}")
    if (args.action !== "update" || !/^[a-f0-9-]{8,64}$/i.test(args.gate_id ?? "")) return false
    const parameters = JSON.parse(args.parameters_json ?? "{}")
    if (parameters.confirmed !== true || parameters.new_run) return false
    const gate = JSON.parse(readFileSync(path.join(root, ".workspace/agent-gates", `${args.gate_id}.json`), "utf8"))
    if (gate.operation !== "update" || gate.state !== "WAITING_BACKGROUND") return false
    if (!/^[a-f0-9-]{36}$/i.test(gate.update_id ?? "")) return false
    if (parameters.update_id && parameters.update_id !== gate.update_id) return false
    const checkpoint = JSON.parse(readFileSync(path.join(root, ".workspace/updates", `${gate.update_id}.json`), "utf8").replace(/^\uFEFF/, ""))
    if (checkpoint.update_id !== gate.update_id || !["indexes", "doctor"].includes(checkpoint.phase)) return false
    for (const [snake, camel] of [["allow_external_updates", "allowExternalUpdates"], ["skip_external_tool_updates", "skipExternalToolUpdates"], ["repair_prerequisites", "repairPrerequisites"]]) {
      const requested = parameters[snake] ?? parameters[camel]
      if (requested !== undefined && requested !== checkpoint.options?.[snake]) return false
    }
    return true
  } catch { return false }
}

export function createRuntimeCheck(root) {
  const loaded = snapshot(root)
  return {
    assertCurrent() {
      const current = snapshot(root)
      const changedFiles = runtimeFiles.filter(file => current.get(file) !== loaded.get(file))
      if (!changedFiles.length) return
      throw new Error(JSON.stringify({
        state: "RESTART_REQUIRED", ready: false, code: "OPENCODE_RESTART_REQUIRED",
        component: "opencode-adapter", changed_files: changedFiles,
        next_action: "Fully quit and reopen OpenCode in the same project, then resume the saved gate_id/setup_id/operation_id. Do not substitute flow1c_action or bash for missing tools.",
        preserved_state: ["local configuration", "gates", "setup checkpoints", "template operations"],
      }))
    },
  }
}
