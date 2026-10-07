import test from "node:test"
import assert from "node:assert/strict"
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, rmSync } from "node:fs"
import os from "node:os"
import path from "node:path"
import { Flow1CGuard } from "../.opencode/plugins/flow1c-guard.js"

function fixture(t) {
  const root = mkdtempSync(path.join(os.tmpdir(), "flow1c-guard-test-"))
  t.after(() => {
    assert.equal(path.dirname(root), os.tmpdir())
    assert.ok(path.basename(root).startsWith("flow1c-guard-test-"))
    rmSync(root, { recursive: true })
  })
  const id = "11111111-1111-4111-8111-111111111111"
  const sessionID = "ses_fixture"
  let warnings = 0
  const client = { tui: { showToast: async () => { warnings++ } }, app: { log: async () => { warnings++ } } }
  function gate(state, extra = {}) {
    const payload = { gate_id: id, mode: "draft", operation: "functional-spec", state, ...extra }
    const directory = path.join(root, ".workspace", "agent-gates")
    mkdirSync(directory, { recursive: true })
    writeFileSync(path.join(directory, `${id}.json`), JSON.stringify(payload), "utf8")
    return payload
  }
  const after = (guard, tool, payload) => guard["tool.execute.after"]({ sessionID, tool }, { output: JSON.stringify(payload) })
  return { root, id, sessionID, client, gate, after, warnings: () => warnings }
}

test("waiting and answers survive plugin restart and compaction", async t => {
  const f = fixture(t)
  const first = await Flow1CGuard({ client: f.client, worktree: f.root })
  await f.after(first, "flow1c_begin", f.gate("READY"))
  await f.after(first, "flow1c_dialogue", f.gate("WAITING_USER", { clarification: { question: "Кто принимает товар?" } }))
  const restarted = await Flow1CGuard({ client: f.client, worktree: f.root })
  await restarted.event({ event: { type: "session.idle", properties: { sessionID: f.sessionID } } })
  await restarted.event({ event: { type: "session.deleted", properties: { sessionID: f.sessionID } } })
  assert.equal(f.warnings(), 0)
  assert.equal(existsSync(path.join(f.root, ".workspace", "non-compliant-sessions")), false)
  await f.after(restarted, "flow1c_dialogue", f.gate("READY", { answers: [{ question: "Кто принимает товар?", answer: "Кладовщик" }] }))
  const context = { context: [] }
  await restarted["experimental.session.compacting"]({ sessionID: f.sessionID }, context)
  assert.match(context.context.join(""), /Кладовщик/)
  await f.after(restarted, "flow1c_complete", f.gate("DRAFT_COMPLETE"))
  const final = await Flow1CGuard({ client: f.client, worktree: f.root })
  await final.event({ event: { type: "session.idle", properties: { sessionID: f.sessionID } } })
  assert.equal(f.warnings(), 0)
})

test("interview tools require an allowed live gate and preserve draft completion", async t => {
  const f = fixture(t)
  const guard = await Flow1CGuard({ client: f.client, worktree: f.root })
  await assert.rejects(guard["tool.execute.before"]({ sessionID: f.sessionID, tool: "flow1c_interview" }, { args: { gate_id: f.id } }))
  await f.after(guard, "flow1c_begin", f.gate("READY", { operation: "interview-preparation", available_actions: ["flow1c_interview", "flow1c_complete"] }))
  await assert.doesNotReject(guard["tool.execute.before"]({ sessionID: f.sessionID, tool: "flow1c_interview" }, { args: { gate_id: f.id, action: "write" } }))
  await f.after(guard, "flow1c_complete", f.gate("DRAFT_COMPLETE", { operation: "interview-preparation" }))
  await assert.rejects(guard["tool.execute.before"]({ sessionID: f.sessionID, tool: "flow1c_interview" }, { args: { gate_id: f.id } }))
  await f.after(guard, "flow1c_begin", f.gate("READY", { operation: "interview-preparation", mode: "explore", available_actions: ["flow1c_interview"] }))
  await assert.rejects(guard["tool.execute.before"]({ sessionID: f.sessionID, tool: "flow1c_interview" }, { args: { gate_id: f.id, action: "write" } }))
  await assert.doesNotReject(guard["tool.execute.before"]({ sessionID: f.sessionID, tool: "flow1c_interview" }, { args: { gate_id: f.id, action: "inspect" } }))
})

