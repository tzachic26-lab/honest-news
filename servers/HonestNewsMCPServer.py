import json
import os
import sys
import urllib.parse
import urllib.request
import html
import re
import time
from datetime import date, timedelta
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET

import anyio
import httpx
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.fastmcp import FastMCP

# Optional Tavily import for web search
try:
    from tavily import TavilyClient
    TAVILY_AVAILABLE = True
except ImportError:
    TAVILY_AVAILABLE = False


mcp = FastMCP("HonestNews")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # ensure Hebrew output works on Windows
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def _log(msg: str) -> None:
    """Log to stderr so it appears in terminal when running under MCP bridge (stdout is used for protocol)."""
    print(msg, file=sys.stderr, flush=True)


_RSS_BASE_URL = "https://news.google.com/rss"
_DEFAULT_QUERY = "ישראל"

_ORIENTATION_SOURCES = {
    "right": [
        "channel14.co.il",
        "israelhayom.co.il",
        "mako.co.il",
        "srugim.co.il",
        "0404.co.il",
        "kikar.co.il",
        "jdn.co.il",
        "bhol.co.il",
        "inn.co.il",
        "mida.org.il",
        "liberal.co.il",
        "maariv.co.il",
    ],
    "left": [
        "haaretz.co.il",
        "news.walla.co.il",
        "themarker.com",
        "ynet.co.il",
        "n12.co.il",
        "globes.co.il",
        "haokets.org",
        "shomrim.news",
        "972mag.com",
        "mako.co.il",
        "democracynow.org",
        "makorrishon.co.il",
    ],
}


def _get_llm() -> ChatOpenAI | None:
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None

    model_name = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    return ChatOpenAI(
        model=model_name,
        api_key=api_key,
        http_client=httpx.Client(verify=False),
        http_async_client=httpx.AsyncClient(verify=False),
    )


def _fetch_rss(query: str) -> str:
    encoded = urllib.parse.quote(query)
    url = f"{_RSS_BASE_URL}/search?q={encoded}&hl=he&gl=IL&ceid=IL:he"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.read().decode("utf-8")
    except Exception as exc:
        print(f"_fetch_rss: failed to fetch RSS: {exc}")
        return ""


def _build_query_for_orientation(query: str, orientation: str | None) -> str:
    if not orientation:
        return query
    norm = _normalize_orientation(orientation)
    sources = _ORIENTATION_SOURCES.get(norm, [])
    if not sources:
        return query
    site_filter = " OR ".join(f"site:{source}" for source in sources)
    return f"{query} ({site_filter})"


def _parse_items(rss_xml: str) -> list[dict[str, str]]:
    if not rss_xml:
        return []
    root = ET.fromstring(rss_xml)
    items: list[dict[str, str]] = []

    for item in root.findall("./channel/item"):
        title = item.findtext("title") or ""
        link = item.findtext("link") or ""
        published = item.findtext("pubDate") or ""
        summary = item.findtext("description") or ""
        source = ""

        source_elem = item.find("source")
        if source_elem is not None:
            source = source_elem.text or ""

        items.append(
            {
                "title": title.strip(),
                "link": link.strip(),
                "published": published.strip(),
                "summary": _clean_summary(summary),  # Use RSS summary for fast loading
                "source": source.strip(),
            }
        )

    return items


def _is_today(pub_date: str) -> bool:
    if not pub_date:
        return False
    try:
        dt = parsedate_to_datetime(pub_date)
    except Exception:
        return False
    return dt.date() == date.today()


def _is_recent(pub_date: str, days: int = 14) -> bool:
    """True if pub_date is within the last `days` days (inclusive)."""
    if not pub_date:
        return False
    try:
        dt = parsedate_to_datetime(pub_date)
    except Exception:
        return False
    cutoff = date.today() - timedelta(days=days)
    return dt.date() >= cutoff


def _clean_summary(summary: str) -> str:
    if not summary:
        return ""
    # Strip HTML tags and decode entities; remove lingering URLs.
    text = re.sub(r"<[^>]+>", " ", summary)
    text = html.unescape(text)
    text = re.sub(r"https?://\S+", "", text)
    return " ".join(text.split()).strip()


