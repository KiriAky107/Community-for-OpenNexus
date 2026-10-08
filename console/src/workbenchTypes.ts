import { parseMetadata } from './types.ts'
import type { Metadata } from './types.ts'

export interface Session { schema_version: 1; session_id: string; principal_id: string; role: 'author' | 'moderator'; namespace: string | null; expires_at: number; csrf: string }
export type State = 'pending' | 'published' | 'rejected' | 'withdrawn'
export interface Decision { id: number; actor: string; action: string; reason: string; timestamp: number }
export interface Item { submission_id: string; state: State; release: Metadata; decisions?: Decision[] }
export interface Page { total: number; offset: number; limit: number; items: Item[] }
export interface FileEntry { path: string; bytes: number; sha256: string }
export interface FilePage { total: number; offset: number; limit: number; items: FileEntry[] }
export interface Inspection { files: number; expanded_size: number; manifest: string; manifest_text: string; manifest_truncated: boolean; archive_sha256: string; archive_bytes: number; review_digest: string; signer_revoked: boolean; signature_verified: true; file_page: FilePage }
export interface Change { path: string; change: 'added' | 'removed' | 'changed'; before: FileEntry | null; after: FileEntry | null }
export interface Previous { release_id: string; available: boolean; version?: string; metadata_fields?: string[]; permissions_added?: string[]; permissions_removed?: string[]; file_page?: { total: number; offset: number; limit: number; items: Change[] } }
export interface Detail extends Item { inspection: Inspection; previous: Previous | null; decisions: Decision[] }
export interface Preview { state: 'validated'; release: Metadata; inspection: Inspection }
export interface Report { id: number; actor: string; release_id: string; reason: string; timestamp: number; review_digest: string }
export interface CursorPage<T> { items: T[]; next_cursor: number | null }
export interface BrowserSession { id: string; issued_at: number; expires_at: number; current: boolean }
export type Kind = 'submit' | 'review' | 'withdraw' | 'report' | 'resolve-report'
export interface Intent { principal_id: string; kind: Kind; target: string; operation_id: string; label: string; path: string; body: Readonly<Record<string, unknown>> }
export interface Receipt { schema_version: 1; operation_id: string; state: string; kind?: Kind; target?: string; confirmed_at?: number; submission_id?: string; release_id?: string; report_id?: number; decision?: string; review_digest?: string }

