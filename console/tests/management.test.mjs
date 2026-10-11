import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import test from 'node:test'
import { WorkbenchClient, freezeIntent } from '../src/workbenchClient.ts'
import { parseAudit, parseKeys, validateReceipt } from '../src/workbenchTypes.ts'

const sha = value => createHash('sha256').update(value).digest('hex')
const session = { schema_version: 1, session_id: 'a'.repeat(32), principal_id: 'reviewer-b', role: 'moderator', namespace: null, expires_at: 1900000000, csrf: 'b'.repeat(64) }
const raw = Buffer.alloc(32, 4)
const state = { key_id: 'owned-key', namespace: 'examples', public_key: raw.toString('base64'), revoked: false }
const key = { ...state, fingerprint: sha(raw), review_digest: sha(JSON.stringify(state)) }
const keyPage = { schema_version: 1, total: 1, offset: 0, limit: 20, items: [key], observed_at: 1800000000 }
const entry = id => ({ id, actor: '审核员', action: 'revoke-key', subject: '包%_', reason: '<script>literal</script>', timestamp: 1800000000 })
const response = value => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } })
async function clientFor(handler, identity = session) {
  const requests = []
  const client = new WorkbenchClient(async (path, options) => {
    requests.push({ path, options })
    return path === '/catalog/v1/web/session' ? response(identity) : handler(path, options)
  })
  await client.connect()
  return { client, requests }
}

test('recent audit binds bounded literal filters and descending cursor', async () => {
  const { client, requests } = await clientFor(() => response({ schema_version: 1, items: [entry(8), entry(4)], next_cursor: 4 }))
  assert.deepEqual((await client.audit({ q: '%_中文', actor: 'reviewer-b', action: 'revoke-key', subject: '包%_' }, 10)).items.map(item => item.id), [8, 4])
  const sent = requests.at(-1), url = new URL(sent.path, 'https://local.example')
  assert.equal(url.searchParams.get('q'), '%_中文'); assert.equal(url.searchParams.get('before'), '10')
  assert.equal(sent.options.credentials, 'same-origin'); assert.equal(sent.options.cache, 'no-store')
  const invalid = await clientFor(() => response({ schema_version: 1, items: [entry(8), entry(9)], next_cursor: 9 }))
  await assert.rejects(invalid.client.audit({ q: '', actor: '', action: '', subject: '' }, 10), /INVALID_RESPONSE/)
  const author = await clientFor(() => { throw new Error('author must not read audit') }, { ...session, role: 'author', namespace: 'examples' })
  await assert.rejects(author.client.audit({ q: '', actor: '', action: '', subject: '' }), /ROLE_REQUIRED/)
})

test('keys verify original public bytes, state digest, paging and author namespace', async () => {
  const { client, requests } = await clientFor(() => response(keyPage))
  assert.equal((await client.keys('%_', 'active')).items[0].fingerprint, key.fingerprint)
  assert.match(requests.at(-1).path, /q=%25_/)
  for (const changed of [{ fingerprint: '0'.repeat(64) }, { revoked: true }, { namespace: 'other' }]) {
    const bad = await clientFor(() => response({ ...keyPage, items: [{ ...key, ...changed }] }))
    await assert.rejects(bad.client.keys(), /INVALID_RESPONSE/)
  }
  const badPage = await clientFor(() => response({ ...keyPage, offset: 20 }))
  await assert.rejects(badPage.client.keys(), /INVALID_RESPONSE/)
  const other = { ...state, namespace: 'other' }
  const author = await clientFor(() => response({ ...keyPage, items: [{ ...key, ...other, review_digest: sha(JSON.stringify(other)) }] }), { ...session, role: 'author', namespace: 'examples' })
  await assert.rejects(author.client.keys(), /INVALID_RESPONSE/)
})

test('key and audit replies arriving after logout or changed role cannot repopulate UI', async () => {
  let release
  const held = new Promise(resolve => { release = resolve })
  const { client } = await clientFor(() => held)
  const pending = client.keys()
  await new Promise(resolve => setImmediate(resolve))
  client.clear(); release(response(keyPage))
  await assert.rejects(pending, /STALE_RESPONSE/)
  let current = session
  const changed = new WorkbenchClient(async path => response(path === '/catalog/v1/web/session' ? current : keyPage))
  await changed.connect(); current = { ...session, role: 'author', namespace: 'examples' }
  await assert.rejects(changed.keys(), /SESSION_CHANGED/); assert.equal(changed.session, null)
})

test('frozen key actions recover only the original target and never write during reconciliation', async () => {
  let receipt
  const { client, requests } = await clientFor((path, options) => {
    if (options.method === 'POST') return response(receipt)
    return response(receipt)
  })
  const operation = freezeIntent(session, 'revoke-key', key.key_id, '撤销', '/catalog/v1/keys/owned-key/revoke', { reason: '实际核对', expected_sha256: key.review_digest })
  receipt = { schema_version: 1, operation_id: operation.operation_id, kind: 'revoke-key', target: key.key_id, key_id: key.key_id, state: 'revoked', confirmed_at: 1800000000 }
  assert.equal((await client.perform(operation)).state, 'revoked')
  const sent = requests.at(-1); assert.equal(JSON.parse(sent.options.body).expected_sha256, key.review_digest); assert.equal(sent.options.headers['X-Community-CSRF'], session.csrf)
  const writes = requests.filter(request => request.options.method === 'POST').length
  assert.deepEqual(await client.reconcile(operation), receipt)
  assert.equal(requests.filter(request => request.options.method === 'POST').length, writes)
  for (const changed of [{ key_id: 'foreign' }, { target: 'foreign' }, { kind: 'withdraw' }, { state: 'active' }]) assert.throws(() => validateReceipt({ ...receipt, ...changed }, operation), /RECEIPT_MISMATCH/)
})

test('strict parsers bound audit rows and require canonical 32-byte keys', () => {
  assert.equal(parseAudit({ schema_version: 1, items: [entry(1)], next_cursor: null }).items[0].reason, '<script>literal</script>')
  assert.throws(() => parseAudit({ schema_version: 1, items: Array.from({ length: 101 }, () => entry(1)), next_cursor: null }), /INVALID_RESPONSE/)
  assert.throws(() => parseKeys({ ...keyPage, items: [{ ...key, public_key: 'x'.repeat(44) }] }), /INVALID_RESPONSE/)
  assert.throws(() => parseKeys({ ...keyPage, items: [{ ...key, fingerprint: '' }] }), /INVALID_RESPONSE/)
})
