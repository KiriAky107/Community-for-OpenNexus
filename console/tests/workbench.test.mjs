import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import test from 'node:test'
import { WorkbenchClient, freezeIntent, localPackage } from '../src/workbenchClient.ts'
import { parseItem, parseReviews, validateReceipt } from '../src/workbenchTypes.ts'

const session = { schema_version: 1, session_id: 'a'.repeat(32), principal_id: 'author-a', role: 'author', namespace: 'examples', expires_at: 1900000000, csrf: 'b'.repeat(64) }
const bytes = new TextEncoder().encode('original ZIP bytes')
const metadata = { schema_version: 1, namespace: 'examples', package_id: 'lesson', type: 'persona', version: '1.0.0', name: 'Lesson', author_id: 'author-a', license: 'MIT', description: 'Original <script>text</script>', sha256: createHash('sha256').update(bytes).digest('hex'), size: bytes.length, platforms: ['windows'], architectures: ['x86_64'], min_app_version: '0.6.0', max_app_version: null, dependencies: {}, permissions: [], changelog: '中文\nEnglish', published_at: '2026-10-08T00:00:00Z', key_id: 'test-key', signature: 'test-signature' }
const json = (value, status = 200) => new Response(JSON.stringify(value), { status })
const item = { submission_id: 'original-submission', state: 'pending', release: metadata }
const intent = () => freezeIntent(session, 'review', item.submission_id, '批准', '/catalog/v1/moderation/reviews', { submission_id: item.submission_id, approve: true, reason: 'Reviewed original files', expected_sha256: 'c'.repeat(64) })
const receipt = operation => ({ schema_version: 1, operation_id: operation.operation_id, kind: operation.kind, target: operation.target, confirmed_at: 1, state: 'published', submission_id: operation.target, review_digest: operation.body.expected_sha256 })

test('workbench uses same-origin RAM sessions and verifies the actor before writes', async () => {
  const calls = []
  const operation = intent()
  const client = new WorkbenchClient(function (path, options) { assert.equal(this, globalThis); calls.push({ path, options }); return Promise.resolve(json(path.endsWith('/session') ? session : receipt(operation))) })
  await client.connect('synthetic-token')
  assert.equal((await client.perform(operation)).state, 'published')
  assert.equal(calls.length, 3)
  for (const { options } of calls) { assert.equal(options.credentials, 'same-origin'); assert.equal(options.cache, 'no-store'); assert.equal(options.redirect, 'error'); assert.equal(options.headers.Authorization, undefined) }
  assert.equal(calls[1].options.method, 'GET')
  assert.equal(calls[2].options.headers['X-Community-CSRF'], session.csrf)
  assert.equal(JSON.parse(calls[2].options.body).operation_id, operation.operation_id)
})

test('unknown write results are never retried; read the original receipt before explicit original retry', async () => {
  let writes = 0, reads = 0, lost = true
  const operation = intent()
  const client = new WorkbenchClient(async path => { if (path.endsWith('/session')) return json(session); if (path.includes('/operations/')) { reads++; return json(receipt(operation)) } writes++; if (lost) throw new TypeError('lost response'); return json(receipt(operation)) })
  await client.connect()
  await assert.rejects(client.perform(operation), /lost response/)
  assert.equal(writes, 1)
  assert.equal((await client.reconcile(operation)).state, 'published')
  assert.equal(writes, 1); assert.equal(reads, 1)
  lost = false
  await client.perform(operation)
  assert.equal(writes, 2)
})

test('frozen intent keeps the reviewed nested payload and checks matching receipts', () => {
  const body = { release: structuredClone(metadata), archive_base64: 'original', expected_sha256: 'c'.repeat(64) }
  const operation = freezeIntent(session, 'submit', 'examples/lesson/1.0.0', '提交', '/catalog/v1/publish/submissions', body)
  body.release.permissions.push('network'); body.archive_base64 = 'changed'
  assert.deepEqual(operation.body.release.permissions, []); assert.equal(operation.body.archive_base64, 'original')
  assert.throws(() => operation.body.release.permissions.push('changed'), TypeError)
  assert.match(operation.operation_id, /^[a-f0-9]{32}$/)
  const original = { schema_version: 1, operation_id: operation.operation_id, kind: 'submit', target: operation.target, state: 'pending', submission_id: 'original', review_digest: operation.body.expected_sha256, confirmed_at: 1 }
  assert.equal(validateReceipt(original, operation).submission_id, 'original')
  for (const change of [{ operation_id: 'd'.repeat(32) }, { target: 'other' }, { kind: 'review' }, { state: 'published' }, { review_digest: 'e'.repeat(64) }]) assert.throws(() => validateReceipt({ ...original, ...change }, operation), /RECEIPT_MISMATCH/)
})

test('identity or role changes discard the session and prevent the prepared write', async () => {
  let changed = false, writes = 0
  const observed = []
  const client = new WorkbenchClient(async path => { if (path.endsWith('/session')) return json(changed ? { ...session, role: 'moderator', namespace: null } : session); writes++; return json({}) }, (value, reason) => observed.push({ value, reason }))
  await client.connect(); changed = true
  await assert.rejects(client.perform(intent()), /SESSION_CHANGED/)
  assert.equal(writes, 0); assert.equal(client.session, null); assert.equal(observed.at(-1).reason, 'SESSION_CHANGED')
})

