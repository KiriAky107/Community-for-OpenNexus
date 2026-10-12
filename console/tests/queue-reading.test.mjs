import assert from 'node:assert/strict'
import test from 'node:test'
import { originalPreview } from '../src/originalText.ts'
import { WorkbenchClient } from '../src/workbenchClient.ts'

test('long original previews preserve literal Unicode and CRLF prefixes with a visible full-text path', () => {
  const text = '原文 <script>not executed</script>\r\n' + '😀'.repeat(900)
  const preview = originalPreview(text)
  assert.equal(preview.truncated, true)
  assert.ok(text.startsWith(preview.preview))
  assert.equal(Array.from(preview.preview).length, 800)
  assert.equal(preview.preview.endsWith('\ud83d'), false)
  const lines = Array.from({ length: 13 }, (_, index) => `${index}: 中文 / English`).join('\n')
  assert.equal(originalPreview(lines).preview, lines.split('\n').slice(0, 12).join('\n'))
  assert.deepEqual(originalPreview('<a>literal</a>\nshort'), { preview: '<a>literal</a>\nshort', truncated: false })
})

const session = { schema_version: 1, session_id: 'a'.repeat(32), principal_id: 'reviewer', role: 'moderator', namespace: null, expires_at: 1900000000, csrf: 'b'.repeat(64) }
const json = value => new Response(JSON.stringify(value))
const empty = (offset = 0) => ({ schema_version: 1, total: 0, offset, limit: 20, items: [] })
const filters = () => ({ q: '%_ 中文', namespace: 'examples', type: 'persona', state: 'rejected' })

test('queue filters are encoded literally, frozen for reads, and pending queues omit history state', async () => {
  const calls = []
  let release
  const client = new WorkbenchClient(async path => {
    calls.push(path)
    if (path.endsWith('/session')) return json(session)
    if (path.includes('/publish/submissions')) { await new Promise(resolve => { release = resolve }); return json(empty(20)) }
    return json(empty())
  })
  await client.connect()
  const input = filters(), request = client.page(false, 20, input)
  while (!release) await new Promise(resolve => setImmediate(resolve))
  input.namespace = 'changed'; release()
  await request
  const url = new URL(calls.find(path => path.includes('/publish/submissions')), 'http://localhost')
  assert.equal(url.searchParams.get('q'), '%_ 中文')
  assert.equal(url.searchParams.get('namespace'), 'examples')
  assert.equal(url.searchParams.get('state'), 'rejected')
  await client.page(true, 0, filters())
  assert.equal(new URL(calls.at(-1), 'http://localhost').searchParams.has('state'), false)
})

test('queue clients reject mismatched pagination and invalid filters before a read', async () => {
  let reads = 0
  const client = new WorkbenchClient(async path => { if (path.endsWith('/session')) return json(session); reads++; return json(empty(40)) })
  await client.connect()
  await assert.rejects(client.page(false, 20, filters()), /INVALID_RESPONSE/)
  await assert.rejects(client.page(false, 0, { ...filters(), type: 'executable' }), /INVALID_REQUEST/)
  await assert.rejects(client.page(false, 0, { ...filters(), q: 'x'.repeat(121) }), /INVALID_REQUEST/)
  assert.equal(reads, 1)
})

test('switching identity while a filtered queue is in flight discards its late response', async () => {
  let release
  const client = new WorkbenchClient(async path => {
    if (path.endsWith('/session')) return json(session)
    await new Promise(resolve => { release = resolve }); return json(empty())
  })
  await client.connect()
  const request = client.page(false, 0, filters())
  while (!release) await new Promise(resolve => setImmediate(resolve))
  client.clear(); release()
  await assert.rejects(request, /STALE_RESPONSE/)
})