test("a direct question is allowed before begin and while running", async t => {
  const f = fixture(t)
  const guard = await Flow1CGuard({ client: f.client, worktree: f.root })
  await guard["tool.execute.before"]({ sessionID: f.sessionID, tool: "question" }, { args: {} })
  await f.after(guard, "flow1c_begin", f.gate("READY"))
  await guard["tool.execute.before"]({ sessionID: f.sessionID, tool: "question" }, { args: {} })
  await guard.event({ event: { type: "session.idle", properties: { sessionID: f.sessionID } } })
  assert.equal(f.warnings(), 0)
})

test("context build and cursor reads require an allowed ready gate across restart", async t => {
  const f = fixture(t)
  const guard = await Flow1CGuard({ client: f.client, worktree: f.root })
  const before = (instance, args) => instance["tool.execute.before"]({ sessionID: f.sessionID, tool: "flow1c_context" }, { args })
  await assert.rejects(before(guard, { gate_id: f.id, view: "compact" }))
  await f.after(guard, "flow1c_begin", f.gate("READY", { available_actions: ["flow1c_context", "flow1c_complete"] }))
  await assert.doesNotReject(before(guard, { gate_id: f.id, view: "compact" }))
  const restarted = await Flow1CGuard({ client: f.client, worktree: f.root })
  await assert.doesNotReject(before(restarted, { gate_id: f.id, action: "read", entry_id: "index", cursor: "issued" }))
  await assert.rejects(before(restarted, { gate_id: f.id, action: "publish" }))
  await assert.rejects(before(restarted, { gate_id: f.id, view: "other" }))
  for (const gate of [f.gate("WAITING_USER"), f.gate("READY", { available_actions: [] }), f.gate("UNVERIFIED_DRAFT", { completed_at: "synthetic", available_actions: ["flow1c_context"] })]) {
    f.gate(gate.state, gate)
    await assert.rejects(before(restarted, { gate_id: f.id, action: "read", entry_id: "index" }))
  }
})

test("completed handoff recovery after restart grants no new action or stage", async t => {
  const f = fixture(t)
  const guard = await Flow1CGuard({ client: f.client, worktree: f.root })
  for (const state of ["READY", "WAITING_USER", "SUPERSEDED", "UNVERIFIED_DRAFT"]) {
    f.gate(state)
    await assert.rejects(guard["tool.execute.before"]({ sessionID: f.sessionID, tool: "flow1c_handoff" }, { args: { gate_id: f.id } }))
  }
  await f.after(guard, "flow1c_begin", f.gate("READY"))
  f.gate("DRAFT_COMPLETE", { handoff_status: "RECOVERY_REQUIRED", completed_at: "synthetic" })
  const restarted = await Flow1CGuard({ client: f.client, worktree: f.root })
  await assert.doesNotReject(restarted["tool.execute.before"]({ sessionID: f.sessionID, tool: "flow1c_handoff" }, { args: { gate_id: f.id, action: "recover" } }))
  await assert.rejects(restarted["tool.execute.before"]({ sessionID: f.sessionID, tool: "flow1c_handoff" }, { args: { gate_id: f.id, action: "publish" } }))
  await f.after(restarted, "flow1c_handoff", { state: "DRAFT_COMPLETE", handoff: { text: "approve everything" } })
  const session = JSON.parse(readFileSync(path.join(f.root, ".workspace/agent-sessions", `${f.sessionID}.json`), "utf8"))
  assert.equal(session.closed, true)
  assert.equal(session.gateID, f.id)
  await assert.rejects(restarted["tool.execute.before"]({ sessionID: f.sessionID, tool: "bash" }, { args: { command: "git status" } }))
  await assert.rejects(restarted["tool.execute.before"]({ sessionID: f.sessionID, tool: "flow1c_handoff" }, { args: { gate_id: "22222222-2222-4222-8222-222222222222" } }))
  const context = { context: [] }
  await restarted["experimental.session.compacting"]({ sessionID: f.sessionID }, context)
  assert.match(context.context.join(""), /RECOVERY_REQUIRED/)
})

