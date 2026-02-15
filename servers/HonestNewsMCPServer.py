import json
import os
import sys
import urllib.parse
import urllib.request
import html
import re
import time
from datetime import date
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET

import anyio
import httpx
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.fastmcp import FastMCP


mcp = FastMCP("HonestNews")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # ensure Hebrew output works on Windows
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


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
                "summary": _clean_summary(summary),
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


def _clean_summary(summary: str) -> str:
    if not summary:
        return ""
    # Strip HTML tags and decode entities; remove lingering URLs.
    text = re.sub(r"<[^>]+>", " ", summary)
    text = html.unescape(text)
    text = re.sub(r"https?://\S+", "", text)
    return " ".join(text.split()).strip()


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


def _parse_llm_sections(text: str) -> dict[str, object]:
    summary = ""
    details_lines: list[str] = []
    key_points: list[str] = []
    source_context = ""

    section = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
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
            summary = f"{summary} {line}".strip()
        elif section == "details":
            details_lines.append(line)
        elif section == "key_points":
            key_points.append(line.lstrip("-").strip())
        elif section == "source_context":
            source_context = f"{source_context} {line}".strip()

    details = "\n".join(details_lines).strip()
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
        print("latest_headlines: using RSS data (LLM disabled)")
        return [
            {
                "title": item["title"],
                "source": item["source"],
                "published": item["published"],
                "summary": item["summary"],
            }
            for item in selected
        ]

    print("latest_headlines: using ChatGPT")
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
    תפקיד: אתה מנתח חדשות בכיר ומומחה בסינתזת מידע ממקורות מרובים.
המשימה: עליך לסכם את החדשות האחרונות בנושא [הכנס כאן את הנושא שלך].
הנחיות לביצוע:
איסוף וסינתזה: סרוק ושלב מידע ממספר מקורות מובילים (לפחות 3-5 מקורות שונים).
מבנה הסיכום:
סקירה כללית: פתח בסיכום תמציתי של האירועים העיקריים (2-3 פסקאות).
חלוקה לפי כיוונים/נרטיבים: חלק את המידע לתתי-נושאים לפי זוויות ראייה שונות (למשל: היבט פוליטי, כלכלי, ביטחוני, או דעות תומכות מול מתנגדות).
ניתוח עומק: עבור כל זווית, הרחב על הפרטים המהותיים, כולל ציטוטים בולטים או נתונים מספריים אם קיימים.
נייטרליות וייחוס: הקפד על שפה אובייקטיבית וייחס את המידע למקורותיו (למשל: "לפי דיווח ב-X...", "מקורות ב-Y טוענים...").
שפה וסגנון: כתוב בעברית רהוטה, מקצועית וברורה. הימנע מחזרתיות ומשימוש במילים ריקות.
פלט נדרש: סיכום ארוך ומפורט (לפחות 500 מילה) המאורגן באמצעות כותרות מודגשות ונקודות (Bullet Points) לשיפור הקריאות.
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
    items = [item for item in parsed_items if _is_today(item.get("published", ""))]
    if not items:
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
        context_limit = min(max(limit * 2, limit), 8)
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

    selected = selected[:limit]
    llm = _get_llm()
    if not llm:
        return {
            "topic": query,
            "orientation": orientation or "neutral",
            "summary": "",
            "key_points": [],
            "sources": [item.get("title", "") for item in selected],
        }

    context = "\n".join(
        f"- כותרת: {item['title']}\n  תקציר: {item['summary']}\n  מקור: {item['source']}\n  תאריך: {item['published']}"
        for item in selected
    )
    prompt = (
        "תפקיד: אתה מנתח חדשות בכיר ומומחה בסינתזת מידע ממקורות מרובים.\n"
        "המשימה: עליך לסכם את החדשות האחרונות בנושא המבוקש, אך ורק על סמך הכותרות והתקצירים שסופקו.\n"
        "הנחיות לביצוע:\n"
        "- איסוף וסינתזה: סרוק ושלב מידע ממספר מקורות מובילים (לפחות 3-5 מקורות שונים) מתוך החומר שסופק.\n"
        "- מבנה הסיכום:\n"
        "  * סקירה כללית: פתח בסיכום תמציתי של האירועים העיקריים (2-3 פסקאות).\n"
        "  * חלוקה לפי כיוונים/נרטיבים: חלק את המידע לתתי-נושאים לפי זוויות ראייה שונות.\n"
        "  * ניתוח עומק: עבור כל זווית, הרחב על הפרטים המהותיים, כולל נתונים מספריים אם קיימים.\n"
        "- נייטרליות וייחוס: הקפד על שפה אובייקטיבית וייחס את המידע למקורותיו (למשל: \"לפי דיווח ב-X...\").\n"
        "- שפה וסגנון: כתוב בעברית רהוטה, מקצועית וברורה. הימנע מחזרתיות.\n"
        "פלט נדרש: החזר JSON בלבד במבנה: {"
        "\"summary\": \"לפחות 500 מילים\", "
        "\"details\": \"18-28 שורות קצרות\", "
        "\"key_points\": [\"נקודה 1\", \"נקודה 2\", \"נקודה 3\", \"נקודה 4\", \"נקודה 5\", \"נקודה 6\", \"נקודה 7\", \"נקודה 8\"], "
        "\"sources\": [\"כותרת 1\", \"כותרת 2\"], "
        "\"source_context\": \"פירוט קצר על מקורות ומועדים\""
        "}.\n"
        "אל תכלול טקסט מחוץ ל-JSON ואל תמציא עובדות."
        f"\nנושא: {query}"
        f"\nנטייה: {orientation or 'neutral'}"
    )
    response = llm.invoke(f"{prompt}\n\n{context}")
    parsed = _parse_llm_json(response.content) or {}

    return {
        "topic": query,
        "orientation": orientation or "neutral",
        "summary": parsed.get("summary", ""),
        "details": parsed.get("details", ""),
        "key_points": parsed.get("key_points", []),
        "sources": parsed.get("sources", [item.get('title', '') for item in selected]),
        "source_context": parsed.get("source_context", ""),
    }

