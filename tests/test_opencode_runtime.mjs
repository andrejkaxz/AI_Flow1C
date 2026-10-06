import test from "node:test"
import assert from "node:assert/strict"
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync } from "node:fs"
import os from "node:os"
import path from "node:path"
import { createRuntimeCheck, isSavedUpdateContinuation } from "../.opencode/lib/runtime.mjs"

function fixture(t) {
  const root = mkdtempSync(path.join(os.tmpdir(), "flow1c-runtime-"))
  t.after(() => rmSync(root, { recursive: true, force: true }))
  mkdirSync(path.join(root, ".opencode/tools"), { recursive: true })
  writeFileSync(path.join(root, ".opencode/tools/flow1c.ts"), "old tools")
  return root
}

test("a cached adapter rejects changed tool definitions without changing checkpoints", t => {
  const root = fixture(t)
  const runtime = createRuntimeCheck(root)
  runtime.assertCurrent()
  const checkpoint = path.join(root, "setup.json")
  writeFileSync(checkpoint, '{"setup_id":"saved","operation_id":"saved-import"}')
  writeFileSync(path.join(root, ".opencode/tools/flow1c.ts"), "new template tools")
  assert.throws(() => runtime.assertCurrent(), error => {
    const result = JSON.parse(error.message)
    assert.equal(result.code, "OPENCODE_RESTART_REQUIRED")
    assert.equal(result.ready, false)
    assert.deepEqual(result.changed_files, [".opencode/tools/flow1c.ts"])
    assert.match(result.next_action, /resume the saved gate_id\/setup_id\/operation_id/)
    return true
  })
  assert.equal(readFileSync(checkpoint, "utf8"), '{"setup_id":"saved","operation_id":"saved-import"}')
  // Reloading the runtime accepts the updated files.
  createRuntimeCheck(root).assertCurrent()
})

test("configuration additions and deletions require a reload; user data changes do not", t => {
  const root = fixture(t)
  const runtime = createRuntimeCheck(root)
  writeFileSync(path.join(root, ".flow1c.local.json"), '{"documentation_path":"user docs"}')
  runtime.assertCurrent()
  writeFileSync(path.join(root, "opencode.json"), "{}")
  assert.throws(() => runtime.assertCurrent(), /OPENCODE_RESTART_REQUIRED/)
  const reloaded = createRuntimeCheck(root)
  rmSync(path.join(root, ".opencode/tools/flow1c.ts"))
  assert.throws(() => reloaded.assertCurrent(), /OPENCODE_RESTART_REQUIRED/)
})

test("a saved index continuation may finish after adapter changes without authorizing new updates", t => {
  const root = fixture(t)
  const gateID = "22222222-2222-4222-8222-222222222222"
  const updateID = "33333333-3333-4333-8333-333333333333"
  mkdirSync(path.join(root, ".workspace/agent-gates"), { recursive: true })
  mkdirSync(path.join(root, ".workspace/updates"), { recursive: true })
  writeFileSync(path.join(root, ".workspace/agent-gates", `${gateID}.json`), JSON.stringify({operation: "update", state: "WAITING_BACKGROUND", update_id: updateID}))
  writeFileSync(path.join(root, ".workspace/updates", `${updateID}.json`), "\uFEFF" + JSON.stringify({update_id: updateID, phase: "indexes", options: {allow_external_updates: false}}))
  const input = parameters => JSON.stringify({action: "update", gate_id: gateID, parameters_json: JSON.stringify(parameters)})
  assert.equal(isSavedUpdateContinuation(root, input({confirmed: true, wait_seconds: 30})), true)
  for (const parameters of [{confirmed: false}, {confirmed: true, new_run: true}, {confirmed: true, allow_external_updates: true}, {confirmed: true, allowExternalUpdates: true}, {confirmed: true, update_id: "different"}]) {
    assert.equal(isSavedUpdateContinuation(root, input(parameters)), false)
  }
  assert.equal(isSavedUpdateContinuation(root, JSON.stringify({action: "publish", gate_id: gateID})), false)
  assert.equal(isSavedUpdateContinuation(root, JSON.stringify({action: "update", gate_id: "../../file"})), false)
})