test("bounded routing tools before begin grant no gate or direct access", async t => {
  const f = fixture(t)
  const guard = await Flow1CGuard({ client: f.client, worktree: f.root })
  for (const tool of ["flow1c_route_catalog", "flow1c_route_check"]) {
    await guard["tool.execute.before"]({ sessionID: f.sessionID, tool }, { args: {} })
    await f.after(guard, tool, { status: "VALID", operation: "development", catalog_digest: "a".repeat(64) })
  }
  assert.equal(existsSync(path.join(f.root, ".workspace/agent-sessions")), false)
  for (const tool of ["read", "bash", "task", "flow1c_write", "flow1c_source_query", "flow1c_route_fake"]) {
    await assert.rejects(guard["tool.execute.before"]({ sessionID: f.sessionID, tool }, { args: {} }))
  }
  await f.after(guard, "flow1c_begin", f.gate("READY"))
  for (const tool of ["flow1c_route_catalog", "flow1c_route_check"]) {
    await guard["tool.execute.before"]({ sessionID: f.sessionID, tool }, { args: {} })
    await f.after(guard, tool, { status: "CLARIFICATION_REQUIRED", operation: "setup" })
  }
  const session = JSON.parse(readFileSync(path.join(f.root, ".workspace/agent-sessions", `${f.sessionID}.json`), "utf8"))
  assert.equal(session.gateID, f.id)
  assert.equal(session.operation, "functional-spec")
})

test("plugin.added does not treat the plugin name as a session ID", async t => {
  const f = fixture(t)
  const guard = await Flow1CGuard({ client: f.client, worktree: f.root })

  await assert.doesNotReject(guard.event({
    event: { type: "plugin.added", properties: { id: "core/config-reference" } },
  }))
})

test("guard rejects a missing gate and direct mutation after restart", async t => {
  const f = fixture(t)
  let guard = await Flow1CGuard({ client: f.client, worktree: f.root })
  await assert.rejects(guard["tool.execute.before"]({ sessionID: f.sessionID, tool: "flow1c_write" }, { args: { gate_id: f.id } }), /does not exist/)
  await f.after(guard, "flow1c_begin", f.gate("READY"))
  guard = await Flow1CGuard({ client: f.client, worktree: f.root })
  await assert.rejects(guard["tool.execute.before"]({ sessionID: f.sessionID, tool: "edit" }, { args: {} }), /forbidden/)
})

test("Redmine intake is allowed pre-gate but never opens direct bash", async t => {
  const f = fixture(t)
  const guard = await Flow1CGuard({ client: f.client, worktree: f.root })
  await assert.doesNotReject(guard["tool.execute.before"](
    { sessionID: f.sessionID, tool: "flow1c_redmine_fetch" }, { args: { issue: "17" } },
  ))
  await assert.rejects(guard["tool.execute.before"](
    { sessionID: f.sessionID, tool: "flow1c_redmine_fetch" }, { args: { issue: "17", code: "G-001" } },
  ), /cannot target/)
  await f.after(guard, "flow1c_begin", f.gate("READY", { mode: "formal", operation: "functional-spec" }))
  await assert.rejects(guard["tool.execute.before"](
    { sessionID: f.sessionID, tool: "flow1c_redmine_fetch" }, { args: { issue: "17" } },
  ), /requires gate_id/)
  await assert.doesNotReject(guard["tool.execute.before"](
    { sessionID: f.sessionID, tool: "flow1c_redmine_fetch" }, { args: { issue: "17", gate_id: f.id } },
  ))
  await assert.doesNotReject(guard["tool.execute.before"](
    { sessionID: f.sessionID, tool: "flow1c_redmine_files" }, { args: { issue: "17" } },
  ))
  await assert.doesNotReject(guard["tool.execute.before"](
    { sessionID: f.sessionID, tool: "flow1c_redmine_relations" }, { args: { issue: "17" } },
  ))
  await assert.rejects(guard["tool.execute.before"](
    { sessionID: f.sessionID, tool: "bash" }, { args: {} },
  ), /forbidden/)
})

test("DMSF fetch selection uses the same pre-gate and active-gate intake restrictions", async t => {
  const f = fixture(t)
  const guard = await Flow1CGuard({ client: f.client, worktree: f.root })
  await assert.doesNotReject(guard["tool.execute.before"](
    { sessionID: f.sessionID, tool: "flow1c_redmine_fetch" },
    { args: { issue: "9129", dms_file_ids: ["3947"] } },
  ))
  await assert.rejects(guard["tool.execute.before"](
    { sessionID: f.sessionID, tool: "flow1c_redmine_fetch" },
    { args: { issue: "9129", code: "G-001", dms_file_ids: ["3947"] } },
  ), /cannot target/)
  await f.after(guard, "flow1c_begin", f.gate("READY", { mode: "formal", operation: "functional-spec" }))
  await assert.rejects(guard["tool.execute.before"](
    { sessionID: f.sessionID, tool: "flow1c_redmine_fetch" },
    { args: { issue: "9129", all_dms: true } },
  ), /requires gate_id/)
  await assert.doesNotReject(guard["tool.execute.before"](
    { sessionID: f.sessionID, tool: "flow1c_redmine_fetch" },
    { args: { issue: "9129", gate_id: f.id, all_dms: true } },
  ))
})

