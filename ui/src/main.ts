import './style.css'

type HeadlineItem = {
  title: string
  source: string
  published: string
  summary: string
}

class MCPClient {
  private statusEl: HTMLElement
  private connected = false

  constructor(statusEl: HTMLElement) {
    this.statusEl = statusEl
  }

  async connect(): Promise<void> {
    if (this.connected) return
    this.statusEl.textContent = 'מתחבר...'
    const response = await fetch('/api/health')
    if (!response.ok) {
      this.statusEl.textContent = 'מנותק'
      throw new Error('Bridge unavailable')
    }
    this.connected = true
    this.statusEl.textContent = 'מחובר'
  }

  async callTool(name: string, args: Record<string, unknown>): Promise<unknown> {
    const response = await fetch('/api/call', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, arguments: args }),
    })
    if (!response.ok) {
      throw new Error('Tool call failed')
    }
    return await response.json()
  }
}

function formatToolResult(result: unknown): string {
  const payload = result as { structuredContent?: unknown; content?: Array<{ type?: string; text?: string }> }
  if (payload?.structuredContent) {
    return JSON.stringify(payload.structuredContent, null, 2)
  }
  if (payload?.content?.length) {
    return payload.content.map((block) => block.text ?? '').filter(Boolean).join('\n')
  }
  return ''
}

type StructuredSummary = {
  summary?: string
  details?: string
  key_points?: string[]
  source_context?: string
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;')
}

function formatStructuredSummary(data: StructuredSummary): string {
  const parts: string[] = []
  if (data.summary) {
    parts.push(`<div class="detail-section"><strong>תקציר</strong><p>${escapeHtml(data.summary)}</p></div>`)
  }
  if (data.details) {
    parts.push(`<div class="detail-section"><strong>פרטים נוספים</strong><p>${escapeHtml(data.details)}</p></div>`)
  }
  if (data.key_points?.length) {
    const items = data.key_points.map((item) => `<li>${escapeHtml(item)}</li>`).join('')
    parts.push(`<div class="detail-section"><strong>נקודות מרכזיות</strong><ul>${items}</ul></div>`)
  }
  if (data.source_context) {
    parts.push(
      `<div class="detail-section"><strong>מקור</strong><p>${escapeHtml(
        data.source_context
      )}</p></div>`
    )
  }
  return parts.join('')
}

