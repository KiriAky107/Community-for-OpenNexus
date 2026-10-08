export const packageTypes = ['theme', 'skill', 'plugin', 'mcp', 'persona', 'template', 'model'] as const
export type PackageType = typeof packageTypes[number]
export const typeNames: Record<PackageType, string> = {
  theme: '主题', skill: '技能', plugin: '插件', mcp: 'MCP', persona: '角色', template: '模板', model: '模型配置',
}
export interface Metadata {
  schema_version: 1; namespace: string; package_id: string; type: PackageType
  version: string; name: string; author_id: string; license: string
  description: string; sha256: string; size: number; platforms: string[]; architectures: string[]
  min_app_version: string; max_app_version: string | null; dependencies: Record<string, string>
  permissions: string[]; changelog: string; published_at: string; key_id: string; signature: string
}
export interface Release extends Metadata { release_id: string; withdrawn: boolean; download_path: string }
export interface Page { schema_version: 1; total: number; offset: number; limit: number; items: Release[] }
export interface SourceKey { key_id: string; namespace: string; public_key: string; revoked: boolean }
export interface Sources { schema_version: 1; source_id: string; keys: SourceKey[] }
export interface ReadResult<T> { data: T; freshness: 'network' | 'revalidated' | 'cached'; checkedAt: number }

function record(value: unknown): asserts value is Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('INVALID_RESPONSE')
}
function string(value: unknown, max = 10000): asserts value is string {
  if (typeof value !== 'string' || value.length > max) throw new Error('INVALID_RESPONSE')
}
function strings(value: unknown, max: number): asserts value is string[] {
  if (!Array.isArray(value) || value.length > max) throw new Error('INVALID_RESPONSE')
  value.forEach(item => string(item, 1000))
}
export function parseMetadata(value: unknown): Metadata {
  record(value)
  if (value.schema_version !== 1 || !packageTypes.includes(value.type as PackageType)) throw new Error('INVALID_RESPONSE')
  for (const key of ['namespace', 'package_id', 'version', 'name', 'author_id', 'license', 'min_app_version', 'published_at', 'key_id', 'signature']) string(value[key], 160)
  for (const key of ['namespace', 'package_id']) if (!/^[a-z0-9][a-z0-9-]{1,63}$/.test(value[key] as string)) throw new Error('INVALID_RESPONSE')
  string(value.description); string(value.changelog); string(value.sha256, 64)
  if (!/^[a-f0-9]{64}$/.test(value.sha256 as string) || !Number.isInteger(value.size) || (value.size as number) <= 0 || (value.size as number) > 10 * 1024 * 1024) throw new Error('INVALID_RESPONSE')
  if (value.max_app_version !== null) string(value.max_app_version, 120)
  strings(value.platforms, 12); strings(value.architectures, 12); strings(value.permissions, 64)
  record(value.dependencies)
  if (Object.keys(value.dependencies).length > 64) throw new Error('INVALID_RESPONSE')
  for (const [key, constraint] of Object.entries(value.dependencies)) { string(key, 1000); string(constraint, 1000) }
  return value as unknown as Metadata
}
export function parseRelease(value: unknown): Release {
  parseMetadata(value); record(value)
  string(value.release_id, 160)
  if (!/^[a-zA-Z0-9-]{1,160}$/.test(value.release_id as string)) throw new Error('INVALID_RESPONSE')
  if (typeof value.withdrawn !== 'boolean') throw new Error('INVALID_RESPONSE')
  // Never follow a catalog-provided URL. Use the server's fixed archive route.
  return { ...value, download_path: `/catalog/v1/releases/${encodeURIComponent(value.release_id as string)}/archive` } as unknown as Release
}
export function parsePage(value: unknown): Page {
  record(value)
  if (value.schema_version !== 1 || !Number.isSafeInteger(value.total) || (value.total as number) < 0 || !Number.isSafeInteger(value.offset) || (value.offset as number) < 0 || !Number.isInteger(value.limit) || (value.limit as number) < 1 || (value.limit as number) > 100 || !Array.isArray(value.items) || value.items.length > (value.limit as number)) throw new Error('INVALID_RESPONSE')
  return { ...value, items: value.items.map(parseRelease) } as unknown as Page
}
export function parseSources(value: unknown): Sources {
  record(value); string(value.source_id, 160)
  if (value.schema_version !== 1 || !Array.isArray(value.keys)) throw new Error('INVALID_RESPONSE')
  const keys = value.keys.map(item => {
    record(item); string(item.namespace, 64); string(item.key_id, 80); string(item.public_key, 44)
    if (!/^[A-Za-z0-9+/]{43}=$/.test(item.public_key as string) || typeof item.revoked !== 'boolean') throw new Error('INVALID_RESPONSE')
    return item as unknown as SourceKey
  })
  if (new Set(keys.map(key => key.key_id)).size !== keys.length) throw new Error('INVALID_RESPONSE')
  return { schema_version: 1, source_id: value.source_id, keys }
}
