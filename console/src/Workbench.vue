<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, shallowRef } from 'vue'
import { CatalogError } from './api.ts'
import { freezeIntent, localPackage, WorkbenchClient, workbenchMessage } from './workbenchClient.ts'
import { stateNames } from './workbenchTypes.ts'
import type { BrowserSession, CursorPage, Detail, Intent, Page, Preview, Receipt, Report, Session } from './workbenchTypes.ts'
import PackageInspection from './PackageInspection.vue'
import logo from './assets/opennexus-logo.svg'

type Pending = { intent: Intent; phase: 'confirm' | 'unknown' | 'missing'; tried: boolean; retryable: boolean }
const session = shallowRef<Session | null>(null), pending = shallowRef<Pending | null>(null)
const preview = shallowRef<Preview | null>(null), detail = shallowRef<Detail | null>(null), page = shallowRef<Page | null>(null)
const reports = shallowRef<CursorPage<Report> | null>(null), sessions = shallowRef<BrowserSession[]>([])
const token = ref(''), busy = ref(false), error = ref(''), notice = ref(''), reason = ref(''), accepted = ref(false)
const revokeChoice = ref<string | null>(null)
const tab = ref<'submissions' | 'reviews' | 'reports' | 'sessions'>('submissions'), selectedReport = shallowRef<Report | null>(null)
const metadataFile = shallowRef<File | null>(null), archiveFile = shallowRef<File | null>(null)
const metadataInput = ref<HTMLInputElement>(), archiveInput = ref<HTMLInputElement>(), confirmation = ref<HTMLElement>()
const prepared = shallowRef<{ url: string; name: string } | null>(null)
let bundle: Awaited<ReturnType<typeof localPackage>> | null = null, generation = 0, expiry: ReturnType<typeof setTimeout> | undefined
const locked = computed(() => busy.value || !!pending.value)
const matched = computed(() => !!pending.value && session.value?.principal_id === pending.value.intent.principal_id)
const logoutBlocked = computed(() => busy.value || (!!pending.value && (matched.value || pending.value.retryable)))
const time = (value: number) => new Date(value * 1000).toLocaleString('zh-CN', { hour12: false })
function clearDownload() { if (prepared.value) URL.revokeObjectURL(prepared.value.url); prepared.value = null }
function clearFiles() { bundle = null; preview.value = null; metadataFile.value = null; archiveFile.value = null; if (metadataInput.value) metadataInput.value.value = ''; if (archiveInput.value) archiveInput.value.value = '' }
function clearActor() {
  generation++; page.value = null; detail.value = null; reports.value = null; sessions.value = []; selectedReport.value = null; revokeChoice.value = null; reason.value = ''; token.value = ''; accepted.value = false; clearFiles(); clearDownload()
  // On expiry keep only the original receipt reference, never an old actor's package payload.
  if (pending.value?.tried) { const original = pending.value.intent; pending.value = { intent: Object.freeze({ ...original, body: Object.freeze({ expected_sha256: original.body.expected_sha256, approve: original.body.approve, decision: original.body.decision }) }), phase: 'unknown', tried: true, retryable: false } }
  else pending.value = null
}
const client = new WorkbenchClient(fetch, (value, cause) => {
  clearTimeout(expiry); session.value = value
  if (!value) { clearActor(); if (cause) error.value = workbenchMessage(new Error(cause)); return }
  expiry = setTimeout(() => client.clear('AUTH_REQUIRED'), Math.max(0, value.expires_at * 1000 - Date.now()))
})
async function read(action: () => Promise<void>) {
  if (busy.value) return
  const ticket = generation; busy.value = true; error.value = ''; notice.value = ''
  try { await action() } catch (failure) { if (ticket === generation) error.value = workbenchMessage(failure) }
  finally { busy.value = false }
}
async function connect() {
  const original = token.value; token.value = '' // Clear the password field before the first network request.
  await read(async () => { await client.connect(original || undefined); tab.value = session.value?.role === 'moderator' ? 'reviews' : 'submissions'; if (!pending.value) page.value = await client.page(tab.value === 'reviews') })
}
async function loadPage(offset = 0) { await read(async () => { clearDownload(); page.value = await client.page(tab.value === 'reviews', offset) }) }
async function chooseTab(value: typeof tab.value) {
  if (locked.value) return
  tab.value = value; detail.value = null; selectedReport.value = null; reason.value = ''; clearDownload()
  await read(async () => { if (value === 'sessions') sessions.value = await client.sessions(); else if (value === 'reports') reports.value = await client.reports(); else page.value = await client.page(value === 'reviews') })
}
async function inspect(id: string, offset = 0) {
  if (locked.value) return
  await read(async () => { clearDownload(); detail.value = await client.detail(id, offset); reason.value = '' })
}
async function selectReport(entry: Report) {
  if (locked.value) return
  selectedReport.value = entry; detail.value = null; reason.value = ''; clearDownload()
  await read(async () => { detail.value = await client.detail(entry.release_id) })
}
async function loadReports(after = 0) {
  if (locked.value) return
  selectedReport.value = null; detail.value = null; reason.value = ''; clearDownload()
  await read(async () => { reports.value = await client.reports(after) })
}
function chooseFile(event: Event, which: 'metadata' | 'archive') {
  if (locked.value) return
  preview.value = null; bundle = null
  const value = (event.target as HTMLInputElement).files?.[0] || null
  if (which === 'metadata') metadataFile.value = value; else archiveFile.value = value
}
async function preflight() {
  if (locked.value || !metadataFile.value || !archiveFile.value) return
  const ticket = generation, metadata = metadataFile.value, archive = archiveFile.value
  await read(async () => {
    const local = await localPackage(metadata, archive)
    if (ticket !== generation) return
    const result = await client.preflight(local.release, local.archive_base64)
    if (ticket !== generation) return
    preview.value = result; bundle = local
  })
}
async function stage(kind: Intent['kind'], target: string, label: string, path: string, body: Record<string, unknown>) {
  if (locked.value || !session.value) return
  pending.value = { intent: freezeIntent(session.value, kind, target, label, path, body), phase: 'confirm', tried: false, retryable: true }; accepted.value = false; error.value = ''; notice.value = ''
  await nextTick(); confirmation.value?.scrollIntoView({ behavior: 'smooth', block: 'center' }); confirmation.value?.focus()
}
function submit() {
  if (!preview.value || !bundle) return
  const release = preview.value.release
  void stage('submit', `${release.namespace}/${release.package_id}/${release.version}`, '提交审核', '/catalog/v1/publish/submissions', { release, archive_base64: bundle.archive_base64, expected_sha256: preview.value.inspection.review_digest })
}
function review(approve: boolean) {
  if (!detail.value || !reason.value.trim()) return
  void stage('review', detail.value.submission_id, approve ? '批准发布' : '拒绝提交', '/catalog/v1/moderation/reviews', { submission_id: detail.value.submission_id, approve, reason: reason.value.trim(), expected_sha256: detail.value.inspection.review_digest })
}
function withdraw() { if (detail.value && reason.value.trim()) void stage('withdraw', detail.value.submission_id, '撤回发行', `/catalog/v1/releases/${detail.value.submission_id}/withdraw`, { reason: reason.value.trim(), expected_sha256: detail.value.inspection.review_digest }) }
function report() { if (detail.value && reason.value.trim()) void stage('report', detail.value.submission_id, '提交举报', `/catalog/v1/releases/${detail.value.submission_id}/reports`, { reason: reason.value.trim() }) }
function resolve(decision: 'addressed' | 'dismissed') { if (selectedReport.value && reason.value.trim()) void stage('resolve-report', String(selectedReport.value.id), decision === 'addressed' ? '标记举报已处理' : '驳回举报', `/catalog/v1/moderation/reports/${selectedReport.value.id}/resolve`, { decision, reason: reason.value.trim(), expected_sha256: selectedReport.value.review_digest }) }
async function confirmed(receipt: Receipt) {
  const original = pending.value?.intent; pending.value = null; accepted.value = false; reason.value = ''; clearDownload()
  notice.value = `${original?.label || '原操作'}已确认 · ${receipt.operation_id}`
  if (original?.kind === 'submit') { clearFiles(); tab.value = 'submissions'; page.value = await client.page(); if (receipt.submission_id) detail.value = await client.detail(receipt.submission_id) }
  else if (original?.kind === 'resolve-report') { selectedReport.value = null; reports.value = await client.reports() }
  else if (detail.value) { detail.value = await client.detail(detail.value.submission_id); page.value = await client.page(tab.value === 'reviews') }
}
async function execute() {
  if (busy.value || !pending.value || !matched.value || !accepted.value || !pending.value.retryable || pending.value.phase === 'unknown') return
  const operation = pending.value.intent
  pending.value = { ...pending.value, phase: 'unknown', tried: true }; busy.value = true; error.value = ''; notice.value = ''
  try { const receipt = await client.perform(operation); if (matched.value) await confirmed(receipt) }
  catch (failure) {
    error.value = workbenchMessage(failure)
    if (failure instanceof CatalogError && failure.status >= 400 && failure.status < 500 && ![401, 408, 429].includes(failure.status) && failure.message !== 'OPERATION_ID_REUSED') {
      pending.value = null; accepted.value = false
      // Preserve the rejection reason while refreshing only the affected read state.
      try { if (detail.value) detail.value = await client.detail(detail.value.submission_id); if (session.value) page.value = await client.page(tab.value === 'reviews') } catch { /* Original failure remains visible. */ }
    }
  } finally { busy.value = false }
}
async function reconcile() {
  if (busy.value || !pending.value || !matched.value || !pending.value.tried) return
  await read(async () => {
    const receipt = await client.reconcile(pending.value!.intent)
    if (receipt.state === 'not_found') { pending.value = { ...pending.value!, phase: 'missing' }; accepted.value = false; notice.value = '尚未发现原编号回执。此查询没有执行任何写入。' }
    else await confirmed(receipt)
  })
}
async function download() {
  if (locked.value || !detail.value) return
  const selected = detail.value, ticket = generation
  await read(async () => { clearDownload(); const blob = await client.archive(selected); if (ticket === generation) prepared.value = { url: URL.createObjectURL(blob), name: `${selected.release.package_id}-${selected.release.version}.zip` } })
}
async function logout() { if (!logoutBlocked.value) await read(() => client.logout()) }
async function revoke() {
  if (locked.value || !revokeChoice.value) return
  const id = revokeChoice.value; revokeChoice.value = null
  await read(async () => { await client.revokeSession(id); if (session.value) sessions.value = await client.sessions() })
}
const beforeUnload = (event: BeforeUnloadEvent) => { if (pending.value?.tried) { event.preventDefault(); event.returnValue = '' } }
onMounted(() => { document.title = '作者与审核 · OpenNexus Community'; window.addEventListener('beforeunload', beforeUnload); void read(async () => { try { await client.connect(); tab.value = session.value?.role === 'moderator' ? 'reviews' : 'submissions'; page.value = await client.page(tab.value === 'reviews') } catch (failure) { if (!(failure instanceof CatalogError && failure.status === 401)) throw failure; error.value = '' } }) })
onUnmounted(() => { window.removeEventListener('beforeunload', beforeUnload); clearTimeout(expiry); clearDownload(); client.clear() })
</script>

