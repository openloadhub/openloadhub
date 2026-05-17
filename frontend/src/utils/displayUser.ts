export function getCanonicalUserLabel(label?: string | null): string {
  const normalized = String(label || '').trim()
  if (!normalized) {
    return '-'
  }
  if (normalized.toLowerCase() === 'administrator') {
    return 'admin'
  }
  return normalized
}
