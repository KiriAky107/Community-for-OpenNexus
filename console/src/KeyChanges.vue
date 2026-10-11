<script setup lang="ts">
import type { KeyChange } from './workbenchTypes.ts'
defineProps<{ items: KeyChange[] }>()
</script>
<template>
  <div class="wb-key-changes">
    <section v-for="change in items" :key="change.field">
      <h4>{{ change.field }}</h4>
      <div class="wb-text-sides"><div v-for="(value, index) in [change.before, change.after]" :key="index">
        <strong>{{ index ? '当前值' : '原值' }}</strong><pre class="changelog wb-manifest">{{ value.present ? value.value : '未声明' }}</pre>
        <p v-if="value.truncated" class="warning">值仅展示前 2048 个字符；请核对完整签名元数据或原清单。</p>
      </div></div>
    </section>
    <p v-if="!items.length" class="muted">没有关键值变化。</p>
  </div>
</template>
