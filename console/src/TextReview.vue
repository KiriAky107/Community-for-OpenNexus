<script setup lang="ts">
import { t } from './locale.ts'
import type { TextDifference, TextSide } from './workbenchTypes.ts'
defineProps<{ value: TextDifference }>()
const labels: Record<TextSide['status'], string> = {
  text: '已按 UTF-8 解码', missing: '该版本没有此文件', previous_unavailable: '前一版本无法校验',
  unsupported_type: '文件类型不在纯文本预览范围', too_large: '文件超过 32 KiB，未读取文本',
  invalid_encoding: '无法按 UTF-8 解码，未替换原字节', binary: '包含二进制控制字节，未展示文本', too_many_lines: '文件超过 1000 行，未展示文本',
}
</script>

<template>
  <section class="detail-section wb-text-review" :aria-label="t('文件文本差异')">
    <h3>{{ t('文件文本差异 ·') }} {{ value.path }}</h3>
    <p class="muted">{{ t('按需读取纯文本；每侧最多 32 KiB / 1000 行，差异最多 32 KiB / 200 行。UTF-8 BOM 不显示，原字节仍以 SHA-256 校验。') }}</p>
    <div class="wb-text-sides">
      <section v-for="(side, index) in [value.before, value.after]" :key="index">
        <h4>{{ index ? t('当前版本') : t('前一版本') }}</h4><p :class="side.status === 'text' ? 'muted' : 'warning'">{{ t(labels[side.status]) }}</p>
        <p v-if="side.file" class="package-id">{{ side.file.bytes }} B · {{ side.file.sha256 }}</p>
        <pre v-if="side.status === 'text'" class="changelog wb-manifest">{{ side.text || t('（空文件）') }}</pre>
      </section>
    </div>
    <h4>{{ t('统一差异') }}</h4>
    <p v-if="value.diff.status === 'unavailable'" class="warning">{{ t('至少一侧不满足文本预览要求，无法生成文本差异。可核对文件哈希和原 ZIP。') }}</p>
    <template v-else>
      <p v-if="value.diff.truncated" class="banner warning">{{ t('差异达到展示上限，已截断；上方前后文本可核对完整的受限文件。') }}</p>
      <pre class="changelog wb-manifest">{{ value.diff.unified || t('文本没有变化。') }}</pre>
    </template>
  </section>
</template>
