/*
 * @taxueseek/argo-dsh — 工作台样式与安全 Markdown 渲染器
 */

export const CSS = `
.aw-root {
  --aw-brand: #2563eb;
  --aw-brand-strong: #1d4ed8;
  --aw-brand-soft: rgba(37, 99, 235, 0.10);
  --aw-brand-ring: rgba(37, 99, 235, 0.25);
  --aw-grad: linear-gradient(135deg, #0ea5e9, #2563eb 52%, #4f46e5);
  --aw-bg: var(--dsw-alias-bg-base, #f8fafc);
  --aw-layer: var(--dsw-alias-bg-layer-1, #ffffff);
  --aw-sunken: var(--dsw-alias-bg-module-platform, #f1f5f9);
  --aw-text: var(--dsw-alias-label-primary, #0f172a);
  --aw-text-2: var(--dsw-alias-label-secondary, #475569);
  --aw-text-3: var(--dsw-alias-label-tertiary, #64748b);
  --aw-border: var(--dsw-alias-border-l1, rgba(15, 23, 42, 0.10));
  --aw-border-2: var(--dsw-alias-border-l2, rgba(15, 23, 42, 0.16));
  --aw-shadow: var(--dsw-elevation-panel, 0 2px 10px rgba(15, 23, 42, 0.05));
  --aw-shadow-hi: var(--dsw-elevation-prominent, 0 10px 28px rgba(15, 23, 42, 0.10));
  --aw-ok: var(--dsw-alias-state-success-primary, #16a34a);
  --aw-warn: var(--dsw-alias-state-warn-primary, #d97706);
  --aw-err: var(--dsw-alias-state-error-primary, #dc2626);
  position: relative;
  height: 100%;
  width: 100%;
  overflow-y: auto;
  background: var(--aw-bg);
  color: var(--aw-text);
  font-family: var(--dsw-font-family, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif);
}
body[data-ds-dark-theme] .aw-root {
  --aw-brand: #60a5fa;
  --aw-brand-strong: #93c5fd;
  --aw-brand-soft: rgba(96, 165, 250, 0.15);
  --aw-brand-ring: rgba(96, 165, 250, 0.32);
}
.aw-hero {
  min-height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 40px 24px 56px;
  background-image: radial-gradient(58% 38% at 50% 0%, rgba(234, 88, 12, 0.06), transparent 72%);
}
.aw-hero-top {
  position: absolute; top: 14px; right: 18px; display: flex; gap: 8px; align-items: center;
}
.aw-hero-inner { width: 100%; max-width: 780px; display: flex; flex-direction: column; align-items: center; }
.aw-slogan {
  font-size: 30px; font-weight: 600; letter-spacing: 0.20em; margin: 0 0 10px;
  text-align: center; color: var(--aw-text);
}
.aw-sub { font-size: 13px; color: var(--aw-text-3); margin: 0 0 24px; text-align: center; }
.aw-box {
  width: 100%; background: var(--aw-layer); border: 1px solid var(--aw-border-2);
  border-radius: 20px; padding: 14px 16px 12px; box-shadow: var(--aw-shadow);
  transition: border-color .18s, box-shadow .18s;
}
.aw-box:focus-within { border-color: var(--aw-brand); box-shadow: 0 0 0 3px var(--aw-brand-ring), var(--aw-shadow); }
.aw-textarea {
  width: 100%; border: none; outline: none; resize: none; background: transparent;
  color: var(--aw-text); font-size: 15px; line-height: 1.6; min-height: 54px; font-family: inherit;
}
.aw-box-foot { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-top: 10px; flex-wrap: wrap; }
.aw-modes { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.aw-mode-btn {
  border: 1px solid var(--aw-border); background: var(--aw-sunken); color: var(--aw-text-2);
  border-radius: 999px; padding: 5px 13px; font-size: 12px; cursor: pointer; transition: all .15s;
}
.aw-mode-btn:hover { border-color: var(--aw-brand); color: var(--aw-text); }
.aw-mode-btn.active {
  background: var(--aw-brand-soft); border-color: var(--aw-brand); color: var(--aw-brand); font-weight: 600;
}
.aw-topic-chip {
  display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: 999px;
  background: rgba(79, 70, 229, 0.12); border: 1px solid rgba(79, 70, 229, 0.35);
  color: var(--aw-brand); font-size: 12px; font-weight: 600;
}
.aw-topic-chip button {
  border: none; background: transparent; color: inherit; cursor: pointer; font-size: 13px; padding: 0 2px; line-height: 1;
}
.aw-send {
  width: 34px; height: 34px; border-radius: 999px; border: none; cursor: pointer;
  background: var(--aw-grad); color: #fff; display: inline-flex; align-items: center; justify-content: center;
  flex-shrink: 0; transition: transform .15s, opacity .15s;
}
.aw-send:disabled { opacity: 0.45; cursor: not-allowed; }
.aw-send:not(:disabled):hover { transform: translateY(-1px); }

.aw-sec-head {
  width: 100%; display: flex; align-items: center; justify-content: space-between;
  margin: 26px 0 10px; font-size: 12px; font-weight: 600; color: var(--aw-text-3);
}
.aw-presets { width: 100%; display: grid; grid-template-columns: repeat(auto-fit, minmax(175px, 1fr)); gap: 10px; }
.aw-preset {
  display: flex; flex-direction: column; justify-content: space-between; gap: 8px;
  text-align: left; background: var(--aw-layer); border: 1px solid var(--aw-border); border-radius: 14px;
  padding: 13px 14px; cursor: pointer; transition: transform .15s, border-color .15s, box-shadow .15s;
}
.aw-preset:hover { transform: translateY(-2px); border-color: var(--aw-brand); box-shadow: var(--aw-shadow); }
.aw-preset.selected { border-color: var(--aw-brand); background: var(--aw-brand-soft); }
.aw-preset-tag { font-size: 13px; color: var(--aw-text); font-weight: 600; }
.aw-preset-desc { font-size: 12px; color: var(--aw-text-2); line-height: 1.45; }
.aw-preset-try {
  align-self: flex-start; font-size: 11px; color: var(--aw-brand); background: transparent;
  border: none; padding: 0; cursor: pointer; font-weight: 600;
}
.aw-preset-try:hover { text-decoration: underline; }

.aw-topbar {
  position: sticky; top: 0; z-index: 30; display: flex; align-items: center; gap: 8px;
  padding: 10px 18px; background: var(--aw-bg); border-bottom: 1px solid var(--aw-border);
  backdrop-filter: blur(10px); flex-wrap: wrap;
}
.aw-icon-btn {
  height: 32px; padding: 0 10px; border-radius: 10px; border: 1px solid var(--aw-border);
  background: var(--aw-layer); color: var(--aw-text-2); font-size: 12px; cursor: pointer;
  display: inline-flex; align-items: center; gap: 6px; flex-shrink: 0;
}
.aw-icon-btn:hover { border-color: var(--aw-brand); color: var(--aw-text); }
.aw-select {
  height: 32px; padding: 0 8px; border-radius: 10px; border: 1px solid var(--aw-border);
  background: var(--aw-layer); color: var(--aw-text); font-size: 12px; outline: none; cursor: pointer;
}
.aw-top-form {
  flex: 1; min-width: 220px; max-width: 560px; display: flex; align-items: center; gap: 8px;
  background: var(--aw-layer); border: 1px solid var(--aw-border-2); border-radius: 14px; padding: 4px 6px 4px 12px;
}
.aw-top-input { flex: 1; border: none; outline: none; background: transparent; color: var(--aw-text); font-size: 13px; min-width: 0; }
.aw-stat-pill {
  font-size: 12px; color: var(--aw-text-2); background: var(--aw-sunken); border: 1px solid var(--aw-border);
  border-radius: 999px; padding: 4px 11px; white-space: nowrap; font-variant-numeric: tabular-nums;
}
.aw-main {
  max-width: 1260px; margin: 0 auto; padding: 20px 22px 48px;
  display: grid; grid-template-columns: minmax(0, 1.45fr) minmax(300px, 1fr); gap: 24px; align-items: start;
}
@media (max-width: 920px) { .aw-main { grid-template-columns: 1fr; } }
.aw-q-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 14px; }
.aw-q-title { font-size: 21px; font-weight: 600; margin: 0; line-height: 1.35; }
.aw-badge {
  font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 999px;
  background: var(--aw-brand-soft); color: var(--aw-brand);
}
.aw-progress {
  display: flex; align-items: center; gap: 8px; padding: 9px 12px; border-radius: 12px;
  background: var(--aw-sunken); border: 1px solid var(--aw-border); font-size: 12px; color: var(--aw-text-2);
  margin-bottom: 16px;
}
.aw-dot { width: 8px; height: 8px; border-radius: 999px; background: var(--aw-brand); animation: awPulse 1.3s infinite; }
@keyframes awPulse { 0%,100% { opacity: .35; transform: scale(.85); } 50% { opacity: 1; transform: scale(1.1); } }
.aw-turn { margin-bottom: 22px; }
.aw-turn-q {
  font-size: 14px; font-weight: 600; padding: 9px 14px; border-radius: 12px;
  background: var(--aw-brand-soft); color: var(--aw-text); margin: 16px 0 12px;
}
.aw-md { font-size: 15px; line-height: 1.82; color: var(--aw-text); }
.aw-md h1, .aw-md h2, .aw-md h3 { margin: 18px 0 8px; font-weight: 600; line-height: 1.4; }
.aw-md h2 { font-size: 17px; padding-left: 10px; border-left: 3px solid var(--aw-brand); }
.aw-md h3 { font-size: 15px; }
.aw-md p { margin: 0 0 10px; }
.aw-md ul, .aw-md ol { margin: 0 0 10px; padding-left: 20px; }
.aw-md li { margin-bottom: 4px; }
.aw-md blockquote {
  margin: 10px 0; padding: 6px 12px; border-left: 3px solid var(--aw-border-2);
  background: var(--aw-sunken); border-radius: 0 8px 8px 0; color: var(--aw-text-2);
}
.aw-md table { width: 100%; border-collapse: collapse; margin: 12px 0; font-size: 13px; }
.aw-md th, .aw-md td { border: 1px solid var(--aw-border); padding: 7px 10px; text-align: left; }
.aw-md th { background: var(--aw-sunken); font-weight: 600; }
.aw-md code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; background: var(--aw-sunken); padding: 1px 5px; border-radius: 5px; }
.aw-codebox { margin: 10px 0; border: 1px solid var(--aw-border); border-radius: 12px; overflow: hidden; background: var(--aw-sunken); }
.aw-codehead { display: flex; justify-content: space-between; align-items: center; padding: 5px 10px; font-size: 11px; color: var(--aw-text-3); border-bottom: 1px solid var(--aw-border); }
.aw-codepre { margin: 0; padding: 10px 12px; overflow-x: auto; font-size: 13px; line-height: 1.55; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.aw-cite {
  display: inline-flex; align-items: center; justify-content: center; min-width: 18px; height: 18px;
  padding: 0 5px; margin: 0 2px; border-radius: 6px; font-size: 11px; font-weight: 600;
  vertical-align: 1px; border: none; font-variant-numeric: tabular-nums;
}
.aw-cite.ok { background: var(--aw-brand-soft); color: var(--aw-brand); cursor: pointer; }
.aw-cite.ok:hover { background: var(--aw-brand); color: #fff; }
.aw-cite.miss { background: var(--aw-sunken); color: var(--aw-text-3); cursor: default; }

.aw-followup {
  margin-top: 20px; display: flex; gap: 8px; align-items: center;
  background: var(--aw-layer); border: 1px solid var(--aw-border-2); border-radius: 16px; padding: 6px 8px 6px 14px;
  box-shadow: var(--aw-shadow);
}
.aw-followup input { flex: 1; border: none; outline: none; background: transparent; color: var(--aw-text); font-size: 14px; }

.aw-right-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
.aw-filters { display: flex; gap: 6px; flex-wrap: wrap; }
.aw-filter {
  font-size: 11px; padding: 3px 9px; border-radius: 999px; border: 1px solid var(--aw-border);
  background: var(--aw-layer); color: var(--aw-text-2); cursor: pointer;
}
.aw-filter.active { background: var(--aw-brand-soft); border-color: var(--aw-brand); color: var(--aw-brand); font-weight: 600; }
.aw-cards { display: flex; flex-direction: column; gap: 10px; }
.aw-card {
  background: var(--aw-layer); border: 1px solid var(--aw-border); border-radius: 14px; padding: 13px 14px;
  box-shadow: var(--aw-shadow); transition: transform .15s, border-color .2s, box-shadow .2s;
}
.aw-card:hover { transform: translateY(-1px); box-shadow: var(--aw-shadow-hi); }
.aw-card.flash { border-color: var(--aw-brand); box-shadow: 0 0 0 3px var(--aw-brand-ring); }
.aw-card-top { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 6px; font-size: 11px; color: var(--aw-text-3); }
.aw-card-src { display: inline-flex; align-items: center; gap: 6px; min-width: 0; }
.aw-idx {
  display: inline-flex; align-items: center; justify-content: center; width: 18px; height: 18px;
  border-radius: 5px; background: var(--aw-brand-soft); color: var(--aw-brand); font-weight: 600; flex-shrink: 0;
}
.aw-verified {
  font-size: 10px; padding: 1px 6px; border-radius: 999px; background: rgba(22, 163, 74, 0.12);
  color: var(--aw-ok); font-weight: 600; flex-shrink: 0;
}
.aw-card-quote {
  font-size: 13px; line-height: 1.65; color: var(--aw-text-2); margin: 0 0 8px;
  padding-left: 9px; border-left: 2px solid var(--aw-border-2);
}
.aw-card-link {
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
  font-size: 13px; font-weight: 600; color: var(--aw-text); text-decoration: none;
}
.aw-card-link:hover { color: var(--aw-brand); }
.aw-popover {
  position: fixed; top: 54px; right: 18px; z-index: 60; width: 380px; max-height: 74vh; overflow-y: auto;
  background: var(--aw-layer); border: 1px solid var(--aw-border-2); border-radius: 14px;
  padding: 14px; box-shadow: var(--aw-shadow-hi); font-size: 12px;
}
.aw-watch-row {
  padding: 10px 0; border-bottom: 1px solid var(--aw-border); display: flex; flex-direction: column; gap: 5px;
}
.aw-watch-actions { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 2px; }
.aw-sel-pill {
  position: fixed; z-index: 90; display: inline-flex; align-items: center; gap: 6px;
  padding: 6px 12px; border-radius: 999px; border: 1px solid var(--aw-border-2);
  background: var(--aw-layer); color: var(--aw-text); box-shadow: var(--aw-shadow-hi);
  font-size: 12px; font-weight: 600; cursor: pointer;
}
.aw-sel-pill:hover { border-color: #2563eb; color: #2563eb; }
`