@mcp.tool()
def headline_details(headline: str) -> dict[str, object]:
    """
    Given a headline string, return more details (title, source, published, summary).
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
        print("headline_details: using RSS data (no LLM)")
        if exact_match:
            details_lines = [
                f"כותרת: {exact_match['title']}",
                f"מקור: {exact_match['source']}",
                f"תאריך פרסום: {exact_match['published']}",
                "",
                "תקציר מהמקור:",
            ]
            summary_text = (exact_match.get("summary") or "").strip()
            if summary_text:
                for part in re.split(r"[.!?]\s+", summary_text):
                    part = part.strip()
                    if part:
                        details_lines.append(f"• {part}")
            else:
                details_lines.append(exact_match.get("summary") or "(אין תקציר)")
            related = [i for i in items[:6] if i.get("title") and i["title"] != exact_match["title"]][:3]
            if related:
                details_lines.append("")
                details_lines.append("כתבות קשורות מהמקור:")
                for r in related:
                    details_lines.append(f"- {r['title']} ({r.get('source', '')})")
            return {
                "title": exact_match["title"],
                "source": exact_match["source"],
                "published": exact_match["published"],
                "summary": exact_match["summary"],
                "details": "\n".join(details_lines),
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

    print("headline_details: using ChatGPT")
    top_items = items[:10]
    context = "\n".join(
        f"- כותרת: {item['title']}\n  תקציר: {item['summary']}\n  מקור: {item['source']}\n  תאריך: {item['published']}"
        for item in top_items
    )
    prompt = (
        "על סמך הכותרת והחומר מהמקורות שסופקו להלן, כתוב בעברית סיכום מפורט. "
        "הסתמך אך ורק על הכותרות והתקצירים שסופקו – אל תמציא עובדות.\n\n"
        "החזר תשובה בפורמט טקסט עם כותרות שדה (בלי JSON):\n"
        "SUMMARY:\n"
        "  (פסקה או שתיים – סיכום כללי של הידיעה, 4–6 משפטים, בהתבסס על המקור)\n"
        "DETAILS:\n"
        "  (חובה: לפחות 10 שורות ועד 20 שורות. כל שורה – עובדה או פרט קונקרטי מהמקור. "
        "כתוב פרטים ספציפיים: שמות, תאריכים, מספרים, ציטוטים, הסברים מהתקצירים.)\n"
        "KEY_POINTS:\n"
        "  (רשימה עם מקפים, 5–8 נקודות עיקריות)\n"
        "SOURCE_CONTEXT:\n"
        "  (מאילו מקורות ומועדים השתמשת)\n\n"
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
        "summary": parsed.get("summary", ""),
        "details": parsed.get("details", ""),
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
