import assert from 'node:assert/strict'
import test from 'node:test'
import { CatalogClient, groupsUrl } from '../src/api.ts'
import { parseGroupPage } from '../src/types.ts'

const release = {
  schema_version: 1, namespace: 'examples', package_id: 'measurements', type: 'persona',
  release_id: 'selected-1', version: '1.0.0', name: 'Measurements', author_id: 'author', license: 'MIT',
  description: '<script>untrusted</script>', sha256: 'a'.repeat(64), size: 100,
  platforms: ['windows'], architectures: ['x86_64'], min_app_version: '0.6.0-beta1', max_app_version: '0.6.0',
  dependencies: {}, permissions: [], changelog: 'Original signed description', published_at: '2026-10-11T00:00:00Z',
  key_id: 'key-1', signature: 'fixture', withdrawn: false, download_path: 'https://untrusted.example/download',
}
const group = { namespace: 'examples', package_id: 'measurements', release, version_count: 3, matching_version_count: 1,
  latest_version: '2.0.0', availability: 'available', selection: 'earlier_available' }
const page = { schema_version: 1, total: 1, offset: 0, limit: 18, items: [group] }
const filters = { app_version: '0.6.0-beta3+build.1', platform: 'windows', architecture: 'x86_64' }
const json = (body, headers = {}) => new Response(JSON.stringify(body), { headers })

test('group URLs preserve compatibility, Unicode, plus versions and pagination', () => {
  const url = new URL(groupsUrl('笔记 %_', 'persona', 18, filters), 'https://fixture.invalid')
  assert.equal(url.pathname, '/catalog/v1/package-groups')
  assert.equal(url.searchParams.get('q'), '笔记 %_')
  assert.equal(url.searchParams.get('app_version'), filters.app_version)
  assert.equal(url.searchParams.get('platform'), 'windows')
  assert.equal(url.searchParams.get('architecture'), 'x86_64')
  assert.equal(url.searchParams.get('offset'), '18')
  assert.equal(url.searchParams.get('limit'), '18')
  assert.equal(url.searchParams.get('type'), 'persona')
  assert.equal(new URL(groupsUrl('', '', 0, { app_version: '', platform: '', architecture: '' }), url).searchParams.has('app_version'), false)
})

test('group parsing binds the selected identity and prevents forged availability and counts', () => {
  const parsed = parseGroupPage(page).items[0]
  assert.equal(parsed.release.download_path, '/catalog/v1/releases/selected-1/archive')
  assert.equal(parsed.release.changelog, release.changelog)
  for (const altered of [
    { namespace: 'other' }, { package_id: 'another' }, { matching_version_count: 4 }, { version_count: 0 },
    { matching_version_count: 0 }, { availability: 'invented' }, { selection: 'latest' },
    { selection: 'unavailable' }, { availability: 'withdrawn' }, { release: { ...release, withdrawn: true } },
  ]) assert.throws(() => parseGroupPage({ ...page, items: [{ ...group, ...altered }] }), /INVALID_RESPONSE/)
  assert.throws(() => parseGroupPage({ ...page, limit: 0 }), /INVALID_RESPONSE/)
  assert.throws(() => parseGroupPage({ ...page, total: -1 }), /INVALID_RESPONSE/)
  assert.equal(parseGroupPage({ ...page, items: [{ ...group, availability: 'withdrawn', selection: 'unavailable', release: { ...release, withdrawn: true } }] }).items[0].selection, 'unavailable')
})

test('group cache is isolated by all filters and revocation changes cannot keep old available cards', async () => {
  let mode = 'network'
  const requests = []
  const client = new CatalogClient(async (path, options) => {
    requests.push({ path, options })
    if (mode === 'offline') throw new TypeError('offline')
    if (mode === '304') return new Response(null, { status: 304 })
    if (mode === 'revoked') return json({ ...page, items: [{ ...group, availability: 'key_revoked', selection: 'unavailable' }] }, { ETag: '"revoked"' })
    return json(page, { ETag: '"original"' })
  })
  const url = groupsUrl('', '', 0, filters)
  await client.groups(url)
  mode = '304'
  assert.equal((await client.groups(url)).freshness, 'revalidated')
  assert.equal(requests.at(-1).options.headers['If-None-Match'], '"original"')
  mode = 'revoked'
  assert.equal((await client.groups(url)).data.items[0].availability, 'key_revoked')
  mode = 'offline'
  assert.equal((await client.groups(url)).data.items[0].selection, 'unavailable')
  for (const modified of [{ ...filters, app_version: '0.6.1' }, { ...filters, platform: 'linux' }, { ...filters, architecture: 'aarch64' }]) {
    await assert.rejects(client.groups(groupsUrl('', '', 0, modified)))
    assert.equal(requests.at(-1).options.headers['If-None-Match'], undefined)
  }
  assert.equal(requests[0].options.credentials, 'omit')
})

test('invalid authoritative group data never falls back to the previous available result', async () => {
  let valid = true
  const client = new CatalogClient(async () => json(valid ? page : { ...page, items: [{ ...group, release: { ...release, package_id: 'wrong' } }] }))
  const url = groupsUrl('', '', 0, filters)
  await client.groups(url); valid = false
  await assert.rejects(client.groups(url), /INVALID_RESPONSE/)
})