def _search_web_context(query: str, max_results: int = 5) -> str:
    """Search the web for additional context using Tavily."""
    if not TAVILY_AVAILABLE:
        _log("Tavily package not installed, skipping web search")
        return ""
    
    tavily_api_key = os.getenv("TAVILY_API_KEY")
    if not tavily_api_key:
        _log("Tavily API key not configured, skipping web search")
        return ""
    
    try:
        _log(f"Searching web for context on: {query}")
        client = TavilyClient(api_key=tavily_api_key)
        response = client.search(
            query=query,
            max_results=max_results,
            search_depth="basic",
            include_answer=True,
            include_raw_content=False,
            include_images=False
        )
        
        # Extract relevant information from search results
        context_parts = []
        if response.get("answer"):
            context_parts.append(f"תשובה כללית: {response['answer']}")
        
        for result in response.get("results", [])[:max_results]:
            title = result.get("title", "")
            url = result.get("url", "")
            content = result.get("content", "")
            if content:
                context_parts.append(f"מקור: {title}\n{content[:500]}")
        
        web_context = "\n\n".join(context_parts)
        _log(f"Retrieved {len(web_context)} characters of web context")
        return web_context
        
    except Exception as exc:
        _log(f"Failed to search web with Tavily: {exc}")
        return ""


def _normalize_text(text: str) -> str:
    text = text.split(" - ")[0]
    text = re.sub(r"[^\w\s]", " ", text.lower())
    return " ".join(text.split())


def _dedupe_items(
    items: list[dict[str, str]],
    limit: int,
    overlap_threshold: float = 0.3,
) -> tuple[list[dict[str, str]], list[str]]:
    selected: list[dict[str, str]] = []
    seen_tokens: list[set[str]] = []
    seen_normalized_titles: set[str] = set()
    filtered_titles: list[str] = []

    for item in items:
        normalized_title = _normalize_text(item.get("title", ""))
        if not normalized_title:
            continue
        if normalized_title in seen_normalized_titles:
            filtered_titles.append(item.get("title", ""))
            continue

        summary_tokens = set(_normalize_text(item.get("summary", "")).split())
        title_tokens = set(normalized_title.split()) | summary_tokens
        if not title_tokens:
            continue

        duplicate = False
        for tokens in seen_tokens:
            overlap = len(title_tokens & tokens) / max(1, len(title_tokens | tokens))
            if overlap >= overlap_threshold:
                filtered_titles.append(item.get("title", ""))
                duplicate = True
                break

        if duplicate:
            continue

        selected.append(item)
        seen_normalized_titles.add(normalized_title)
        seen_tokens.append(title_tokens)
        if len(selected) >= limit:
            break

    return selected, filtered_titles


def _normalize_orientation(value: str) -> str:
    value = value.strip().lower()
    if value in {"left", "שמאל"}:
        return "left"
    if value in {"right", "ימין"}:
        return "right"
    if value in {"neutral", "center", "מרכז", "ניטרלי"}:
        return "neutral"
    raise ValueError("orientation must be one of: left, right, neutral")


def _filter_by_orientation(items: list[dict[str, str]], orientation: str) -> list[dict[str, str]]:
    llm = _get_llm()
    if not llm:
        print("latest_headlines: no LLM available for orientation filtering, returning unfiltered items")
        return items

    orientation = _normalize_orientation(orientation)
    prompt_items = [
        {
            "id": idx,
            "title": item.get("title", ""),
            "summary": item.get("summary", ""),
        }
        for idx, item in enumerate(items)
    ]
    prompt = (
        "סווג כל כותרת לנטייה פוליטית בישראל: left, right, neutral. "
        "החזר JSON בלבד במבנה: {\"items\": [{\"id\": 0, \"orientation\": \"left\"}, ...]}. "
        "אל תמציא עובדות."
    )
    response = llm.invoke(f"{prompt}\n\n{json.dumps(prompt_items, ensure_ascii=False)}")
    parsed = _parse_llm_json_any(response.content)
    if not isinstance(parsed, dict):
        return items
    items_list = parsed.get("items")
    if not isinstance(items_list, list):
        return items

    by_id = {}
    for entry in items_list:
        if not isinstance(entry, dict):
            continue
        item_id = entry.get("id")
        item_orientation = str(entry.get("orientation", "")).lower()
        if isinstance(item_id, int):
            by_id[item_id] = item_orientation

    filtered = [
        item
        for idx, item in enumerate(items)
        if by_id.get(idx) == orientation
    ]
    return filtered or items


