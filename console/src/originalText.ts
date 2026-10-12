/** A prefix preview; the full original remains the separate display source. */
export function originalPreview(text: string): { preview: string; truncated: boolean } {
  let preview = '', characters = 0, lines = 1
  for (const character of text) {
    if (characters === 800 || (character === '\n' && lines === 12)) break
    preview += character; characters++
    if (character === '\n') lines++
  }
  return { preview, truncated: preview.length < text.length }
}
