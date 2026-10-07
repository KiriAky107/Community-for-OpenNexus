import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import test from 'node:test'
import { availability, CatalogClient, catalogUrl, detailUrl } from '../src/api.ts'
import { parseRelease } from '../src/types.ts'

const archive = new TextEncoder().encode('isolated archive fixture')
const release = {
  schema_version: 1, namespace: 'examples', package_id: 'note-reviewer', type: 'skill',
  release_id: 'fixture-release', version: '1.2.0', name: '笔记检查', author_id: 'author', license: 'MIT',
  description: '<script>untrusted</script>', sha256: createHash('sha256').update(archive).digest('hex'), size: archive.length,
  platforms: ['windows'], architectures: ['x86_64'], min_app_version: '0.6.0', max_app_version: null,
  dependencies: {}, permissions: ['vault.read'], changelog: '## 中文\n原文\n## English\nOriginal text',
  published_at: '2026-10-08T00:00:00Z', key_id: 'fixture-key', signature: 'fixture', withdrawn: false,
  download_path: 'https://untrusted.example/escape',
}
const sources = { schema_version: 1, source_id: 'fixture', keys: [{ namespace: 'examples', key_id: 'fixture-key', public_key: 'A'.repeat(43) + '=', revoked: false }] }
const page = { schema_version: 1, total: 1, offset: 0, limit: 18, items: [release] }
const json = (body, headers = {}) => new Response(JSON.stringify(body), { headers })
const url = catalogUrl('笔记 %_', 'skill', 0)

test('native browser fetch retains its Window receiver', async () => {
  const client = new CatalogClient(function () {
    assert.equal(this, globalThis)
    return Promise.resolve(json(page))
  })
  assert.equal((await client.page(url)).data.total, 1)
})

test('ETags revalidate only the same full query; offline reads never fabricate unseen results', async () => {
  let mode = 'online'
  const requests = []
  const client = new CatalogClient(async (path, options) => {
    requests.push({ path, options })
    if (mode === 'offline') throw new TypeError('offline')
    return mode === '304' ? new Response(null, { status: 304 }) : json(page, { ETag: '"one"' })
  })
  const first = await client.page(url)
  mode = '304'
  const second = await client.page(url)
  assert.equal(second.freshness, 'revalidated')
  assert.equal(requests[1].options.headers['If-None-Match'], '"one"')
  assert.equal(requests[0].options.credentials, 'omit')
  mode = 'offline'
  const cached = await client.page(url)
  assert.equal(cached.freshness, 'cached')
  assert.ok(cached.checkedAt >= first.checkedAt)
  assert.equal(availability(cached.data.items[0], sources, false), '正在显示离线缓存，请连接后重新确认')
  await assert.rejects(client.page(catalogUrl('别的查询', 'skill', 0)))
  assert.equal(requests.at(-1).options.headers['If-None-Match'], undefined)
})

test('authoritative missing or invalid responses do not silently substitute old metadata', async () => {
  let mode = 'online'
  const client = new CatalogClient(async () => mode === 'online' ? json(page) : mode === 'invalid' ? json({ ...page, items: [{ ...release, size: -1 }] }) : new Response(null, { status: Number(mode) }))
  await client.page(url)
  mode = '503'; assert.equal((await client.page(url)).freshness, 'cached')
  mode = '404'; await assert.rejects(client.page(url), /HTTP_404/)
  mode = 'invalid'; await assert.rejects(client.page(url), /INVALID_RESPONSE/)
  const empty = new CatalogClient(async () => new Response(null, { status: 304 }))
  await assert.rejects(empty.page(url), /INVALID_NOT_MODIFIED/)
})

test('memory cache is bounded and streams are limited without trusting Content-Length', async () => {
  let online = true
  const client = new CatalogClient(async () => { if (!online) throw new TypeError('offline'); return json(page) })
  for (let index = 0; index < 17; index++) await client.page(catalogUrl(String(index), '', 0))
  online = false
  await assert.rejects(client.page(catalogUrl('0', '', 0)))
  assert.equal((await client.page(catalogUrl('16', '', 0))).freshness, 'cached')
  const huge = new CatalogClient(async () => new Response(new ReadableStream({ start(controller) { controller.enqueue(new Uint8Array(4 * 1024 * 1024 + 1)); controller.close() } })))
  await assert.rejects(huge.page(url), /RESPONSE_TOO_LARGE/)
})