def _parse_llm_json(text: str) -> dict[str, object] | None:
    if not text:
        return None
    cleaned = text.strip()
    cleaned = re.sub(r"^```json\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^```\s*", "", cleaned)
    cleaned = re.sub(r"```$", "", cleaned).strip()
    cleaned = re.sub(r"^summary\s*", "", cleaned, flags=re.IGNORECASE).strip()

    normalized = (
        cleaned.replace('\\"', '"')
        .replace("\\n", "\n")
        .replace("True", "true")
        .replace("False", "false")
        .replace("None", "null")
        .replace("'", '"')
    )

    for candidate in (cleaned, normalized, _extract_json_block(cleaned), _extract_json_block(normalized)):
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except Exception:
            continue
    return None


def _extract_json_block(text: str) -> str | None:
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return None


def _parse_llm_json_any(text: str) -> object | None:
    if not text:
        return None
    cleaned = text.strip()
    cleaned = re.sub(r"^```json\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^```\s*", "", cleaned)
    cleaned = re.sub(r"```$", "", cleaned).strip()
    cleaned = re.sub(r"^summary\s*", "", cleaned, flags=re.IGNORECASE).strip()

    normalized = (
        cleaned.replace('\\"', '"')
        .replace("\\n", "\n")
        .replace("True", "true")
        .replace("False", "false")
        .replace("None", "null")
        .replace("'", '"')
    )

    for candidate in (cleaned, normalized, _extract_json_block(cleaned), _extract_json_block(normalized)):
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except Exception:
            continue
    return None


def _normalize_newlines(s: str) -> str:
    """Convert literal \\n in text to real newlines so they display as line breaks."""
    if not s:
        return s
    return s.replace("\\n", "\n")


def _parse_llm_sections(text: str) -> dict[str, object]:
    summary = ""
    details_lines: list[str] = []
    key_points: list[str] = []
    source_context = ""

    section = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            if section == "summary" and summary:
                summary = summary + "\n\n"
            continue
        upper = line.upper()
        if upper.startswith("SUMMARY:"):
            section = "summary"
            summary = line.split(":", 1)[1].strip()
            continue
        if upper.startswith("DETAILS:"):
            section = "details"
            details_line = line.split(":", 1)[1].strip()
            if details_line:
                details_lines.append(details_line)
            continue
        if upper.startswith("KEY_POINTS:"):
            section = "key_points"
            kp_line = line.split(":", 1)[1].strip()
            if kp_line:
                key_points.append(kp_line.lstrip("-").strip())
            continue
        if upper.startswith("SOURCE_CONTEXT:"):
            section = "source_context"
            source_context = line.split(":", 1)[1].strip()
            continue

        if section == "summary":
            if not summary:
                summary = line
            elif summary.endswith("\n\n"):
                summary = summary + line
            else:
                summary = summary + " " + line
            summary = summary.strip()
        elif section == "details":
            details_lines.append(line)
        elif section == "key_points":
            key_points.append(line.lstrip("-").strip())
        elif section == "source_context":
            source_context = f"{source_context} {line}".strip()

    details = _normalize_newlines("\n".join(details_lines).strip())
    summary = _normalize_newlines(summary)
    return {
        "summary": summary,
        "details": details,
        "key_points": [kp for kp in key_points if kp],
        "source_context": source_context,
    }


def _coerce_json_like_to_sections(text: str) -> str:
    cleaned = text.strip()
    # Remove code fences if present.
    cleaned = re.sub(r"^```json\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^```\s*", "", cleaned)
    cleaned = re.sub(r"```$", "", cleaned).strip()

    # Convert a JSON-like blob into labeled sections.
    cleaned = cleaned.replace("{", "").replace("}", "")
    cleaned = cleaned.replace('"summary":', "SUMMARY:")
    cleaned = cleaned.replace('"details":', "\nDETAILS:")
    cleaned = cleaned.replace('"key_points":', "\nKEY_POINTS:")
    cleaned = cleaned.replace('"source_context":', "\nSOURCE_CONTEXT:")

    # Turn arrays into bullet points.
    cleaned = cleaned.replace("[", "\n- ").replace("]", "")

    # Replace commas between items with newlines.
    cleaned = cleaned.replace('", "', '"\n')
    cleaned = cleaned.replace('",', '"\n')
    cleaned = cleaned.replace('",\n', '"\n')

    # Strip extra quotes around values.
    cleaned = cleaned.replace('"', "")
    return cleaned.strip()


