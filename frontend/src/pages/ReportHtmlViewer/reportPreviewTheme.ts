export type ReportPreviewTheme = 'light' | 'dark'

const PREVIEW_STYLE_ID = 'olh-report-preview-theme'

const REPORT_PREVIEW_CSS = `
:root {
  --accent: #0d9488 !important;
  --accent-soft: #ccfbf1 !important;
}

body[data-olh-preview-theme] {
  letter-spacing: 0;
}

body[data-olh-preview-theme='light'] {
  --bg: #f4f8f8;
  --panel: #ffffff;
  --line: #d8e4e4;
  --text: #111827;
  --muted: #526171;
  --accent: #0d9488;
  --accent-soft: #ccfbf1;
  background: linear-gradient(180deg, #f8fbfb 0%, #eef6f6 100%) !important;
  color: var(--text) !important;
}

body[data-olh-preview-theme='dark'] {
  color-scheme: dark;
  --bg: #071019;
  --panel: #0b1118;
  --line: #273244;
  --text: #e5edf4;
  --muted: #9aa9ba;
  --accent: #14b8a6;
  --accent-soft: rgba(20, 184, 166, 0.12);
  background: linear-gradient(180deg, #071019 0%, #04080d 100%) !important;
  color: var(--text) !important;
}

body[data-olh-preview-theme] .hero {
  position: relative !important;
  overflow: hidden !important;
  border: 1px solid rgba(20, 184, 166, 0.16) !important;
  border-radius: 16px !important;
  background: linear-gradient(135deg, #111827 0%, #182231 100%) !important;
  box-shadow:
    inset 0 1px 0 rgba(20, 184, 166, 0.16),
    0 16px 36px rgba(15, 23, 42, 0.14) !important;
}

body[data-olh-preview-theme='dark'] .hero {
  background: linear-gradient(135deg, #0b1118 0%, #111827 100%) !important;
  box-shadow:
    inset 0 1px 0 rgba(20, 184, 166, 0.14),
    0 18px 44px rgba(0, 0, 0, 0.4) !important;
}

body[data-olh-preview-theme] .hero::before {
  content: '';
  position: absolute;
  inset: 0 0 auto;
  height: 2px;
  background: linear-gradient(90deg, transparent 0%, rgba(20, 184, 166, 0.7) 50%, transparent 100%);
}

body[data-olh-preview-theme] .hero h1 {
  font-size: 28px !important;
  letter-spacing: 0 !important;
}

body[data-olh-preview-theme] .section {
  border-radius: 14px !important;
  box-shadow: 0 10px 26px rgba(15, 23, 42, 0.055) !important;
}

body[data-olh-preview-theme='dark'] .section {
  border-color: var(--line) !important;
  background: var(--panel) !important;
  box-shadow: none !important;
}

body[data-olh-preview-theme] .meta-item,
body[data-olh-preview-theme] .metric-card,
body[data-olh-preview-theme] .trend-card,
body[data-olh-preview-theme] .ai-summary-box,
body[data-olh-preview-theme] .ai-panel,
body[data-olh-preview-theme] details,
body[data-olh-preview-theme] .empty-state {
  border-radius: 10px !important;
  border-color: var(--line) !important;
}

body[data-olh-preview-theme='light'] .meta-item,
body[data-olh-preview-theme='light'] .metric-card,
body[data-olh-preview-theme='light'] .trend-card,
body[data-olh-preview-theme='light'] .ai-summary-box,
body[data-olh-preview-theme='light'] .ai-panel,
body[data-olh-preview-theme='light'] details,
body[data-olh-preview-theme='light'] .empty-state {
  background: #f8fbfb !important;
}

body[data-olh-preview-theme='dark'] .meta-item,
body[data-olh-preview-theme='dark'] .metric-card,
body[data-olh-preview-theme='dark'] .trend-card,
body[data-olh-preview-theme='dark'] .ai-summary-box,
body[data-olh-preview-theme='dark'] .ai-panel,
body[data-olh-preview-theme='dark'] details,
body[data-olh-preview-theme='dark'] .empty-state {
  background: #0f1722 !important;
}

body[data-olh-preview-theme] .metric-value {
  color: var(--text) !important;
}

body[data-olh-preview-theme] .olh-report-status-value {
  display: inline-flex !important;
  align-items: center;
  width: max-content;
  min-height: 22px;
  padding: 2px 8px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: color-mix(in srgb, var(--bg) 70%, var(--panel) 30%);
  color: var(--muted) !important;
  font-size: 13px !important;
  font-weight: 700 !important;
}

body[data-olh-preview-theme] .olh-report-status-value--failed {
  border-color: #fecaca !important;
  background: #fff1f2 !important;
  color: #b91c1c !important;
}

body[data-olh-preview-theme] .olh-report-status-value--success {
  border-color: rgba(13, 148, 136, 0.28) !important;
  background: rgba(13, 148, 136, 0.08) !important;
  color: #0f766e !important;
}

body[data-olh-preview-theme] .olh-report-status-value--running {
  border-color: #fde68a !important;
  background: #fffbeb !important;
  color: #92400e !important;
}

body[data-olh-preview-theme='dark'] .olh-report-status-value {
  border-color: #273244 !important;
  background: #111827 !important;
  color: #9aa9ba !important;
}

body[data-olh-preview-theme='dark'] .olh-report-status-value--failed {
  border-color: rgba(248, 113, 113, 0.34) !important;
  background: rgba(248, 113, 113, 0.12) !important;
  color: #fca5a5 !important;
}

body[data-olh-preview-theme='dark'] .olh-report-status-value--success {
  border-color: rgba(20, 184, 166, 0.28) !important;
  background: rgba(20, 184, 166, 0.1) !important;
  color: #5eead4 !important;
}

body[data-olh-preview-theme='dark'] .olh-report-status-value--running {
  border-color: rgba(251, 191, 36, 0.28) !important;
  background: rgba(251, 191, 36, 0.1) !important;
  color: #fbbf24 !important;
}

body[data-olh-preview-theme='dark'] .metric-value {
  color: #e5edf4 !important;
}

body[data-olh-preview-theme] a {
  color: var(--accent) !important;
}

body[data-olh-preview-theme] th {
  background: color-mix(in srgb, var(--bg) 72%, var(--panel) 28%) !important;
  color: var(--muted) !important;
}

body[data-olh-preview-theme='dark'] td {
  border-bottom-color: #243041 !important;
}

body[data-olh-preview-theme='dark'] .trend-chart {
  background: #07131d !important;
}

body[data-olh-preview-theme='dark'] .trend-grid-line {
  stroke: #2a3a4d !important;
}

body[data-olh-preview-theme='dark'] .trend-axis-label {
  fill: #9aa9ba !important;
}

body[data-olh-preview-theme] polyline[stroke="#1d4ed8"],
body[data-olh-preview-theme] path[stroke="#1d4ed8"],
body[data-olh-preview-theme] line[stroke="#1d4ed8"],
body[data-olh-preview-theme] circle[stroke="#1d4ed8"] {
  stroke: var(--accent) !important;
}

body[data-olh-preview-theme] polyline[stroke="#7c3aed"],
body[data-olh-preview-theme] path[stroke="#7c3aed"],
body[data-olh-preview-theme] line[stroke="#7c3aed"],
body[data-olh-preview-theme] circle[stroke="#7c3aed"] {
  stroke: #64748b !important;
}

body[data-olh-preview-theme='dark'] polyline[stroke="#7c3aed"],
body[data-olh-preview-theme='dark'] path[stroke="#7c3aed"],
body[data-olh-preview-theme='dark'] line[stroke="#7c3aed"],
body[data-olh-preview-theme='dark'] circle[stroke="#7c3aed"] {
  stroke: #94a3b8 !important;
}

body[data-olh-preview-theme] .trend-legend-dot[style*="#1d4ed8"] {
  background: var(--accent) !important;
}

body[data-olh-preview-theme] .trend-legend-dot[style*="#7c3aed"] {
  background: #64748b !important;
}

body[data-olh-preview-theme='dark'] .trend-legend-dot[style*="#7c3aed"] {
  background: #94a3b8 !important;
}

body[data-olh-preview-theme='dark'] pre {
  border: 1px solid var(--line) !important;
  background: #03070b !important;
}
`