function tryParseStructuredSummary(value: unknown): StructuredSummary | null {
  if (value && typeof value === 'object') {
    const data = value as StructuredSummary
    if (typeof (data as { summary?: unknown }).summary === 'string') {
      const summaryText = (data as { summary: string }).summary
      if (summaryText.includes('"summary"') || summaryText.includes("'summary'")) {
        const inner = tryParseStructuredSummary(summaryText)
        if (inner) {
          return inner
        }
      }
    }
    if (data.summary || data.details || data.key_points || data.source_context) {
      return data
    }
    if (typeof (data as { summary?: unknown }).summary === 'string') {
      return data
    }
    if (typeof (data as { result?: unknown }).result === 'object') {
      const inner = tryParseStructuredSummary((data as { result: unknown }).result)
      if (inner) {
        return inner
      }
    }
  }
  if (typeof value !== 'string') {
    return null
  }
  let trimmed = value.trim()
  if (!trimmed) return null
  if (trimmed.toLowerCase().startsWith('summary')) {
    trimmed = trimmed.replace(/^summary\s*/i, '').trim()
  }
  const unfenced = trimmed
    .replace(/^```json\s*/i, '')
    .replace(/^```\s*/i, '')
    .replace(/```$/i, '')
    .trim()
  if (unfenced !== trimmed) {
    return tryParseStructuredSummary(unfenced)
  }
  try {
    const parsed = JSON.parse(trimmed) as StructuredSummary
    return parsed
  } catch {
    const summaryPrefix = 'summary'
    if (trimmed.toLowerCase().startsWith(summaryPrefix)) {
      const braceStart = trimmed.indexOf('{')
      const braceEnd = trimmed.lastIndexOf('}')
      if (braceStart >= 0 && braceEnd > braceStart) {
        const slice = trimmed.slice(braceStart, braceEnd + 1)
        try {
          return JSON.parse(slice) as StructuredSummary
        } catch {
          // fall through
        }
      }
    }
    // Handle escaped JSON string inside a JSON object
    const normalized = trimmed.replace(/\\"/g, '"').replace(/\\n/g, '\n')
    try {
      return JSON.parse(normalized) as StructuredSummary
    } catch {
      // Try to extract JSON object from a larger string
      const start = normalized.indexOf('{')
      const end = normalized.lastIndexOf('}')
      if (start >= 0 && end > start) {
        const slice = normalized.slice(start, end + 1)
        try {
          return JSON.parse(slice) as StructuredSummary
        } catch {
          const normalizedSlice = slice
            .replace(/\\"/g, '"')
            .replace(/\\n/g, '\n')
            .replace(/\bNone\b/g, 'null')
            .replace(/\bTrue\b/g, 'true')
            .replace(/\bFalse\b/g, 'false')
            .replace(/'/g, '"')
          try {
            return JSON.parse(normalizedSlice) as StructuredSummary
          } catch {
            return null
          }
        }
      }
      return null
    }
  }
}

function extractSearchSummary(result: unknown): StructuredSummary | null {
  const payload = result as {
    structuredContent?: unknown
    content?: Array<{ type?: string; text?: string }>
  }
  if (payload.structuredContent && typeof payload.structuredContent === 'object') {
    const structured = payload.structuredContent as StructuredSummary & {
      sources?: string[]
    }
    if (structured.summary || structured.key_points || structured.details || structured.source_context) {
      if (!structured.source_context && structured.sources?.length) {
        structured.source_context = structured.sources.join('\n')
      }
      return structured
    }
  }
  if (payload.content?.length) {
    const text = payload.content.map((block) => block.text ?? '').join('\n').trim()
    if (text) {
      return tryParseStructuredSummary(text)
    }
  }
  return tryParseStructuredSummary(result)
}

document.querySelector<HTMLDivElement>('#app')!.innerHTML = `
  <div class="page">
    <header class="header">
      <div class="brand">
        <div class="brand-icon">📰</div>
        <div class="brand-title">חדשות אמת</div>
      </div>
      <div class="header-actions">
        <div class="status">
          <span class="status-dot"></span>
          <span id="status-text">מנותק</span>
        </div>
        <button type="button" class="credit-button">קרדיט: יוצר האתר</button>
      </div>
    </header>

    <section id="list-view" class="panel">
      <div class="section-title">
        <div>
          <h1>כותרות אחרונות</h1>
          <p>כל העדכונים החשובים מישראל במקום אחד.</p>
        </div>
        <div class="controls">
          <div class="search-row" role="search">
            <div class="search-field">
              <button id="search-icon" type="button" class="search-icon" aria-label="חיפוש">🔍</button>
              <input
                id="topic-input"
                type="text"
                placeholder="חיפוש לפי כותרת, תוכן או קטגוריה..."
                aria-label="חיפוש חופשי"
              />
            </div>
          </div>
          <div class="filters-row">
            <label>
              מספר כותרות
              <input id="limit-input" type="number" min="1" max="12" value="6" />
            </label>
            <div class="orientation-bar" role="group" aria-label="נטייה פוליטית">
              <button type="button" class="bar-option" data-orientation="right">ימין</button>
              <button type="button" class="bar-option" data-orientation="neutral">ניטרלי</button>
              <button type="button" class="bar-option" data-orientation="left">שמאל</button>
            </div>
            <div id="load-state" class="load-state">טוען...</div>
          </div>
        </div>
      </div>
      <div id="search-results" class="search-results hidden">
        <div class="search-results-header">
          <div class="pill" id="search-results-source"></div>
          <h3 id="search-results-title"></h3>
          <div class="detail-meta" id="search-results-meta"></div>
        </div>
        <div id="search-results-body" class="detail-body"></div>
      </div>
      <div id="error-banner" class="error-banner hidden"></div>
      <div id="headlines" class="cards-grid"></div>
    </section>

    <section id="detail-view" class="panel hidden">
      <button id="back-button" class="link-button">← חזרה לכותרות</button>
      <div class="detail-header">
        <div class="pill" id="detail-source"></div>
        <h2 id="detail-title"></h2>
        <div class="detail-meta" id="detail-meta"></div>
        <p id="detail-summary" class="detail-lead"></p>
      </div>
      <div id="detail-body" class="detail-body"></div>
    </section>
  </div>
`

const statusText = document.querySelector<HTMLSpanElement>('#status-text')!
const statusDot = document.querySelector<HTMLSpanElement>('.status-dot')!
const limitInput = document.querySelector<HTMLInputElement>('#limit-input')!
const loadState = document.querySelector<HTMLDivElement>('#load-state')!
const errorBanner = document.querySelector<HTMLDivElement>('#error-banner')!
const headlinesContainer = document.querySelector<HTMLDivElement>('#headlines')!
const listView = document.querySelector<HTMLElement>('#list-view')!
const detailView = document.querySelector<HTMLElement>('#detail-view')!
const backButton = document.querySelector<HTMLButtonElement>('#back-button')!
const detailSource = document.querySelector<HTMLDivElement>('#detail-source')!
const detailTitle = document.querySelector<HTMLHeadingElement>('#detail-title')!
const detailMeta = document.querySelector<HTMLDivElement>('#detail-meta')!
const detailSummary = document.querySelector<HTMLParagraphElement>('#detail-summary')!
const detailBody = document.querySelector<HTMLDivElement>('#detail-body')!
const searchResults = document.querySelector<HTMLDivElement>('#search-results')!
const searchResultsSource = document.querySelector<HTMLDivElement>('#search-results-source')!
const searchResultsTitle = document.querySelector<HTMLHeadingElement>('#search-results-title')!
const searchResultsMeta = document.querySelector<HTMLDivElement>('#search-results-meta')!
const searchResultsBody = document.querySelector<HTMLDivElement>('#search-results-body')!
const topicInput = document.querySelector<HTMLInputElement>('#topic-input')!
const searchIcon = document.querySelector<HTMLButtonElement>('#search-icon')!
const orientationButtons = Array.from(
  document.querySelectorAll<HTMLButtonElement>('.orientation-bar .bar-option')
)
let selectedOrientation = 'neutral'

const client = new MCPClient(statusText)

async function ensureConnected(): Promise<void> {
  try {
    await client.connect()
    statusDot.classList.add('online')
  } catch (error) {
    statusDot.classList.remove('online')
    statusText.textContent = 'Disconnected'
    throw error
  }
}

function renderHeadlines(items: HeadlineItem[]): void {
  headlinesContainer.innerHTML = ''
  items.forEach((item) => {
    const card = document.createElement('article')
    card.className = 'headline-card'
    card.innerHTML = `
      <div class="card-meta">
        <span class="pill">${item.source || 'News'}</span>
        <span>${item.published || ''}</span>
      </div>
      <h3>${item.title}</h3>
      <p>${item.summary}</p>
      <div class="card-footer">Read details</div>
    `
    card.addEventListener('click', () => {
      void openDetailsView(item.title, item.source, item.published, item.summary)
    })
    headlinesContainer.appendChild(card)
  })
}

function normalizeJsonText(text: string): string {
  return text
    .replace(/\bNone\b/g, 'null')
    .replace(/\bTrue\b/g, 'true')
    .replace(/\bFalse\b/g, 'false')
    .replace(/'/g, '"')
}

function extractHeadlines(result: unknown): HeadlineItem[] | null {
  const payload = result as {
    structuredContent?: unknown
    content?: Array<{ type?: string; text?: string }>
  }
  const structured = payload.structuredContent
  if (Array.isArray(structured)) {
    return structured as HeadlineItem[]
  }
  if (structured && typeof structured === 'object') {
    const inner = (structured as { result?: unknown }).result
    if (Array.isArray(inner)) {
      return inner as HeadlineItem[]
    }
  }
  if (payload.content?.length) {
    const text = payload.content.map((block) => block.text ?? '').join('\n').trim()
    if (!text) return null
    try {
      const parsed = JSON.parse(text)
      if (Array.isArray(parsed)) {
        return parsed as HeadlineItem[]
      }
      if (parsed && typeof parsed === 'object' && Array.isArray((parsed as { result?: unknown }).result)) {
        return (parsed as { result: HeadlineItem[] }).result
      }
    } catch {
      try {
        const parsed = JSON.parse(normalizeJsonText(text))
        if (Array.isArray(parsed)) {
          return parsed as HeadlineItem[]
        }
        if (parsed && typeof parsed === 'object' && Array.isArray((parsed as { result?: unknown }).result)) {
          return (parsed as { result: HeadlineItem[] }).result
        }
      } catch {
        return null
      }
    }
  }
  return null
}

async function loadHeadlines(): Promise<void> {
  errorBanner.classList.add('hidden')
  loadState.textContent = 'מחפש...'
  await ensureConnected()
  const limit = Number.parseInt(limitInput.value || '5', 10)
  const result = await client.callTool('latest_headlines', {
    limit,
    orientation: selectedOrientation === 'neutral' ? null : selectedOrientation,
  })
  const items = extractHeadlines(result)
  if (items) {
    renderHeadlines(items)
    loadState.textContent = `נטענו ${items.length} כותרות`
  } else {
    loadState.textContent = 'אין נתונים'
    const fallback = formatToolResult(result)
    errorBanner.textContent = fallback
      ? `לא ניתן לקרוא את הכותרות מהשרת.\n${fallback}`
      : 'לא ניתן לקרוא את הכותרות מהשרת.'
    errorBanner.classList.remove('hidden')
  }
}

limitInput.addEventListener('change', () => {
  void loadHeadlines()
})

orientationButtons.forEach((button) => {
  button.addEventListener('click', () => {
    orientationButtons.forEach((item) => item.classList.remove('active'))
    button.classList.add('active')
    selectedOrientation = button.dataset.orientation ?? ''
    void loadHeadlines()
  })
})

async function runTopicSearch(): Promise<void> {
  const query = topicInput.value.trim()
  if (!query) {
    return
  }
  errorBanner.classList.add('hidden')
  loadState.textContent = 'טוען...'
  searchResults.classList.add('hidden')
  searchResultsBody.innerHTML = ''
  await ensureConnected()
  const result = await client.callTool('summarize_news_topic', {
    query,
    orientation: selectedOrientation === 'neutral' ? null : selectedOrientation,
    limit: Number.parseInt(limitInput.value || '6', 10),
  })
  const summary = extractSearchSummary(result)
  if (summary) {
    searchResultsSource.textContent =
      selectedOrientation === 'right' ? 'ימין' : selectedOrientation === 'left' ? 'שמאל' : 'ניטרלי'
    searchResultsTitle.textContent = query
    searchResultsMeta.textContent = 'חיפוש נושא'
    searchResultsBody.innerHTML = formatStructuredSummary(summary)
    searchResults.classList.remove('hidden')
    loadState.textContent = 'החיפוש הושלם'
    return
  }
  const fallback = formatToolResult(result)
  if (fallback) {
    searchResultsSource.textContent =
      selectedOrientation === 'right' ? 'ימין' : selectedOrientation === 'left' ? 'שמאל' : 'ניטרלי'
    searchResultsTitle.textContent = query
    searchResultsMeta.textContent = 'חיפוש נושא'
    searchResultsBody.innerHTML = `<p>${escapeHtml(fallback)}</p>`
    searchResults.classList.remove('hidden')
    loadState.textContent = 'החיפוש הושלם'
    return
  }
  loadState.textContent = 'אין נתונים'
  errorBanner.textContent = 'לא נמצאו תוצאות עבור החיפוש המבוקש.'
  errorBanner.classList.remove('hidden')
}

topicInput.addEventListener('keydown', (event) => {
  if (event.key !== 'Enter') return
  event.preventDefault()
  void runTopicSearch()
})

searchIcon.addEventListener('click', () => {
  void runTopicSearch()
})

orientationButtons.forEach((item) => item.classList.remove('active'))
orientationButtons
  .find((button) => button.dataset.orientation === 'neutral')
  ?.classList.add('active')

void loadHeadlines()

backButton.addEventListener('click', () => {
  detailView.classList.add('hidden')
  listView.classList.remove('hidden')
})

async function openDetailsView(
  headline: string,
  source: string,
  published: string,
  summary: string
): Promise<void> {
  await ensureConnected()
  listView.classList.add('hidden')
  detailView.classList.remove('hidden')
  detailSource.textContent = source || 'חדשות'
  detailTitle.textContent = headline
  detailMeta.textContent = `${published || 'ללא תאריך'}`
  detailSummary.textContent = summary || ''
  detailBody.innerHTML = '<p>טוען פרטים...</p>'

  const result = await client.callTool('headline_details', { headline })
  const payload = result as { structuredContent?: unknown; content?: Array<{ type?: string; text?: string }> }
  const content = payload.structuredContent
  const structured = tryParseStructuredSummary(content)
  if (structured) {
    detailBody.innerHTML = formatStructuredSummary(structured)
    return
  }
  if (content && typeof content === 'object') {
    const summaryValue = (content as { summary?: unknown }).summary
    const summaryStructured = tryParseStructuredSummary(summaryValue)
    if (summaryStructured) {
      detailBody.innerHTML = formatStructuredSummary(summaryStructured)
      return
    }
  }
  const fallbackText = formatToolResult(result)
  const fallbackStructured = tryParseStructuredSummary(fallbackText)
  if (fallbackStructured) {
    detailBody.innerHTML = formatStructuredSummary(fallbackStructured)
    return
  }
  const contentText = payload.content?.map((block) => block.text ?? '').join('\n') ?? ''
  const contentStructured = tryParseStructuredSummary(contentText)
  if (contentStructured) {
    detailBody.innerHTML = formatStructuredSummary(contentStructured)
    return
  }
  detailBody.innerHTML = `<div class="detail-section">${escapeHtml(fallbackText)}</div>`
}