export const stateNames: Record<State, string> = { pending: '待审核', published: '已发布', rejected: '已拒绝', withdrawn: '已撤回' }
export function object(value: unknown): Record<string, unknown> { if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('INVALID_RESPONSE'); return value as Record<string, unknown> }
export function text(value: unknown, max = 10000): string { if (typeof value !== 'string' || value.length > max) throw new Error('INVALID_RESPONSE'); return value }
export function integer(value: unknown): number { if (!Number.isSafeInteger(value) || (value as number) < 0) throw new Error('INVALID_RESPONSE'); return value as number }
export function hash(value: unknown): string { const result = text(value, 64); if (!/^[a-f0-9]{64}$/.test(result)) throw new Error('INVALID_RESPONSE'); return result }
export function id(value: unknown): string { const result = text(value, 160); if (!/^[a-zA-Z0-9-]{1,160}$/.test(result)) throw new Error('INVALID_RESPONSE'); return result }
function bool(value: unknown): boolean { if (typeof value !== 'boolean') throw new Error('INVALID_RESPONSE'); return value }
function array(value: unknown, max: number): unknown[] { if (!Array.isArray(value) || value.length > max) throw new Error('INVALID_RESPONSE'); return value }
function strings(value: unknown): string[] { return array(value, 100).map(item => text(item, 1000)) }
function schema(value: unknown): Record<string, unknown> { const item = object(value); if (item.schema_version !== 1) throw new Error('INVALID_RESPONSE'); return item }
export function parseSession(value: unknown): Session {
  const item = schema(value)
  if (!['author', 'moderator'].includes(item.role as string)) throw new Error('INVALID_RESPONSE')
  return { schema_version: 1, session_id: id(item.session_id), principal_id: text(item.principal_id, 80), role: item.role as Session['role'], namespace: item.namespace === null ? null : text(item.namespace, 64), expires_at: integer(item.expires_at), csrf: hash(item.csrf) }
}
function decisions(value: unknown): Decision[] { return array(value, 20).map(raw => { const item = object(raw); return { id: integer(item.id), actor: text(item.actor, 80), action: text(item.action, 30), reason: text(item.reason, 2000), timestamp: integer(item.timestamp) } }) }
export function parseItem(value: unknown): Item {
  const item = object(value), state = text(item.state, 20) as State
  if (!Object.hasOwn(stateNames, state)) throw new Error('INVALID_RESPONSE')
  return { submission_id: id(item.submission_id), state, release: parseMetadata(item.release), ...(item.decisions ? { decisions: decisions(item.decisions) } : {}) }
}
function pageShape(value: unknown) {
  const item = object(value), limit = integer(item.limit)
  if (limit < 1 || limit > 100) throw new Error('INVALID_RESPONSE')
  return { total: integer(item.total), offset: integer(item.offset), limit, items: array(item.items, limit) }
}
export function parsePage(value: unknown): Page { const page = pageShape(schema(value)); return { ...page, items: page.items.map(parseItem) } }
export function parseReviews(value: unknown): Page { const page = pageShape(schema(value)); return { ...page, items: page.items.map(raw => parseItem({ ...object(raw), state: 'pending' })) } }
function file(value: unknown): FileEntry { const item = object(value); return { path: text(item.path, 65536), bytes: integer(item.bytes), sha256: hash(item.sha256) } }
function parseInspection(value: unknown): Inspection {
  const item = object(value), page = pageShape(item.file_page)
  if (item.signature_verified !== true) throw new Error('INVALID_RESPONSE')
  return { files: integer(item.files), expanded_size: integer(item.expanded_size), manifest: text(item.manifest, 65536), manifest_text: text(item.manifest_text, 65536), manifest_truncated: bool(item.manifest_truncated), archive_sha256: hash(item.archive_sha256), archive_bytes: integer(item.archive_bytes), review_digest: hash(item.review_digest), signer_revoked: bool(item.signer_revoked), signature_verified: true, file_page: { ...page, items: page.items.map(file) } }
}
export function parsePreview(value: unknown): Preview { const item = schema(value); if (item.state !== 'validated') throw new Error('INVALID_RESPONSE'); return { state: 'validated', release: parseMetadata(item.release), inspection: parseInspection(item.inspection) } }
export function parseDetail(value: unknown): Detail {
  const item = schema(value), base = parseItem(item)
  let previous: Previous | null = null
  if (item.previous !== null) {
    const old = object(item.previous)
    previous = { release_id: id(old.release_id), available: bool(old.available) }
    if (previous.available) {
      const page = pageShape(old.file_page)
      previous = { ...previous, version: text(old.version, 120), metadata_fields: strings(old.metadata_fields), permissions_added: strings(old.permissions_added), permissions_removed: strings(old.permissions_removed), file_page: { ...page, items: page.items.map(raw => { const entry = object(raw); if (!['added', 'removed', 'changed'].includes(entry.change as string)) throw new Error('INVALID_RESPONSE'); return { path: text(entry.path, 65536), change: entry.change as Change['change'], before: entry.before === null ? null : file(entry.before), after: entry.after === null ? null : file(entry.after) } }) } }
    }
  }
  return { ...base, inspection: parseInspection(item.inspection), previous, decisions: decisions(item.decisions) }
}
export function parseReports(value: unknown): CursorPage<Report> {
  const item = schema(value)
  return { next_cursor: item.next_cursor === null ? null : integer(item.next_cursor), items: array(item.items, 100).map(raw => { const entry = object(raw); return { id: integer(entry.id), actor: text(entry.actor, 80), release_id: id(entry.release_id), reason: text(entry.reason, 2000), timestamp: integer(entry.timestamp), review_digest: hash(entry.review_digest) } }) }
}
export function parseSessions(value: unknown): BrowserSession[] { const item = schema(value); return array(item.items, 8).map(raw => { const entry = object(raw); return { id: id(entry.id), issued_at: integer(entry.issued_at), expires_at: integer(entry.expires_at), current: bool(entry.current) } }) }
export function parseReceipt(value: unknown): Receipt { const item = schema(value); text(item.operation_id, 32); text(item.state, 30); return item as unknown as Receipt }
export function validateReceipt(value: Receipt, intent: Intent): Receipt {
  if (value.operation_id !== intent.operation_id) throw new Error('RECEIPT_MISMATCH')
  if (value.state === 'not_found') return value
  const states = { submit: 'pending', review: intent.body.approve ? 'published' : 'rejected', withdraw: 'withdrawn', report: 'reported', 'resolve-report': 'resolved' }
  if (value.kind !== intent.kind || value.target !== intent.target || value.state !== states[intent.kind]) throw new Error('RECEIPT_MISMATCH')
  integer(value.confirmed_at)
  if (intent.kind === 'submit') { id(value.submission_id); if (value.review_digest !== intent.body.expected_sha256) throw new Error('RECEIPT_MISMATCH') }
  if (intent.kind === 'review' && (value.submission_id !== intent.target || value.review_digest !== intent.body.expected_sha256)) throw new Error('RECEIPT_MISMATCH')
  if (['withdraw', 'report'].includes(intent.kind) && value.release_id !== intent.target) throw new Error('RECEIPT_MISMATCH')
  if (intent.kind === 'report') integer(value.report_id)
  if (intent.kind === 'resolve-report' && (String(value.report_id) !== intent.target || value.decision !== intent.body.decision)) throw new Error('RECEIPT_MISMATCH')
  return value
}