const injectPreviewStyle = (html: string) => {
  const styleTag = `<style id="${PREVIEW_STYLE_ID}">${REPORT_PREVIEW_CSS}</style>`
  if (/<\/head>/i.test(html)) {
    return html.replace(/<\/head>/i, `${styleTag}</head>`)
  }
  return `${styleTag}${html}`
}

const injectPreviewTheme = (html: string, theme: ReportPreviewTheme) => {
  if (!/<body\b/i.test(html)) {
    return html
  }

  return html.replace(/<body\b([^>]*)>/i, (_match, rawAttributes = '') => {
    const attributes = String(rawAttributes).replace(/\sdata-olh-preview-theme=(["']).*?\1/i, '')
    return `<body${attributes} data-olh-preview-theme="${theme}">`
  })
}

const resolveStatusClassName = (value: string) => {
  const normalized = value.trim().toLowerCase()
  if (/(failed|failure|fail|error|aborted|stopped|cancelled|canceled|失败|异常|终止)/.test(normalized)) {
    return 'olh-report-status-value--failed'
  }
  if (/(completed|success|succeeded|passed|done|成功|完成|通过)/.test(normalized)) {
    return 'olh-report-status-value--success'
  }
  if (/(running|pending|queued|generating|进行|运行|等待|生成)/.test(normalized)) {
    return 'olh-report-status-value--running'
  }
  return 'olh-report-status-value--unknown'
}

const decorateReportPreviewHtml = (html: string, theme: ReportPreviewTheme) => {
  if (typeof DOMParser === 'undefined') {
    return injectPreviewStyle(injectPreviewTheme(html, theme))
  }

  const parser = new DOMParser()
  const document = parser.parseFromString(html, 'text/html')
  if (document.querySelector('parsererror') || !document.body) {
    return injectPreviewStyle(injectPreviewTheme(html, theme))
  }

  document.body.setAttribute('data-olh-preview-theme', theme)
  const style = document.createElement('style')
  style.id = PREVIEW_STYLE_ID
  style.textContent = REPORT_PREVIEW_CSS
  document.head.appendChild(style)

  document.querySelectorAll('.meta-item').forEach(item => {
    const label = item.querySelector('.meta-label')?.textContent?.trim().toLowerCase() || ''
    if (!label.includes('运行状态') && !label.includes('run status')) {
      return
    }
    const value = item.querySelector('.meta-value')
    if (!value) {
      return
    }
    value.classList.add('olh-report-status-value', resolveStatusClassName(value.textContent || ''))
  })

  return `<!DOCTYPE html>\n${document.documentElement.outerHTML}`
}

export const buildReportPreviewBlob = async (
  originalBlob: Blob,
  theme: ReportPreviewTheme,
): Promise<Blob> => {
  try {
    const html = await originalBlob.text()
    if (!/<(?:!doctype\s+html|html|head|body)\b/i.test(html)) {
      return originalBlob
    }
    const themedHtml = decorateReportPreviewHtml(html, theme)
    return new Blob([themedHtml], { type: 'text/html;charset=utf-8' })
  } catch {
    return originalBlob
  }
}