@mcp.tool()
def latest_headlines(limit: int = 5, orientation: str | None = None) -> list[dict[str, str]]:
    """
    השתמש בכלי החיפוש שלך כדי למצוא את 5 כותרות החדשות המרכזיות מהשעות האחרונות ב-[ציין תחום: ישראל / עולם / טכנולוגיה].
     עבור כל כותרת, ספק סיכום של 5 שורות . הצג את התוצאות כרשימה מסודרת ונקייה.
    """
    limit = max(1, limit)
    rss_query = _build_query_for_orientation(_DEFAULT_QUERY, orientation)
    rss_xml = _fetch_rss(rss_query)
    items = [item for item in _parse_items(rss_xml) if _is_today(item.get("published", ""))]
    dedupe_limit = max(limit * 8, limit)
    selected, filtered_titles = _dedupe_items(items, dedupe_limit)

    if filtered_titles:
        print("latest_headlines: filtered similar headlines:")
        for title in filtered_titles:
            if title:
                print(f" - {title}")

    if orientation:
        norm_orientation = _normalize_orientation(orientation)
        if norm_orientation != "neutral":
            filtered = _filter_by_orientation(selected, norm_orientation)
            if filtered:
                selected, _ = _dedupe_items(filtered, limit, overlap_threshold=0.2)
                if len(selected) < limit:
                    selected, _ = _dedupe_items(filtered, limit, overlap_threshold=0.1)
            else:
                print("latest_headlines: no orientation matches, returning empty list")
                selected = []

    selected = selected[:limit]

    llm = _get_llm()
    use_llm = os.getenv("NEWS_HEADLINES_USE_LLM", "0") == "1"
    if not llm or not use_llm:
        _log("latest_headlines: using RSS data (LLM disabled)")
        return [
            {
                "title": item["title"],
                "source": item["source"],
                "published": item["published"],
                "summary": item["summary"],
            }
            for item in selected
        ]

    _log("latest_headlines: using ChatGPT")
    combined = "\n".join(
        f"- כותרת: {item['title']}\n  תקציר: {item['summary']}"
        for item in selected
    )
    prompt = (
       """
    השתמש בכלי החיפוש שלך כדי למצוא את 5 כותרות החדשות המרכזיות מהשעות האחרונות ב-[ציין תחום: ישראל / עולם / טכנולוגיה].
     עבור כל כותרת, ספק סיכום של 5 שורות . הצג את התוצאות כרשימה מסודרת ונקייה.
    """
        "[{title: ..., summary: ...}, ...]"
    )
    response = llm.invoke(f"{prompt}\n\n{combined}")

    try:
        rewritten = json.loads(response.content)
    except Exception:
        rewritten = None

    results: list[dict[str, str]] = []
    for idx, item in enumerate(selected):
        title = item["title"]
        summary = item["summary"]
        if isinstance(rewritten, list) and idx < len(rewritten):
            title = rewritten[idx].get("title", title)
            summary = rewritten[idx].get("summary", summary)
        results.append(
            {
                "title": title,
                "source": item["source"],
                "published": item["published"],
                "summary": summary,
            }
        )
    return results