<template>
  <header class="topbar wb-topbar"><a class="brand" href="/" @click="pending && $event.preventDefault()"><img :src="logo" alt="OpenNexus"><span><strong>OpenNexus</strong><small>Community workbench</small></span></a><a class="wb-catalog-link" href="/" @click="pending && $event.preventDefault()">公开目录</a><a class="docs-link" href="https://github.com/KiriAky107/Community-for-OpenNexus#core-workflows" target="_blank" rel="noreferrer">使用说明 ↗</a></header>
  <main class="wb-main">
    <section class="intro wb-intro"><p class="eyebrow">Author &amp; moderation</p><h1>提交成果，<span>审阅实际内容。</span></h1><p>作者上传已签名的发行文件，审核员独立检查内容与权限。每次写入都经过确认，并保留原编号回执。</p></section>
    <p v-if="error" class="banner error" role="alert">{{ error }}</p><p v-if="notice" class="banner success" role="status">{{ notice }}</p>
    <section v-if="!session" class="wb-card wb-login"><h2>登录工作台</h2><p class="muted">使用管理员提供的作者或审核员 Token。签名私钥保留在作者设备上。</p><form @submit.prevent="connect"><label>身份 Token<input v-model="token" type="password" autocomplete="off" spellcheck="false" maxlength="256" placeholder="粘贴身份 Token" :disabled="busy"></label><div class="wb-actions"><button class="primary" :disabled="busy || !token">登录</button><button type="button" :disabled="busy" @click="connect">确认已有会话</button></div></form></section>
    <template v-else>
      <section class="wb-session"><div><strong>{{ session.principal_id }}</strong><span>{{ session.role === 'author' ? '作者' : '审核员' }}{{ session.namespace ? ` · ${session.namespace}` : '' }}</span><small>到期 {{ time(session.expires_at) }}</small></div><button :disabled="logoutBlocked" @click="logout">退出</button></section>
      <p v-if="pending && !matched" class="banner warning">有其他身份的原操作待核对。请使用原身份重新登录，当前身份不能访问原回执。</p>
      <nav class="wb-tabs" aria-label="工作台"><button :class="{ chosen: tab === 'submissions' }" :disabled="locked" @click="chooseTab('submissions')">{{ session.role === 'author' ? '我的提交' : '全部提交' }}</button><button v-if="session.role === 'moderator'" :class="{ chosen: tab === 'reviews' }" :disabled="locked" @click="chooseTab('reviews')">待审核</button><button v-if="session.role === 'moderator'" :class="{ chosen: tab === 'reports' }" :disabled="locked" @click="chooseTab('reports')">举报处理</button><button :class="{ chosen: tab === 'sessions' }" :disabled="locked" @click="chooseTab('sessions')">我的会话</button></nav>
      <section v-if="session.role === 'author' && tab === 'submissions' && !pending" class="wb-card"><div class="section-heading"><h2>准备新发行</h2><span class="muted">只读预检</span></div><div class="wb-upload"><label>已签名元数据 JSON<input ref="metadataInput" type="file" accept=".json,application/json" :disabled="locked" @change="chooseFile($event, 'metadata')"></label><label>原始 ZIP<input ref="archiveInput" type="file" accept=".zip,application/zip" :disabled="locked" @change="chooseFile($event, 'archive')"></label></div><p class="muted">JSON ≤ 1 MiB，ZIP ≤ 10 MiB。先核对本地哈希，再由服务验证签名与类型清单。</p><button :disabled="locked || !metadataFile || !archiveFile" @click="preflight">检查签名与归档</button><template v-if="preview"><PackageInspection :release="preview.release" :inspection="preview.inspection" :busy="locked"/><button class="primary" :disabled="locked" @click="submit">审阅后提交</button></template></section>
      <section v-if="tab === 'submissions' || tab === 'reviews'" class="wb-grid">
        <aside class="wb-card wb-list"><div class="section-heading"><h2>{{ tab === 'reviews' ? '待审核' : '提交记录' }} · {{ page?.total || 0 }}</h2><button :disabled="locked" @click="loadPage(page?.offset || 0)">刷新</button></div><p v-if="!page?.items.length" class="muted">{{ busy ? '正在读取…' : '此页暂无提交' }}</p><button v-for="item in page?.items" :key="item.submission_id" class="wb-item" :class="{ chosen: detail?.submission_id === item.submission_id }" :disabled="locked" @click="inspect(item.submission_id)"><strong>{{ item.release.name }}</strong><small>{{ item.release.namespace }}/{{ item.release.package_id }}</small><span>{{ item.release.version }} · {{ stateNames[item.state] }}</span></button><nav v-if="page" class="pagination compact" aria-label="提交分页"><button :disabled="locked || !page.offset" @click="loadPage(Math.max(0, page.offset - 20))">上一页</button><span>{{ Math.floor(page.offset / 20) + 1 }}</span><button :disabled="locked || page.offset + page.limit >= page.total" @click="loadPage(page.offset + page.limit)">下一页</button></nav></aside>
        <article class="wb-card"><template v-if="detail"><div class="section-heading"><strong>{{ stateNames[detail.state] }}</strong><button :disabled="locked" @click="inspect(detail.submission_id, detail.inspection.file_page.offset)">更新详情</button></div><PackageInspection :release="detail.release" :inspection="detail.inspection" :previous="detail.previous" :busy="locked" paged @page="inspect(detail.submission_id, $event)"/><div class="wb-actions"><button :disabled="locked" @click="download">校验原 ZIP</button><a v-if="prepared" class="save-archive" :href="prepared.url" :download="prepared.name">保存已校验 ZIP</a></div><section v-if="detail.decisions.length" class="detail-section"><h3>最近的处理记录</h3><div v-for="decision in detail.decisions" :key="decision.id" class="wb-decision"><strong>{{ decision.action }} · {{ decision.actor }}</strong><small>{{ time(decision.timestamp) }}</small><p>{{ decision.reason || '无补充原因' }}</p></div></section><section class="detail-section"><label>处理或举报原因<textarea v-model="reason" rows="3" maxlength="2000" :disabled="locked" placeholder="说明审阅依据或具体问题"></textarea></label><div class="wb-actions"><template v-if="session.role === 'moderator' && detail.state === 'pending'"><button class="primary" :disabled="locked || !reason.trim() || detail.inspection.signer_revoked || detail.release.author_id === session.principal_id" @click="review(true)">批准发布</button><button :disabled="locked || !reason.trim() || detail.release.author_id === session.principal_id" @click="review(false)">拒绝提交</button></template><button v-if="detail.state === 'published'" :disabled="locked || !reason.trim()" @click="withdraw">撤回发行</button><button v-if="detail.state === 'published'" :disabled="locked || !reason.trim()" @click="report">举报发行</button></div></section></template><div v-else class="empty"><h2>选择一个提交</h2><p>核对原始说明、实际文件和前一发布的变化。</p></div></article>
      </section>
      <section v-else-if="tab === 'reports'" class="wb-grid"><aside class="wb-card wb-list"><div class="section-heading"><h2>待处理举报</h2><button :disabled="locked" @click="loadReports()">刷新</button></div><p v-if="!reports?.items.length" class="muted">此页暂无举报</p><button v-for="entry in reports?.items" :key="entry.id" class="wb-item" :class="{ chosen: selectedReport?.id === entry.id }" :disabled="locked" @click="selectReport(entry)"><strong>#{{ entry.id }} · {{ entry.actor }}</strong><small>{{ entry.release_id }}</small><span>{{ entry.reason }}</span></button><button :disabled="locked || reports?.next_cursor == null" @click="loadReports(reports?.next_cursor || 0)">下一页举报</button></aside><article class="wb-card"><template v-if="selectedReport"><h2>举报 #{{ selectedReport.id }}</h2><p class="package-id">发行 {{ selectedReport.release_id }} · {{ time(selectedReport.timestamp) }}</p><pre class="changelog">{{ selectedReport.reason }}</pre><template v-if="detail"><PackageInspection :release="detail.release" :inspection="detail.inspection" :previous="detail.previous" :busy="locked" paged @page="inspect(detail.submission_id, $event)"/><div class="wb-actions"><button :disabled="locked" @click="download">校验原 ZIP</button><a v-if="prepared" class="save-archive" :href="prepared.url" :download="prepared.name">保存已校验 ZIP</a><button v-if="detail.state === 'published'" :disabled="locked || !reason.trim()" @click="withdraw">撤回发行</button></div></template><label>处理原因<textarea v-model="reason" rows="4" maxlength="2000" :disabled="locked"></textarea></label><div class="wb-actions"><button class="primary" :disabled="locked || !reason.trim()" @click="resolve('addressed')">标记已处理</button><button :disabled="locked || !reason.trim()" @click="resolve('dismissed')">驳回举报</button></div></template><p v-else class="empty">选择举报并填写处理原因。</p></article></section>
      <section v-else class="wb-card"><div class="section-heading"><h2>我的有效会话 · {{ sessions.length }}/8</h2><button :disabled="locked" @click="read(async () => { sessions = await client.sessions() })">刷新</button></div><p class="muted">只列出当前身份的会话。撤销当前会话会退出此页面。</p><div v-for="entry in sessions" :key="entry.id" class="wb-session-row"><div><strong>{{ entry.current ? '当前浏览器' : '其他浏览器' }}</strong><code>{{ entry.id }}</code><small>创建 {{ time(entry.issued_at) }} · 到期 {{ time(entry.expires_at) }}</small></div><button :disabled="locked" @click="revokeChoice = entry.id">撤销会话</button></div><div v-if="revokeChoice" class="banner warning"><span>确认撤销会话 {{ revokeChoice }}？</span><div class="wb-actions"><button :disabled="locked" @click="revoke">确认撤销</button><button :disabled="locked" @click="revokeChoice = null">取消</button></div></div></section>
    </template>
    <section v-if="pending && matched" ref="confirmation" class="wb-card wb-confirm" tabindex="-1" aria-label="原操作确认"><p class="eyebrow">{{ pending.phase === 'confirm' ? 'Review & confirm' : 'Reconcile original operation' }}</p><h2>{{ pending.intent.label }}</h2><p class="package-id">{{ pending.intent.target }}</p><dl v-if="pending.intent.kind === 'submit' && preview" class="facts"><dt>作者与许可</dt><dd>{{ preview.release.author_id }} · {{ preview.release.license }}</dd><dt>声明权限</dt><dd>{{ preview.release.permissions.join(', ') || '无' }}</dd><dt>原归档 SHA-256</dt><dd><code>{{ preview.inspection.archive_sha256 }}</code></dd></dl><p>原操作编号 <code>{{ pending.intent.operation_id }}</code></p><p v-if="pending.intent.body.reason" class="prose">{{ pending.intent.body.reason }}</p><p v-if="pending.phase === 'unknown'" class="banner warning">写入结果尚未确认。先查询原编号；找到回执后不会再次写入。</p><p v-if="pending.phase === 'missing' && !pending.retryable" class="banner warning">原会话失效后已清空正文，只保留回执编号。可继续查询原结果；如需重试，请将原内容与原编号交由管理员核对。</p><label v-if="pending.phase !== 'unknown' && pending.retryable" class="wb-check"><input v-model="accepted" type="checkbox" :disabled="busy">我已审阅上述内容，确认以此原编号执行。</label><div class="wb-actions"><button v-if="pending.phase !== 'unknown' && pending.retryable" class="primary" :disabled="busy || !accepted" @click="execute">{{ pending.tried ? '按原编号与原内容重试' : '确认执行' }}</button><button v-if="pending.tried" :disabled="busy" @click="reconcile">只读查询原编号</button><button v-if="!pending.tried" :disabled="busy" @click="pending = null; accepted = false">返回修改</button></div></section>
    <p v-if="busy" class="wb-working" role="status">正在等待服务结果…</p>
  </main><footer><span>OpenNexus Community</span><span>签名内容 · 独立审核 · 原操作回执</span></footer>
</template>
