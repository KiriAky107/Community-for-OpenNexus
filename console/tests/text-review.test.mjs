import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import test from 'node:test'
import { WorkbenchClient } from '../src/workbenchClient.ts'
import { parseDetail, parseTextDifference } from '../src/workbenchTypes.ts'

const session = { schema_version: 1, session_id: 'a'.repeat(32), principal_id: 'author-a', role: 'author', namespace: 'examples', expires_at: 1900000000, csrf: 'b'.repeat(64) }
const digest = value => createHash('sha256').update(value).digest('hex')
const file = (text, path = 'nested/read me &.md') => ({ path, bytes: Buffer.byteLength(text), sha256: digest(text) })
const before = file('old\n'), after = file('<script>literal</script>\n')
const reviewed = () => ({ submission_id: 'current', inspection: { review_digest: 'c'.repeat(64), file_page: { items: [after] } }, previous: { release_id: 'previous', available: true, file_page: { items: [{ path: after.path, change: 'changed', before, after }] } } })
const reply = () => ({ schema_version: 1, submission_id: 'current', review_digest: 'c'.repeat(64), previous_release_id: 'previous', path: after.path, limits: { text_bytes: 32768, text_lines: 1000, diff_bytes: 32768, diff_lines: 200 }, before: { status: 'text', file: { ...before }, text: 'old\n' }, after: { status: 'text', file: { ...after }, text: '<script>literal</script>\n' }, diff: { status: 'available', unified: '-old\n+<script>literal</script>\n', truncated: false } })
const json = value => new Response(JSON.stringify(value))

test('text requests are same-origin reads bound to original document, path, digest and prior release', async () => {
  const calls = []
  const client = new WorkbenchClient(async (path, options) => { calls.push({ path, options }); return json(path.endsWith('/session') ? session : reply()) })
  await client.connect()
  const result = await client.textDifference(reviewed(), after.path)
  assert.equal(result.after.text, '<script>literal</script>\n')
  const request = calls.at(-1), url = new URL(request.path, 'http://owned.test')
  assert.equal(url.pathname, '/catalog/v1/publish/submissions/current/inspection/text')
  assert.equal(url.searchParams.get('path'), after.path); assert.equal(url.searchParams.get('previous_release_id'), 'previous')
  assert.equal(request.options.method, 'GET'); assert.equal(request.options.body, undefined)
  assert.equal(request.options.cache, 'no-store'); assert.equal(request.options.credentials, 'same-origin')
  assert.ok(calls.at(-2).path.endsWith('/session'))
})

test('mismatched review, prior release, file hash and plaintext are rejected', async () => {
  for (const mutate of [value => value.review_digest = 'd'.repeat(64), value => value.previous_release_id = 'other', value => value.submission_id = 'other', value => value.after.file.sha256 = 'f'.repeat(64), value => value.after.text = 'incorrect text']) {
    const client = new WorkbenchClient(async path => { if (path.endsWith('/session')) return json(session); const result = reply(); mutate(result); return json(result) })
    await client.connect(); await assert.rejects(client.textDifference(reviewed(), after.path), /REVIEW_CHANGED|INVALID_RESPONSE/)
  }
})

test('decoder rejects unknown reasons, unbounded text, fake limits and hidden unavailable content', () => {
  for (const mutate of [value => value.after.status = 'unknown', value => value.after.text = 'x'.repeat(32769), value => value.limits.text_bytes = 5000000, value => value.diff.unified = '中'.repeat(11000), value => value.after.status = 'binary', value => { value.diff.status = 'unavailable'; value.diff.unified = ''; value.diff.truncated = true }]) {
    const result = reply(); mutate(result); assert.throws(() => parseTextDifference(result), /INVALID_RESPONSE/)
  }
  const result = reply(); result.after = { status: 'binary', file: after, text: null }; result.diff = { status: 'unavailable', unified: '', truncated: false }
  assert.equal(parseTextDifference(result).after.status, 'binary')
})

test('logout clears a late plaintext response and a changed role prevents file reads', async () => {
  let complete, signal, roleChanged = false
  const client = new WorkbenchClient(async (path, options) => {
    if (path.endsWith('/session')) return json(roleChanged ? { ...session, role: 'moderator' } : session)
    signal = options.signal
    return new Promise(resolve => { complete = () => resolve(json(reply())) })
  })
  await client.connect()
  const pending = client.textDifference(reviewed(), after.path)
  await new Promise(resolve => setTimeout(resolve, 0))
  client.clear(); assert.ok(signal.aborted); complete()
  await assert.rejects(pending, /STALE_RESPONSE/)
  await client.connect(); roleChanged = true
  await assert.rejects(client.textDifference(reviewed(), after.path), /SESSION_CHANGED/)
  assert.equal(client.session, null)
})

test('UTF-8 BOM text and removed members keep byte-level hash verification', async () => {
  const original = Buffer.concat([Buffer.from([239, 187, 191]), Buffer.from('hello')])
  const result = reply(), selected = reviewed()
  result.after.text = 'hello'; result.after.file = { ...after, bytes: original.length, sha256: digest(original) }
  selected.previous.file_page.items[0].after = result.after.file
  const client = new WorkbenchClient(async path => json(path.endsWith('/session') ? session : result))
  await client.connect(); assert.equal((await client.textDifference(selected, after.path)).after.text, 'hello')
  result.after = { status: 'missing', file: null, text: null }
  selected.previous.file_page.items[0].after = null
  assert.equal((await client.textDifference(selected, after.path)).after.status, 'missing')
})

test('optional bounded key summaries keep older inspection responses compatible', () => {
  const metadata = { schema_version: 1, namespace: 'examples', package_id: 'lesson', type: 'persona', version: '2.0.0', name: 'Lesson', author_id: 'author-a', license: 'MIT', description: 'Review', sha256: 'e'.repeat(64), size: 100, platforms: ['windows'], architectures: ['x86_64'], min_app_version: '0.6.0-beta1', max_app_version: null, dependencies: {}, permissions: [], changelog: 'Original', published_at: '2026-10-11T00:00:00Z', key_id: 'test-key', signature: 'test-signature' }
  const page = { total: 1, offset: 0, limit: 100, items: [after] }
  const detail = { schema_version: 1, submission_id: 'current', state: 'pending', release: metadata, decisions: [], inspection: { files: 1, expanded_size: after.bytes, manifest: 'persona.json', manifest_text: '{}', manifest_truncated: false, archive_sha256: metadata.sha256, archive_bytes: 100, review_digest: 'c'.repeat(64), signer_revoked: false, signature_verified: true, file_page: page }, previous: { release_id: 'previous', available: true, version: '1.0.0', metadata_fields: [], permissions_added: [], permissions_removed: [], file_page: { ...page, items: [{ path: after.path, change: 'changed', before, after }] } } }
  assert.equal(parseDetail(detail).previous.key_changes, undefined)
  const change = { field: 'system_prompt', before: { present: false, value: '', truncated: false }, after: { present: true, value: '😀'.repeat(2048), truncated: true } }
  detail.previous.key_changes = [change]
  detail.previous.manifest_changes = { status: 'available', total: 41, items: [change] }
  assert.equal(parseDetail(detail).previous.manifest_changes.total, 41)
  detail.previous.manifest_changes.items = Array(41).fill(change)
  assert.throws(() => parseDetail(detail), /INVALID_RESPONSE/)
})
