<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { t } from './locale.ts'
import { originalPreview } from './originalText.ts'
const props = defineProps<{ text: string; label: string; prose?: boolean }>()
const preview = computed(() => originalPreview(props.text)), full = ref<HTMLDetailsElement>()
watch(() => props.text, () => { if (full.value) full.value.open = false })
</script>

<template>
  <section class="original-text" :aria-label="label">
    <pre :class="prose ? 'prose' : 'changelog'">{{ preview.preview }}</pre>
    <template v-if="preview.truncated">
      <p class="muted original-text-note">{{ t('原文预览已缩短，展开可读取全部内容。') }}</p>
      <details ref="full"><summary>{{ t('展开完整原文') }} · {{ label }}</summary><pre :class="prose ? 'prose' : 'changelog'">{{ text }}</pre></details>
    </template>
  </section>
</template>
