import { existsSync, mkdirSync, readFileSync, writeFileSync, renameSync, realpathSync } from "node:fs"
import path from "node:path"
import { createHash } from "node:crypto"

const forbidden = new Set([
  "read", "glob", "list", "edit", "write", "apply_patch", "task", "webfetch", "websearch",
])

const readOnlyGit = new Set(["status", "log", "show", "diff", "diff-tree", "rev-list", "rev-parse", "merge-base",
  "for-each-ref", "branch", "ls-tree", "name-rev", "remote"])
const mutatingGit = new Set(["add", "commit", "push", "pull", "fetch", "checkout", "switch", "restore", "merge",
  "rebase", "cherry-pick", "revert", "reset", "clean", "stash", "config", "worktree", "tag"])
const deniedFlags = new Set(["--ext-diff", "--textconv", "--exec", "--upload-pack", "--receive-pack"])

function tokenizeSegment(segment) {
  const tokens = []
  let token = "", quote = null, escaping = false
  for (const character of segment) {
    if (escaping) { token += character; escaping = false; continue }
    if (character === "\\" && quote !== "'") { escaping = true; continue }
    if (quote) {
      if (character === quote) quote = null
      else token += character
      continue
    }
    if (character === "'" || character === '"') { quote = character; continue }
    if (/\s/.test(character)) { if (token) { tokens.push(token); token = "" }; continue }
    token += character
  }
  if (quote || escaping) throw new Error("FLOW1C Git guard rejects malformed shell quoting")
  if (token) tokens.push(token)
  return tokens
}

function splitPipeline(command) {
  if (!command || typeof command !== "string") throw new Error("FLOW1C Git guard requires a command string")
  let quote = null, escaping = false, current = ""
  const segments = []
  for (let index = 0; index < command.length; index++) {
    const character = command[index]
    const next = command[index + 1] ?? ""
    if (escaping) { current += character; escaping = false; continue }
    if (character === "\\" && quote !== "'") { current += character; escaping = true; continue }
    if (quote) { current += character; if (character === quote) quote = null; continue }
    if (character === "'" || character === '"') { current += character; quote = character; continue }
    if (character === "$" && ["(", "{"].includes(next)) throw new Error("FLOW1C Git guard rejects shell substitution")
    if (character === "`" || character === ";" || character === "\n" || character === "\r"
        || character === ">" || character === "<" || character === "(" || character === ")") {
      throw new Error("FLOW1C Git guard rejects shell control syntax and redirects")
    }
    if ((character === "&" && next === "&") || (character === "|" && next === "|")) {
      throw new Error("FLOW1C Git guard rejects chained shell commands")
    }
    if (character === "|") { segments.push(current.trim()); current = ""; continue }
    current += character
  }
  if (quote) throw new Error("FLOW1C Git guard rejects malformed shell quoting")
  segments.push(current.trim())
  if (segments.some(segment => !segment) || segments.length > 2) throw new Error("FLOW1C Git guard permits at most two nonempty pipeline segments")
  return segments
}

function containedPath(root, candidate) {
  const resolved = path.resolve(root, candidate)
  const relative = path.relative(root, resolved)
  return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative))
}

function validatePathTokens(tokens, cwd, start) {
  for (const token of tokens.slice(start)) {
    if (token === "--" || token.startsWith("-") || /^[0-9a-f]{7,64}(\.\.[0-9a-f]{7,64})?$/i.test(token)) continue
    if (!token.startsWith("../") && !token.startsWith("..\\") && /^[\w./@{}^~:-]+\.\.[\w./@{}^~:-]+$/.test(token)) continue
    if (token.includes("../") || token.includes("..\\") || path.isAbsolute(token)) {
      if (!containedPath(cwd, token)) throw new Error("FLOW1C Git guard rejects paths outside the allowed repository")
    }
  }
}

