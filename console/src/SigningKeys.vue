<script setup lang="ts">
import type { KeyPage, SigningKey } from './workbenchTypes.ts'
defineProps<{ page: KeyPage | null; selected: SigningKey | null; query: { q: string; state: string }; reason: string; moderator: boolean; busy: boolean }>()
defineEmits<{ search: []; page: [offset: number]; select: [key: SigningKey]; revoke: []; 'update:reason': [value: string] }>()
const time = (value: number) => new Date(value * 1000).toLocaleString('zh-CN', { hour12: false })
</script>
<template>
  <section class="wb-grid" aria-label="签名公钥">
    <aside class="wb-card wb-list">
      <h2>签名公钥 · {{ page?.total || 0 }}</h2><p class="muted">{{ moderator ? '审核员可查看全部已登记公钥。' : '仅列出当前作者命名空间的公钥。' }}</p>
      <form @submit.prevent="$emit('search')"><label>公钥搜索<input v-model="query.q" maxlength="120" :disabled="busy" placeholder="编号或命名空间的字面文本"></label><label>公钥状态<select v-model="query.state" :disabled="busy"><option value="">全部状态</option><option value="active">有效</option><option value="revoked">已撤销</option></select></label><button :disabled="busy">筛选公钥</button></form>
      <small v-if="page" class="muted">读取时间 {{ time(page.observed_at) }} · 刷新后获取当前状态</small>
      <p v-if="!page?.items.length" class="muted">此页暂无公钥。</p>
      <button v-for="key in page?.items" :key="key.key_id" class="wb-item" :class="{ chosen: selected?.key_id === key.key_id }" :disabled="busy" @click="$emit('select', key)"><strong>{{ key.key_id }}</strong><small>{{ key.namespace }}</small><span>{{ key.revoked ? '已撤销' : '有效' }}</span></button>
      <nav v-if="page" class="pagination compact" aria-label="公钥分页"><button :disabled="busy || !page.offset" @click="$emit('page', Math.max(0, page.offset - 20))">上一页公钥</button><span>{{ Math.floor(page.offset / 20) + 1 }}</span><button :disabled="busy || page.offset + page.limit >= page.total" @click="$emit('page', page.offset + page.limit)">下一页公钥</button></nav>
    </aside>
    <article class="wb-card"><template v-if="selected">
      <h2>公钥 {{ selected.key_id }}</h2><dl class="facts"><dt>命名空间</dt><dd>{{ selected.namespace }}</dd><dt>状态</dt><dd>{{ selected.revoked ? '已撤销' : '有效' }}</dd><dt>Ed25519 公钥 · Base64</dt><dd><code>{{ selected.public_key }}</code></dd><dt>原32字节 SHA-256 指纹</dt><dd><code>{{ selected.fingerprint }}</code></dd><dt>当前状态审核摘要</dt><dd><code>{{ selected.review_digest }}</code></dd></dl>
      <p class="muted">公钥由管理员在服务端核对来源后登记。作者私钥保留离线，不在网页上传。撤销不会改写公钥或删除发行原件。</p>
      <template v-if="moderator && !selected.revoked"><p class="banner warning">撤销后，使用此公钥的发行无法再提交、批准或下载。已安装并应用的用户数据保留。</p><label>撤销原因<textarea :value="reason" maxlength="2000" rows="3" :disabled="busy" @input="$emit('update:reason', ($event.target as HTMLTextAreaElement).value)"></textarea></label><button :disabled="busy || !reason.trim()" @click="$emit('revoke')">审阅后撤销公钥</button></template>
    </template><p v-else class="empty">选择一个公钥，核对原字节、指纹与当前状态。</p></article>
  </section>
</template>
