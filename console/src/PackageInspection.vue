<script setup lang="ts">
import { t } from './locale.ts'
import { nextTick, ref, watch } from 'vue'
import { typeNames } from './types.ts'
import type { Metadata } from './types.ts'
import type { Inspection, Previous, TextDifference } from './workbenchTypes.ts'
import TextReview from './TextReview.vue'
import KeyChanges from './KeyChanges.vue'
import OriginalText from './OriginalText.vue'
const props = defineProps<{ release: Metadata; inspection: Inspection; previous?: Previous | null; textReview?: TextDifference | null; busy: boolean; paged?: boolean }>()
defineEmits<{ page: [offset: number]; text: [path: string] }>()
const textPanel = ref<HTMLElement>()
watch(() => props.textReview, value => { if (value) void nextTick(() => { textPanel.value?.scrollIntoView({ block: 'start', behavior: 'smooth' }); textPanel.value?.focus({ preventScroll: true }) }) })
const bytes = (value: number) => value < 1024 ? `${value} B` : `${(value / 1024).toFixed(1)} KiB`
</script>

<template>
  <div class="wb-inspection">
    <div class="section-heading"><h2>{{ release.name }}</h2><span class="type-badge">{{ t(typeNames[release.type]) }}</span></div>
    <p class="package-id">{{ release.namespace }}/{{ release.package_id }} · {{ release.version }}</p>
    <OriginalText :text="release.description" :label="t('包说明')" prose />
    <dl class="facts">
      <dt>{{ t('作者与许可') }}</dt><dd>{{ release.author_id }} · {{ release.license }}</dd>
      <dt>{{ t('兼容范围') }}</dt><dd>{{ release.min_app_version }} – {{ release.max_app_version || t('未设上限') }} · {{ release.platforms.join(', ') }} · {{ release.architectures.join(', ') }}</dd>
      <dt>{{ t('声明权限') }}</dt><dd>{{ release.permissions.join(', ') || t('无') }}</dd>
      <dt>{{ t('依赖') }}</dt><dd v-if="!Object.keys(release.dependencies).length">{{ t('无') }}</dd><dd v-else><div v-for="(range, name) in release.dependencies" :key="name">{{ name }}: {{ range }}</div></dd>
      <dt>{{ t('归档') }}</dt><dd>{{ bytes(inspection.archive_bytes) }} · {{ t('归档文件数量', { count: inspection.files, expanded: bytes(inspection.expanded_size) }) }}</dd>
      <dt>SHA-256</dt><dd><code>{{ inspection.archive_sha256 }}</code></dd>
      <dt>{{ t('签名') }}</dt><dd :class="inspection.signer_revoked ? 'withdrawn' : 'available'">{{ t('已验证 ·') }} {{ release.key_id }}{{ inspection.signer_revoked ? t(' · 公钥已撤销，不能批准') : '' }}</dd>
    </dl>
    <section class="detail-section"><h3>{{ t('原始更新说明') }}</h3><OriginalText :text="release.changelog" :label="t('原始更新说明')" /></section>
    <section class="detail-section"><h3>{{ t('类型清单 ·') }} {{ inspection.manifest }}</h3>
      <p v-if="inspection.manifest_truncated" class="banner warning">{{ t('清单仅展示前 64 KiB，内容已截断。下载原 ZIP 检查完整内容。') }}</p>
      <pre class="changelog wb-manifest">{{ inspection.manifest_text }}</pre>
    </section>
    <section class="detail-section"><h3>{{ t('实际文件 ·') }} {{ inspection.file_page.total }}</h3>
      <ul class="wb-files"><li v-for="file in inspection.file_page.items" :key="file.path"><strong>{{ file.path }}</strong><span>{{ bytes(file.bytes) }}</span><code>{{ file.sha256 }}</code><button v-if="paged" :disabled="busy" :aria-label="t('查看文本路径', { path: file.path })" @click="$emit('text', file.path)">{{ t('查看文本') }}</button></li></ul>
      <p v-if="!paged && inspection.file_page.total > inspection.file_page.items.length" class="warning">{{ t('预检只展示前 100 个文件；提交后可分页查看全部文件。') }}</p>
    </section>
    <section v-if="previous" class="detail-section"><h3>{{ t('与前一发布的差异') }}{{ previous.version ? ` · ${previous.version}` : '' }}</h3>
      <p v-if="!previous.available" class="warning">{{ t('前一发布无法校验，当前包仍按实际内容展示。') }}</p>
      <template v-else>
        <p>{{ t('元数据变化：') }}{{ previous.metadata_fields?.join(', ') || t('无') }}</p>
        <p>{{ t('新增权限：') }}{{ previous.permissions_added?.join(', ') || t('无') }}{{ t('；移除权限：') }}{{ previous.permissions_removed?.join(', ') || t('无') }}</p>
        <template v-if="previous.key_changes"><h4>{{ t('兼容、依赖与许可关键变化') }}</h4><KeyChanges :items="previous.key_changes"/></template>
        <details v-if="previous.manifest_changes"><summary>{{ t('类型清单关键变化 ·') }} {{ previous.manifest_changes.total }}</summary>
          <template v-if="previous.manifest_changes.status === 'available'"><KeyChanges :items="previous.manifest_changes.items"/><p v-if="previous.manifest_changes.total > previous.manifest_changes.items.length" class="warning">{{ t('变化字段截断', { total: previous.manifest_changes.total }) }}</p></template>
          <p v-else class="warning">{{ previous.manifest_changes.status === 'too_large' ? t('清单超过 32 KiB，未生成字段摘要。') : previous.manifest_changes.status === 'different_type' ? t('清单类型不同，未生成字段摘要。') : t('此清单格式使用纯文本审核，未生成 JSON 字段摘要。') }}</p>
        </details>
        <ul class="wb-files"><li v-for="change in previous.file_page?.items" :key="change.path"><strong>{{ change.path }}</strong><span>{{ { added: t('新增'), removed: t('移除'), changed: t('修改') }[change.change] }}</span><code>{{ change.before?.sha256 || t('无') }} → {{ change.after?.sha256 || t('无') }}</code><button :disabled="busy" :aria-label="t('查看差异路径', { path: change.path })" @click="$emit('text', change.path)">{{ t('查看差异') }}</button></li></ul>
        <p v-if="!previous.file_page?.total" class="muted">{{ t('实际文件没有变化。') }}</p>
      </template>
    </section>
    <div v-if="textReview" ref="textPanel" tabindex="-1"><TextReview :value="textReview"/></div>
    <nav v-if="paged" class="pagination compact" :aria-label="t('文件与差异分页')">
      <button :disabled="busy || !inspection.file_page.offset" @click="$emit('page', Math.max(0, inspection.file_page.offset - 100))">{{ t('前 100 项') }}</button>
      <span>{{ t('文件分页起点', { start: inspection.file_page.offset + 1 }) }}</span>
      <button :disabled="busy || inspection.file_page.offset + 100 >= Math.max(inspection.file_page.total, previous?.file_page?.total || 0)" @click="$emit('page', inspection.file_page.offset + 100)">{{ t('后 100 项') }}</button>
    </nav>
    <details><summary>{{ t('完整签名元数据') }}</summary><pre class="changelog wb-manifest">{{ JSON.stringify(release, null, 2) }}</pre></details>
  </div>
</template>