function renderInline(h, text, cards, onCite) {
  const parts = []
  const tokenRe = /(\[\d+\]|`[^`]+`|\*\*[^*]+\*\*|\[[^\]]+\]\(https?:\/\/[^)]+\))/g
  let last = 0
  let m
  let k = 0
  while ((m = tokenRe.exec(text)) !== null) {
    if (m.index > last) parts.push(text.slice(last, m.index))
    const tok = m[0]
    if (tok.startsWith('[') && /^\[(\d+)\]$/.test(tok)) {
      const idx = Number(tok.slice(1, -1))
      const card = cards.find(c => c.index === idx)
      parts.push(h('button', {
        key: `c-${k++}`,
        type: 'button',
        className: `aw-cite ${card ? 'ok' : 'miss'}`,
        title: card ? card.title : '未匹配到对应证据卡',
        onClick: card ? () => onCite(card.id) : undefined,
      }, String(idx)))
    } else if (tok.startsWith('`')) {
      parts.push(h('code', { key: `cd-${k++}` }, tok.slice(1, -1)))
    } else if (tok.startsWith('**')) {
      parts.push(h('strong', { key: `b-${k++}` }, tok.slice(2, -2)))
    } else {
      const lm = tok.match(/^\[([^\]]+)\]\((https?:\/\/[^)]+)\)$/)
      if (lm) {
        parts.push(h('a', {
          key: `a-${k++}`,
          href: lm[2],
          target: '_blank',
          rel: 'noopener noreferrer',
          style: { color: 'var(--aw-brand)' },
        }, lm[1]))
      } else {
        parts.push(tok)
      }
    }
    last = m.index + tok.length
  }
  if (last < text.length) parts.push(text.slice(last))
  return parts
}