@mcp.tool()
def summarize_news_topic(query: str, orientation: str | None = None, limit: int = 6) -> dict[str, object]:
    """
    Summarize news on a topic the way ChatGPT would when asked for more info. Uses the LLM
    (OpenAI) when OPENAI_API_KEY is set for a connected, conversational summary; otherwise
    falls back to RSS-only. Returns summary, details, key_points, sources, source_context.
    """
    query_text = query.strip()
    if not query_text:
        return {
            "topic": query,
            "orientation": orientation or "neutral",
            "summary": "",
            "details": "",
            "key_points": [],
            "sources": [],
            "source_context": "",
        }
    primary_query = query_text
    rss_query = _build_query_for_orientation(primary_query, orientation)
    rss_xml = _fetch_rss(rss_query)
    parsed_items = _parse_items(rss_xml)
    # Prefer items from the last 1–2 weeks for overview + background; fall back to all if few.
    items = [item for item in parsed_items if _is_recent(item.get("published", ""), 14)]
    if len(items) < 5:
        items = parsed_items
    normalized_query = query_text.lower()
    if normalized_query:
        words = [word for word in re.split(r"\s+", normalized_query) if len(word) > 2]
        if words:
            matched = [
                item
                for item in items
                if any(
                    word in item.get("title", "").lower()
                    or word in item.get("summary", "").lower()
                    for word in words
                )
            ]
            if matched:
                items = matched
    limit = max(1, limit)
    if not items:
        return {
            "topic": query,
            "orientation": orientation or "neutral",
            "summary": "",
            "details": "",
            "key_points": [],
            "sources": [],
            "source_context": "לא נמצאו כותרות תואמות לחיפוש.",
        }
    context_limit_env = os.getenv("NEWS_SEARCH_CONTEXT_LIMIT")
    try:
        context_limit = int(context_limit_env) if context_limit_env else None
    except ValueError:
        context_limit = None
    if context_limit is None:
        context_limit = min(max(limit * 3, 20), 30)
    selected, _ = _dedupe_items(items, context_limit)
    if not selected:
        return {
            "topic": query,
            "orientation": orientation or "neutral",
            "summary": "",
            "details": "",
            "key_points": [],
            "sources": [],
            "source_context": "לא נמצאו כותרות תואמות לחיפוש.",
        }

    if orientation:
        norm_orientation = _normalize_orientation(orientation)
        if norm_orientation != "neutral":
            filtered = _filter_by_orientation(selected, norm_orientation)
            if filtered:
                selected = filtered

    # Send more items to the LLM for a richer overview and background (cap at 25).
    selected = selected[: min(len(selected), 25)]
    llm = _get_llm()
    if not llm:
        _log("summarize_news_topic: using RSS only (no LLM). Set OPENAI_API_KEY for LLM summary.")
        # No LLM: build one connected summary from RSS sentences (no "title - source" list).
        sentences = []
        for item in selected[:12]:
            s = (item.get("summary") or "").strip()
            if s:
                for sent in re.split(r"[.!?]\s+", s):
                    sent = sent.strip()
                    if len(sent) > 15:
                        sentences.append(sent)
        if not sentences:
            for item in selected[:10]:
                t = (item.get("title") or "").strip()
                if t:
                    sentences.append(t + ".")
        connectors = ("בנוסף, ", "לפי הדיווחים, ", "עם זאת, ", "כמו כן, ", "בדיווחים נמסר כי ", "")
        paras = []
        i = 0
        c = 0
        while i < len(sentences) and len(paras) < 5:
            chunk = sentences[i : i + 4]
            i += 4
            prefix = connectors[c % len(connectors)] if c else "להלן סיכום על פי הדיווחים האחרונים. "
            paras.append(prefix + " ".join(chunk) + ("." if not chunk[-1].endswith(".") else ""))
            c += 1
        summary = "\n\n".join(paras) if paras else "לא נמצא תוכן לסיכום. להפעלת סיכום מחובר הגדר OPENAI_API_KEY."
        details_lines = ["עיקרי הדיווחים מהמקורות:\n"]
        for item in selected[:10]:
            if item.get("summary"):
                details_lines.append(item.get("summary", "").strip()[:200] + ("..." if len(item.get("summary", "")) > 200 else ""))
            details_lines.append(f"(מקור: {item.get('source', '')})\n")
        return {
            "topic": query,
            "orientation": orientation or "neutral",
            "summary": summary,
            "details": "\n".join(details_lines),
            "key_points": [(item.get("title", "") or "")[:80] for item in selected[:6]],
            "sources": [item.get("title", "") for item in selected],
            "source_context": f"מקורות מהשבועיים האחרונים, {len(selected)} פריטים.",
        }

    _log("summarize_news_topic: using LLM (ChatGPT)")
    context = "\n".join(
        f"- כותרת: {item['title']}\n  תקציר: {item['summary']}\n  מקור: {item['source']}\n  תאריך: {item['published']}"
        for item in selected
    )
    prompt = (
        "אתה ChatGPT. המשתמש ביקש עוד מידע על נושא בחדשות. ענה בעברית בתשובה אחת רצופה. השתמש רק בכותרות והתקצירים למטה – אל תמציא עובדות.\n\n"
        "פורמט התשובה (כותרות שדה בדיוק כך, בלי JSON):\n\n"
        "SUMMARY:\n"
        "  כאן רק פרוזה רצופה: 3–5 פסקאות. כל פסקה – כמה משפטים מחוברים (השתמש ב\"בנוסף\", \"לפי הדיווחים\", \"עם זאת\").\n"
        "  אסור להעתיק רשימת כותרות או שורות כמו \"כותרת - מקור\". אסור רשימת נקודות. רק משפטים שמתחברים לפסקאות.\n"
        "  להשאיר שורה ריקה בין פסקאות.\n\n"
        "DETAILS:\n"
        "  10–20 שורות עם עובדות וציטוטים (כל שורה עובדה אחת).\n\n"
        "KEY_POINTS:\n"
        "  רשימה עם מקף בתחילת כל שורה, 5–8 נקודות.\n\n"
        "SOURCE_CONTEXT:\n"
        "  משפט אחד: מאילו מקורות ומועדים.\n\n"
        "נושא: "
        f"{query}"
        f"\nנטייה: {orientation or 'neutral'}"
    )
    response = llm.invoke(f"{prompt}\n\n{context}")
    parsed = _parse_llm_sections(response.content)
    if not parsed.get("summary") and ("summary" in response.content.lower() or "SUMMARY" in response.content):
        parsed = _parse_llm_sections(_coerce_json_like_to_sections(response.content))

    return {
        "topic": query,
        "orientation": orientation or "neutral",
        "summary": _normalize_newlines(parsed.get("summary", "")),
        "details": _normalize_newlines(parsed.get("details", "")),
        "key_points": parsed.get("key_points", []),
        "sources": parsed.get("sources", [item.get('title', '') for item in selected]),
        "source_context": parsed.get("source_context", ""),
    }

