import assert from 'node:assert/strict'
import fs from 'node:fs'
import test from 'node:test'
import { parse } from 'vue/compiler-sfc'
import { watchEffect, nextTick } from 'vue'
import { messages } from '../src/messages.ts'
import { localizedDate, preferredLocale, preferenceKey, translate } from '../src/localeCore.ts'
import { locale, setLocale, t } from '../src/locale.ts'
import { freezeIntent, WorkbenchClient, workbenchMessage } from '../src/workbenchClient.ts'
import { message } from '../src/api.ts'

test('language preference accepts only the two non-sensitive values and tolerates blocked storage', () => {
  assert.equal(preferredLocale(() => 'en', 'zh-CN'), 'en')
  assert.equal(preferredLocale(() => 'zh-CN', 'en-US'), 'zh-CN')
  assert.equal(preferredLocale(() => 'invalid', 'zh-Hant'), 'zh-CN')
  assert.equal(preferredLocale(() => { throw Error('blocked') }, 'en-GB'), 'en')
  assert.equal(preferredLocale(() => null, 'fr-FR'), 'en')
})

test('UI templates and known client errors have translations; original signed and author text is outside the translator', () => {
  const chinese = /[\u3400-\u9fff]/
  for (const [key, pair] of Object.entries(messages)) {
    assert.equal(pair.length, 2, key)
    assert.equal(chinese.test(pair[1]), false, key)
  }
  const files = ['App.vue', 'Workbench.vue', 'PackageInspection.vue', 'AuditPanel.vue', 'SigningKeys.vue', 'TextReview.vue', 'KeyChanges.vue']
  for (const name of files) {
    const source = fs.readFileSync(new URL('../src/' + name, import.meta.url), 'utf8')
    const ast = parse(source).descriptor.template.ast
    function inspect(node) {
      if (node.type === 2) assert.equal(chinese.test(node.content), false, `${name}: ${node.content}`)
      for (const prop of node.props || []) if (prop.type === 6 && prop.value) assert.equal(chinese.test(prop.value.content), false, `${name}: ${prop.name}`)
      for (const child of node.children || []) inspect(child)
    }
    inspect(ast)
    for (const match of source.matchAll(/\bt\('([^'\n]+)'/g)) assert.ok(Object.hasOwn(messages, match[1]), `${name}: ${match[1]}`)
    assert.doesNotMatch(source, /\{\{\s*t\((?:release|selected\.data|entry|decision|side|value\.diff)\.(?:description|changelog|reason|text|unified)/)
  }
  for (const name of ['api.ts', 'workbenchClient.ts']) {
    const source = fs.readFileSync(new URL('../src/' + name, import.meta.url), 'utf8')
    const mapping = source.slice(source.lastIndexOf('const messages:'))
    for (const match of mapping.matchAll(/([A-Z_0-9]+): '([^']+)'/g)) {
      const original = name === 'api.ts' ? message(new Error(match[1])) : workbenchMessage(new Error(match[1]))
      assert.equal(chinese.test(translate(original, 'en')), false, match[1])
    }
  }
})

test('dates use the selected locale and never change the original timestamp', () => {
  const value = '2026-10-11T14:10:11Z'
  assert.equal(localizedDate(value, 'en'), new Date(value).toLocaleString('en-US', { hour12: false }))
  assert.equal(localizedDate(value, 'zh-CN'), new Date(value).toLocaleString('zh-CN', { hour12: false }))
  assert.equal(localizedDate('invalid original timestamp', 'en'), 'invalid original timestamp')
})

test('translated feedback performs one parameter pass and treats unknown text as original text', () => {
  const original = '<script>中文 {id}</script>'
  assert.equal(translate('操作已确认', 'en', { label: original, id: 'original-id' }), original + ' confirmed · original-id')
  assert.equal(translate(original, 'en'), original)
  for (const key of ['constructor', '__proto__', 'toString']) assert.equal(translate(key, 'en'), key)
})

test('switching language during a frozen action changes UI text only; exact original body and receipt reconciliation remain', async () => {
  const stored = [], calls = []
  globalThis.document = { documentElement: { lang: '' } }
  globalThis.localStorage = { setItem: (key, value) => stored.push([key, value]) }
  const session = { schema_version: 1, session_id: 'a'.repeat(32), principal_id: 'reviewer', role: 'moderator', namespace: null, expires_at: 1900000000, csrf: 'b'.repeat(64) }
  const reason = '审核原文 中文 <script>{id}</script>'
  const operation = freezeIntent(session, 'review', 'original-submission', '批准发布', '/catalog/v1/moderation/reviews', { submission_id: 'original-submission', approve: true, reason, expected_sha256: 'c'.repeat(64) })
  const original = JSON.stringify(operation)
  const receipt = { schema_version: 1, kind: 'review', target: operation.target, operation_id: operation.operation_id, state: 'published', submission_id: operation.target, review_digest: operation.body.expected_sha256, confirmed_at: 1 }
  let displayed = '', lost = true
  const stop = watchEffect(() => { displayed = t(operation.label) })
  const client = new WorkbenchClient(async (path, options) => {
    calls.push({ path, options })
    if (path.endsWith('/session')) return new Response(JSON.stringify(session))
    if (path.includes('/operations/')) return new Response(JSON.stringify(receipt))
    if (lost) throw Error('lost response after commit')
    return new Response(JSON.stringify(receipt))
  })
  await client.connect()
  const count = calls.length
  setLocale('en'); await nextTick()
  assert.equal(displayed, 'Approve publication'); assert.equal(document.documentElement.lang, 'en')
  assert.equal(calls.length, count); assert.equal(JSON.stringify(operation), original)
  await assert.rejects(client.perform(operation), /lost response/)
  assert.equal(calls.at(-1).options.body, JSON.stringify(operation.body))
  setLocale('zh-CN'); await nextTick()
  assert.equal(displayed, '批准发布'); assert.equal(JSON.stringify(operation), original)
  assert.equal((await client.reconcile(operation)).operation_id, operation.operation_id)
  assert.equal(calls.filter(call => call.options.method === 'POST').length, 1)
  lost = false
  assert.deepEqual(stored, [[preferenceKey, 'en'], [preferenceKey, 'zh-CN']])
  globalThis.localStorage.setItem = () => { throw Error('blocked') }
  setLocale('en'); await nextTick(); assert.equal(locale.value, 'en')
  setLocale('invalid'); assert.equal(locale.value, 'en')
  stop(); delete globalThis.document; delete globalThis.localStorage
})