function validateSegment(segment, cwd) {
  const tokens = tokenizeSegment(segment)
  if (!tokens.length || /^[A-Za-z_][A-Za-z0-9_]*=/.test(tokens[0])) throw new Error("FLOW1C Git guard rejects environment assignments")
  const executable = path.basename(tokens[0]).toLowerCase().replace(/\.exe$/, "")
  if (executable === "git") {
    if (tokens[1] === "-c" || tokens.some(token => token === "-c" || deniedFlags.has(token) || token.startsWith("--config-env"))) {
      throw new Error("FLOW1C Git guard rejects Git configuration and external helpers")
    }
    const subcommand = tokens[1]
    if (!subcommand || mutatingGit.has(subcommand) || !readOnlyGit.has(subcommand)) throw new Error(`FLOW1C Git guard rejects git ${subcommand ?? ""}`)
    if (subcommand === "remote" && !((tokens.length === 3 && tokens[2] === "-v") || (tokens[2] === "get-url" && tokens.length === 4))) {
      throw new Error("FLOW1C Git guard allows only git remote -v/get-url")
    }
    if (subcommand === "branch" && tokens.some(token => ["-d", "-D", "-f", "--delete", "--force", "-m", "-M", "-c", "-C"].includes(token))) {
      throw new Error("FLOW1C Git guard rejects mutating git branch flags")
    }
    validatePathTokens(tokens, cwd, 2)
    return
  }
  if (executable === "grep" || executable === "rg") {
    if (tokens.some(token => token === "--pre" || token.startsWith("--pre=") || token === "--files-from")) {
      throw new Error("FLOW1C Git guard rejects external grep helpers")
    }
    validatePathTokens(tokens, cwd, 1)
    return
  }
  throw new Error(`FLOW1C Git guard rejects executable ${executable}`)
}

const closedStates = new Set(["COMPLETE", "COMPLETE_WITH_DEVIATIONS", "CONSULTATION_COMPLETE", "DRAFT_COMPLETE", "SUPERSEDED"])
function readRecord(file) {
  try { return JSON.parse(readFileSync(file, "utf8")) } catch { return null }
}
function writeRecord(file, value) {
  mkdirSync(path.dirname(file), { recursive: true })
  const temporary = `${file}.${process.pid}.tmp`
  writeFileSync(temporary, JSON.stringify(value, null, 2) + "\n", "utf8")
  renameSync(temporary, file)
}

function resultText(output) {
  if (typeof output?.output === "string") return output.output
  if (Array.isArray(output?.content)) {
    return output.content.filter((part) => part?.type === "text").map((part) => part.text).join("\n")
  }
  return ""
}

function resultJson(output) {
  try {
    return JSON.parse(resultText(output))
  } catch {
    return null
  }
}

function sessionIDFromEvent(event) {
  return event?.properties?.sessionID ?? event?.properties?.info?.id ?? null
}

