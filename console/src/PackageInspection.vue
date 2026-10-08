<script setup lang="ts">
import { typeNames } from './types.ts'
import type { Metadata } from './types.ts'
import type { Inspection, Previous } from './workbenchTypes.ts'
defineProps<{ release: Metadata; inspection: Inspection; previous?: Previous | null; busy: boolean; paged?: boolean }>()
defineEmits<{ page: [offset: number] }>()
const bytes = (value: number) => value < 1024 ? `${value} B` : `${(value / 1024).toFixed(1)} KiB`
</script>

<template>
  <div class="wb-inspection">
    <div class="section-heading"><h2>{{ release.name }}</h2><span class="type-badge">{{ typeNames[release.type] }}</span></div>
    <p class="package-id">{{ release.namespace }}/{{ release.package_id }} · {{ release.version }}</p>
    <p class="prose">{{ release.description }}</p>
    <dl class="facts">
      <dt>作者与许可</dt><dd>{{ release.author_id }} · {{ release.license }}</dd>
      <dt>兼容范围</dt><dd>{{ release.min_app_version }} – {{ release.max_app_version || '未设上限' }} · {{ release.platforms.join(', ') }} · {{ release.architectures.join(', ') }}</dd>
      <dt>声明权限</dt><dd>{{ release.permissions.join(', ') || '无' }}</dd>
      <dt>依赖</dt><dd v-if="!Object.keys(release.dependencies).length">无</dd><dd v-else><div v-for="(range, name) in release.dependencies" :key="name">{{ name }}: {{ range }}</div></dd>
      <dt>归档</dt><dd>{{ bytes(inspection.archive_bytes) }} · {{ inspection.files }} 个文件 · 展开 {{ bytes(inspection.expanded_size) }}</dd>
      <dt>SHA-256</dt><dd><code>{{ inspection.archive_sha256 }}</code></dd>
      <dt>签名</dt><dd :class="inspection.signer_revoked ? 'withdrawn' : 'available'">已验证 · {{ release.key_id }}{{ inspection.signer_revoked ? ' · 公钥已撤销，不能批准' : '' }}</dd>
    </dl>
    <section class="detail-section"><h3>原始更新说明</h3><pre class="changelog">{{ release.changelog }}</pre></section>
    <section class="detail-section"><h3>类型清单 · {{ inspection.manifest }}</h3>
      <p v-if="inspection.manifest_truncated" class="banner warning">清单仅展示前 64 KiB，内容已截断。下载原 ZIP 检查完整内容。</p>
      <pre class="changelog wb-manifest">{{ inspection.manifest_text }}</pre>
    </section>
    <section class="detail-section"><h3>实际文件 · {{ inspection.file_page.total }}</h3>
      <ul class="wb-files"><li v-for="file in inspection.file_page.items" :key="file.path"><strong>{{ file.path }}</strong><span>{{ bytes(file.bytes) }}</span><code>{{ file.sha256 }}</code></li></ul>
      <p v-if="!paged && inspection.file_page.total > inspection.file_page.items.length" class="warning">预检只展示前 100 个文件；提交后可分页查看全部文件。</p>
    </section>
    <section v-if="previous" class="detail-section"><h3>与前一发布的差异{{ previous.version ? ` · ${previous.version}` : '' }}</h3>
      <p v-if="!previous.available" class="warning">前一发布无法校验，当前包仍按实际内容展示。</p>
      <template v-else>
        <p>元数据变化：{{ previous.metadata_fields?.join(', ') || '无' }}</p>
        <p>新增权限：{{ previous.permissions_added?.join(', ') || '无' }}；移除权限：{{ previous.permissions_removed?.join(', ') || '无' }}</p>
        <ul class="wb-files"><li v-for="change in previous.file_page?.items" :key="change.path"><strong>{{ change.path }}</strong><span>{{ { added: '新增', removed: '移除', changed: '修改' }[change.change] }}</span><code>{{ change.before?.sha256 || '无' }} → {{ change.after?.sha256 || '无' }}</code></li></ul>
        <p v-if="!previous.file_page?.total" class="muted">实际文件没有变化。</p>
      </template>
    </section>
    <nav v-if="paged" class="pagination compact" aria-label="文件与差异分页">
      <button :disabled="busy || !inspection.file_page.offset" @click="$emit('page', Math.max(0, inspection.file_page.offset - 100))">前 100 项</button>
      <span>从 {{ inspection.file_page.offset + 1 }} 项起</span>
      <button :disabled="busy || inspection.file_page.offset + 100 >= Math.max(inspection.file_page.total, previous?.file_page?.total || 0)" @click="$emit('page', inspection.file_page.offset + 100)">后 100 项</button>
    </nav>
    <details><summary>完整签名元数据</summary><pre class="changelog wb-manifest">{{ JSON.stringify(release, null, 2) }}</pre></details>
  </div>
</template>