test('public strings remain literal, detail URLs are encoded, archive URLs are derived locally', () => {
  const parsed = parseRelease(release)
  assert.equal(parsed.description, release.description)
  assert.equal(parsed.changelog, release.changelog)
  assert.equal(parsed.download_path, '/catalog/v1/releases/fixture-release/archive')
  assert.equal(detailUrl(release), '/packages/examples/note-reviewer?version=1.2.0')
  assert.equal(new URL('https://fixture' + url).searchParams.get('q'), '笔记 %_')
})

test('revocation is checked even if catalog metadata is unchanged and its ETag remains valid', async () => {
  let revoked = false, downloaded = false
  const client = new CatalogClient(async path => {
    if (path.endsWith('/sources')) return json({ ...sources, keys: sources.keys.map(key => ({ ...key, revoked })) })
    if (path.endsWith('/archive')) { downloaded = true; return new Response(archive) }
    return json(release)
  })
  await client.sources(); revoked = true
  await assert.rejects(client.download(parseRelease(release), sources.keys[0].public_key), /签名公钥已撤销/)
  assert.equal(downloaded, false)
  assert.equal(availability(release, { ...sources, keys: [] }), '签名公钥已移除或未登记')
  assert.equal(availability({ ...release, withdrawn: true }, sources), '发行已撤回')
})

test('fresh preflight rejects changed metadata or key before requesting an archive', async () => {
  for (const changed of [{ ...release, permissions: ['vault.write'] }, { ...release, withdrawn: true }]) {
    const paths = []
    const client = new CatalogClient(async path => { paths.push(path); return json(path.endsWith('/sources') ? sources : changed) })
    await assert.rejects(client.download(parseRelease(release), sources.keys[0].public_key))
    assert.ok(!paths.some(path => path.endsWith('/archive')))
  }
  const client = new CatalogClient(async path => json(path.endsWith('/sources') ? sources : release))
  await assert.rejects(client.download(parseRelease(release), 'B'.repeat(43) + '='), /METADATA_CHANGED/)
})

test('download validates exact length and SHA-256 and never uses a metadata-provided external URL', async () => {
  for (const bytes of [archive, new Uint8Array(archive.length), archive.slice(0, -1), new Uint8Array(archive.length + 1)]) {
    const paths = []
    const client = new CatalogClient(async path => {
      paths.push(path)
      return path.endsWith('/sources') ? json(sources) : path.endsWith('/archive') ? new Response(bytes) : json(release)
    })
    if (bytes === archive) assert.equal((await client.download(parseRelease(release), sources.keys[0].public_key)).size, archive.length)
    else await assert.rejects(client.download(parseRelease(release), sources.keys[0].public_key), /ARCHIVE_|RESPONSE_TOO_LARGE/)
    assert.equal(paths.at(-1), '/catalog/v1/releases/fixture-release/archive')
  }
})

test('sources have no stale fallback and missing crypto stops downloads with a clear failure', async () => {
  let online = true
  const client = new CatalogClient(async path => {
    if (!online) throw new TypeError('offline')
    return path.endsWith('/sources') ? json(sources) : path.endsWith('/archive') ? new Response(archive) : json(release)
  })
  await client.sources(); online = false; await assert.rejects(client.sources())
  online = true
  const original = Object.getOwnPropertyDescriptor(globalThis, 'crypto')
  Object.defineProperty(globalThis, 'crypto', { configurable: true, value: undefined })
  try { await assert.rejects(client.download(parseRelease(release), sources.keys[0].public_key), /SECURE_CONTEXT_REQUIRED/) }
  finally { Object.defineProperty(globalThis, 'crypto', original) }
})