export const Flow1CGuard = async ({ client, worktree }) => {
  const sessions = new Map()
  function sessionFile(id) {
    if (!/^[a-zA-Z0-9_-]+$/.test(id)) throw new Error("Invalid session ID")
    return path.join(worktree, ".workspace", "agent-sessions", `${id}.json`)
  }
  function getSession(id) {
    const state = sessions.get(id) ?? readRecord(sessionFile(id))
    if (state) {
      const gate = readRecord(path.join(worktree, ".workspace", "agent-gates", `${state.gateID}.json`))
      if (gate) {
        state.gateState = gate.state
        state.waitingForUser = gate.awaiting_user_input === true || gate.state === "WAITING_USER" || state.waitingForUser === true
        if (closedStates.has(gate.state)) state.closed = true
      }
      sessions.set(id, state)
    }
    return state
  }
  function saveSession(id, state) {
    sessions.set(id, state)
    writeRecord(sessionFile(id), state)
  }
  return ({
  "tool.execute.before": async (input, output) => {
    const activeSession = getSession(input.sessionID)
    if (forbidden.has(input.tool)) {
      throw new Error(`Flow1C permits only gated flow1c_* tools; direct ${input.tool} is forbidden`)
    }
    if (["bash", "grep"].includes(input.tool)) {
      if (!activeSession) throw new Error(`Flow1C rejects ${input.tool} before flow1c_begin`)
      const gate = readRecord(path.join(worktree, ".workspace", "agent-gates", `${activeSession.gateID}.json`))
      if (!gate || !["explore", "draft"].includes(gate.mode) || closedStates.has(gate.state)) {
        throw new Error(`Flow1C forbidden: ${input.tool} is permitted only in an active explore/draft gate`)
      }
      const local = readRecord(path.join(worktree, ".flow1c.local.json")) ?? {}
      const allowedRoots = [worktree, local.extension_path].filter(Boolean).map(value => {
        try { return realpathSync(path.resolve(String(value))) } catch { return path.resolve(String(value)) }
      })
      const requestedCwd = String(output.args?.workdir ?? output.args?.cwd ?? worktree)
      let canonicalCwd
      try { canonicalCwd = realpathSync(path.resolve(requestedCwd)) } catch { canonicalCwd = path.resolve(requestedCwd) }
      if (!allowedRoots.includes(canonicalCwd)) throw new Error("FLOW1C Git guard rejects a working directory outside configured repositories")
      if (input.tool === "bash") {
        const command = String(output.args?.command ?? output.args?.cmd ?? "")
        const segments = splitPipeline(command)
        for (const segment of segments) validateSegment(segment, canonicalCwd)
        activeSession.pendingCommand = {
          fingerprint: createHash("sha256").update(JSON.stringify({ cwd: canonicalCwd, segments })).digest("hex"),
          segments: segments.length,
        }
      } else {
        const searchPath = String(output.args?.path ?? output.args?.include ?? ".")
        if (!containedPath(canonicalCwd, searchPath)) throw new Error("FLOW1C Git guard rejects grep outside the allowed repository")
        activeSession.pendingCommand = {
          fingerprint: createHash("sha256").update(JSON.stringify({ tool: "grep", cwd: canonicalCwd,
            pattern: String(output.args?.pattern ?? ""), path: searchPath })).digest("hex"),
          segments: 1,
        }
      }
      saveSession(input.sessionID, activeSession)
    }
    if (input.tool === "flow1c_redmine_fetch") {
      const gateID = String(output.args?.gate_id ?? "")
      const code = String(output.args?.code ?? "")
      if (activeSession && !gateID) throw new Error("An active FLOW1C session requires gate_id for Redmine intake")
      if (!gateID && code) throw new Error("Pre-gate Redmine intake cannot target a work item")
    }
    if (input.tool === "flow1c_redmine_upload" && typeof output.args?.confirmed !== "boolean") {
      throw new Error("Redmine DMSF upload requires an explicit confirmed boolean")
    }
    if (input.tool === "question") {
      const state = getSession(input.sessionID)
      if (state) saveSession(input.sessionID, { ...state, waitingForUser: true })
    }
    if (input.tool.startsWith("flow1c_") && input.tool !== "flow1c_begin"
        && !(input.tool === "flow1c_redmine_fetch" && !output.args?.gate_id)
        && input.tool !== "flow1c_redmine_files"
        && input.tool !== "flow1c_redmine_relations"
        && input.tool !== "flow1c_route_catalog"
        && input.tool !== "flow1c_route_check"
        && input.tool !== "flow1c_redmine_upload") {
      const gateID = String(output.args?.gate_id ?? "")
      if (!/^[a-f0-9-]{8,64}$/i.test(gateID)) throw new Error("A valid gate_id is required")
      const gatePath = path.join(worktree, ".workspace", "agent-gates", `${gateID}.json`)
      if (!existsSync(gatePath)) throw new Error("The supplied FLOW1C gate does not exist")
      if (input.tool === "flow1c_handoff") {
        const gate = readRecord(gatePath)
        const completedDraft = gate?.state === "UNVERIFIED_DRAFT" && !!gate?.completed_at
        if (!gate || !(closedStates.has(gate.state) && gate.state !== "SUPERSEDED" || completedDraft)) {
          throw new Error("Handoff requires a saved completed gate")
        }
        if (![undefined, "read", "recover"].includes(output.args?.action)) throw new Error("Invalid handoff action")
      }
      if (input.tool === "flow1c_interview") {
        const gate = readRecord(gatePath)
        if (!gate || !["READY", "READY_WITH_DEVIATIONS", "UNVERIFIED_DRAFT"].includes(gate.state)) throw new Error("Interview gate is closed or waiting for input")
        if (gate.operation !== "interview-preparation" || !gate.available_actions?.includes(input.tool)) throw new Error("Interview tool is unavailable for this gate")
        if (output.args?.action === "write" && gate.mode !== "draft") throw new Error("Interview workbook writes require draft mode")
      }
      if (input.tool === "flow1c_context") {
        const gate = readRecord(gatePath)
        if (!gate || gate.completed_at || !["READY", "READY_WITH_DEVIATIONS", "UNVERIFIED_DRAFT"].includes(gate.state)) throw new Error("Context requires an active ready gate")
        if (!gate.available_actions?.includes(input.tool)) throw new Error("Context tool is unavailable for this gate")
        if (![undefined, "build", "read"].includes(output.args?.action)) throw new Error("Invalid context action")
        if (![undefined, "full", "compact"].includes(output.args?.view)) throw new Error("Invalid context view")
      }
      if (["flow1c_template", "flow1c_document"].includes(input.tool)) {
        const gate = readRecord(gatePath)
        if (!gate || closedStates.has(gate.state)) throw new Error("Template gate is closed or invalid")
        if (!gate.available_actions?.includes(input.tool)) throw new Error("Template tool is unavailable for this gate")
        const draftDocument = gate.operation === "template-document" && gate.mode === "draft"
        const formalDocument = ["functional-spec", "testing"].includes(gate.operation) && gate.mode === "formal"
        if (input.tool === "flow1c_document" && !draftDocument && !formalDocument) {
          throw new Error("Document writes require a pinned draft or specialized formal document gate")
        }
      }
      const state = getSession(input.sessionID)
      if (state && state.gateID !== gateID) throw new Error("Gate belongs to another active request")
    }
  },

  "tool.execute.after": async (input, output) => {
    const payload = resultJson(output)
    if (input.tool === "flow1c_begin" && payload?.gate_id) {
      saveSession(input.sessionID, {
        gateID: payload.gate_id,
        operation: payload.operation,
        mode: payload.mode ?? "formal",
        waitingForUser: payload.awaiting_user_input === true || payload.state === "WAITING_USER",
        gateState: payload.state,
        setupID: payload.setup_id ?? null,
        closed: false,
      })
      return
    }
    const state = getSession(input.sessionID)
    if (!state) return
    if (["bash", "grep"].includes(input.tool) && state.pendingCommand) {
      const text = resultText(output)
      state.commandEvidence = [...(state.commandEvidence ?? []), {
        ...state.pendingCommand,
        outputDigest: createHash("sha256").update(text.slice(0, 100000)).digest("hex"),
        recordedAt: new Date().toISOString(),
      }].slice(-50)
      delete state.pendingCommand
    }
    if (input.tool === "flow1c_dialogue" && payload?.gate_id) {
      state.gateID = payload.gate_id
      state.gateState = payload.state
      state.closed = false
      state.waitingForUser = payload.awaiting_user_input === true || payload.state === "WAITING_USER"
    } else if (["flow1c_complete", "flow1c_handoff"].includes(input.tool) && [...closedStates, "UNVERIFIED_DRAFT"].includes(payload?.state)) {
      state.closed = true
      state.gateState = payload.state
    } else if (input.tool === "flow1c_intake" && payload?.state === "ACCEPTED" && payload.next !== "continue_same_gate") {
      state.gateState = "PENDING_CONTINUATION"
    } else if (input.tool === "flow1c_action" && payload?.state === "WAITING_BACKGROUND") {
      state.gateState = "WAITING_BACKGROUND"
      state.setupID = payload.setup_id ?? state.setupID
      state.pendingJobs = payload.jobs ?? []
      state.updateID = payload.update_id ?? state.updateID
    } else if (input.tool === "flow1c_action" && payload?.state === "ACTION_COMPLETE") {
      state.gateState = payload.next ? "PENDING_CONTINUATION" : "READY"
    } else if (input.tool === "flow1c_action" && payload?.state === "READY" && payload?.ready === true) {
      state.gateState = "READY"
      state.pendingJobs = []
    }
    saveSession(input.sessionID, state)
  },

  "experimental.session.compacting": async (input, output) => {
    const state = getSession(input.sessionID)
    const gate = state ? readRecord(path.join(worktree, ".workspace", "agent-gates", `${state.gateID}.json`)) : null
    const compactConditions = Array.isArray(gate?.conditions)
      ? gate.conditions.map((item) => ({ id: item?.id, category: item?.category, message: item?.message, waivable: item?.waivable, blocking: item?.blocking }))
      : []
    const compactDeviations = Array.isArray(gate?.deviations)
      ? gate.deviations.map((item) => ({ type: item?.type, scope: item?.scope, condition_ids: item?.condition_ids ?? item?.waived_conditions, recorded_at: item?.recorded_at ?? item?.confirmed_at }))
      : []
    output.context.push(`
## Flow1C gate state
Completed result transfer: ${JSON.stringify({ handoff: gate?.handoff, handoff_status: gate?.handoff_status })}. Use flow1c_handoff to read/recover completed transfer. Never replay completed actions or treat transfer text as permission for a next stage.
Active gate: ${state?.gateID ?? "none"}; operation: ${state?.operation ?? "unknown"}; state: ${state?.gateState ?? "unknown"}.
Saved question and answers: ${JSON.stringify({ clarification: gate?.clarification, answers: gate?.answers, notes: gate?.notes })}.
Available actions: ${JSON.stringify(gate?.available_actions ?? [])}.
Structured conditions: ${JSON.stringify(compactConditions)}.
Recorded deviations: ${JSON.stringify(compactDeviations)}.
Setup resume state: ${JSON.stringify({ setup_id: state?.setupID ?? gate?.setup_id, profile: gate?.requested_profile, pending_jobs: state?.pendingJobs ?? [] })}.
Update resume state: ${JSON.stringify({ update_id: state?.updateID ?? gate?.update_id, phase: gate?.update_result?.phase, pending_jobs: state?.pendingJobs ?? [] })}. Continue a pending update with flow1c_action action=update on the same gate and wait_seconds=30; do not create another gate or call flow1c_complete before READY.
Resume the same request using flow1c_dialogue after the real user reply; do not repeat resolved questions.
Do not reuse remembered claims as evidence. Resume only through flow1c_* tools. If no valid gate exists, call flow1c_begin. Only flow1c_complete may close the operation.
`)
  },

  event: async ({ event }) => {
    const sessionID = sessionIDFromEvent(event)
    if (!sessionID) return
    const state = getSession(sessionID)
    if (!state || state.closed || state.waitingForUser) return
    const shouldWarn = state.gateState === "READY" || state.gateState === "PENDING_CONTINUATION"
    if (event.type === "session.idle" && shouldWarn) {
      try {
        await client.tui.showToast({
          body: {
            title: "Flow1C: NON_COMPLIANT",
            message: `Операция ${state.operation} остановлена без успешного flow1c_complete. Gate: ${state.gateID}`,
            variant: "warning",
            duration: 10000,
          },
        })
      } catch {
        await client.app.log({ body: { service: "flow1c-guard", level: "warn", message: `NON_COMPLIANT ${state.operation} ${state.gateID}` } })
      }
    }
    if (shouldWarn && ["session.deleted", "session.idle"].includes(event.type)) {
      const directory = path.join(worktree, ".workspace", "non-compliant-sessions")
      mkdirSync(directory, { recursive: true })
      writeRecord(path.join(directory, `${sessionID}.json`), {
        schema_version: 1,
        session_id: sessionID,
        gate_id: state.gateID,
        operation: state.operation,
        state: "NON_COMPLIANT",
        reason: event.type === "session.deleted" ? "session ended without successful flow1c_complete" : "agent became idle while continuation was mandatory",
        recorded_at: new Date().toISOString(),
      })
    }
  },
})
}
