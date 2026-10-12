<script setup lang="ts">
import OriginalText from './OriginalText.vue'
import { formatDate, t } from './locale.ts'
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { availability, CatalogClient, detailUrl, groupsUrl, message, versionsUrl } from './api.ts'
import type { Compatibility } from './api.ts'
import { packageTypes, typeNames } from './types.ts'
import type { GroupPage, PackageGroup, PackageType, Page, ReadResult, Release, Sources } from './types.ts'
import LanguageSelector from './LanguageSelector.vue'
import logo from './assets/opennexus-logo.svg'

const client = new CatalogClient()
const query = ref(''), kind = ref<PackageType | ''>(''), appliedQuery = ref(''), offset = ref(0)
const appliedKind = ref<PackageType | ''>('')
const filters = ref<Compatibility>({ app_version: '', platform: '', architecture: '' })
const appliedFilters = ref<Compatibility>({ ...filters.value })
const filterSummary = computed(() => Object.values(appliedFilters.value).filter(Boolean).join(' · '))
const page = ref<ReadResult<GroupPage> | null>(null), versions = ref<ReadResult<Page> | null>(null)
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
const sourceName = computed(() => sources.value?.source_id || t('来源尚未确认'))
const busy = computed(() => loading.value || downloading.value)
const formatTime = (time: number) => formatDate(time)
const size = (bytes: number) => bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KiB`
const freshness = (result: ReadResult<unknown>) => result.freshness === 'cached' ? '离线缓存' : result.freshness === 'revalidated' ? '已重新确认' : '实时读取'
function cardState(group: PackageGroup): string {
  const release = group.release
  if (group.availability === 'invalid_version') return '版本声明无效'
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
  const reads = packageIdentity
    ? client.page(versionsUrl(packageIdentity.namespace, packageIdentity.id, versionOffset.value)).then(result => ({ kind: 'releases' as const, result }))
    : client.groups(groupsUrl(appliedQuery.value, appliedKind.value, offset.value, appliedFilters.value)).then(result => ({ kind: 'groups' as const, result }))
  const [listing, sourceRead] = await Promise.allSettled([reads, client.sources()])
  if (ticket !== generation) return
  if (sourceRead.status === 'fulfilled') sources.value = sourceRead.value.data
  else sourceError.value = '暂时无法确认公钥状态，下载已禁用。'
  if (listing.status === 'rejected') { error.value = message(listing.reason); loading.value = false; return }
  if (listing.value.kind === 'groups') { page.value = listing.value.result; loading.value = false; return }
  if (!packageIdentity) { loading.value = false; return }
  const versionListing = listing.value.result
  versions.value = versionListing
  const version = params.get('version') || ''
  try {
    let item = versionListing.data.items.find(release => !version || release.version === version)
    let itemRead = versionListing
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
  filters.value = {
    app_version: (params.get('app_version') || '').slice(0, 120),
    platform: (params.get('platform') || '').slice(0, 80),
    architecture: (params.get('architecture') || '').slice(0, 80),
  }
  appliedFilters.value = { ...filters.value }
  const start = Number(params.get('offset') || 0)
  offset.value = Number.isSafeInteger(start) && start >= 0 && start <= 2147483647 ? start : 0
  versionOffset.value = 0
  void load()
}
function search(start = -1): void {
  if (busy.value) return
  offset.value = Math.max(0, start)
  if (start < 0) {
    appliedQuery.value = query.value.trim(); appliedKind.value = kind.value
    appliedFilters.value = Object.fromEntries(Object.entries(filters.value).map(([key, value]) => [key, value.trim()])) as unknown as Compatibility
  }
  const params = new URLSearchParams({ q: appliedQuery.value, offset: String(offset.value) })
  if (appliedKind.value) params.set('type', appliedKind.value)
  for (const [key, value] of Object.entries(appliedFilters.value)) if (value) params.set(key, value)
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
    <LanguageSelector/>
    <div class="source-pill"><i :class="{ confirmed: sources }"></i>{{ sourceName }}</div>
    <a class="wb-catalog-link" href="/workbench">{{ t('作者与审核') }}</a>
    <a class="docs-link" href="https://github.com/KiriAky107/Community-for-OpenNexus#quick-start" target="_blank" rel="noreferrer">{{ t('部署与使用 ↗') }}</a>
  </header>
  <main>
    <template v-if="!identity">
      <section class="intro"><p class="eyebrow">OPENNEXUS / EXTENSIONS</p><h1>{{ t('找到适合你的') }}<span>{{ t('知识工作流。') }}</span></h1><p>{{ t('浏览主题、技能与工具，先了解许可、兼容范围和权限，再把扩展带入你的工作区。') }}</p></section>
      <form class="search" @submit.prevent="search()">
        <label class="search-input"><span>{{ t('搜索目录') }}</span><input v-model="query" :aria-label="t('搜索目录')" maxlength="120" :placeholder="t('名称或描述，例如：笔记、研究…')" :disabled="busy" /></label>
        <label><span>{{ t('类型') }}</span><select v-model="kind" :aria-label="t('扩展类型')" :disabled="busy"><option value="">{{ t('全部类型') }}</option><option v-for="type in packageTypes" :key="type" :value="type">{{ t(typeNames[type]) }}</option></select></label>
        <label class="compat-filter"><span>{{ t('OpenNexus 版本') }}</span><input v-model="filters.app_version" :aria-label="t('OpenNexus 版本')" maxlength="120" :placeholder="t('例如 0.6.0-beta3')" :disabled="busy" /></label>
        <label class="compat-filter"><span>{{ t('平台') }}</span><input v-model="filters.platform" :aria-label="t('平台')" maxlength="80" list="platforms" :placeholder="t('全部平台')" :disabled="busy" /></label>
        <label class="compat-filter"><span>{{ t('架构') }}</span><input v-model="filters.architecture" :aria-label="t('架构')" maxlength="80" list="architectures" :placeholder="t('全部架构')" :disabled="busy" /></label>
        <datalist id="platforms"><option value="windows"/><option value="linux"/><option value="macos"/></datalist>
        <datalist id="architectures"><option value="x86_64"/><option value="aarch64"/></datalist>
        <div class="search-actions"><button class="primary" type="submit" :disabled="busy">{{ t('搜索') }}</button><button type="button" :disabled="busy" @click="load">{{ t('刷新') }}</button></div>
      </form>
      <div v-if="page" class="results-bar"><p><strong>{{ t('扩展数量', { count: page.data.total }) }}</strong><span v-if="appliedQuery"> · “{{ appliedQuery }}”</span><span v-if="filterSummary"> · {{ filterSummary }}</span><span class="muted"> {{ t('· 每个扩展显示一个版本') }}</span></p><small :class="{ warning: page.freshness === 'cached' }">{{ t(freshness(page)) }} · {{ formatTime(page.checkedAt) }}</small></div>
    </template>
    <template v-else><a class="back" href="/">{{ t('← 返回目录') }}</a><div class="detail-top"><p class="eyebrow">{{ identity.namespace }} / {{ identity.id }}</p><button :disabled="busy" @click="load">{{ t('重新确认') }}</button></div></template>
    <p v-if="sourceError" class="banner warning" role="status">{{ t(sourceError) }}</p>
    <p v-if="error" class="banner error" role="alert">{{ t(error) }}<button :disabled="busy" @click="load">{{ t('重试') }}</button></p>
    <p v-if="notice" class="banner success" role="status">{{ t(notice) }}</p>
    <p v-if="loading" class="loading" role="status">{{ t('正在读取目录与来源…') }}</p>
    <template v-if="page && !identity">
      <p v-if="page.freshness === 'cached'" class="banner warning">{{ t('服务暂时不可用，显示本次会话中相同查询的缓存。详情下载需要重新连接确认。') }}</p>
      <div v-if="page.data.items.length" class="catalog-grid">
        <a v-for="group in page.data.items" :key="`${group.namespace}/${group.package_id}`" class="package-card" :href="detailUrl(group.release)">
          <div class="card-top"><span class="type-badge">{{ t(typeNames[group.release.type]) }}</span><span v-if="group.release.withdrawn" class="state withdrawn">{{ t('已撤回') }}</span><span v-else class="state" :class="{ available: cardState(group) === '可下载' }">{{ t(cardState(group)) }}</span></div>
          <h2>{{ group.release.name }}</h2><p class="package-id">{{ group.namespace }}/{{ group.package_id }}</p><p class="description">{{ group.release.description || t('暂无说明') }}</p>
          <p class="version-summary">{{ t('版本筛选数量', { total: group.version_count, matching: group.matching_version_count }) }}</p>
          <p v-if="group.selection === 'earlier_available'" class="version-summary warning">{{ t('已选择较早可用版本') }} {{ group.release.version }} {{ t('· 最新发布') }} {{ group.latest_version }}</p>
          <p v-else-if="group.selection === 'unavailable'" class="version-summary warning">{{ t('符合筛选的发行均不可用，打开详情核对状态。') }}</p>
          <div class="card-footer"><strong>{{ group.release.version }}</strong><span>{{ group.release.license }}</span><span class="view">{{ t('查看详情 →') }}</span></div>
        </a>
      </div>
      <div v-else class="empty"><h2>{{ t('没有匹配的扩展') }}</h2><p>{{ t('试试其他关键词、扩展类型或兼容条件。') }}</p></div>
      <nav class="pagination" :aria-label="t('目录分页')"><button :disabled="busy || page.data.offset === 0" @click="search(Math.max(0, page.data.offset - page.data.limit))">{{ t('上一页') }}</button><span>{{ page.data.total ? page.data.offset + 1 : 0 }}–{{ page.data.offset + page.data.items.length }} / {{ page.data.total }}</span><button :disabled="busy || page.data.offset + page.data.limit >= page.data.total" @click="search(page.data.offset + page.data.limit)">{{ t('下一页') }}</button></nav>
    </template>
    <section v-if="selected && identity" class="detail-grid">
      <article class="detail-body">
        <div class="detail-heading"><span class="type-badge">{{ t(typeNames[selected.data.type]) }}</span><h1>{{ selected.data.name }}</h1><p>{{ selected.data.version }} · {{ selected.data.author_id }} · {{ selected.data.license }}</p></div>
        <OriginalText :text="selected.data.description || t('暂无说明')" :label="t('包说明')" prose />
        <section class="detail-section"><h2>{{ t('兼容与权限') }}</h2><dl class="facts"><dt>OpenNexus</dt><dd>≥ {{ selected.data.min_app_version }}<template v-if="selected.data.max_app_version">，≤ {{ selected.data.max_app_version }}</template></dd><dt>{{ t('平台 / 架构') }}</dt><dd>{{ selected.data.platforms.join('、') || t('未声明') }} / {{ selected.data.architectures.join('、') || t('未声明') }}</dd><dt>{{ t('请求权限') }}</dt><dd><ul v-if="selected.data.permissions.length"><li v-for="permission in selected.data.permissions" :key="permission"><code>{{ permission }}</code></li></ul><span v-else>{{ t('未声明权限') }}</span></dd><dt>{{ t('依赖') }}</dt><dd><ul v-if="Object.keys(selected.data.dependencies).length"><li v-for="(constraint, name) in selected.data.dependencies" :key="name"><code>{{ name }} {{ constraint }}</code></li></ul><span v-else>{{ t('无声明依赖') }}</span></dd></dl></section>
        <section class="detail-section"><h2>{{ t('更新说明') }}</h2><OriginalText :text="selected.data.changelog || t('暂无更新说明')" :label="t('更新说明')" /></section>
        <section class="detail-section"><h2>{{ t('发行记录') }}</h2><dl class="facts"><dt>{{ t('发布于') }}</dt><dd>{{ formatDate(selected.data.published_at) }}</dd><dt>SHA-256</dt><dd><code class="break">{{ selected.data.sha256 }}</code></dd><dt>{{ t('签名') }}</dt><dd>Ed25519 · {{ selected.data.key_id }}<details><summary>{{ t('查看签名与公钥') }}</summary><p>{{ t('签名') }} <code class="break">{{ selected.data.signature }}</code></p><p>{{ t('公钥') }} <code class="break">{{ key?.public_key || t('当前来源未提供') }}</code></p><p class="muted">{{ t('归档下载核对大小与哈希；密码学签名验证由 OpenNexus 安装流程完成。') }}</p></details></dd></dl></section>
      </article>
      <aside class="detail-aside">
        <section class="download-card"><p class="eyebrow">{{ sourceName }}</p><h2>{{ selected.data.version }}</h2><p :class="{ warning: reason, available: !reason }">{{ t(reason || '发行与公钥状态可用') }}</p><button class="primary" :disabled="busy || !!reason" @click="download">{{ downloading ? t('正在校验归档…') : t('下载归档', { size: size(selected.data.size) }) }}</button><a v-if="prepared" class="save-archive" :href="prepared.url" :download="prepared.name">{{ t('保存已校验 ZIP ↓') }}</a><small>{{ t(freshness(selected)) }} · {{ formatTime(selected.checkedAt) }}</small><p class="muted">{{ t('在桌面端安装前，确认兼容性、签名和所需权限。') }}</p></section>
        <section v-if="versions" class="version-card"><div class="section-heading"><h2>{{ t('版本记录') }}</h2><small>{{ t('发行数量', { count: versions.data.total }) }}</small></div><button v-for="release in versions.data.items" :key="release.release_id" class="version" :class="{ chosen: release.release_id === selected.data.release_id }" :disabled="busy" @click="selectVersion(release)"><span>{{ release.version }}</span><small>{{ release.withdrawn ? t('已撤回') : t('查看') }}</small></button><nav class="pagination compact" :aria-label="t('版本分页')"><button :disabled="busy || versionOffset === 0" @click="changeVersions(Math.max(0, versionOffset - 20))">{{ t('上一页') }}</button><button :disabled="busy || versionOffset + 20 >= versions.data.total" @click="changeVersions(versionOffset + 20)">{{ t('下一页') }}</button></nav></section>
      </aside>
    </section>
  </main>
  <footer><span>OpenNexus Community</span><span>{{ t('公开目录 · 包元数据按原文展示') }}</span><a href="https://github.com/KiriAky107/Community-for-OpenNexus" target="_blank" rel="noreferrer">GitHub ↗</a></footer>
</template>