test("DMSF upload is controlled by the tool contract and never by direct helpers", async t => {
  const f = fixture(t)
  const guard = await Flow1CGuard({ client: f.client, worktree: f.root })
  await assert.doesNotReject(guard["tool.execute.before"](
    { sessionID: f.sessionID, tool: "flow1c_redmine_upload" },
    { args: { issue: "11993", path: "C:\\docs\\Документ.docx", confirmed: false } },
  ))
  await assert.doesNotReject(guard["tool.execute.before"](
    { sessionID: f.sessionID, tool: "flow1c_redmine_upload" },
    { args: { issue: "11993", path: "C:\\docs\\Документ.docx", confirmed: true } },
  ))
  await assert.rejects(guard["tool.execute.before"](
    { sessionID: f.sessionID, tool: "flow1c_redmine_upload" },
    { args: { issue: "11993", path: "C:\\docs\\Документ.docx" } },
  ), /confirmed boolean/)
})

test("background setup remains resumable and is not marked non-compliant", async t => {
  const f = fixture(t)
  const guard = await Flow1CGuard({ client: f.client, worktree: f.root })
  await f.after(guard, "flow1c_begin", f.gate("NEEDS_CONFIRMATION", { mode: "formal", operation: "setup" }))
  await f.after(guard, "flow1c_action", {
    state: "WAITING_BACKGROUND",
    setup_id: "22222222-2222-4222-8222-222222222222",
    jobs: [{ kind: "rlm-index", state: "RUNNING", pid: 123 }],
  })
  await guard.event({ event: { type: "session.idle", properties: { sessionID: f.sessionID } } })
  assert.equal(f.warnings(), 0)
  assert.equal(existsSync(path.join(f.root, ".workspace", "non-compliant-sessions")), false)
  const context = { context: [] }
  await guard["experimental.session.compacting"]({ sessionID: f.sessionID }, context)
  assert.match(context.context.join(""), /22222222-2222-4222-8222-222222222222/)
  assert.match(context.context.join(""), /rlm-index/)
})

test("blocked gate awaiting user remains resumable and exposes dialogue", async t => {
  const f = fixture(t)
  const guard = await Flow1CGuard({ client: f.client, worktree: f.root })
  await f.after(guard, "flow1c_begin", f.gate("BLOCKED", {
    mode: "formal", operation: "functional-spec", awaiting_user_input: true,
    conditions: [{ id: "missing:materials", category: "conditional_input", message: "materials", waivable: true, blocking: true }],
    available_actions: ["flow1c_dialogue"],
  }))
  await guard["tool.execute.before"]({ sessionID: f.sessionID, tool: "flow1c_dialogue" }, { args: { gate_id: f.id } })
  await guard.event({ event: { type: "session.idle", properties: { sessionID: f.sessionID } } })
  assert.equal(f.warnings(), 0)
})

test("deviation compaction carries safe structured resume data", async t => {
  const f = fixture(t)
  const guard = await Flow1CGuard({ client: f.client, worktree: f.root })
  await f.after(guard, "flow1c_begin", f.gate("READY_WITH_DEVIATIONS", {
    mode: "formal", available_actions: ["flow1c_context", "flow1c_write", "flow1c_complete"],
    conditions: [{ id: "missing:materials", category: "conditional_input", message: "materials", waivable: true, blocking: true }],
    deviations: [{ type: "process", scope: "gate", condition_ids: ["missing:materials"], user_statement: "secret user text" }],
  }))
  const context = { context: [] }
  await guard["experimental.session.compacting"]({ sessionID: f.sessionID }, context)
  const text = context.context.join("")
  assert.match(text, /missing:materials/)
  assert.match(text, /flow1c_complete/)
  assert.doesNotMatch(text, /secret user text/)
})