test('a delayed authentication check cannot send a write after the whole request deadline', async () => {
  let delayed = false, resolveCheck, writes = 0
  const client = new WorkbenchClient(async path => { if (path.endsWith('/session')) { if (delayed) return new Promise(resolve => { resolveCheck = resolve }); return json(session) } writes++; return json({}) }, () => {}, 15)
  await client.connect(); delayed = true
  await assert.rejects(client.perform(intent()), /REQUEST_TIMEOUT/)
  resolveCheck(json(session)); await new Promise(resolve => setTimeout(resolve, 5))
  assert.equal(writes, 0)
})

test('late responses and malformed unauthorized responses cannot resurrect an old session', async () => {
  let pending, delayed = false
  const client = new WorkbenchClient(async () => delayed ? new Promise(resolve => { pending = resolve }) : json(session))
  await client.connect(); delayed = true
  const read = client.sessions(); client.clear()
  pending(json(session))
  await assert.rejects(read, /STALE_RESPONSE/); assert.equal(client.session, null)
  const expired = new WorkbenchClient(async () => delayed ? new Response('not JSON', { status: 401 }) : json(session))
  delayed = false; await expired.connect(); delayed = true
  await assert.rejects(expired.sessions(), /AUTH_REQUIRED/); assert.equal(expired.session, null)
})

test('the deadline covers response bodies and rejects successful receipts with a different target', async () => {
  let stalled = false, stream
  const client = new WorkbenchClient(async path => path.endsWith('/session') ? json(session) : stalled ? new Response(new ReadableStream({ start(controller) { stream = controller } })) : json({ schema_version: 1, items: [] }), () => {}, 15)
  await client.connect(); stalled = true
  await assert.rejects(client.sessions(), /REQUEST_TIMEOUT/)
  stream.enqueue(new TextEncoder().encode(JSON.stringify({ schema_version: 1, items: [] }))); stream.close()
  await new Promise(resolve => setTimeout(resolve, 5))
  assert.equal(client.session.principal_id, session.principal_id)
  const operation = intent()
  const bad = new WorkbenchClient(async path => json(path.endsWith('/session') ? session : { ...receipt(operation), target: 'wrong' }))
  await bad.connect(); await assert.rejects(bad.perform(operation), /RECEIPT_MISMATCH/)
})

test('pending review records have their route-defined state; arbitrary prototype states are rejected', () => {
  const page = parseReviews({ schema_version: 1, total: 1, offset: 0, limit: 20, items: [{ submission_id: item.submission_id, release: metadata }] })
  assert.equal(page.items[0].state, 'pending'); assert.equal(page.items[0].release.description, metadata.description)
  assert.throws(() => parseItem({ ...item, state: 'constructor' }))
})

test('local signed metadata and exact ZIP hashes are checked before any upload; extra private fields are rejected', async () => {
  const archive = new File([bytes], 'lesson.zip')
  const release = value => new File([JSON.stringify(value)], 'release.json')
  const valid = await localPackage(release(metadata), archive)
  assert.equal(atob(valid.archive_base64), new TextDecoder().decode(bytes))
  assert.equal(valid.release.signature, metadata.signature)
  await assert.rejects(localPackage(release({ ...metadata, private_key: 'must never leave this device' }), archive), /INVALID_METADATA/)
  await assert.rejects(localPackage(release({ ...metadata, sha256: 'a'.repeat(64) }), archive), /ARCHIVE_HASH_MISMATCH/)
  await assert.rejects(localPackage(release(metadata), new File([bytes, bytes], 'wrong.zip')), /ARCHIVE_SIZE_MISMATCH/)
})

test('privileged download validates original bytes and includes session verification in its deadline', async () => {
  const inspection = { files: 1, expanded_size: bytes.length, manifest: 'persona.json', manifest_text: '{}', manifest_truncated: false, archive_sha256: metadata.sha256, archive_bytes: bytes.length, review_digest: 'c'.repeat(64), signer_revoked: false, signature_verified: true, file_page: { total: 1, offset: 0, limit: 100, items: [{ path: 'persona.json', bytes: bytes.length, sha256: metadata.sha256 }] } }
  const detail = { schema_version: 1, ...item, inspection, previous: null, decisions: [] }
  let corrupt = false, archiveReads = 0
  const client = new WorkbenchClient(async path => path.endsWith('/session') ? json(session) : path.endsWith('/inspection?offset=0&limit=100') ? json(detail) : (archiveReads++, new Response(corrupt ? new Uint8Array(bytes.length) : bytes)))
  await client.connect()
  assert.deepEqual(new Uint8Array(await (await client.archive(detail)).arrayBuffer()), bytes)
  corrupt = true; await assert.rejects(client.archive(detail), /ARCHIVE_HASH_MISMATCH/)
  const slow = new WorkbenchClient(async path => path.endsWith('/session') ? json(session) : new Promise(resolve => setTimeout(() => resolve(json(detail)), 40)), () => {}, 10)
  await slow.connect(); await assert.rejects(slow.archive(detail), /REQUEST_TIMEOUT/)
  assert.equal(archiveReads, 2)
})