export function renderMarkdown(h, markdown, cards, onCite) {
  const lines = String(markdown || '').split('\n')
  const blocks = []
  let i = 0
  let key = 0

  while (i < lines.length) {
    const line = lines[i]
    if (line.trim().startsWith('```')) {
      const lang = line.trim().slice(3).trim() || 'text'
      const codeLines = []
      i++
      while (i < lines.length && !lines[i].trim().startsWith('```')) {
        codeLines.push(lines[i])
        i++
      }
      i++
      const codeText = codeLines.join('\n')
      blocks.push(h('div', { key: `cb-${key++}`, className: 'aw-codebox' },
        h('div', { className: 'aw-codehead' },
          h('span', null, lang),
          h('button', {
            type: 'button',
            className: 'aw-filter',
            onClick: () => navigator.clipboard?.writeText?.(codeText),
          }, '复制')),
        h('pre', { className: 'aw-codepre' }, codeText)))
      continue
    }

    if (line.includes('|') && i + 1 < lines.length && /^\s*\|?[\s:-]+\|/.test(lines[i + 1])) {
      const parseRow = r => r.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(s => s.trim())
      const headers = parseRow(line)
      i += 2
      const rows = []
      while (i < lines.length && lines[i].includes('|') && lines[i].trim() !== '') {
        rows.push(parseRow(lines[i]))
        i++
      }
      blocks.push(h('table', { key: `tb-${key++}` },
        h('thead', null, h('tr', null, headers.map((hd, idx) => h('th', { key: idx }, renderInline(h, hd, cards, onCite))))),
        h('tbody', null, rows.map((r, rIdx) => h('tr', { key: rIdx }, r.map((cell, cIdx) => h('td', { key: cIdx }, renderInline(h, cell, cards, onCite))))))))
      continue
    }

    const hm = line.match(/^(#{1,3})\s+(.+)$/)
    if (hm) {
      const tag = `h${hm[1].length}`
      blocks.push(h(tag, { key: `h-${key++}` }, renderInline(h, hm[2], cards, onCite)))
      i++
      continue
    }

    if (line.trim().startsWith('> ')) {
      const qLines = []
      while (i < lines.length && lines[i].trim().startsWith('> ')) {
        qLines.push(lines[i].trim().slice(2))
        i++
      }
      blocks.push(h('blockquote', { key: `bq-${key++}` }, renderInline(h, qLines.join(' '), cards, onCite)))
      continue
    }

    if (/^\s*[-*]\s+/.test(line)) {
      const items = []
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*[-*]\s+/, ''))
        i++
      }
      blocks.push(h('ul', { key: `ul-${key++}` }, items.map((it, idx) => h('li', { key: idx }, renderInline(h, it, cards, onCite)))))
      continue
    }

    if (/^\s*\d+\.\s+/.test(line)) {
      const items = []
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*\d+\.\s+/, ''))
        i++
      }
      blocks.push(h('ol', { key: `ol-${key++}` }, items.map((it, idx) => h('li', { key: idx }, renderInline(h, it, cards, onCite)))))
      continue
    }

    if (line.trim() === '') {
      i++
      continue
    }

    const pLines = [line]
    i++
    while (i < lines.length && lines[i].trim() !== '' && !/^(#{1,3}\s|```|>\s|\s*[-*]\s|\s*\d+\.\s)/.test(lines[i])) {
      pLines.push(lines[i])
      i++
    }
    blocks.push(h('p', { key: `p-${key++}` }, renderInline(h, pLines.join(' '), cards, onCite)))
  }

  return blocks
}
