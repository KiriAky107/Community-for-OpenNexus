<script setup lang="ts">
import { formatDate, t } from './locale.ts'
import type { AuditEntry, AuditFilters, CursorPage } from './workbenchTypes.ts'
defineProps<{ page: CursorPage<AuditEntry> | null; filters: AuditFilters; busy: boolean; previous: boolean }>()
defineEmits<{ search: []; next: []; back: []; refresh: [] }>()
const time = (value: number) => formatDate(value * 1000)
</script>
<template>
  <section class="wb-card" :aria-label="t('审计记录')">
    <div class="section-heading"><h2>{{ t('审计记录') }}</h2><button :disabled="busy" @click="$emit('refresh')">{{ t('读取最新记录') }}</button></div>
    <p class="muted">{{ t('按编号由新到旧分页。新记录不会挤入已翻阅的下一页；筛选改变后从最新记录重新读取。原因与原回执按原文显示。') }}</p>
    <form class="wb-filters" @submit.prevent="$emit('search')">
      <label>{{ t('字面搜索') }}<input v-model="filters.q" maxlength="120" :disabled="busy" :placeholder="t('身份、动作、目标或原因')"></label>
      <label>{{ t('操作身份') }}<input v-model="filters.actor" maxlength="80" :disabled="busy" :placeholder="t('精确身份编号')"></label>
      <label>{{ t('动作') }}<input v-model="filters.action" maxlength="80" :disabled="busy" :placeholder="t('例如 revoke-key')"></label>
      <label>{{ t('目标') }}<input v-model="filters.subject" maxlength="256" :disabled="busy" :placeholder="t('精确目标编号')"></label>
      <button :disabled="busy">{{ t('应用筛选') }}</button>
    </form>
    <p v-if="!page?.items.length" class="muted">{{ t('此页暂无符合条件的审计记录。') }}</p>
    <article v-for="entry in page?.items" :key="entry.id" class="wb-decision">
      <div class="section-heading"><strong>#{{ entry.id }} · {{ entry.action }}</strong><small>{{ time(entry.timestamp) }}</small></div>
      <p>{{ t('身份') }} <code>{{ entry.actor }}</code> {{ t('· 目标') }} <code>{{ entry.subject }}</code></p>
      <details v-if="entry.reason"><summary>{{ t('查看原始原因或回执') }}</summary><pre class="changelog">{{ entry.reason }}</pre></details>
    </article>
    <nav class="pagination compact" :aria-label="t('审计分页')"><button :disabled="busy || !previous" @click="$emit('back')">{{ t('上一页审计') }}</button><button :disabled="busy || page?.next_cursor == null" @click="$emit('next')">{{ t('下一页审计') }}</button></nav>
  </section>
</template>
