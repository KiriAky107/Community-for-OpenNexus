<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { availability, CatalogClient, catalogUrl, detailUrl, message, versionsUrl } from './api.ts'
import { packageTypes, typeNames } from './types.ts'
import type { PackageType, Page, ReadResult, Release, Sources } from './types.ts'
import logo from './assets/opennexus-logo.svg'

const client = new CatalogClient()
const query = ref(''), kind = ref<PackageType | ''>(''), appliedQuery = ref(''), offset = ref(0)
const appliedKind = ref<PackageType | ''>('')
const page = ref<ReadResult<Page> | null>(null), versions = ref<ReadResult<Page> | null>(null)
const selected = ref<ReadResult<Release> | null>(null), sources = ref<Sources | null>(null)
const loading = ref(false), downloading = ref(false), error = ref(''), notice = ref('')
const sourceError = ref(''), versionOffset = ref(0), path = ref(location.pathname)
const prepared = ref<{ url: string; name: string } | null>(null)
let generation = 0
function clearPrepared(): void {
  if (prepared.value) URL.revokeObjectURL(prepared.value.url)
  prepared.value = null
}
const identity = computed(() => {
  const parts = path.value.match(/^\/packages\/([a-z0-9][a-z0-9-]{1,63})\/([a-z0-9][a-z0-9-]{1,63})\/?$/)
  return parts ? { namespace: parts[1]!, id: parts[2]! } : null
})
const reason = computed(() => selected.value ? availability(selected.value.data, sources.value, selected.value.freshness !== 'cached') : '尚未选择发行')
const key = computed(() => sources.value?.keys.find(item => item.key_id === selected.value?.data.key_id && item.namespace === selected.value?.data.namespace))
const sourceName = computed(() => sources.value?.source_id || '来源尚未确认')
const busy = computed(() => loading.value || downloading.value)
const formatTime = (time: number) => new Date(time).toLocaleString('zh-CN', { hour12: false })
const size = (bytes: number) => bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KiB`
const freshness = (result: ReadResult<unknown>) => result.freshness === 'cached' ? '离线缓存' : result.freshness === 'revalidated' ? '已重新确认' : '实时读取'
function cardState(release: Release): string {
  const status = availability(release, sources.value, page.value?.freshness !== 'cached')
  if (!status) return '可下载'
  if (status === '签名公钥已撤销') return '公钥已撤销'
  if (status === '签名公钥已移除或未登记') return '公钥未登记'
  return page.value?.freshness === 'cached' ? '离线记录' : '来源未确认'
}

async function load(): Promise<void> {
  const ticket = ++generation
  clearPrepared()
  loading.value = true; error.value = ''; notice.value = ''; sourceError.value = ''
  sources.value = null; page.value = null; selected.value = null; versions.value = null
  path.value = location.pathname
  const params = new URLSearchParams(location.search)
  const packageIdentity = identity.value
  if (!packageIdentity) document.title = 'OpenNexus Community'
  const reads = packageIdentity ? client.page(versionsUrl(packageIdentity.namespace, packageIdentity.id, versionOffset.value)) : client.page(catalogUrl(appliedQuery.value, appliedKind.value, offset.value))
  const [listing, sourceRead] = await Promise.allSettled([reads, client.sources()])
  if (ticket !== generation) return
  if (sourceRead.status === 'fulfilled') sources.value = sourceRead.value.data
  else sourceError.value = '暂时无法确认公钥状态，下载已禁用。'
  if (listing.status === 'rejected') { error.value = message(listing.reason); loading.value = false; return }
  if (!packageIdentity) { page.value = listing.value; loading.value = false; return }
  versions.value = listing.value
  const version = params.get('version') || ''
  try {
    let item = listing.value.data.items.find(release => !version || release.version === version)
    let itemRead = listing.value
    if (version && !item) {
      itemRead = await client.page(versionsUrl(packageIdentity.namespace, packageIdentity.id, 0, version))
      item = itemRead.data.items[0]
    }
    if (!item) throw new Error('HTTP_404')
    const releaseRead = await client.release(item.release_id)
    if (ticket !== generation) return
    if (releaseRead.data.namespace !== packageIdentity.namespace || releaseRead.data.package_id !== packageIdentity.id || (version && releaseRead.data.version !== version)) throw new Error('INVALID_RESPONSE')
    selected.value = releaseRead
    document.title = `${releaseRead.data.name} · ${releaseRead.data.version} — OpenNexus Community`
  } catch (failure) { if (ticket === generation) error.value = message(failure) }
  finally { if (ticket === generation) loading.value = false }
}
function readLocation(): void {
  const params = new URLSearchParams(location.search)
  query.value = (params.get('q') || '').slice(0, 120); appliedQuery.value = query.value
  const type = params.get('type') as PackageType
  kind.value = packageTypes.includes(type) ? type : ''
  appliedKind.value = kind.value
  const start = Number(params.get('offset') || 0)
  offset.value = Number.isSafeInteger(start) && start >= 0 && start <= 2147483647 ? start : 0
  versionOffset.value = 0
  void load()
}
function search(start = -1): void {
  if (busy.value) return
  offset.value = Math.max(0, start)
  if (start < 0) { appliedQuery.value = query.value.trim(); appliedKind.value = kind.value }
  const params = new URLSearchParams({ q: appliedQuery.value, offset: String(offset.value) })
  if (appliedKind.value) params.set('type', appliedKind.value)
  history.pushState(null, '', `/?${params}`)
  void load()
}
function selectVersion(release: Release): void {
  if (busy.value) return
  history.pushState(null, '', detailUrl(release)); void load()
}
function changeVersions(start: number): void {
  if (busy.value) return
  // Preserve the selected release while browsing other version pages.
  if (selected.value) history.replaceState(null, '', detailUrl(selected.value.data))
  versionOffset.value = start; void load()
}
async function download(): Promise<void> {
  if (!selected.value || !key.value || reason.value || busy.value) return
  const reviewed = selected.value.data, reviewedKey = key.value.public_key, ticket = generation
  downloading.value = true; error.value = ''; notice.value = ''
  try {
    const blob = await client.download(reviewed, reviewedKey)
    if (ticket !== generation) return
    clearPrepared()
    prepared.value = { url: URL.createObjectURL(blob), name: `${reviewed.namespace}-${reviewed.package_id}-${reviewed.version.replace(/[^a-zA-Z0-9.+-]/g, '_')}.zip` }
    notice.value = '归档大小与 SHA-256 已核对，请点击“保存已校验 ZIP”。安装时请在 OpenNexus 中确认签名与权限。'
  } catch (failure) {
    if (ticket !== generation) return
    await load()
    if (generation === ticket + 1) error.value = message(failure)
  } finally { downloading.value = false }
}
onMounted(() => { readLocation(); window.addEventListener('popstate', readLocation) })
onUnmounted(() => { generation++; clearPrepared(); window.removeEventListener('popstate', readLocation) })
</script>

<template>
  <header class="topbar">
    <a class="brand" href="/"><img :src="logo" alt="OpenNexus" /><span><strong>OpenNexus</strong><small>Community</small></span></a>
    <div class="source-pill"><i :class="{ confirmed: sources }"></i>{{ sourceName }}</div>
    <a class="docs-link" href="https://github.com/KiriAky107/Community-for-OpenNexus#quick-start" target="_blank" rel="noreferrer">部署与使用 ↗</a>
  </header>
  <main>
    <template v-if="!identity">
      <section class="intro"><p class="eyebrow">OPENNEXUS / EXTENSIONS</p><h1>找到适合你的<span>知识工作流。</span></h1><p>浏览主题、技能与工具，先了解许可、兼容范围和权限，再把扩展带入你的工作区。</p></section>
      <form class="search" @submit.prevent="search()">
        <label class="search-input"><span>搜索目录</span><input v-model="query" aria-label="搜索目录" maxlength="120" placeholder="名称或描述，例如：笔记、研究…" :disabled="busy" /></label>
        <label><span>类型</span><select v-model="kind" aria-label="扩展类型" :disabled="busy"><option value="">全部类型</option><option v-for="type in packageTypes" :key="type" :value="type">{{ typeNames[type] }}</option></select></label>
        <button class="primary" type="submit" :disabled="busy">搜索</button><button type="button" :disabled="busy" @click="load">刷新</button>
      </form>
      <div v-if="page" class="results-bar"><p><strong>{{ page.data.total }}</strong> 个发行<span v-if="appliedQuery"> · “{{ appliedQuery }}”</span><span class="muted"> · 同一扩展可有多个版本</span></p><small :class="{ warning: page.freshness === 'cached' }">{{ freshness(page) }} · {{ formatTime(page.checkedAt) }}</small></div>
    </template>
    <template v-else><a class="back" href="/">← 返回目录</a><div class="detail-top"><p class="eyebrow">{{ identity.namespace }} / {{ identity.id }}</p><button :disabled="busy" @click="load">重新确认</button></div></template>
    <p v-if="sourceError" class="banner warning" role="status">{{ sourceError }}</p>
    <p v-if="error" class="banner error" role="alert">{{ error }}<button :disabled="busy" @click="load">重试</button></p>
    <p v-if="notice" class="banner success" role="status">{{ notice }}</p>
    <p v-if="loading" class="loading" role="status">正在读取目录与来源…</p>
    <template v-if="page && !identity">
      <p v-if="page.freshness === 'cached'" class="banner warning">服务暂时不可用，显示本次会话中相同查询的缓存。详情下载需要重新连接确认。</p>
      <div v-if="page.data.items.length" class="catalog-grid">
        <a v-for="release in page.data.items" :key="release.release_id" class="package-card" :href="detailUrl(release)">
          <div class="card-top"><span class="type-badge">{{ typeNames[release.type] }}</span><span v-if="release.withdrawn" class="state withdrawn">已撤回</span><span v-else class="state" :class="{ available: cardState(release) === '可下载' }">{{ cardState(release) }}</span></div>
          <h2>{{ release.name }}</h2><p class="package-id">{{ release.namespace }}/{{ release.package_id }}</p><p class="description">{{ release.description || '暂无说明' }}</p>
          <div class="card-footer"><strong>{{ release.version }}</strong><span>{{ release.license }}</span><span class="view">查看详情 →</span></div>
        </a>
      </div>
      <div v-else class="empty"><h2>没有匹配的发行</h2><p>试试其他关键词或扩展类型。</p></div>
      <nav class="pagination" aria-label="目录分页"><button :disabled="busy || page.data.offset === 0" @click="search(Math.max(0, page.data.offset - page.data.limit))">上一页</button><span>{{ page.data.total ? page.data.offset + 1 : 0 }}–{{ page.data.offset + page.data.items.length }} / {{ page.data.total }}</span><button :disabled="busy || page.data.offset + page.data.limit >= page.data.total" @click="search(page.data.offset + page.data.limit)">下一页</button></nav>
    </template>
    <section v-if="selected && identity" class="detail-grid">
      <article class="detail-body">
        <div class="detail-heading"><span class="type-badge">{{ typeNames[selected.data.type] }}</span><h1>{{ selected.data.name }}</h1><p>{{ selected.data.version }} · {{ selected.data.author_id }} · {{ selected.data.license }}</p></div>
        <p class="prose">{{ selected.data.description || '暂无说明' }}</p>
        <section class="detail-section"><h2>兼容与权限</h2><dl class="facts"><dt>OpenNexus</dt><dd>≥ {{ selected.data.min_app_version }}<template v-if="selected.data.max_app_version">，≤ {{ selected.data.max_app_version }}</template></dd><dt>平台 / 架构</dt><dd>{{ selected.data.platforms.join('、') || '未声明' }} / {{ selected.data.architectures.join('、') || '未声明' }}</dd><dt>请求权限</dt><dd><ul v-if="selected.data.permissions.length"><li v-for="permission in selected.data.permissions" :key="permission"><code>{{ permission }}</code></li></ul><span v-else>未声明权限</span></dd><dt>依赖</dt><dd><ul v-if="Object.keys(selected.data.dependencies).length"><li v-for="(constraint, name) in selected.data.dependencies" :key="name"><code>{{ name }} {{ constraint }}</code></li></ul><span v-else>无声明依赖</span></dd></dl></section>
        <section class="detail-section"><h2>更新说明</h2><pre class="changelog">{{ selected.data.changelog || '暂无更新说明' }}</pre></section>
        <section class="detail-section"><h2>发行记录</h2><dl class="facts"><dt>发布于</dt><dd>{{ selected.data.published_at }}</dd><dt>SHA-256</dt><dd><code class="break">{{ selected.data.sha256 }}</code></dd><dt>签名</dt><dd>Ed25519 · {{ selected.data.key_id }}<details><summary>查看签名与公钥</summary><p>签名 <code class="break">{{ selected.data.signature }}</code></p><p>公钥 <code class="break">{{ key?.public_key || '当前来源未提供' }}</code></p><p class="muted">归档下载核对大小与哈希；密码学签名验证由 OpenNexus 安装流程完成。</p></details></dd></dl></section>
      </article>
      <aside class="detail-aside">
        <section class="download-card"><p class="eyebrow">{{ sourceName }}</p><h2>{{ selected.data.version }}</h2><p :class="{ warning: reason, available: !reason }">{{ reason || '发行与公钥状态可用' }}</p><button class="primary" :disabled="busy || !!reason" @click="download">{{ downloading ? '正在校验归档…' : `下载归档 · ${size(selected.data.size)}` }}</button><a v-if="prepared" class="save-archive" :href="prepared.url" :download="prepared.name">保存已校验 ZIP ↓</a><small>{{ freshness(selected) }} · {{ formatTime(selected.checkedAt) }}</small><p class="muted">在桌面端安装前，确认兼容性、签名和所需权限。</p></section>
        <section v-if="versions" class="version-card"><div class="section-heading"><h2>版本记录</h2><small>{{ versions.data.total }} 个发行</small></div><button v-for="release in versions.data.items" :key="release.release_id" class="version" :class="{ chosen: release.release_id === selected.data.release_id }" :disabled="busy" @click="selectVersion(release)"><span>{{ release.version }}</span><small>{{ release.withdrawn ? '已撤回' : '查看' }}</small></button><nav class="pagination compact" aria-label="版本分页"><button :disabled="busy || versionOffset === 0" @click="changeVersions(Math.max(0, versionOffset - 20))">上一页</button><button :disabled="busy || versionOffset + 20 >= versions.data.total" @click="changeVersions(versionOffset + 20)">下一页</button></nav></section>
      </aside>
    </section>
  </main>
  <footer><span>OpenNexus Community</span><span>公开目录 · 包元数据按原文展示</span><a href="https://github.com/KiriAky107/Community-for-OpenNexus" target="_blank" rel="noreferrer">GitHub ↗</a></footer>
</template>