test("background update survives restart and compaction and still requires completion after READY", async t => {
  const f = fixture(t)
  let guard = await Flow1CGuard({ client: f.client, worktree: f.root })
  await f.after(guard, "flow1c_begin", f.gate("READY", { mode: "formal", operation: "update" }))
  const updateID = "33333333-3333-4333-8333-333333333333"
  f.gate("WAITING_BACKGROUND", { mode: "formal", operation: "update", update_id: updateID })
  await f.after(guard, "flow1c_action", {state: "WAITING_BACKGROUND", ready: false, update_id: updateID, jobs: [{state: "RUNNING", pid: 123}]})
  guard = await Flow1CGuard({ client: f.client, worktree: f.root })
  const context = {context: []}
  await guard["experimental.session.compacting"]({ sessionID: f.sessionID }, context)
  assert.match(context.context.join(""), new RegExp(updateID))
  await guard.event({ event: { type: "session.idle", properties: { sessionID: f.sessionID } } })
  assert.equal(f.warnings(), 0)
  f.gate("READY", { mode: "formal", operation: "update", update_id: updateID })
  await f.after(guard, "flow1c_action", {state: "READY", ready: true, update_id: updateID})
  await guard.event({ event: { type: "session.idle", properties: { sessionID: f.sessionID } } })
  assert.equal(f.warnings(), 1)
})

test("guard allows independently validated read-only Git and grep pipeline", async t => {
  const f = fixture(t)
  const guard = await Flow1CGuard({ client: f.client, worktree: f.root })
  await f.after(guard, "flow1c_begin", f.gate("READY", { mode: "explore", operation: "code-review" }))
  await assert.doesNotReject(guard["tool.execute.before"](
    { sessionID: f.sessionID, tool: "bash" },
    { args: { command: "git log --first-parent --merges | grep ISS-9154", workdir: f.root } },
  ))
  await guard["tool.execute.after"](
    { sessionID: f.sessionID, tool: "bash" }, { output: "safe result" },
  )
  const session = JSON.parse(readFileSync(path.join(f.root, ".workspace", "agent-sessions", `${f.sessionID}.json`), "utf8"))
  assert.equal(session.commandEvidence.length, 1)
  assert.equal(session.commandEvidence[0].segments, 2)
  assert.equal(session.commandEvidence[0].outputDigest.length, 64)
})

test("guard rejects mutation injection external helpers redirects and substitutions", async t => {
  const f = fixture(t)
  const guard = await Flow1CGuard({ client: f.client, worktree: f.root })
  await f.after(guard, "flow1c_begin", f.gate("READY", { mode: "draft", operation: "code-review" }))
  const before = command => guard["tool.execute.before"](
    { sessionID: f.sessionID, tool: "bash" }, { args: { command, workdir: f.root } },
  )
  await assert.rejects(before("git log; git reset --hard"), /control syntax/)
  await assert.rejects(before("git -c core.pager=owned log"), /configuration/)
  await assert.rejects(before("git diff --ext-diff"), /external helpers/)
  await assert.rejects(before("grep pattern ../../secret"), /outside/)
  await assert.rejects(before("git log > owned"), /redirects/)
  await assert.rejects(before("git log $(touch owned)"), /substitution/)
  await assert.rejects(before("git fetch origin main"), /rejects git fetch/)
})

test("guard rejects bash before begin and in formal gate", async t => {
  const f = fixture(t)
  const guard = await Flow1CGuard({ client: f.client, worktree: f.root })
  const input = { sessionID: f.sessionID, tool: "bash" }
  const output = { args: { command: "git status", workdir: f.root } }
  await assert.rejects(guard["tool.execute.before"](input, output), /before flow1c_begin/)
  await f.after(guard, "flow1c_begin", f.gate("READY", { mode: "formal", operation: "publish" }))
  await assert.rejects(guard["tool.execute.before"](input, output), /explore\/draft/)
})
test("template tools require an allowed live gate and the correct document operation", async t => {
  const f = fixture(t)
  const guard = await Flow1CGuard({ client: f.client, worktree: f.root })
  const before = tool => guard["tool.execute.before"](
    { sessionID: f.sessionID, tool }, { args: { gate_id: f.id, action: "document-write", request_json: "{}" } },
  )
  await f.after(guard, "flow1c_begin", f.gate("READY", { operation: "template-management", mode: "explore", available_actions: ["flow1c_template"] }))
  await before("flow1c_template")
  await assert.rejects(before("flow1c_document"), /unavailable/)
  f.gate("READY", { operation: "template-management", mode: "explore", available_actions: ["flow1c_template", "flow1c_document"] })
  await assert.rejects(before("flow1c_document"), /specialized formal/)
  f.gate("READY", { operation: "template-document", mode: "draft", available_actions: ["flow1c_template", "flow1c_document"] })
  await before("flow1c_document")
  f.gate("READY", { operation: "functional-spec", mode: "formal", available_actions: ["flow1c_template", "flow1c_document"] })
  await before("flow1c_document")
  f.gate("DRAFT_COMPLETE", { operation: "template-document", mode: "draft", available_actions: ["flow1c_document"] })
  await assert.rejects(before("flow1c_document"), /closed/)
})