@mcp.tool()
def headline_details(headline: str) -> dict[str, object]:
    """
    Given a headline string, return rich details: title, source, published, summary, and
    a detailed body (up to 40 lines) that includes ~80% of the source content, formatted
    like ChatGPT (paragraphs, clear sections, readable flow).
    """
    rss_xml = _fetch_rss(headline)
    items = _parse_items(rss_xml)
    if not items:
        return {
            "title": headline,
            "source": "",
            "published": "",
            "summary": "",
            "details": "",
            "key_points": [],
            "source_context": "לא ניתן לגשת לתוצאות החיפוש (בעיה בחיבור או ב-DNS).",
        }

    exact_match = None
    lowered_headline = headline.lower()
    for item in items:
        if item["title"].lower() == lowered_headline:
            exact_match = item
            break

    if exact_match is None:
        for item in items:
            if lowered_headline in item["title"].lower():
                exact_match = item
                break

    llm = _get_llm()
    if not llm:
        _log("headline_details: using RSS data (no LLM)")
        if exact_match:
            # Structured details: clear sections separated by \n\n for display.
            summary_text = (exact_match.get("summary") or "").strip()
            header = f"מקור: {exact_match['source']} | תאריך: {exact_match['published']}"
            body_parts = []
            if summary_text:
                sentences = [s.strip() for s in re.split(r"[.!?]\s+", summary_text) if s.strip()]
                line_count = 0
                i = 0
                max_lines = 32
                while i < len(sentences) and line_count < max_lines:
                    para = []
                    for _ in range(min(3, len(sentences) - i)):
                        para.append(sentences[i])
                        i += 1
                    body_parts.append(" ".join(para) + ("." if not para[-1].endswith(".") else ""))
                    line_count += 1
            else:
                body_parts.append(exact_match.get("summary") or "(אין תקציר)")
            details_str = header + "\n\nעיקרי הדיווח:\n\n" + "\n\n".join(body_parts)
            related = [i for i in items[:6] if i.get("title") and i["title"] != exact_match["title"]][:3]
            if related:
                details_str += "\n\nכתבות קשורות:\n\n" + "\n".join(f"• {r['title']} ({r.get('source', '')})" for r in related)
            return {
                "title": exact_match["title"],
                "source": exact_match["source"],
                "published": exact_match["published"],
                "summary": exact_match["summary"],
                "details": details_str,
                "key_points": [],
                "source_context": f"מקור: {exact_match['source']}, תאריך: {exact_match['published']}",
            }
        return {
            "title": headline,
            "source": "",
            "published": "",
            "summary": "",
            "details": "",
            "key_points": [],
            "source_context": "",
        }

    _log("headline_details: using ChatGPT")
    
    # Search web for additional context
    web_context = ""
    if exact_match:
        web_context = _search_web_context(exact_match["title"], max_results=3)
        if web_context:
            _log("Successfully retrieved web context")
        else:
            _log("No web context retrieved")
    
    top_items = items[:8]
    context = "\n".join(
        f"- כותרת: {item['title']}\n  תקציר: {item['summary']}\n  מקור: {item['source']}\n  תאריך: {item['published']}"
        for item in top_items
    )
    
    # Add web context to the prompt if available
    if web_context:
        context = f"הקשר נוסף מהרשת:\n{web_context}\n\nמקורות חדשות:\n{context}"
    
    prompt = (
        "על סמך החומר מהמקורות להלן בלבד, כתוב סיכום בעברית.\n\n"
        "הוראות חשובות מאוד:\n"
        "1. אסור להמציא פרטים שלא קיימים במקורות\n"
        "2. אסור לנחש או להשלים מידע חסר\n"
        "3. אסור לכתוב דברים שאין להם בסיס במקורות\n"
        "4. אם מידע לא מספיק - כתוב רק מה שיש, ללא פנטזיות\n"
        "5. כל משפט חייב להיות מבוסס על מידע מהמקורות שלהלן\n\n"
        "השתמש רק במידע שמופיע במקורות למטה. לא להוסיף דבר משלך.\n\n"
        "פורמט (כותרות שדה, בלי JSON):\n"
        "SUMMARY:\n (סיכום קצר של מה שיש במקורות, בלי המצאות)\n"
        "DETAILS:\n (הרחבה רק של מה שיש במקורות, ללא הוספות)\n"
        "KEY_POINTS:\n (נקודות עיקריות מהמקורות בלבד)\n"
        "SOURCE_CONTEXT:\n (מקורות ותאריכים)\n\n"
        "כותרת מבוקשת: "
        f"{headline}"
    )
    response = llm.invoke(f"{prompt}\n\n{context}")
    parsed = _parse_llm_sections(response.content)
    if not parsed.get("summary") and "summary" in response.content:
        parsed = _parse_llm_sections(_coerce_json_like_to_sections(response.content))
    return {
        "title": exact_match["title"] if exact_match else headline,
        "source": exact_match["source"] if exact_match else "",
        "published": exact_match["published"] if exact_match else "",
        "summary": _normalize_newlines(parsed.get("summary", "")),
        "details": _normalize_newlines(parsed.get("details", "")),
        "key_points": parsed.get("key_points", []),
        "source_context": parsed.get("source_context", ""),
    }


if __name__ == "__main__":
    if "--client" in sys.argv:
        async def run_client() -> None:
            server = StdioServerParameters(
                command=sys.executable,
                args=[os.path.abspath(__file__)],
                cwd=os.path.dirname(os.path.abspath(__file__)),
            )

            async with stdio_client(server) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    print("tools:", [tool.name for tool in tools.tools])

                    headlines = await session.call_tool(
                        "latest_headlines",
                        {"limit": 3},
                    )
                    print(
                        "latest_headlines:",
                        headlines.structuredContent or headlines.content,
                    )

                    headline_title = None
                    structured = headlines.structuredContent
                    if isinstance(structured, list) and structured:
                        headline_title = structured[0].get("title")
                    elif isinstance(structured, dict):
                        headline_title = structured.get("title")

                    if headline_title:
                        details = await session.call_tool(
                            "headline_details",
                            {"headline": headline_title},
                        )
                        print("headline_details:", details.structuredContent or details.content)

        anyio.run(run_client)
    else:
        mcp.run(transport="stdio")
