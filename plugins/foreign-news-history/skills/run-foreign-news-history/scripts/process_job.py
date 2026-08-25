#!/usr/bin/env python3
"""Build foreign-news article history rows from HWP/HWPX work files.

The script is intentionally local-only.  It never edits source documents or Google
Sheets.  It produces deterministic JSON/checkpoint files that a separate sync step
can inspect before any external write.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib
import importlib.util
import json
import re
import sys
import unicodedata
import zipfile
import zlib
from dataclasses import asdict, dataclass, field, replace
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree as ET


RESULT_HEADERS = [
    "상계월",
    "작업일",
    "작업조",
    "초벌 담당",
    "초벌 작업자",
    "최종 담당",
    "최종 작업자",
    "카테고리",
    "매체명",
    "날짜",
    "제목",
    "최종 보고서 포함 여부",
    "유사보도 여부",
    "일일일본동향",
    "비고",
]

SOURCE_HEADERS = [
    "보도일",
    "보도시각 (KST)",
    "URL (단축)",
    "온라인 기사 URL",
    "매체국가",
    "매체명 (원어)",
    "매체명 (한글)",
    "발신지",
    "언어",
    "기자명",
    "제목 (한글)",
]

# Human comparison workflow: reference sources first, then the afternoon folder,
# then the morning folder.  This is chronological first-inflow order, not an
# execution-specific inference, so run_context priorities must never reorder it.
FIXED_COMPARISON_ORDER = ("reference", "afternoon", "morning")
REFERENCE_SOURCE_ORDER = ("regular", "japan")
INITIAL_DRAFT_SOURCE_KINDS = {"domestic_draft", "global_draft"}
COPIED_WORKFILE_SOURCE_KINDS = {
    "afternoon_aggregate",
    "morning_auxiliary",
    "morning_aggregate",
    "late_morning_aggregate",
}

# These are workflow-stage labels, not person mappings.  They are intentionally
# fixed because they define the meaning of the result columns.
WORKFILE_ROLE_LABELS = {
    "domestic_draft": ("1조", "국내"),
    "global_draft": ("1조", "글로벌"),
    "afternoon_aggregate": ("오후", "오후/총괄"),
    "afternoon_aggregate_omitted": ("1조", "오후/총괄"),
    "morning_auxiliary": ("2조", "보조"),
    "morning_aggregate": ("2조", "오전/총괄"),
    "late_morning_aggregate": ("1조", "오후/총괄"),
}
REFERENCE_ROLE_LABELS = {
    "regular": ("정기", "오후/총괄"),
}


def special_source_actual_owner(
    source_kind: str,
    owner: str,
    article_date: str = "",
    job_date: dt.date | None = None,
    qualify_same_day_auxiliary: bool = True,
) -> str:
    """Keep the morning stage for same-day Japan items edited by an auxiliary."""
    owner = clean_text(owner)
    if not owner or "/" in owner:
        return owner
    parsed_article_date = parse_date(article_date)
    is_job_date = bool(
        job_date
        and parsed_article_date
        and (parsed_article_date.month, parsed_article_date.day)
        == (job_date.month, job_date.day)
    )
    if (
        qualify_same_day_auxiliary
        and clean_text(source_kind) == "morning_auxiliary"
        and is_job_date
    ):
        return f"오전/{owner}"
    return owner

ARTICLE_HEADING = re.compile(r"^\s*(?P<star>\*)?\s*<(?P<meta>[^>]+)>\s*(?P<title>.+?)\s*$")
DATE_IN_META = re.compile(r"(?<!\d)(?P<month>\d{1,2})\.(?P<day>\d{1,2})(?!\d)")
# NFKC turns the compatibility jamo `ㅇ` into choseong `ᄋ`.
FRONT_ARTICLE = re.compile(r"^[ㅇᄋ○◦•]\s*(.+)$")
FRONT_CATEGORY = re.compile(r"^[□■▣]\s*(.+)$")


@dataclass
class Article:
    source_file: str
    order: int
    category: str
    media: str
    date: str
    body_title: str
    canonical_title: str = ""
    starred: bool = False
    similar: bool = False
    body_present: bool = False
    body_content_count: int = 0
    raw_heading: str = ""
    front_title_applied: bool = False
    group_representative: bool = False

    @property
    def match_titles(self) -> list[str]:
        values = [self.canonical_title, self.body_title]
        return [value for index, value in enumerate(values) if value and value not in values[:index]]


@dataclass
class Candidate:
    source_type: str
    title: str
    media: str = ""
    date: str = ""
    source_file: str = ""
    workgroup: str = ""
    owner: str = ""
    worker: str = ""
    url: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="외신동향 작업 이력 로컬 처리")
    parser.add_argument("--job-date", help="선택 입력. 없으면 최종보고서/실행 컨텍스트에서 판정")
    parser.add_argument("--morning-dir", required=True)
    parser.add_argument("--afternoon-dir", required=True)
    parser.add_argument("--final-report", required=True)
    parser.add_argument("--source-json", required=True)
    parser.add_argument("--schedule-json", required=True, help="작업일 요일을 선택한 근무 시트 근거 JSON")
    parser.add_argument("--run-context", required=True, help="현재 실행에서 Codex가 근거와 함께 작성한 JSON")
    parser.add_argument(
        "--japan-input",
        help="선택 입력. 제공하는 경우 현재 작업일의 일본언론동향 원본 정확한 경로",
    )
    parser.add_argument("--output-dir")
    parser.add_argument("--config")
    return parser.parse_args()


def skill_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_config_path() -> Path:
    return skill_root() / "assets" / "harness.config.json"


def source_spreadsheet_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return the read-only operational workbook config.

    The legacy single ``spreadsheet`` block remains supported so older run
    configs and synthetic fixtures do not break when the input and result
    workbooks are split.
    """
    return config.get("source_spreadsheet") or config.get("spreadsheet") or {}


def result_spreadsheet_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return the result workbook config, with legacy fallback."""
    return config.get("result_spreadsheet") or config.get("spreadsheet") or {}


def google_sheets_write_enabled(config: dict[str, Any]) -> bool:
    """Only an explicit true value enables writes to the result spreadsheet."""
    return config.get("sync", {}).get("google_sheets_write_enabled") is True


def clean_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    text = text.replace("汫╨", " ").replace("汫h", " ")
    text = "".join(ch if ch >= " " or ch in "\n\t" else " " for ch in text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_key(value: Any) -> str:
    text = unicodedata.normalize("NFKC", clean_text(value)).casefold()
    text = text.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    return re.sub(r"[^0-9a-z가-힣]", "", text)


def ngrams(value: str, size: int = 2) -> set[str]:
    key = normalize_key(value)
    if len(key) <= size:
        return {key} if key else set()
    return {key[index : index + size] for index in range(len(key) - size + 1)}


def text_similarity(left: str, right: str) -> float:
    a, b = normalize_key(left), normalize_key(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    sequence = SequenceMatcher(None, a, b).ratio()
    left_grams, right_grams = ngrams(a), ngrams(b)
    union = left_grams | right_grams
    jaccard = len(left_grams & right_grams) / len(union) if union else 0.0
    containment = min(len(a), len(b)) / max(len(a), len(b)) if a in b or b in a else 0.0
    return max(sequence, 0.55 * sequence + 0.45 * jaccard, 0.94 * containment)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def extract_hwpx(path: Path) -> list[str]:
    paragraphs: list[str] = []
    with zipfile.ZipFile(path) as archive:
        names = sorted(
            name
            for name in archive.namelist()
            if re.fullmatch(r"Contents/section\d+\.xml", name)
        )
        if not names:
            raise ValueError(f"HWPX 본문 XML이 없습니다: {path}")
        for name in names:
            root = ET.fromstring(archive.read(name))
            for element in root.iter():
                if local_name(element.tag) != "p":
                    continue
                if any(
                    child is not element and local_name(child.tag) == "p"
                    for child in element.iter()
                ):
                    continue
                text = "".join(
                    child.text or ""
                    for child in element.iter()
                    if local_name(child.tag) == "t"
                )
                text = clean_text(text)
                if text:
                    paragraphs.append(text)
    return paragraphs


def load_olefile_module() -> Any:
    """Load olefile from the runtime or the plugin's redistribution copy."""
    try:
        return importlib.import_module("olefile")
    except ImportError:
        vendor_path = Path(__file__).resolve().parent / "_vendor" / "olefile.py"
        if not vendor_path.is_file():
            raise RuntimeError(
                "HWP 읽기 모듈을 찾지 못했습니다. 플러그인에 포함된 olefile 배포본을 확인하세요."
            )
        spec = importlib.util.spec_from_file_location("foreign_news_history_olefile", vendor_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("플러그인에 포함된 HWP 읽기 모듈을 불러오지 못했습니다.")
        module = importlib.util.module_from_spec(spec)
        sys.modules.setdefault(spec.name, module)
        spec.loader.exec_module(module)
        return module


def extract_hwp(path: Path) -> list[str]:
    try:
        olefile = load_olefile_module()
    except Exception as exc:  # pragma: no cover - packaging error
        raise RuntimeError(f"HWP 읽기 모듈 초기화 실패: {exc}") from exc

    paragraphs: list[str] = []
    with olefile.OleFileIO(str(path)) as ole:
        header = ole.openstream("FileHeader").read()
        compressed = bool(header[36] & 1)
        section_names = [
            "/".join(parts)
            for parts in ole.listdir()
            if len(parts) == 2 and parts[0] == "BodyText" and parts[1].startswith("Section")
        ]
        section_names.sort(key=lambda name: int(re.search(r"\d+$", name).group()))
        for section_name in section_names:
            data = ole.openstream(section_name).read()
            if compressed:
                data = zlib.decompress(data, -15)
            offset = 0
            while offset + 4 <= len(data):
                record_header = int.from_bytes(data[offset : offset + 4], "little")
                offset += 4
                tag_id = record_header & 0x3FF
                size = (record_header >> 20) & 0xFFF
                if size == 0xFFF:
                    if offset + 4 > len(data):
                        break
                    size = int.from_bytes(data[offset : offset + 4], "little")
                    offset += 4
                payload = data[offset : offset + size]
                offset += size
                if tag_id != 67:
                    continue
                text = clean_text(payload.decode("utf-16le", errors="ignore"))
                if text:
                    paragraphs.append(text)
    return paragraphs


def extract_paragraphs(path: Path) -> list[str]:
    suffix = path.suffix.lower()
    if suffix == ".hwpx":
        return extract_hwpx(path)
    if suffix == ".hwp":
        return extract_hwp(path)
    raise ValueError(f"지원하지 않는 문서 형식: {path}")


def parse_meta(meta: str) -> tuple[str, str]:
    meta = clean_text(meta)
    match = DATE_IN_META.search(meta)
    if match:
        media = meta[: match.start()].strip()
        date_value = f"{int(match.group('month'))}.{int(match.group('day'))}"
    else:
        media = meta
        date_value = ""
    return clean_text(media), date_value


def canonical_media(value: str) -> str:
    """Preserve the report's media spelling; normalize only for comparison elsewhere."""
    return clean_text(value)


def display_media(value: str) -> str:
    """Drop a non-Latin country/source prefix without maintaining a media alias table."""
    media = canonical_media(value)
    first, separator, remainder = media.partition(" ")
    if (
        separator
        and remainder
        and not re.search(r"[A-Za-z0-9]", first)
        # A multi-character prefix is only unambiguous when the actual outlet
        # contains Latin text.  A one-character CJK/Hangul token is the compact
        # country/source notation also used before non-Latin outlet names.
        and (re.search(r"[A-Za-z]", remainder) or len(first) == 1)
    ):
        return remainder
    return media


def display_title(value: str) -> str:
    """Apply only presentation cleanup confirmed by the authoritative result format."""
    title = clean_text(value)
    title = title.replace("‧", "·")
    title = re.sub(r"^\[영상\]\s*", "", title)
    title = re.sub(r"…\s+", "…", title)

    # HWPX paragraphs sometimes encode a closing curly quote as another opening
    # quote.  Balance only an already-open pair; do not rewrite standalone marks.
    opened = False
    balanced: list[str] = []
    for character in title:
        if character == "“":
            if opened:
                balanced.append("”")
                opened = False
            else:
                balanced.append(character)
                opened = True
        elif character == "”":
            balanced.append(character)
            opened = False
        else:
            balanced.append(character)
    return "".join(balanced)


def strip_author_suffix(title: str) -> str:
    title = clean_text(title)
    match = re.search(r"\s+\(([^()]*)\)\s*$", title)
    if not match:
        return title
    suffix = match.group(1)
    author_hints = (
        ",",
        "기고",
        "편집위원",
        "평론가",
        "매니저",
        "특파원",
        "기자",
        "Kim",
        "Lee",
        "Park",
        "Chen",
        "Tan",
        "Young",
    )
    if any(hint in suffix for hint in author_hints) or re.fullmatch(r"[A-Za-z .'-]{5,}", suffix):
        return title[: match.start()].strip()
    return title


def front_entries(paragraphs: list[str]) -> list[dict[str, str]]:
    first_body = next((index for index, text in enumerate(paragraphs) if ARTICLE_HEADING.match(text)), len(paragraphs))
    category = ""
    entries: list[dict[str, str]] = []
    for paragraph in paragraphs[:first_body]:
        category_match = FRONT_CATEGORY.match(paragraph)
        if category_match:
            category = clean_text(category_match.group(1)).strip("[] ")
            continue
        article_match = FRONT_ARTICLE.match(paragraph)
        if not article_match:
            continue
        text = article_match.group(1).strip()
        media = ""
        media_match = re.match(r"^(.*)\s+\(([^()]*)\)\s*$", text)
        if media_match:
            text, media = media_match.group(1).strip(), media_match.group(2).strip()
        entries.append({"category": category, "title": text, "media": media})
    return entries


def is_body_category_candidate(paragraphs: list[str], index: int) -> bool:
    text = clean_text(paragraphs[index])
    if not text or len(text) > 35:
        return False
    if text.startswith(("-", "[", "<", "*", "ㅇ", "ᄋ", "", "□", "■", "▣")):
        return False
    if re.search(r"[,，.!?。?!]$", text):
        return False
    # A body category is a short standalone label followed by an article heading,
    # optionally through bracketed subheadings.  No category vocabulary is assumed.
    checked = 0
    for following in paragraphs[index + 1 :]:
        candidate = clean_text(following)
        if not candidate:
            continue
        checked += 1
        if ARTICLE_HEADING.match(candidate):
            return True
        if candidate.startswith("[") and candidate.endswith("]"):
            if checked < 5:
                continue
        return False
    return False


def front_category_sequence(entries: list[dict[str, str]]) -> list[str]:
    """Return the first-page category sequence without a fixed vocabulary."""
    categories: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        category = clean_text(entry.get("category"))
        key = normalize_key(category)
        if category and key and key not in seen:
            categories.append(category)
            seen.add(key)
    return categories


def body_category_candidate_indices(paragraphs: list[str]) -> list[int]:
    first_heading = next(
        (index for index, text in enumerate(paragraphs) if ARTICLE_HEADING.match(text)),
        None,
    )
    if first_heading is None:
        return []
    last_front_article = max(
        (
            index
            for index, text in enumerate(paragraphs[:first_heading])
            if FRONT_ARTICLE.match(text)
        ),
        default=-1,
    )
    return [
        index
        for index in range(last_front_article + 1, len(paragraphs))
        if is_body_category_candidate(paragraphs, index)
    ]


def body_category_map(paragraphs: list[str], entries: list[dict[str, str]]) -> dict[int, str]:
    front_categories = front_category_sequence(entries)
    front_by_key = {
        normalize_key(category): category
        for category in front_categories
    }
    mapping: dict[int, str] = {}
    for index in body_category_candidate_indices(paragraphs):
        raw = clean_text(paragraphs[index]).strip("[] ")
        if not front_by_key:
            mapping[index] = raw
            continue
        # Match the actual category name, allowing punctuation/spacing variants.
        # Never pair by ordinal position: one missed label would shift every
        # following article into the preceding category.
        canonical = front_by_key.get(normalize_key(raw))
        if canonical:
            mapping[index] = canonical
    return mapping


def final_category_alignment_errors(
    paragraphs: list[str],
    entries: list[dict[str, str]],
) -> list[str]:
    """Fail closed when first-page and body category structure disagree."""
    front_categories = front_category_sequence(entries)
    if not front_categories:
        return ["최종보고서 첫 장 상위 카테고리를 찾지 못함"]

    front_by_key = {
        normalize_key(category): category
        for category in front_categories
    }
    body_raw = [
        clean_text(paragraphs[index]).strip("[] ")
        for index in body_category_candidate_indices(paragraphs)
    ]
    body_known: list[str] = []
    body_known_keys: list[str] = []
    body_unknown: list[str] = []
    for raw in body_raw:
        key = normalize_key(raw)
        canonical = front_by_key.get(key)
        if not canonical:
            body_unknown.append(raw)
            continue
        body_known.append(canonical)
        body_known_keys.append(key)

    front_keys = [normalize_key(category) for category in front_categories]
    present_keys = set(body_known_keys)
    missing = [
        category
        for category, key in zip(front_categories, front_keys)
        if key not in present_keys
    ]
    errors: list[str] = []
    if missing:
        errors.append("본문에서 찾지 못한 첫 장 카테고리: " + ", ".join(missing))
    if body_unknown:
        errors.append("첫 장과 대응하지 않는 본문 카테고리 후보: " + ", ".join(body_unknown))
    if not missing and not body_unknown and body_known_keys != front_keys:
        errors.append(
            "첫 장·본문 카테고리 개수·순서 불일치: "
            + " → ".join(body_known)
        )
    return errors


def parse_document(
    path: Path,
    *,
    require_category_alignment: bool = False,
) -> list[Article]:
    paragraphs = extract_paragraphs(path)
    entries = front_entries(paragraphs)
    if require_category_alignment:
        category_errors = final_category_alignment_errors(paragraphs, entries)
        if category_errors:
            raise ValueError(
                "최종보고서 카테고리 구조 불일치: " + "; ".join(category_errors)
            )
    category_positions = body_category_map(paragraphs, entries)
    category = ""
    articles: list[Article] = []
    positions: list[int] = []
    for index, paragraph in enumerate(paragraphs):
        if index in category_positions:
            category = category_positions[index]
            continue
        heading = ARTICLE_HEADING.match(paragraph)
        if not heading:
            continue
        media, date_value = parse_meta(heading.group("meta"))
        title = strip_author_suffix(heading.group("title"))
        article = Article(
            source_file=str(path),
            order=len(articles) + 1,
            category=category,
            media=media,
            date=date_value,
            body_title=title,
            canonical_title=title,
            starred=bool(heading.group("star")),
            similar=bool(heading.group("star")) or not bool(date_value),
            raw_heading=paragraph,
        )
        articles.append(article)
        positions.append(index)

    body_category_indices = set(body_category_candidate_indices(paragraphs))
    for article_index, article in enumerate(articles):
        start = positions[article_index] + 1
        end = positions[article_index + 1] if article_index + 1 < len(positions) else len(paragraphs)
        content = []
        for paragraph_index in range(start, end):
            if paragraph_index in body_category_indices:
                break
            paragraph = paragraphs[paragraph_index]
            if paragraph.startswith("[") and paragraph.endswith("]"):
                break
            cleaned = paragraph.lstrip("- ").strip()
            if cleaned:
                content.append(cleaned)
        article.body_present = bool(content)
        article.body_content_count = len(content)

    apply_front_titles(articles, entries)
    previous_category = ""
    for article in articles:
        if article.category:
            previous_category = article.category
        elif previous_category:
            article.category = previous_category
    return articles


def media_tokens(value: str) -> set[str]:
    parts = re.split(r"[/,·]", clean_text(value))
    tokens: set[str] = set()
    for part in parts:
        cleaned = canonical_media(part)
        key = normalize_key(cleaned)
        if key:
            tokens.add(key)
        words = cleaned.split()
        if len(words) > 1 and len(words[0]) <= 3:
            suffix = normalize_key(" ".join(words[1:]))
            if suffix:
                tokens.add(suffix)
    return tokens


def media_similarity(left: str, right: str) -> float:
    a, b = media_tokens(left), media_tokens(right)
    if not a or not b:
        return 0.0
    if any(x in y or y in x for x in a for y in b):
        return 1.0
    return max(text_similarity(x, y) for x in a for y in b)


def apply_front_titles(articles: list[Article], entries: list[dict[str, str]]) -> None:
    unused = set(range(len(entries)))
    grouped_articles: set[int] = set()

    # A slash-separated first-page item represents a report group.  The media order
    # identifies the representative; subsequent contiguous media items are similar
    # reports even when an editor left a date/body or omitted the star marker.
    for entry_index, entry in enumerate(entries):
        media_parts = [clean_text(part) for part in entry["media"].split("/") if clean_text(part)]
        if len(media_parts) < 2:
            continue
        anchors = [
            (text_similarity(article.body_title, entry["title"]), index)
            for index, article in enumerate(articles)
            if (not entry["category"] or article.category == entry["category"])
            and media_similarity(article.media, media_parts[0]) >= 0.8
        ]
        if not anchors:
            continue
        _, anchor_index = max(anchors, key=lambda pair: (pair[0], -pair[1]))
        group = [anchor_index]
        cursor = anchor_index
        for media_part in media_parts[1:]:
            matched_index = None
            for index in range(cursor + 1, len(articles)):
                article = articles[index]
                if entry["category"] and article.category and article.category != entry["category"]:
                    break
                if media_similarity(article.media, media_part) >= 0.8:
                    matched_index = index
                    break
            if matched_index is None:
                break
            group.append(matched_index)
            cursor = matched_index
        if len(group) < 2:
            continue
        for position, article_index in enumerate(group):
            article = articles[article_index]
            if entry["category"]:
                article.category = entry["category"]
            if position == 0:
                # A slash group is one edited first-page entry.  Its first media
                # is the representative.  Only inherit the group title when it
                # still describes that representative; media order alone is not
                # enough because editors occasionally reuse a slash bullet for
                # adjacent, unrelated body articles.
                if text_similarity(article.body_title, entry["title"]) >= 0.4:
                    article.canonical_title = entry["title"]
                    article.front_title_applied = True
                article.group_representative = True
            if position:
                article.similar = True
            grouped_articles.add(article_index)
        unused.discard(entry_index)

    # Dated representatives get first claim on each first-page item.  This keeps a
    # combined media bullet from incorrectly overwriting distinct similar-report titles.
    indexed_articles = sorted(enumerate(articles), key=lambda item: (item[1].similar, item[1].order))
    for article_index, article in indexed_articles:
        if article_index in grouped_articles:
            continue
        choices: list[tuple[float, float, int]] = []
        for index in unused:
            entry = entries[index]
            if article.category and entry["category"] and article.category != entry["category"]:
                continue
            title_score = text_similarity(article.body_title, entry["title"])
            media_score = media_similarity(article.media, entry["media"])
            score = 0.72 * title_score + 0.28 * media_score
            choices.append((score, title_score, index))
        if not choices:
            continue
        score, title_score, best_index = max(choices)
        threshold = 0.58 if not article.similar else 0.88
        if score >= threshold and (not article.similar or title_score >= 0.82):
            entry_title = entries[best_index]["title"]
            # Preserve a meaningful middle-dot compound from the body when the
            # front title merely collapsed it (for example, 미·일 -> 미일).
            # Other punctuation edits on the front page remain authoritative.
            collapsed_middle_dot = bool(
                any(mark in article.body_title for mark in ("·", "‧"))
                and not any(mark in entry_title for mark in ("·", "‧"))
                and normalize_key(article.body_title) == normalize_key(entry_title)
            )
            if not collapsed_middle_dot:
                article.canonical_title = entry_title
            article.front_title_applied = True
            if entries[best_index]["category"]:
                article.category = entries[best_index]["category"]
            unused.remove(best_index)


def iter_document_files(path: Path) -> Iterable[Path]:
    if path.is_file():
        if path.suffix.lower() in {".hwp", ".hwpx"}:
            yield path
        return
    if not path.exists():
        return
    for child in sorted(path.rglob("*")):
        if child.is_file() and child.suffix.lower() in {".hwp", ".hwpx"}:
            yield child


def parse_date(value: Any) -> dt.date | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        try:
            return dt.date(1899, 12, 30) + dt.timedelta(days=float(value))
        except (OverflowError, ValueError):
            return None
    text = clean_text(value)
    if not text:
        return None
    for pattern in (
        r"(?P<y>20\d{2})[-./년 ]+(?P<m>\d{1,2})[-./월 ]+(?P<d>\d{1,2})",
        r"(?P<m>\d{1,2})[-./월 ]+(?P<d>\d{1,2})",
    ):
        match = re.search(pattern, text)
        if not match:
            continue
        year = int(match.groupdict().get("y") or dt.date.today().year)
        try:
            return dt.date(year, int(match.group("m")), int(match.group("d")))
        except ValueError:
            return None
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def rows_from_json(path: Path) -> list[dict[str, Any]]:
    def canonical_header(value: Any) -> str:
        cleaned = clean_text(value)
        compact = re.sub(r"[\s*]+", "", cleaned).replace("TINYURL", "")
        for expected in SOURCE_HEADERS:
            expected_compact = re.sub(r"\s+", "", expected)
            if compact == expected_compact:
                return expected
        return cleaned

    def canonical_row(row: dict[str, Any]) -> dict[str, Any]:
        return {canonical_header(key): value for key, value in row.items()}

    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(data, dict) and "values" in data:
        values = data["values"]
        if not values:
            return []
        headers = [canonical_header(value) for value in values[0]]
        return [dict(zip(headers, row)) for row in values[1:] if any(value not in (None, "") for value in row)]
    if isinstance(data, list) and (not data or isinstance(data[0], dict)):
        return [canonical_row(row) for row in data]
    raise ValueError("정기 작업내역 JSON은 values 배열 또는 행 객체 배열이어야 합니다.")


def source_scan_audit_errors(
    path: Path,
    target_date: dt.date,
    expected_spreadsheet_id: str = "",
    expected_sheet_name: str = "",
) -> list[str]:
    """Check that history came from a bounded formatted-value range scan."""
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        return ["정기 작업내역 조회 감사정보가 없음"]
    audit = data.get("source_audit")
    if not isinstance(audit, dict):
        return ["정기 작업내역 조회 감사정보가 없음"]
    errors: list[str] = []
    if int(audit.get("schema_version", 0) or 0) < 2:
        errors.append("정기 작업내역 조회 감사정보 버전이 오래됨")
    if clean_text(audit.get("retrieval_method")) != "bounded_range_scan":
        errors.append("정기 작업내역을 bounded range scan으로 조회하지 않음")
    if clean_text(audit.get("value_render_option")) != "FORMATTED_VALUE":
        errors.append("정기 작업내역 표시값 조회가 아님")
    if clean_text(audit.get("target_date")) != target_date.isoformat():
        errors.append("정기 작업내역 조회 대상일이 작업일 전날과 다름")
    expected_spreadsheet_id = clean_text(expected_spreadsheet_id)
    expected_sheet_name = clean_text(expected_sheet_name)
    if (
        expected_spreadsheet_id
        and clean_text(audit.get("spreadsheet_id")) != expected_spreadsheet_id
    ):
        errors.append("정기 작업내역이 설정된 입력 스프레드시트에서 오지 않음")
    if expected_sheet_name and clean_text(audit.get("sheet_name")) != expected_sheet_name:
        errors.append("정기 작업내역이 설정된 입력 탭에서 오지 않음")
    scan_ranges = audit.get("scan_ranges")
    if not isinstance(scan_ranges, list) or not scan_ranges:
        scan_ranges = [audit.get("scan_range")]
    if any(
        not re.search(r"(?:^|!)[A-Z]+\d+:[A-Z]+\d+$", clean_text(scan_range))
        for scan_range in scan_ranges
    ):
        errors.append("정기 작업내역 조회 범위가 유한한 A1 범위가 아님")
    values = data.get("values")
    matched = max(0, len(values) - 1) if isinstance(values, list) else -1
    try:
        recorded_matched = int(audit.get("matched_row_count", -1))
        scanned = int(audit.get("scanned_row_count", 0))
    except (TypeError, ValueError):
        recorded_matched, scanned = -1, 0
    if matched < 0 or recorded_matched != matched:
        errors.append("정기 작업내역 조회 감사 행 수와 실제 행 수가 다름")
    if scanned < matched:
        errors.append("정기 작업내역 스캔 행 수가 일치 행 수보다 작음")
    return errors


def profile_fields(profile: dict[str, Any] | None) -> tuple[str, str, str]:
    profile = profile or {}
    return (
        clean_text(profile.get("workgroup")),
        clean_text(profile.get("owner")),
        clean_text(profile.get("worker")),
    )


def role_semantic_errors(
    source_type: str,
    source_kind: str,
    workgroup: str,
    owner: str,
    actual_edit_source_kind: str = "",
) -> list[str]:
    """Validate that role labels describe the selected current-run source."""
    source_type = clean_text(source_type)
    source_kind = clean_text(source_kind)
    workgroup = clean_text(workgroup)
    owner = clean_text(owner)
    actual_edit_source_kind = clean_text(actual_edit_source_kind)
    errors: list[str] = []

    if source_type == "regular":
        expected_group, expected_owner = REFERENCE_ROLE_LABELS[source_type]
        if actual_edit_source_kind:
            actual_edit_role = WORKFILE_ROLE_LABELS.get(actual_edit_source_kind)
            if actual_edit_role is None:
                errors.append(
                    f"정기 유입 실제 편집 source_kind 미확인: {actual_edit_source_kind}"
                )
            else:
                expected_owner = actual_edit_role[1]
        if workgroup and workgroup != expected_group:
            errors.append(f"{source_type} 작업조는 {expected_group}이어야 함")
        if owner and owner != expected_owner:
            errors.append(f"{source_type} 초벌 담당은 {expected_owner}이어야 함")
        return errors

    if source_type in REFERENCE_ROLE_LABELS:
        expected_group, expected_owner = REFERENCE_ROLE_LABELS[source_type]
        if workgroup and workgroup != expected_group:
            errors.append(f"{source_type} 작업조는 {expected_group}이어야 함")
        if owner and owner != expected_owner:
            errors.append(f"{source_type} 초벌 담당은 {expected_owner}이어야 함")
        return errors

    if source_type == "worker":
        expected = WORKFILE_ROLE_LABELS.get(source_kind)
        if expected is None:
            return [f"작업자 파일 source_kind 미확인: {source_kind or '빈 값'}"]
        expected_group, expected_owner = expected
        if workgroup and workgroup not in {expected_group, "순방"}:
            errors.append(f"{source_kind} 작업조는 {expected_group} 또는 근거 있는 순방이어야 함")
        if owner and owner != expected_owner:
            errors.append(f"{source_kind} 초벌 담당은 {expected_owner}이어야 함")
        return errors

    if source_type == "japan":
        if workgroup and workgroup != "일본문화원":
            errors.append("일본동향 유입의 작업조는 일본문화원이어야 함")
        if owner:
            if actual_edit_source_kind:
                expected = WORKFILE_ROLE_LABELS.get(actual_edit_source_kind)
                if expected is None:
                    errors.append(f"일본동향 실제 편집 source_kind 미확인: {actual_edit_source_kind}")
                else:
                    allowed_owners = {expected[1]}
                    if actual_edit_source_kind == "morning_auxiliary":
                        allowed_owners.add(f"오전/{expected[1]}")
                    if owner not in allowed_owners:
                        expected_owner = " 또는 ".join(sorted(allowed_owners))
                        errors.append(
                            f"일본동향 초벌 담당은 실제 편집 단계 {expected_owner}이어야 함"
                        )
            elif owner not in {value[1] for value in WORKFILE_ROLE_LABELS.values()}:
                errors.append("일본동향 초벌 담당이 허용된 실제 편집 단계 표기가 아님")
        return errors

    if source_type:
        errors.append(f"지원하지 않는 유입 경로: {source_type}")
    return errors


def profile_is_complete(
    profile: dict[str, Any] | None,
    required_fields: tuple[str, ...] = ("workgroup", "owner", "worker"),
    valid_schedule_refs: dict[str, str] | set[str] | None = None,
    require_schedule: bool = False,
) -> bool:
    complete = bool(profile) and all(
        clean_text(profile.get(field)) for field in required_fields
    ) and any(clean_text(item) for item in profile.get("evidence", []))
    if not complete or not require_schedule:
        return bool(complete)
    refs = {clean_text(item) for item in profile.get("schedule_refs", []) if clean_text(item)}
    if isinstance(valid_schedule_refs, dict):
        matched_refs = refs & set(valid_schedule_refs)
        worker = clean_text(profile.get("worker"))
        return bool(matched_refs) and any(
            clean_text(valid_schedule_refs[ref]) == worker for ref in matched_refs
        )
    return bool(refs & (valid_schedule_refs or set()))


def validate_schedule_evidence(
    schedule: dict[str, Any],
    job_date: dt.date,
    config: dict[str, Any],
) -> dict[str, str]:
    try:
        schema_version = int(schedule.get("schema_version", 0))
    except (TypeError, ValueError):
        schema_version = 0
    source = schedule.get("source") or {}
    sheet_config = source_spreadsheet_config(config)
    expected_heading = clean_text(sheet_config.get("schedule_heading")) or "동향 스케줄"
    if (
        schema_version < 2
        or clean_text(schedule.get("heading")) != expected_heading
        or not clean_text(schedule.get("heading_cell"))
        or not clean_text(source.get("range"))
    ):
        raise ValueError(
            "동향 스케줄 근거가 현재 근무 탭의 표 제목과 실제 조회 범위를 동적으로 확인한 형식이 아닙니다. "
            "근무 탭을 다시 조회해 동향스케줄.json을 생성해야 합니다."
        )
    if clean_text(schedule.get("job_date")) != job_date.isoformat():
        raise ValueError("동향 스케줄 근거의 작업일이 최종보고서 작업일과 다릅니다.")
    expected_weekday = ("월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일")[job_date.weekday()]
    if clean_text(schedule.get("weekday")) != expected_weekday:
        raise ValueError("동향 스케줄 근거의 요일이 작업일과 다릅니다.")
    expected_spreadsheet = clean_text(sheet_config.get("id"))
    expected_title = clean_text(sheet_config.get("title"))
    expected_sheet = clean_text(sheet_config.get("schedule_sheet"))
    if expected_spreadsheet and clean_text(source.get("spreadsheet_id")) != expected_spreadsheet:
        raise ValueError("동향 스케줄 근거가 설정된 입력 스프레드시트에서 오지 않았습니다.")
    if (
        expected_title
        and clean_text(source.get("spreadsheet_title"))
        and clean_text(source.get("spreadsheet_title")) != expected_title
    ):
        raise ValueError("동향 스케줄 근거의 입력 스프레드시트 제목이 설정과 다릅니다.")
    if expected_sheet and clean_text(source.get("sheet_name")) != expected_sheet:
        raise ValueError("동향 스케줄 근거의 탭이 설정된 근무 탭과 다릅니다.")
    refs = {
        clean_text(item.get("ref")): clean_text(item.get("worker"))
        for item in schedule.get("assignments", [])
        if isinstance(item, dict)
        and clean_text(item.get("ref"))
        and clean_text(item.get("worker"))
        and clean_text(item.get("worker_cell"))
    }
    if not refs:
        raise ValueError("동향 스케줄 근거에 작업일 담당자 행이 없습니다.")
    return refs


def regular_candidates(
    path: Path,
    target_date: dt.date,
    profile: dict[str, Any] | None,
    valid_schedule_refs: dict[str, str] | set[str] | None = None,
    require_schedule: bool = False,
    require_scan_audit: bool = False,
    expected_spreadsheet_id: str = "",
    expected_sheet_name: str = "",
) -> tuple[list[Candidate], list[str]]:
    if require_scan_audit:
        audit_errors = source_scan_audit_errors(
            path,
            target_date,
            expected_spreadsheet_id,
            expected_sheet_name,
        )
        if audit_errors:
            raise ValueError("; ".join(audit_errors))
    rows = rows_from_json(path)
    headers = set(rows[0]) if rows else set()
    warnings: list[str] = []
    missing = [header for header in ("보도일", "제목 (한글)") if header not in headers]
    if missing:
        warnings.append("정기 작업내역 필수 열 누락: " + ", ".join(missing))
    workgroup, owner, worker = profile_fields(profile)
    semantic_errors = role_semantic_errors("regular", "", workgroup, owner)
    profile_complete = profile_is_complete(
        profile,
        valid_schedule_refs=valid_schedule_refs,
        require_schedule=require_schedule,
    ) and not semantic_errors
    if not profile_complete:
        warnings.append("정기 작업내역 역할 근거가 실행 컨텍스트에 없거나 불완전함")
    warnings.extend(f"정기 작업내역 역할 의미 불일치: {error}" for error in semantic_errors)
    candidates: list[Candidate] = []
    for row in rows:
        row_date = parse_date(row.get("보도일"))
        work_date = parse_date(row.get("작업날짜"))
        one_day_work_date_carryover = bool(
            work_date == target_date
            and row_date == target_date - dt.timedelta(days=1)
        )
        if row_date != target_date and not one_day_work_date_carryover:
            continue
        title = clean_text(row.get("제목 (한글)"))
        if not title:
            continue
        media = clean_text(row.get("매체명 (원어)")) or clean_text(row.get("매체명 (한글)"))
        candidates.append(
            Candidate(
                source_type="regular",
                title=title,
                media=canonical_media(media),
                date=f"{row_date.month}.{row_date.day}",
                source_file=str(path),
                workgroup=workgroup,
                owner=owner,
                worker=worker,
                url=clean_text(row.get("온라인 기사 URL")) or clean_text(row.get("URL (단축)")),
                extra={
                    **row,
                    "comparison_stage": "reference",
                    "one_day_work_date_carryover": one_day_work_date_carryover,
                    "priority": int((profile or {}).get("priority", 0)),
                    "profile_complete": profile_complete,
                    "profile_evidence": (profile or {}).get("evidence", []),
                    "schedule_refs": (profile or {}).get("schedule_refs", []),
                    "source_history_operational_fields": bool(
                        clean_text(row.get("작업날짜")) and clean_text(row.get("작업 조"))
                    ),
                },
            )
        )
    if not candidates:
        warnings.append(f"정기 작업내역에서 {target_date.isoformat()} 행을 찾지 못했습니다.")
    return candidates, warnings


def worker_candidates(
    paths: list[Path],
    final_hash: str,
    run_context: dict[str, Any],
    comparison_stages: dict[str, str] | None = None,
    valid_schedule_refs: dict[str, str] | set[str] | None = None,
    require_schedule: bool = False,
) -> tuple[list[Candidate], list[dict[str, Any]], list[str]]:
    candidates: list[Candidate] = []
    files: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen_hashes: set[str] = set()
    profiles = run_context.get("files", [])
    for path in paths:
        digest = sha256_file(path)
        resolved = str(path.resolve())
        comparison_stage = clean_text((comparison_stages or {}).get(resolved))
        profile = next(
            (
                item
                for item in profiles
                if clean_text(item.get("sha256")) == digest
                or (item.get("path") and Path(item["path"]).resolve() == path.resolve())
            ),
            None,
        )
        workgroup, owner, worker = profile_fields(profile)
        source_kind = clean_text((profile or {}).get("source_kind"))
        semantic_errors = role_semantic_errors(
            "worker", source_kind, workgroup, owner
        )
        profile_complete = profile_is_complete(
            profile,
            valid_schedule_refs=valid_schedule_refs,
            require_schedule=require_schedule,
        ) and not semantic_errors
        file_info = {
            "path": resolved,
            "size": path.stat().st_size,
            "sha256": digest,
            "role": "final_report_duplicate" if digest == final_hash else clean_text((profile or {}).get("source_kind")) or "unresolved",
            "comparison_stage": comparison_stage or "unresolved",
            "deduplicated": digest == final_hash or digest in seen_hashes,
            "context_resolved": profile_complete,
            "evidence": (profile or {}).get("evidence", []),
            "schedule_refs": (profile or {}).get("schedule_refs", []),
        }
        files.append(file_info)
        if digest == final_hash or digest in seen_hashes:
            seen_hashes.add(digest)
            continue
        seen_hashes.add(digest)
        if not profile_complete:
            warnings.append(f"파일 역할 근거 불완전: {path.name}")
        warnings.extend(f"파일 역할 의미 불일치: {path.name}: {error}" for error in semantic_errors)
        if comparison_stage not in {"morning", "afternoon"}:
            warnings.append(f"오전·오후 비교 단계 미확인: {path.name}")
        try:
            articles = parse_document(path)
        except Exception as exc:
            raise ValueError(f"작업자 파일 파싱 실패: {path.name}: {exc}") from exc
        if not articles:
            raise ValueError(f"작업자 파일에서 기사 제목을 찾지 못했습니다: {path.name}")
        for article in articles:
            candidates.append(
                Candidate(
                    source_type="worker",
                    title=article.canonical_title or article.body_title,
                    media=article.media,
                    date=article.date,
                    source_file=resolved,
                    workgroup=workgroup,
                    owner=owner,
                    worker=worker,
                    extra={
                        "comparison_stage": comparison_stage,
                        "priority": int((profile or {}).get("priority", 0)),
                        "source_kind": source_kind,
                        "include_unmatched": bool((profile or {}).get("include_unmatched", False)),
                        "front_title_applied": article.front_title_applied,
                        "group_representative": article.group_representative,
                        "starred": article.starred,
                        "similar": article.similar,
                        "body_title": article.body_title,
                        "body_content_count": article.body_content_count,
                        "profile_complete": profile_complete,
                        "profile_evidence": (profile or {}).get("evidence", []),
                        "schedule_refs": (profile or {}).get("schedule_refs", []),
                        "category": article.category,
                        "article_order": article.order,
                    },
                )
            )
    return candidates, files, warnings


def workfile_revision_rank(path: str) -> int:
    stem = Path(path).stem
    if "최종" in stem:
        return 10_000
    revisions = [int(value) for value in re.findall(r"(\d+)\s*차", stem)]
    return max(revisions, default=0)


def apply_latest_group_titles(
    articles: list[Article],
    candidates: list[Candidate],
    review_threshold: float,
) -> None:
    """Recover an edited slash-group title omitted from the final front page."""
    group_candidates = [
        candidate for candidate in candidates
        if candidate.extra.get("group_representative")
        and clean_text(candidate.extra.get("source_kind")) in {"afternoon_aggregate", "morning_aggregate"}
    ]
    for article in articles:
        if article.front_title_applied:
            continue
        matches: list[tuple[tuple[int, int, int], Candidate]] = []
        for candidate in group_candidates:
            if media_similarity(article.media, candidate.media) < 0.85:
                continue
            if article.date and candidate.date and parse_date(article.date) != parse_date(candidate.date):
                continue
            source_body_title = clean_text(candidate.extra.get("body_title"))
            if not source_body_title:
                continue
            if max(text_similarity(title, source_body_title) for title in article.match_titles) < review_threshold:
                continue
            stage = clean_text(candidate.extra.get("comparison_stage"))
            stage_rank = 2 if stage == "morning" else 1
            key = (
                stage_rank,
                workfile_revision_rank(candidate.source_file),
                int(candidate.extra.get("article_order", 0)),
            )
            matches.append((key, candidate))
        if not matches:
            continue
        _, selected = max(matches, key=lambda item: item[0])
        if normalize_key(selected.title) == normalize_key(article.body_title):
            continue
        article.canonical_title = selected.title
        article.front_title_applied = True
        article.group_representative = True


def apply_latest_aggregate_categories(
    articles: list[Article],
    candidates: list[Candidate],
    review_threshold: float,
    auto_threshold: float,
) -> None:
    """Use a compatible category from the latest morning aggregate."""
    aggregate_candidates = [
        candidate
        for candidate in candidates
        if clean_text(candidate.extra.get("source_kind")) == "morning_aggregate"
        and clean_text(candidate.extra.get("category"))
    ]
    for article in articles:
        matches = [
            (candidate_score(article, candidate), candidate)
            for candidate in aggregate_candidates
            if candidate_score(article, candidate) >= max(review_threshold, auto_threshold)
        ]
        if not matches:
            continue
        _, selected = max(
            matches,
            key=lambda item: (
                workfile_revision_rank(item[1].source_file),
                item[0],
                int(item[1].extra.get("article_order", 0)),
            ),
        )
        latest_category = clean_text(selected.extra.get("category"))
        current_key = normalize_key(article.category)
        latest_key = normalize_key(latest_category)
        if current_key and latest_key and (current_key in latest_key or latest_key in current_key):
            article.category = latest_category


def source_history_has_operational_fields(candidates: list[Candidate]) -> bool:
    """Return whether the selected source snapshot carries current work metadata."""
    return any(
        bool(candidate.extra.get("source_history_operational_fields"))
        for candidate in candidates
    )


def workbook_date_column_mode(candidates: list[Candidate]) -> str:
    """Preserve the date-cell convention carried by the source-history schema."""
    if candidates and not source_history_has_operational_fields(candidates):
        return "numeric_month_day"
    return "text"


def adjacent_compound_category_labels(
    final_categories: list[str],
    aggregate_front_categories: list[str],
) -> dict[str, str]:
    """Map adjacent final categories to an explicit aggregate-front compound label.

    This is deliberately data-driven: the returned spelling and separator come
    from a supplied workfile's front-page category table, never from a built-in
    category dictionary.
    """
    ordered_final = list(dict.fromkeys(clean_text(value) for value in final_categories if clean_text(value)))
    labels = [clean_text(value) for value in aggregate_front_categories if clean_text(value)]
    mappings: dict[str, str] = {}
    for left, right in zip(ordered_final, ordered_final[1:]):
        combined_key = f"{normalize_key(left)}{normalize_key(right)}"
        if not combined_key:
            continue
        matches = [
            label for label in labels
            if normalize_key(label) == combined_key
            and normalize_key(label) not in {normalize_key(left), normalize_key(right)}
        ]
        if len(set(matches)) == 1:
            mappings[normalize_key(left)] = matches[0]
    return mappings


def apply_aggregate_front_category_labels(
    articles: list[Article],
    candidates: list[Candidate],
) -> None:
    """Adopt compound category labels explicitly printed by aggregate workfiles."""
    aggregate_kinds = {"morning_aggregate", "afternoon_aggregate"}
    sources: dict[str, tuple[int, int]] = {}
    for candidate in candidates:
        source_kind = clean_text(candidate.extra.get("source_kind"))
        source_file = clean_text(candidate.source_file)
        if source_kind not in aggregate_kinds or not source_file:
            continue
        stage_rank = 1 if clean_text(candidate.extra.get("comparison_stage")) == "afternoon" else 0
        sources[source_file] = max(
            sources.get(source_file, (-1, -1)),
            (workfile_revision_rank(source_file), stage_rank),
        )

    ranked_labels: list[tuple[tuple[int, int], str]] = []
    for source_file, (revision, stage_rank) in sources.items():
        path = Path(source_file)
        if not path.exists():
            continue
        try:
            categories = front_category_sequence(front_entries(extract_paragraphs(path)))
        except (OSError, ValueError, zipfile.BadZipFile):
            continue
        source_rank = (revision, stage_rank)
        ranked_labels.extend((source_rank, category) for category in categories)

    if not ranked_labels:
        return
    best_rank = max(rank for rank, _ in ranked_labels)
    best_labels = [label for rank, label in ranked_labels if rank == best_rank]
    mappings = adjacent_compound_category_labels(
        [article.category for article in articles],
        best_labels,
    )
    for article in articles:
        replacement = mappings.get(normalize_key(article.category))
        if replacement:
            article.category = replacement


def align_group_child_titles(articles: list[Article]) -> None:
    """Keep implicit group children consistent with the edited representative."""
    representative_subject = ""
    for article in articles:
        if article.group_representative:
            match = re.match(r"^([가-힣])\s*대통령\b", article.canonical_title)
            representative_subject = match.group(1) if match else ""
            continue
        if not article.similar:
            representative_subject = ""
            continue
        if not representative_subject or article.starred:
            continue
        match = re.match(r"^(?P<name>[가-힣]{2,4})\s*대통령(?P<rest>\s*[,，].*)$", article.canonical_title)
        if match and match.group("name").startswith(representative_subject):
            article.canonical_title = f"{representative_subject} 대통령{match.group('rest')}"


def candidate_as_article(candidate: Candidate) -> Article:
    return Article(
        source_file=candidate.source_file,
        order=int(candidate.extra.get("article_order", 0)),
        category=clean_text(candidate.extra.get("category")),
        media=candidate.media,
        date=candidate.date,
        body_title=clean_text(candidate.extra.get("body_title")) or candidate.title,
        canonical_title=candidate.title,
    )


def infer_representative_draft_lineage(
    final_articles: list[Article],
    worker_candidates: list[Candidate],
    matching: dict[str, float],
) -> dict[int, Candidate]:
    """Recover a draft lineage for a representative inserted during aggregation.

    The inference is deliberately structural: the final row must introduce a
    similar-report cluster, and the matching afternoon aggregate row must sit
    between nearby rows that both map to the same current individual draft.
    """
    initial_drafts = [
        candidate
        for candidate in worker_candidates
        if clean_text(candidate.extra.get("source_kind")) in INITIAL_DRAFT_SOURCE_KINDS
    ]
    aggregate_files: dict[str, list[Candidate]] = {}
    for candidate in worker_candidates:
        if clean_text(candidate.extra.get("source_kind")) != "afternoon_aggregate":
            continue
        aggregate_files.setdefault(candidate.source_file, []).append(candidate)
    for candidates in aggregate_files.values():
        candidates.sort(key=lambda item: int(item.extra.get("article_order", 0)))

    def direct_draft(candidate: Candidate) -> Candidate | None:
        ranked = ranked_origin_matches(
            candidate_as_article(candidate),
            initial_drafts,
            matching["auto_threshold"],
        )
        return ranked[0][1] if ranked else None

    inferred: dict[int, Candidate] = {}
    for index, article in enumerate(final_articles[:-1]):
        if not final_articles[index + 1].similar:
            continue
        aggregate_ranked = ranked_origin_matches(
            article,
            [candidate for candidates in aggregate_files.values() for candidate in candidates],
            matching["auto_threshold"],
        )
        if not aggregate_ranked:
            continue
        aggregate_score, aggregate = aggregate_ranked[0]
        sequence = aggregate_files[aggregate.source_file]
        position = sequence.index(aggregate)

        cluster_end = index + 1
        while cluster_end < len(final_articles) and final_articles[cluster_end].similar:
            cluster_end += 1
        similar_count = cluster_end - index - 1
        before_position = position - 1
        after_position = position + similar_count + 1
        if before_position < 0 or after_position >= len(sequence):
            continue
        # The boundary articles must themselves be direct draft matches.  Looking
        # several rows away can incorrectly pull an unrelated representative into
        # a draft merely because the same editor handled surrounding sections.
        before = direct_draft(sequence[before_position])
        after = direct_draft(sequence[after_position])
        if before is None or after is None:
            continue
        before_key = (
            clean_text(before.extra.get("source_kind")),
            str(Path(before.source_file).resolve()),
            before.workgroup,
            before.owner,
            before.worker,
        )
        after_key = (
            clean_text(after.extra.get("source_kind")),
            str(Path(after.source_file).resolve()),
            after.workgroup,
            after.owner,
            after.worker,
        )
        if before_key != after_key:
            continue
        evidence = list(aggregate.extra.get("profile_evidence", []))
        evidence.extend(before.extra.get("profile_evidence", []))
        evidence.append(
            "유사보도 대표행의 취합 위치가 동일 개별 초안의 직접 일치 기사 사이에 있음"
        )
        inferred[article.order] = replace(
            aggregate,
            workgroup=before.workgroup,
            owner=before.owner,
            worker=before.worker,
            extra={
                **aggregate.extra,
                "source_kind": clean_text(before.extra.get("source_kind")),
                "profile_complete": bool(before.extra.get("profile_complete", False)),
                "profile_evidence": list(dict.fromkeys(evidence)),
                "schedule_refs": list(before.extra.get("schedule_refs", [])),
                "lineage_inferred_from_aggregate": True,
                "lineage_score": round(aggregate_score, 4),
            },
        )
    return inferred


def japan_candidates(
    path: Path | None,
    profile: dict[str, Any] | None,
    valid_schedule_refs: dict[str, str] | set[str] | None = None,
    require_schedule: bool = False,
) -> tuple[list[Candidate], list[str]]:
    if path is None:
        return [], ["일본동향 선택 입력 미제공: 일일일본동향 열은 공란으로 유지"]
    workgroup, declared_owner, declared_worker = profile_fields(profile)
    # The Japan source is a bundle, not an edit-stage file.  A role declared on
    # the bundle cannot prove that every article was first edited by the same
    # person.  Seed only its special workgroup and let
    # enrich_special_source_roles() derive owner/worker per article from the
    # current run's schedule-backed workfiles.
    owner = ""
    worker = ""
    candidates: list[Candidate] = []
    warnings: list[str] = []
    semantic_errors = role_semantic_errors("japan", "", workgroup, "")
    profile_complete = False
    warnings.extend(f"일본동향 역할 의미 불일치: {error}" for error in semantic_errors)
    if declared_owner or declared_worker:
        warnings.append(
            "일본동향 묶음의 공통 담당·작업자 선언은 사용하지 않고 "
            "현재 작업본 비교로 기사별 재판정"
        )
    if path.suffix.lower() == ".json" and path.is_file():
        try:
            rows = rows_from_json(path)
            for row in rows:
                title = clean_text(row.get("제목") or row.get("제목 (한글)"))
                if title:
                    candidates.append(
                        Candidate(
                            source_type="japan",
                            title=title,
                            media=clean_text(row.get("매체명") or row.get("매체명 (한글)")),
                            date=clean_text(row.get("날짜") or row.get("보도일")),
                            source_file=str(path.resolve()),
                            workgroup=workgroup,
                            owner=owner,
                            worker=worker,
                            extra={
                                "comparison_stage": "reference",
                                "priority": int((profile or {}).get("priority", 0)),
                                "profile_complete": profile_complete,
                                "profile_evidence": (profile or {}).get("evidence", []),
                                "schedule_refs": (profile or {}).get("schedule_refs", []),
                                "body_content_count": 0,
                            },
                        )
                    )
        except Exception as exc:
            raise ValueError(f"일본동향 JSON 파싱 실패: {exc}") from exc
        if not candidates:
            raise ValueError("일본동향 JSON에서 기사를 찾지 못했습니다.")
        return candidates, warnings

    for document in iter_document_files(path):
        try:
            for article in parse_document(document):
                candidates.append(
                    Candidate(
                        source_type="japan",
                        title=article.canonical_title or article.body_title,
                        media=article.media,
                        date=article.date,
                        source_file=str(document.resolve()),
                        workgroup=workgroup,
                        owner=owner,
                        worker=worker,
                        extra={
                            "comparison_stage": "reference",
                            "priority": int((profile or {}).get("priority", 0)),
                            "profile_complete": profile_complete,
                            "profile_evidence": (profile or {}).get("evidence", []),
                            "schedule_refs": (profile or {}).get("schedule_refs", []),
                            "body_content_count": article.body_content_count,
                        },
                    )
                )
        except Exception as exc:
            raise ValueError(f"일본동향 파일 파싱 실패: {document.name}: {exc}") from exc
    if not candidates:
        raise ValueError("일본동향 입력에서 기사를 찾지 못했습니다.")
    return candidates, warnings


def candidate_score(article: Article, candidate: Candidate) -> float:
    title_score = max(text_similarity(title, candidate.title) for title in article.match_titles)
    media_score = media_similarity(article.media, candidate.media)
    date_score = 1.0 if article.date and candidate.date and normalize_key(article.date) == normalize_key(candidate.date) else 0.0
    return min(1.0, 0.88 * title_score + 0.08 * media_score + 0.04 * date_score)


def candidate_identity_matches(article: Article, candidate: Candidate) -> bool:
    """Require exact title identity plus compatible outlet and non-conflicting date."""
    exact_title = normalize_key(candidate.title) in {
        normalize_key(title) for title in article.match_titles
    }
    if not exact_title or media_similarity(article.media, candidate.media) < 0.85:
        return False
    article_date = parse_date(article.date)
    candidate_date = parse_date(candidate.date)
    return not (article_date and candidate_date and article_date != candidate_date)


def reference_candidates_for_article(
    article: Article,
    candidates: list[Candidate],
) -> list[Candidate]:
    """Keep compatible outlets plus exact title/date translated-outlet evidence."""
    article_date = parse_date(article.date)
    exact_titles = {
        normalize_key(title) for title in article.match_titles if normalize_key(title)
    }
    return [
        candidate
        for candidate in candidates
        if (
            media_similarity(article.media, candidate.media) >= 0.85
            or (
                article_date is not None
                and parse_date(candidate.date) == article_date
                and normalize_key(candidate.title) in exact_titles
            )
        )
    ]


def regular_reference_covers_explicit_similar_variant(
    regular: Candidate,
    worker_candidates: list[Candidate],
    matching: dict[str, float],
) -> bool:
    """Detect a regular-history title explicitly retained as a similar row."""
    probe = candidate_as_article(regular)
    return any(
        candidate.extra.get("similar", False)
        and clean_text(candidate.extra.get("source_kind")) in {
            "afternoon_aggregate", "morning_aggregate"
        }
        and media_similarity(regular.media, candidate.media) >= 0.85
        and candidate_score(probe, candidate) >= matching["auto_threshold"]
        for candidate in worker_candidates
    )


def explicit_similar_cluster_regular(
    article: Article,
    regular_candidates: list[Candidate],
    worker_candidates: list[Candidate],
    matching: dict[str, float],
) -> tuple[float, Candidate] | None:
    """Recover the regular representative of an explicit variant cluster.

    A final representative can be retitled while the latest aggregate retains
    the exact regular-history wording as its explicit similar row. That second,
    independent occurrence identifies the earlier regular inflow even when the
    representative title alone falls just below the ordinary review threshold.
    """
    covered = [
        candidate
        for candidate in regular_candidates
        if regular_reference_covers_explicit_similar_variant(
            candidate,
            worker_candidates,
            matching,
        )
    ]
    ranked = ranked_matches(article, covered)
    if not ranked or ranked[0][0] < matching["review_threshold"] * 0.8:
        return None
    if (
        len(ranked) > 1
        and ranked[0][0] - ranked[1][0] < matching["ambiguity_margin"]
    ):
        return None
    return ranked[0]


def ranked_matches(article: Article, candidates: list[Candidate]) -> list[tuple[float, Candidate]]:
    return sorted(
        ((candidate_score(article, candidate), candidate) for candidate in candidates),
        key=lambda pair: (pair[0], int(pair[1].extra.get("priority", 0))),
        reverse=True,
    )


def ranked_origin_matches(
    article: Article,
    candidates: list[Candidate],
    review_threshold: float,
) -> list[tuple[float, Candidate]]:
    eligible = [
        pair
        for pair in ((candidate_score(article, candidate), candidate) for candidate in candidates)
        if pair[0] >= review_threshold
    ]
    return sorted(
        eligible,
        key=lambda pair: (int(pair[1].extra.get("priority", 0)), pair[0]),
        reverse=True,
    )


def unique_rewritten_trend_duplicate(
    article: Article,
    regular_candidates: list[Candidate],
    worker_candidates: list[Candidate],
    matching: dict[str, float],
) -> tuple[float, Candidate] | None:
    """Recover a uniquely rewritten direct trend draft duplicated in regular history.

    A direct draft can be retitled in an aggregate far enough to fall below the
    normal review threshold. Treat it as the same trend-work item only when the
    regular match is automatic, exactly one direct draft has compatible media
    and date plus meaningful residual title overlap, and a later aggregate
    independently carries the final title at the automatic threshold.
    """
    if article.similar and not article.raw_heading:
        # Synthetic similar rows recovered from an aggregate are copied rows,
        # not final-report evidence of a uniquely rewritten direct draft.
        return None
    if not article.similar and any(
        candidate_identity_matches(article, candidate)
        for candidate in regular_candidates
    ):
        # An exact regular representative is not a rewritten trend lineage.
        return None
    if not ranked_origin_matches(
        article,
        regular_candidates,
        matching["auto_threshold"],
    ):
        return None
    direct_drafts = [
        candidate
        for candidate in worker_candidates
        if clean_text(candidate.extra.get("source_kind")) in INITIAL_DRAFT_SOURCE_KINDS
        and not candidate.extra.get("similar", False)
        and int(candidate.extra.get("body_content_count", 0)) > 0
    ]
    residual_threshold = matching["review_threshold"] * 0.8
    compatible: list[tuple[float, Candidate]] = []
    article_date = parse_date(article.date)
    for candidate in direct_drafts:
        if media_similarity(article.media, candidate.media) < 0.85:
            continue
        candidate_date = parse_date(candidate.date)
        if article_date and candidate_date and article_date != candidate_date:
            continue
        score = candidate_score(article, candidate)
        if score >= residual_threshold:
            compatible.append((score, candidate))
    if len(compatible) != 1:
        return None

    aggregate_kinds = {"afternoon_aggregate", "morning_aggregate"}
    aggregate_match = any(
        clean_text(candidate.extra.get("source_kind")) in aggregate_kinds
        and media_similarity(article.media, candidate.media) >= 0.85
        and candidate_score(article, candidate) >= matching["auto_threshold"]
        for candidate in worker_candidates
    )
    return compatible[0] if aggregate_match else None


def enrich_special_source_roles(
    candidates: list[Candidate],
    worker_candidates: list[Candidate],
    matching: dict[str, float],
    job_date: dt.date | None = None,
    qualify_same_day_auxiliary: bool = True,
) -> list[Candidate]:
    """Keep a special workgroup while deriving each article's editor.

    A Japan report is assigned as one work packet.  Some days its articles are
    carried by an auxiliary file and on other days the aggregate editor imports
    the complete packet.  Normally the current, schedule-backed file that covers
    the most supplied Japan articles supplies the role.  When a substantive
    auxiliary file covers all but one item, preserve that contribution for its
    covered articles and use the aggregate editor only for uncovered or
    heading-only items.
    """
    eligible_workers = [
        item for item in worker_candidates if item.extra.get("profile_complete", False)
    ]
    file_scores: list[dict[str, Any]] = []
    for source_file in dict.fromkeys(item.source_file for item in eligible_workers):
        current = [item for item in eligible_workers if item.source_file == source_file]
        matched_scores: dict[int, float] = {}
        matched_workers: dict[int, Candidate] = {}
        for candidate in candidates:
            probe = Article(
                source_file=candidate.source_file,
                order=0,
                category="",
                media=candidate.media,
                date=candidate.date,
                body_title=candidate.title,
                canonical_title=candidate.title,
            )
            ranked = ranked_matches(probe, current)
            if ranked and ranked[0][0] >= matching["review_threshold"]:
                matched_scores[id(candidate)] = ranked[0][0]
                matched_workers[id(candidate)] = ranked[0][1]
        if matched_scores:
            representative = current[0]
            stage = clean_text(representative.extra.get("comparison_stage"))
            file_scores.append({
                "coverage": len(matched_scores),
                "average": sum(matched_scores.values()) / len(matched_scores),
                "stage_order": 1 if stage == "afternoon" else 0,
                "source_file": source_file,
                "representative": representative,
                "matches": matched_scores,
                "matched_workers": matched_workers,
                "source_kind": clean_text(representative.extra.get("source_kind")),
            })

    def record_key(record: dict[str, Any]) -> tuple[int, float, int]:
        return record["coverage"], record["average"], record["stage_order"]

    selected_record = max(file_scores, key=record_key) if file_scores else None
    aggregate_kinds = {"afternoon_aggregate", "morning_aggregate"}
    aggregate_records = [record for record in file_scores if record["source_kind"] in aggregate_kinds]
    auxiliary_records = [record for record in file_scores if record["source_kind"] == "morning_auxiliary"]
    best_aggregate = max(aggregate_records, key=record_key) if aggregate_records else None
    best_auxiliary = max(auxiliary_records, key=record_key) if auxiliary_records else None

    selected_by_candidate: dict[int, dict[str, Any]] = {}
    if selected_record:
        selected_by_candidate = {id(candidate): selected_record for candidate in candidates}
    if (
        best_aggregate
        and best_auxiliary
        and best_aggregate["coverage"] > best_auxiliary["coverage"]
        and len(candidates) > 1
        and best_auxiliary["coverage"] == len(candidates) - 1
    ):
        # A nearly complete auxiliary packet can contain the substantive work for
        # only part of a Japan bundle. Preserve that article-level contribution,
        # while the aggregate editor remains responsible for heading-only or
        # uncovered items.
        for candidate in candidates:
            auxiliary_match = best_auxiliary["matched_workers"].get(id(candidate))
            if (
                auxiliary_match is not None
                and int(auxiliary_match.extra.get("body_content_count", 0)) > 0
            ):
                selected_by_candidate[id(candidate)] = best_auxiliary
            else:
                selected_by_candidate[id(candidate)] = best_aggregate

    enriched: list[Candidate] = []
    for candidate in candidates:
        existing_errors = role_semantic_errors(
            "japan",
            "",
            candidate.workgroup,
            candidate.owner,
            clean_text(candidate.extra.get("actual_edit_source_kind")),
        )
        if (
            candidate.owner
            and candidate.worker
            and candidate.extra.get("profile_complete")
            and not existing_errors
        ):
            enriched.append(candidate)
            continue
        record = selected_by_candidate.get(id(candidate))
        selected = record["representative"] if record else None
        if selected is None:
            enriched.append(candidate)
            continue
        coverage = int(record["coverage"])
        selected_score = float(record["average"])
        selected_stage = clean_text(selected.extra.get("comparison_stage"))
        refs = list(selected.extra.get("schedule_refs", []))
        selected_source_kind = clean_text(selected.extra.get("source_kind"))
        selected_owner = special_source_actual_owner(
            selected_source_kind,
            selected.owner,
            candidate.date,
            job_date,
            qualify_same_day_auxiliary,
        )
        evidence = list(candidate.extra.get("profile_evidence", []))
        evidence.extend(selected.extra.get("profile_evidence", []))
        evidence.append(
            f"일본동향 전체 기사 취급 파일 대조: {selected_stage} "
            f"{Path(selected.source_file).name} (일치 {coverage}건, 평균 {selected_score:.3f})"
        )
        extra = {
            **candidate.extra,
            "profile_evidence": list(dict.fromkeys(clean_text(item) for item in evidence if clean_text(item))),
            "schedule_refs": refs,
            "actual_edit_stage": selected_stage,
            "actual_edit_file": selected.source_file,
            "actual_edit_source_kind": selected_source_kind,
        }
        semantic_errors = role_semantic_errors(
            "japan",
            "",
            candidate.workgroup,
            selected_owner,
            selected_source_kind,
        )
        extra["profile_complete"] = bool(
            candidate.workgroup and selected.owner and selected.worker and refs and not semantic_errors
        )
        extra["role_semantic_errors"] = semantic_errors
        enriched.append(replace(candidate, owner=selected_owner, worker=selected.worker, extra=extra))
    return enriched


def plausible_reference_conflicts(
    article: Article,
    pools: dict[str, list[Candidate]],
    review_threshold: float,
) -> list[tuple[float, Candidate]]:
    """Find earlier-source candidates hidden by a large title rewrite.

    This never changes the origin automatically.  It creates a review gate when
    a later work file is an exact match but an earlier source has the same current
    media/date evidence and a non-trivial title relationship below the automatic
    matching threshold.
    """
    same_media_date: list[tuple[float, float, Candidate]] = []
    for source_type in REFERENCE_SOURCE_ORDER:
        for candidate in pools.get(source_type, []):
            score = candidate_score(article, candidate)
            if score >= review_threshold:
                continue
            title_score = max(text_similarity(title, candidate.title) for title in article.match_titles)
            same_date = bool(
                article.date
                and candidate.date
                and normalize_key(article.date) == normalize_key(candidate.date)
            )
            same_media = media_similarity(article.media, candidate.media) >= 0.85
            if same_date and same_media:
                same_media_date.append((score, title_score, candidate))
    conflicts: list[tuple[float, Candidate]] = []
    for score, title_score, candidate in same_media_date:
        if title_score >= 0.2:
            conflicts.append((score, candidate))
    return sorted(conflicts, key=lambda pair: pair[0], reverse=True)


def unique_rewritten_regular_before_copied_workfile(
    article: Article,
    regular_candidates: list[Candidate],
    worker_candidates: list[Candidate],
    matching: dict[str, float],
) -> tuple[float, Candidate] | None:
    """Recover a uniquely identifiable regular item before a copied workfile.

    Aggregate and morning auxiliary files are later copy/import stages, so an
    exact title in one of them is not proof that the article first entered there.
    A heavily rewritten regular-history title can still establish the earlier
    inflow when it is the only regular item from the same media and date and
    retains a meaningful title relationship.  Individual drafts remain direct
    provenance and deliberately disable this inference.
    """
    automatic_initial_draft = ranked_origin_matches(
        article,
        [
            candidate
            for candidate in worker_candidates
            if clean_text(candidate.extra.get("source_kind")) in INITIAL_DRAFT_SOURCE_KINDS
        ],
        matching["auto_threshold"],
    )
    if automatic_initial_draft:
        return None

    automatic_copied_workfile = ranked_origin_matches(
        article,
        [
            candidate
            for candidate in worker_candidates
            if clean_text(candidate.extra.get("source_kind")) in COPIED_WORKFILE_SOURCE_KINDS
        ],
        matching["auto_threshold"],
    )
    if not automatic_copied_workfile:
        return None

    if not article.date:
        exact_title = {
            normalize_key(title) for title in article.match_titles if normalize_key(title)
        }
        exact_regular = [
            candidate
            for candidate in regular_candidates
            if normalize_key(candidate.title) in exact_title
        ]
        if len(exact_regular) == 1:
            return candidate_score(article, exact_regular[0]), exact_regular[0]
        ranked_undated = ranked_matches(article, regular_candidates)
        if (
            ranked_undated
            and ranked_undated[0][0] >= matching["auto_threshold"]
            and (
                len(ranked_undated) == 1
                or ranked_undated[0][0] - ranked_undated[1][0]
                >= matching["ambiguity_margin"]
            )
        ):
            return ranked_undated[0]

    same_date = [
        candidate
        for candidate in regular_candidates
        if article.date
        and candidate.date
        and normalize_key(article.date) == normalize_key(candidate.date)
    ]
    same_media_date = [
        candidate
        for candidate in same_date
        if media_similarity(article.media, candidate.media) >= 0.85
    ]
    rewrite_threshold = matching["review_threshold"] * (2.0 / 3.0)

    # The final report can use a translated or abbreviated outlet spelling while
    # regular history preserves the source-language name.  Do not maintain an
    # outlet alias table: accept the top same-date title only when it is both
    # meaningful and clearly separated from every competing regular item.  An
    # exact morning auxiliary remains direct article-level provenance when no
    # same-media regular candidate corroborates the translated outlet identity.
    if same_media_date:
        ranked_regular = ranked_matches(article, same_media_date)
    else:
        if any(
            clean_text(candidate.extra.get("source_kind")) == "morning_auxiliary"
            for _, candidate in automatic_copied_workfile
        ):
            return None
        ranked_regular = ranked_matches(article, same_date)
    if not ranked_regular:
        return None
    score, candidate = ranked_regular[0]
    if score < rewrite_threshold:
        return None
    if (
        len(ranked_regular) > 1
        and score - ranked_regular[1][0] < matching["ambiguity_margin"]
    ):
        return None
    return score, candidate


def unique_rewritten_japan_reference(
    article: Article,
    japan_candidates: list[Candidate],
    matching: dict[str, float],
) -> tuple[float, Candidate] | None:
    """Select one strongly evidenced Japan rewrite without a manual confirmation.

    Exact Japan/regular duplicates keep the normal regular-first rule.  This
    narrow case covers a substantially rewritten Japan title: there must be one
    and only one same-media candidate within one day, and its score must remain
    meaningful while still below the ordinary review threshold.
    """
    same_media_near_date: list[Candidate] = []
    article_date = parse_date(article.date)
    for candidate in japan_candidates:
        if media_similarity(article.media, candidate.media) < 0.85:
            continue
        candidate_date = parse_date(candidate.date)
        if (
            article_date
            and candidate_date
            and abs((article_date - candidate_date).days) > 1
        ):
            continue
        same_media_near_date.append(candidate)
    if len(same_media_near_date) != 1:
        return None
    candidate = same_media_near_date[0]
    score = candidate_score(article, candidate)
    rewrite_threshold = matching["review_threshold"] * 0.6
    if score < rewrite_threshold or score >= matching["review_threshold"]:
        return None
    return score, candidate


def choose_origin(
    article: Article,
    pools: dict[str, list[Candidate]],
    matching: dict[str, float],
    origin_policy: dict[str, Any] | None = None,
) -> tuple[Candidate | None, float, list[str], dict[str, float]]:
    # Kept in the signature so older run_context files remain readable.  The
    # comparison order itself is fixed by the human workflow and cannot be
    # overridden by execution-specific source_order/priority values.
    _ = origin_policy
    reasons: list[str] = []
    best_scores: dict[str, float] = {}
    chosen: Candidate | None = None
    chosen_score = 0.0
    chosen_stage = ""
    chosen_ranked: list[tuple[float, Candidate]] = []
    ranked_by_source: dict[str, list[tuple[float, Candidate]]] = {}
    for source_type, candidates in pools.items():
        origin_candidates = (
            reference_candidates_for_article(article, candidates)
            if source_type in REFERENCE_SOURCE_ORDER
            else candidates
        )
        score_ranked = ranked_matches(article, origin_candidates)
        ranked = ranked_origin_matches(article, origin_candidates, matching["review_threshold"])
        ranked_by_source[source_type] = ranked
        best_scores[source_type] = round(score_ranked[0][0], 4) if score_ranked else 0.0

    workers = pools.get("worker", [])
    workers_by_stage = {
        stage: [candidate for candidate in workers if clean_text(candidate.extra.get("comparison_stage")) == stage]
        for stage in FIXED_COMPARISON_ORDER[1:]
    }
    ranked_by_stage = {
        stage: ranked_origin_matches(article, candidates, matching["review_threshold"])
        for stage, candidates in workers_by_stage.items()
    }
    for stage, candidates in workers_by_stage.items():
        score_ranked = ranked_matches(article, candidates)
        best_scores[stage] = round(score_ranked[0][0], 4) if score_ranked else 0.0

    initial_drafts = [
        candidate
        for candidate in workers
        if clean_text(candidate.extra.get("source_kind")) in INITIAL_DRAFT_SOURCE_KINDS
    ]
    ranked_initial_drafts = ranked_origin_matches(
        article,
        initial_drafts,
        matching["review_threshold"],
    )
    rewritten_regular = unique_rewritten_regular_before_copied_workfile(
        article,
        pools.get("regular", []),
        workers,
        matching,
    )
    rewritten_japan = unique_rewritten_japan_reference(
        article,
        pools.get("japan", []),
        matching,
    )
    rewritten_trend = unique_rewritten_trend_duplicate(
        article,
        pools.get("regular", []),
        workers,
        matching,
    )
    clustered_regular = explicit_similar_cluster_regular(
        article,
        pools.get("regular", []),
        workers,
        matching,
    )
    rewritten_reference_selected = False
    rewritten_trend_selected = False

    # Reference history is first in the workflow, but an exact current draft can
    # still establish provenance when the reference title is only a different
    # story variant. A regular title explicitly retained as a similar row covers
    # the whole variant cluster and keeps reference precedence.
    reference_choice: tuple[float, Candidate] | None = None
    for source_type in REFERENCE_SOURCE_ORDER:
        ranked = ranked_by_source.get(source_type, [])
        if ranked:
            reference_choice = ranked[0]
            break
    regular_variant_cluster = bool(
        clustered_regular is not None
        and not article.similar
        and (
            reference_choice is None
            or reference_choice[1] is clustered_regular[1]
        )
    )
    if ranked_initial_drafts:
        initial_score, initial_candidate = ranked_initial_drafts[0]
        reference_score = (
            reference_choice[0]
            if reference_choice
            else max(
                (best_scores.get(source_type, 0.0) for source_type in REFERENCE_SOURCE_ORDER),
                default=0.0,
            )
        )
        reference_candidate = reference_choice[1] if reference_choice else None
        direct_identity = candidate_identity_matches(article, initial_candidate)
        reference_identity = bool(
            reference_candidate
            and candidate_identity_matches(article, reference_candidate)
        )
        draft_preferred = bool(
            (
                reference_choice is None
                and initial_score - reference_score >= matching["ambiguity_margin"]
            )
            or (
                reference_choice is not None
                and reference_score < matching["auto_threshold"]
                and initial_score >= matching["auto_threshold"]
                and initial_score - reference_score >= matching["ambiguity_margin"]
            )
            or (
                reference_candidate is not None
                and reference_candidate.source_type == "regular"
                and direct_identity
                and not reference_identity
                and not regular_variant_cluster
            )
        )
        if draft_preferred:
            chosen_score = initial_score
            chosen = initial_candidate
            chosen_ranked = [(chosen_score, chosen), *ranked_initial_drafts[1:]]
            chosen_stage = clean_text(initial_candidate.extra.get("comparison_stage")) or "afternoon"

    if clustered_regular is not None and reference_choice is None:
        chosen_score, chosen = clustered_regular
        chosen_ranked = [clustered_regular]
        chosen_stage = "reference"

    if (
        chosen is None
        and rewritten_trend is not None
        and not regular_variant_cluster
        and not (
            ranked_by_source.get("japan")
            and ranked_by_source["japan"][0][0] >= matching["auto_threshold"]
        )
    ):
        chosen_score, rewritten_candidate = rewritten_trend
        chosen = rewritten_candidate
        chosen_ranked = [(chosen_score, chosen)]
        chosen_stage = clean_text(chosen.extra.get("comparison_stage")) or "afternoon"
        rewritten_trend_selected = True

    if rewritten_japan is not None:
        chosen_score, chosen = rewritten_japan
        chosen_ranked = [rewritten_japan]
        chosen_stage = "reference"
        rewritten_reference_selected = True

    # Stage 1: Google Sheets regular history and the supplied Japan report.
    # If an article is in both, the long-standing business rule selects regular
    # as the origin while Japan membership is still recorded separately as O.
    if chosen is None:
        for source_type in REFERENCE_SOURCE_ORDER:
            ranked = ranked_by_source.get(source_type, [])
            if ranked:
                chosen_ranked = ranked
                chosen_score, chosen = ranked[0]
                chosen_stage = "reference"
                break

    if chosen is None and rewritten_regular is not None:
        chosen_score, chosen = rewritten_regular
        chosen_ranked = [rewritten_regular]
        chosen_stage = "reference"
        rewritten_reference_selected = True

    # Stage 2 and 3 are determined from the explicit input folder, not the
    # filename, source_kind, run_context priority, or a person's identity.
    if chosen is None:
        for stage in FIXED_COMPARISON_ORDER[1:]:
            ranked = ranked_by_stage[stage]
            if ranked:
                chosen_ranked = ranked
                chosen_score, chosen = ranked[0]
                chosen_stage = stage
                break

    if chosen is None:
        reasons.append("유입 경로를 확인하지 못함")
        return None, 0.0, reasons, best_scores
    chosen.extra["comparison_stage"] = chosen_stage
    clear_initial_draft = bool(
        clean_text(chosen.extra.get("source_kind")) in INITIAL_DRAFT_SOURCE_KINDS
        and chosen_score >= matching["review_threshold"]
        and chosen_score - max(
            (best_scores.get(source_type, 0.0) for source_type in REFERENCE_SOURCE_ORDER),
            default=0.0,
        ) >= matching["ambiguity_margin"]
    )
    if (
        chosen_score < matching["auto_threshold"]
        and not clear_initial_draft
        and not rewritten_reference_selected
        and not rewritten_trend_selected
    ):
        reasons.append(f"낮은 매칭 점수 {chosen_score:.3f}")
    if not chosen.extra.get("profile_complete", False):
        reasons.append("유입 파일의 작업자·역할 근거 불완전")
    if (
        chosen_stage != "reference"
        and clean_text(chosen.extra.get("source_kind")) not in INITIAL_DRAFT_SOURCE_KINDS
    ):
        reference_conflicts = plausible_reference_conflicts(
            article,
            pools,
            matching["review_threshold"],
        )
        if reference_conflicts:
            conflict_score, conflict = reference_conflicts[0]
            reasons.append(
                "정기·일본동향 저점수 후보 직접 대조 필요: "
                f"{conflict.source_type} {Path(conflict.source_file).name or '현재 원본'} "
                f"(점수 {conflict_score:.3f})"
            )
    chosen_provenance = (chosen.workgroup, chosen.owner, chosen.worker)
    chosen_priority = int(chosen.extra.get("priority", 0))
    same_stage_ranked = sorted(
        chosen_ranked,
        key=lambda pair: (pair[0], int(pair[1].extra.get("priority", 0))),
        reverse=True,
    )
    alternative = next(
        (
            pair
            for pair in same_stage_ranked
            if pair[1] is not chosen
            if (pair[1].workgroup, pair[1].owner, pair[1].worker) != chosen_provenance
            and int(pair[1].extra.get("priority", 0)) >= chosen_priority
        ),
        None,
    )
    if alternative and chosen_score - alternative[0] < matching["ambiguity_margin"]:
        reasons.append(
            f"후보 점수 차이 작음 {chosen_score:.3f}/{alternative[0]:.3f}"
        )
    return chosen, chosen_score, reasons, best_scores


def confirmed_origin(
    article: Article,
    pools: dict[str, list[Candidate]],
    confirmation: dict[str, Any] | None,
) -> tuple[Candidate | None, float, list[str]]:
    if not confirmation:
        return None, 0.0, []
    evidence = [clean_text(item) for item in confirmation.get("evidence", []) if clean_text(item)]
    source_type = clean_text(confirmation.get("source_type"))
    if not evidence or source_type not in pools:
        return None, 0.0, ["기사별 유입 경로 확인값의 근거 또는 경로가 올바르지 않음"]
    candidates = list(pools[source_type])
    source_file = clean_text(confirmation.get("source_file"))
    if source_file:
        expected_path = Path(source_file).resolve()
        candidates = [candidate for candidate in candidates if Path(candidate.source_file).resolve() == expected_path]
    source_title = clean_text(confirmation.get("source_title"))
    if source_title:
        exact = [candidate for candidate in candidates if normalize_key(candidate.title) == normalize_key(source_title)]
        candidates = exact or candidates
    ranked = ranked_matches(article, candidates)
    if not ranked:
        return None, 0.0, ["기사별 유입 경로 확인값에 해당하는 현재 후보를 찾지 못함"]
    return ranked[0][1], ranked[0][0], []


def confirmed_article_roles(
    confirmation: dict[str, Any] | None,
    valid_schedule_refs: dict[str, str] | set[str] | None = None,
    require_schedule: bool = False,
) -> tuple[dict[str, Any] | None, list[str]]:
    if not confirmation:
        return None, []
    evidence = [clean_text(item) for item in confirmation.get("evidence", []) if clean_text(item)]
    profile = {
        "workgroup": clean_text(confirmation.get("workgroup")),
        "owner": clean_text(confirmation.get("owner")),
        "worker": clean_text(confirmation.get("worker")),
        "evidence": evidence,
        "schedule_refs": confirmation.get("schedule_refs", []),
    }
    if not profile_is_complete(
        profile,
        valid_schedule_refs=valid_schedule_refs,
        require_schedule=require_schedule,
    ):
        return None, ["기사별 역할 확인값의 역할·근무표 근거가 불완전함"]
    reference_file = clean_text(confirmation.get("reference_file"))
    reference_sha256 = clean_text(confirmation.get("reference_sha256"))
    if not reference_file or not reference_sha256:
        return None, ["기사별 역할 확인값의 기준 파일 또는 SHA-256이 없음"]
    reference_path = Path(reference_file)
    if not reference_path.exists() or not reference_path.is_file():
        return None, ["기사별 역할 확인값의 기준 파일을 찾지 못함"]
    if sha256_file(reference_path) != reference_sha256:
        return None, ["기사별 역할 확인값의 기준 파일이 확인 이후 변경됨"]
    return profile, []


def article_context_identity_score(item: dict[str, Any], article: Article) -> int:
    """Score an article-scoped context item without relying on a unique order."""
    article_titles = {
        normalize_key(article.canonical_title),
        normalize_key(article.body_title),
    } - {""}
    item_titles = {
        normalize_key(item.get("article_title")),
        normalize_key(item.get("reference_title")),
    } - {""}
    article_media = normalize_key(article.media)
    item_media = {
        normalize_key(item.get("article_media")),
        normalize_key(item.get("reference_media")),
    } - {""}
    score = 0
    if article_titles and item_titles and article_titles & item_titles:
        score += 4
    if article_media and item_media and article_media in item_media:
        score += 2
    return score


def article_scoped_confirmation(
    items: list[dict[str, Any]],
    article: Article,
    order: int | None = None,
) -> dict[str, Any] | None:
    """Select a confirmation by order plus current/reference article identity."""
    scoped_order = article.order if order is None else order
    candidates = [item for item in items if item.get("order") == scoped_order]
    if len(candidates) <= 1:
        return candidates[0] if candidates else None
    ranked = sorted(
        (
            (article_context_identity_score(item, article), index, item)
            for index, item in enumerate(candidates)
        ),
        key=lambda value: (value[0], -value[1]),
        reverse=True,
    )
    if ranked[0][0] > 0 and (len(ranked) == 1 or ranked[0][0] > ranked[1][0]):
        return ranked[0][2]
    if not any(
        clean_text(item.get(field))
        for item in candidates
        for field in ("article_title", "reference_title", "article_media", "reference_media")
    ):
        # Backward compatibility for older contexts whose orders were unique.
        return candidates[0]
    return None


def apply_confirmed_article_roles(
    origin: Candidate | None,
    confirmation: dict[str, Any] | None,
    valid_schedule_refs: dict[str, str] | set[str] | None = None,
    require_schedule: bool = False,
) -> tuple[Candidate | None, list[str], bool]:
    """Apply a hash-bound role confirmation after automatic origin rewrites.

    Automatic provenance rules can replace the selected candidate (for example,
    the narrow regular carry-over and late-morning aggregate classifications).
    A same-run authoritative role confirmation must therefore be the final role
    transformation, while retaining the automatically selected source candidate.
    """
    role_profile, reasons = confirmed_article_roles(
        confirmation,
        valid_schedule_refs,
        require_schedule,
    )
    if not role_profile:
        return origin, reasons, False
    if not origin:
        return origin, [*reasons, "기사별 역할 확인값을 적용할 유입 후보가 없음"], False
    confirmed_extra = {
        **origin.extra,
        "profile_complete": True,
        "role_confirmed_from_reference": True,
    }
    return (
        replace(
            origin,
            workgroup=clean_text(role_profile.get("workgroup")),
            owner=clean_text(role_profile.get("owner")),
            worker=clean_text(role_profile.get("worker")),
            extra=confirmed_extra,
        ),
        reasons,
        True,
    )


def confirmed_article_additions(
    run_context: dict[str, Any],
    pools: dict[str, list[Candidate]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Resolve rows that only a hashed, same-run authoritative result can establish."""
    resolved: list[dict[str, Any]] = []
    warnings: list[str] = []
    for position, confirmation in enumerate(run_context.get("article_additions", []), start=1):
        evidence = [clean_text(item) for item in confirmation.get("evidence", []) if clean_text(item)]
        reference_file = clean_text(confirmation.get("reference_file"))
        reference_sha256 = clean_text(confirmation.get("reference_sha256"))
        kind = clean_text(confirmation.get("kind"))
        source_type = clean_text(confirmation.get("source_type"))
        source_title = clean_text(confirmation.get("source_title"))
        label = f"기사 추가 확인값 {position}"
        if not evidence or kind not in {"similar", "omitted"} or source_type not in pools or not source_title:
            warnings.append(f"{label}: 종류·유입 경로·원본 제목·근거가 불완전함")
            continue
        if not reference_file or not reference_sha256:
            warnings.append(f"{label}: 기준 파일 또는 SHA-256이 없음")
            continue
        reference_path = Path(reference_file)
        if not reference_path.exists() or not reference_path.is_file():
            warnings.append(f"{label}: 기준 파일을 찾지 못함")
            continue
        if sha256_file(reference_path) != reference_sha256:
            warnings.append(f"{label}: 기준 파일이 확인 이후 변경됨")
            continue
        candidates = [
            candidate
            for candidate in pools[source_type]
            if normalize_key(candidate.title) == normalize_key(source_title)
        ]
        source_file = clean_text(confirmation.get("source_file"))
        if source_file:
            expected_path = Path(source_file).resolve()
            candidates = [
                candidate
                for candidate in candidates
                if Path(candidate.source_file).resolve() == expected_path
            ]
        if len(candidates) != 1:
            warnings.append(f"{label}: 현재 입력의 원본 기사를 하나로 특정하지 못함")
            continue
        candidate = candidates[0]
        category = clean_text(confirmation.get("category")) or clean_text(candidate.extra.get("category"))
        if not category:
            warnings.append(f"{label}: 기준표의 카테고리가 없음")
            continue
        after_order = confirmation.get("after_order")
        if kind == "similar" and (not isinstance(after_order, int) or after_order < 1):
            warnings.append(f"{label}: 유사보도 삽입 위치가 없음")
            continue
        article = Article(
            source_file=candidate.source_file,
            order=0,
            category=category,
            media=clean_text(confirmation.get("media")) or candidate.media,
            date=clean_text(confirmation.get("date")) or candidate.date,
            body_title=clean_text(confirmation.get("canonical_title")) or candidate.title,
            canonical_title=clean_text(confirmation.get("canonical_title")) or candidate.title,
            similar=kind == "similar",
            body_present=False,
            raw_heading="",
        )
        resolved.append(
            {
                "kind": kind,
                "after_order": after_order,
                "article": article,
                "candidate": candidate,
                "evidence": evidence,
                "reference_file": str(reference_path.resolve()),
            }
        )
    return resolved, warnings


def confirmed_japan_candidate(
    candidates: list[Candidate],
    confirmation: dict[str, Any] | None,
) -> tuple[Candidate | None, list[str]]:
    if not confirmation:
        return None, []
    evidence = [clean_text(item) for item in confirmation.get("evidence", []) if clean_text(item)]
    source_title = clean_text(confirmation.get("source_title"))
    if not evidence or not source_title:
        return None, ["일본동향 기사 확인값의 제목 또는 근거가 올바르지 않음"]
    selected = list(candidates)
    source_file = clean_text(confirmation.get("source_file"))
    if source_file:
        expected_path = Path(source_file).resolve()
        selected = [candidate for candidate in selected if Path(candidate.source_file).resolve() == expected_path]
    selected = [
        candidate
        for candidate in selected
        if normalize_key(candidate.title) == normalize_key(source_title)
    ]
    if len(selected) != 1:
        return None, ["일본동향 기사 확인값에 해당하는 현재 원본 기사를 하나로 특정하지 못함"]
    return selected[0], []


def confirmed_japan_origin(
    origin: Candidate | None,
    article: Article,
    candidates: list[Candidate],
    confirmation: dict[str, Any] | None,
) -> tuple[Candidate | None, float, list[str]]:
    """Prefer a directly confirmed Japan original over a guessed regular rewrite."""
    if not confirmation or confirmation.get("included") is not True:
        return origin, 0.0, []
    confirmed, reasons = confirmed_japan_candidate(candidates, confirmation)
    if confirmed is None:
        return origin, 0.0, reasons
    if origin is not None and origin.source_type != "regular":
        return origin, 0.0, reasons
    return confirmed, candidate_score(article, confirmed), reasons


def confirmed_japan_exclusion(
    confirmation: dict[str, Any] | None,
) -> tuple[bool, list[str]]:
    """Validate a same-run authoritative confirmation that Japan membership is blank."""
    if not confirmation or confirmation.get("included") is not False:
        return False, []
    evidence = [clean_text(item) for item in confirmation.get("evidence", []) if clean_text(item)]
    reference_file = clean_text(confirmation.get("reference_file"))
    reference_sha256 = clean_text(confirmation.get("reference_sha256"))
    if not evidence or not reference_file or not reference_sha256:
        return False, ["일본동향 제외 확인값의 기준 파일·SHA-256·근거가 불완전함"]
    reference_path = Path(reference_file)
    if not reference_path.is_file():
        return False, ["일본동향 제외 확인값의 기준 파일을 찾지 못함"]
    if sha256_file(reference_path) != reference_sha256:
        return False, ["일본동향 제외 확인값의 기준 파일이 확인 이후 변경됨"]
    return True, []


def japan_membership(
    article: Article,
    candidates: list[Candidate],
    threshold: float,
    confirmation: dict[str, Any] | None = None,
) -> tuple[bool, float, list[str]]:
    ranked = ranked_matches(article, candidates)
    automatic_score = ranked[0][0] if ranked else 0.0
    excluded, exclusion_reasons = confirmed_japan_exclusion(confirmation)
    if excluded:
        return False, automatic_score, []
    if automatic_score >= threshold:
        return True, automatic_score, exclusion_reasons
    confirmed, reasons = confirmed_japan_candidate(candidates, confirmation)
    if confirmed:
        return True, candidate_score(article, confirmed), []
    review_candidates: list[tuple[float, Candidate]] = []
    for candidate in candidates:
        if media_similarity(article.media, candidate.media) < 0.85:
            continue
        article_date = parse_date(article.date)
        candidate_date = parse_date(candidate.date)
        near_date = (
            not article_date
            or not candidate_date
            or abs((article_date - candidate_date).days) <= 1
        )
        if near_date:
            review_candidates.append((candidate_score(article, candidate), candidate))
    if len(review_candidates) == 1:
        score, candidate = review_candidates[0]
        if score >= threshold * 0.3:
            reasons.append(
                "일본동향 동일 매체·인접 날짜 저점수 후보 직접 대조 필요: "
                f"{candidate.title} (점수 {score:.3f})"
            )
    return False, automatic_score, [*exclusion_reasons, *reasons]


def omitted_worker_candidates(
    final_articles: list[Article],
    candidates: list[Candidate],
    matching: dict[str, float],
) -> list[Candidate]:
    morning_aggregates = [
        candidate
        for candidate in candidates
        if clean_text(candidate.extra.get("source_kind")) == "morning_aggregate"
    ]

    def carried_into_morning(candidate: Candidate) -> bool:
        if clean_text(candidate.extra.get("source_kind")) != "afternoon_aggregate":
            return False
        return bool(
            ranked_origin_matches(
                candidate_as_article(candidate),
                morning_aggregates,
                matching["review_threshold"],
            )
        )

    eligible = [
        candidate
        for candidate in candidates
        if candidate.extra.get("include_unmatched", False)
        or carried_into_morning(candidate)
    ]

    def unique_final_rewrite(candidate: Candidate) -> bool:
        same_media_date = [
            article
            for article in final_articles
            if media_similarity(article.media, candidate.media) >= 0.85
            and (
                not article.date
                or not candidate.date
                or parse_date(article.date) == parse_date(candidate.date)
            )
        ]
        if len(same_media_date) != 1:
            return False
        return candidate_score(same_media_date[0], candidate) >= 0.2

    unmatched = [
        candidate
        for candidate in eligible
        if (
            not final_articles
            or (
                max(candidate_score(article, candidate) for article in final_articles)
                < matching["review_threshold"]
                and not unique_final_rewrite(candidate)
            )
        )
    ]
    stage_order = {"afternoon": 0, "morning": 1}
    unmatched.sort(
        key=lambda item: (
            stage_order.get(clean_text(item.extra.get("comparison_stage")), 2),
            -int(item.extra.get("priority", 0)),
        )
    )
    unique: list[Candidate] = []
    for candidate in unmatched:
        duplicate = any(
            text_similarity(candidate.title, existing.title) >= 0.9
            and (
                not candidate.media
                or not existing.media
                or media_similarity(candidate.media, existing.media) >= 0.8
            )
            for existing in unique
        )
        if not duplicate:
            if clean_text(candidate.extra.get("source_kind")) == "afternoon_aggregate":
                candidate = replace(
                    candidate,
                    workgroup="1조",
                    owner="오후/총괄",
                    extra={
                        **candidate.extra,
                        "source_kind": "afternoon_aggregate_omitted",
                    },
                )
            unique.append(candidate)
    return unique


def closest_current_category(
    value: str,
    final_articles: list[Article],
    threshold: float,
    candidate: Candidate | None = None,
    ambiguity_margin: float = 0.035,
) -> tuple[str, bool]:
    raw = clean_text(value)
    categories = list(dict.fromkeys(article.category for article in final_articles if article.category))
    if not raw or not categories:
        return raw, False
    exact = next((category for category in categories if normalize_key(category) == normalize_key(raw)), None)
    if exact:
        return exact, True
    contextual_scores: dict[str, float] = {}
    if candidate is not None:
        contextual_scores = {
            category: max(
                (
                    candidate_score(article, candidate)
                    for article in final_articles
                    if article.category == category
                ),
                default=0.0,
            )
            for category in categories
        }
    ranked = sorted(
        (
            (
                0.55 * text_similarity(raw, category)
                + 0.45 * contextual_scores.get(category, 0.0),
                text_similarity(raw, category),
                contextual_scores.get(category, 0.0),
                category,
            )
            for category in categories
        ),
        reverse=True,
    )
    if ranked:
        combined, lexical, contextual, category = ranked[0]
        runner_up = ranked[1][0] if len(ranked) > 1 else 0.0
        if lexical >= threshold or (
            candidate is not None
            and contextual >= 0.25
            and combined - runner_up >= ambiguity_margin
        ):
            return category, True
    return raw, False


def omitted_candidate_category(
    candidate: Candidate,
    final_articles: list[Article],
    matching: dict[str, float],
) -> tuple[str, bool]:
    """Map a reliable current label, otherwise preserve the explicit source label."""
    raw = clean_text(candidate.extra.get("category"))
    category, mapped = closest_current_category(
        raw,
        final_articles,
        matching["review_threshold"],
        candidate,
        matching["ambiguity_margin"],
    )
    if raw and not mapped:
        return raw, True
    return category, mapped


def automatic_similar_additions(
    final_articles: list[Article],
    worker_candidates: list[Candidate],
    pools: dict[str, list[Candidate]],
    matching: dict[str, float],
) -> list[dict[str, Any]]:
    """Recover explicit similar rows present in the latest morning aggregate."""
    aggregates = [
        candidate
        for candidate in worker_candidates
        if clean_text(candidate.extra.get("source_kind")) == "morning_aggregate"
    ]
    if not aggregates:
        return []
    latest_revision = max(workfile_revision_rank(candidate.source_file) for candidate in aggregates)
    latest = [
        candidate
        for candidate in aggregates
        if workfile_revision_rank(candidate.source_file) == latest_revision
    ]
    by_file: dict[str, list[Candidate]] = {}
    for candidate in latest:
        by_file.setdefault(candidate.source_file, []).append(candidate)
    for sequence in by_file.values():
        sequence.sort(key=lambda item: int(item.extra.get("article_order", 0)))

    additions: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for candidate in latest:
        if not candidate.extra.get("similar", False):
            continue
        key = (normalize_key(candidate.media), normalize_key(candidate.date), normalize_key(candidate.title))
        if key in seen:
            continue
        seen.add(key)
        if any(candidate_score(article, candidate) >= 0.9 for article in final_articles):
            continue
        sequence = by_file[candidate.source_file]
        position = sequence.index(candidate)
        anchor_candidate = next(
            (
                sequence[index]
                for index in range(position - 1, -1, -1)
                if not sequence[index].extra.get("similar", False)
            ),
            None,
        )
        if anchor_candidate is None:
            continue
        anchor_matches = sorted(
            (
                (candidate_score(article, anchor_candidate), article)
                for article in final_articles
            ),
            key=lambda item: item[0],
            reverse=True,
        )
        if not anchor_matches or anchor_matches[0][0] < matching["review_threshold"]:
            continue
        _, anchor_article = anchor_matches[0]
        article = candidate_as_article(candidate)
        article.category = clean_text(candidate.extra.get("category")) or anchor_article.category
        article.similar = True
        article.starred = bool(candidate.extra.get("starred", False))
        origin, score, reasons, best_scores = choose_origin(article, pools, matching)
        if origin is None:
            origin = candidate
        additions.append(
            {
                "kind": "similar",
                "after_order": anchor_article.order,
                "article": article,
                "candidate": origin,
                "source_candidate": candidate,
                "score": score,
                "best_scores": best_scores,
                "origin_reasons": reasons,
                "evidence": [
                    "최신 오전 취합본에서 대표 기사 다음의 명시적 유사보도 행으로 확인"
                ],
                "automatic": True,
            }
        )
    return additions


def legacy_late_morning_aggregate_origin(
    article: Article,
    origin: Candidate | None,
    pools: dict[str, list[Candidate]],
    matching: dict[str, float],
    target_date: dt.date,
    enabled: bool,
) -> Candidate | None:
    """Preserve the legacy late-shift label for a narrow aggregate-only item.

    Older source-history snapshots do not carry the current ``작업날짜`` and
    ``작업 조`` fields.  In that schema, a target-date article that appears for
    the first time only in the latest morning aggregate used the established
    ``1조/오후/총괄`` label.  Current operational snapshots keep the actual
    morning aggregate role instead.  The schema is current-run evidence, so
    this compatibility path does not depend on a date, person, or outlet.
    """
    if not enabled:
        return origin
    if origin is None or clean_text(origin.extra.get("source_kind")) != "morning_aggregate":
        return origin
    article_date = parse_date(article.date)
    if not article_date or (
        article_date.month,
        article_date.day,
    ) != (target_date.month, target_date.day):
        return origin
    morning_aggregates = [
        candidate
        for candidate in pools.get("worker", [])
        if clean_text(candidate.extra.get("source_kind")) == "morning_aggregate"
    ]
    if not morning_aggregates:
        return origin
    latest_revision = max(
        workfile_revision_rank(candidate.source_file)
        for candidate in morning_aggregates
    )
    if workfile_revision_rank(origin.source_file) != latest_revision:
        return origin
    earlier_sources = [
        candidate
        for source_type, candidates in pools.items()
        for candidate in candidates
        if not (
            source_type == "worker"
            and candidate.source_file == origin.source_file
            and clean_text(candidate.extra.get("source_kind")) == "morning_aggregate"
        )
    ]
    if ranked_origin_matches(
        article,
        earlier_sources,
        matching["review_threshold"],
    ):
        return origin
    return replace(
        origin,
        workgroup="1조",
        owner="오후/총괄",
        extra={**origin.extra, "source_kind": "late_morning_aggregate"},
    )


def regular_reintroduced_in_morning_origin(
    article: Article,
    origin: Candidate | None,
    pools: dict[str, list[Candidate]],
    matching: dict[str, float],
    target_date: dt.date,
) -> Candidate | None:
    """Keep a narrow regular/morning hybrid for a one-day carry-over item.

    This does not change ordinary regular attribution. It applies only when the
    selected regular row was worked on the target date but reported exactly one
    day earlier, the article is absent from every afternoon input, and an
    automatic match first appears in a complete morning aggregate with no
    competing morning individual/auxiliary draft.
    """
    if origin is None or origin.source_type != "regular":
        return origin
    if not origin.extra.get("one_day_work_date_carryover"):
        return origin
    if parse_date(origin.extra.get("작업날짜")) != target_date:
        return origin
    if parse_date(origin.extra.get("보도일")) != target_date - dt.timedelta(days=1):
        return origin

    workers = pools.get("worker", [])
    afternoon = [
        candidate
        for candidate in workers
        if clean_text(candidate.extra.get("comparison_stage")) == "afternoon"
    ]
    if ranked_origin_matches(article, afternoon, matching["review_threshold"]):
        return origin

    morning_non_aggregates = [
        candidate
        for candidate in workers
        if clean_text(candidate.extra.get("comparison_stage")) == "morning"
        and clean_text(candidate.extra.get("source_kind")) != "morning_aggregate"
    ]
    if ranked_origin_matches(
        article,
        morning_non_aggregates,
        matching["review_threshold"],
    ):
        return origin

    morning_aggregates = [
        candidate
        for candidate in workers
        if clean_text(candidate.extra.get("comparison_stage")) == "morning"
        and clean_text(candidate.extra.get("source_kind")) == "morning_aggregate"
        and candidate.extra.get("profile_complete", False)
    ]
    automatic_matches = [
        (candidate_score(article, candidate), candidate)
        for candidate in morning_aggregates
        if candidate_score(article, candidate) >= matching["auto_threshold"]
    ]
    if not automatic_matches:
        return origin
    score, selected = min(
        automatic_matches,
        key=lambda pair: (
            workfile_revision_rank(pair[1].source_file),
            -pair[0],
            int(pair[1].extra.get("article_order", 0)),
        ),
    )
    evidence = [
        *origin.extra.get("profile_evidence", []),
        *selected.extra.get("profile_evidence", []),
        (
            "정기 전일 보도분이 오후 입력에는 없고 오전 총괄본에서 최초 재반영됨: "
            f"{Path(selected.source_file).name} (일치 {score:.3f})"
        ),
    ]
    return replace(
        origin,
        owner=selected.owner,
        worker=selected.worker,
        extra={
            **origin.extra,
            "actual_edit_stage": "morning",
            "actual_edit_file": selected.source_file,
            "actual_edit_source_kind": "morning_aggregate",
            "regular_reintroduced_in_morning": True,
            "profile_complete": True,
            "profile_evidence": list(
                dict.fromkeys(clean_text(item) for item in evidence if clean_text(item))
            ),
            "schedule_refs": list(selected.extra.get("schedule_refs", [])),
        },
    )


def reorder_front_only_results(
    articles: list[Article],
    rows: list[list[Any]],
    details: list[dict[str, Any]],
) -> tuple[list[list[Any]], list[dict[str, Any]]]:
    """Keep a same-file front-only packet together across a later insertion."""
    records = list(zip(articles, rows, details))
    stage_rank = {stage: index for index, stage in enumerate(FIXED_COMPARISON_ORDER)}
    index = 0
    while index + 2 < len(records):
        first, middle, third = records[index : index + 3]
        same_front_section = bool(
            not first[0].body_present
            and not middle[0].body_present
            and not third[0].body_present
            and first[0].category == middle[0].category == third[0].category
        )
        first_key = (
            clean_text(first[2].get("origin_source_kind")),
            clean_text(first[2].get("origin_file")),
        )
        third_key = (
            clean_text(third[2].get("origin_source_kind")),
            clean_text(third[2].get("origin_file")),
        )
        first_stage = clean_text(first[2].get("origin_comparison_stage"))
        middle_stage = clean_text(middle[2].get("origin_comparison_stage"))
        if (
            same_front_section
            and first_key == third_key
            and all(first_key)
            and first_key
            != (
                clean_text(middle[2].get("origin_source_kind")),
                clean_text(middle[2].get("origin_file")),
            )
            and stage_rank.get(first_stage, len(stage_rank))
            < stage_rank.get(middle_stage, len(stage_rank))
        ):
            records[index + 1], records[index + 2] = third, middle
            index += 3
            continue
        index += 1
    return [record[1] for record in records], [record[2] for record in records]


def result_row(
    article: Article,
    job_date: dt.date,
    origin: Candidate | None,
    japan_value: str,
    final_profile: dict[str, Any] | None,
    final_disposition: dict[str, Any] | None,
    reasons: list[str],
    in_final_report: bool = True,
) -> list[Any]:
    similar = article.similar
    _, final_owner, final_worker = profile_fields(final_profile)
    if similar or not in_final_report:
        final_owner = clean_text((final_disposition or {}).get("not_representative_owner"))
    note = "확인 필요: " + "; ".join(reasons) if reasons else ""
    effective_date = article.date or (origin.date if origin else "")
    return [
        f"{job_date.year}년 {job_date.month:02d}월",
        f"{job_date.month}월 {job_date.day}일",
        origin.workgroup if origin else "",
        origin.owner if origin else "",
        origin.worker if origin else "",
        final_owner,
        final_worker,
        article.category,
        display_media(article.media),
        effective_date or "없음",
        display_title(article.canonical_title or article.body_title),
        "O" if in_final_report and not similar else "X",
        "O" if in_final_report and similar else "X",
        japan_value,
        note,
    ]


def validate_result(
    final_articles: list[Article],
    rows: list[list[Any]],
    omitted_count: int = 0,
    match_details: list[dict[str, Any]] | None = None,
) -> list[str]:
    errors: list[str] = []
    if len(final_articles) + omitted_count != len(rows):
        errors.append(
            f"최종 기사 {len(final_articles)}건 + 미포함 {omitted_count}건과 결과 행 {len(rows)}건 불일치"
        )
    if any(len(row) != len(RESULT_HEADERS) for row in rows):
        errors.append("A:O 15열이 아닌 결과 행 존재")
    if any(not row[10] for row in rows):
        errors.append("제목이 빈 결과 행 존재")
    if len({article.order for article in final_articles}) != len(final_articles):
        errors.append("최종 기사 순번 중복")
    if match_details is not None:
        if len(match_details) != len(rows):
            errors.append("결과 행과 유입 근거 행 수 불일치")
        for row, detail in zip(rows, match_details):
            if detail.get("role_confirmed_from_reference"):
                continue
            semantic_errors = role_semantic_errors(
                clean_text(detail.get("origin")),
                clean_text(detail.get("origin_source_kind")),
                clean_text(row[2]),
                clean_text(row[3]),
                clean_text(detail.get("origin_actual_edit_source_kind")),
            )
            if semantic_errors:
                errors.append(
                    f"{clean_text(row[10]) or '제목 없음'} 역할 의미 불일치: "
                    + ", ".join(semantic_errors)
                )
    return errors


def run_fingerprint(paths: Iterable[Path]) -> str:
    records = []
    seen: set[str] = set()
    for path in paths:
        resolved = str(path.resolve())
        if resolved in seen or not path.is_file():
            continue
        seen.add(resolved)
        records.append({"path": resolved, "sha256": sha256_file(path)})
    encoded = json.dumps(sorted(records, key=lambda item: item["path"]), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def context_job_date(run_context: dict[str, Any]) -> tuple[dt.date | None, list[str]]:
    item = run_context.get("job_date") or {}
    if isinstance(item, str):
        return dt.date.fromisoformat(item), []
    value = clean_text(item.get("value"))
    evidence = [clean_text(entry) for entry in item.get("evidence", []) if clean_text(entry)]
    return (dt.date.fromisoformat(value), evidence) if value else (None, evidence)


def document_job_date(final_report: Path) -> tuple[dt.date | None, str]:
    try:
        paragraphs = extract_paragraphs(final_report)
    except Exception:
        paragraphs = []
    for paragraph in paragraphs[:30]:
        match = re.search(r"(?P<year>20\d{2})\s*[./년-]\s*(?P<month>\d{1,2})\s*[./월-]\s*(?P<day>\d{1,2})", paragraph)
        if match:
            value = dt.date(int(match.group("year")), int(match.group("month")), int(match.group("day")))
            return value, f"최종보고서 본문: {paragraph}"
    match = re.search(r"(?<!\d)(?P<year>\d{2})(?P<month>\d{2})(?P<day>\d{2})(?!\d)", final_report.stem)
    if match:
        value = dt.date(2000 + int(match.group("year")), int(match.group("month")), int(match.group("day")))
        return value, f"최종보고서 파일명: {final_report.name}"
    return None, ""


def resolve_japan_input(final_report: Path, explicit: str | Path | None = None) -> Path | None:
    if not explicit:
        return None
    selected = Path(explicit).resolve()
    if not selected.is_file():
        raise FileNotFoundError(f"일본언론동향 입력 파일 없음: {selected}")
    final_date, _ = document_job_date(final_report)
    selected_date, _ = document_job_date(selected)
    if final_date and selected_date and selected_date != final_date:
        raise ValueError(
            f"일본언론동향 작업일 {selected_date.isoformat()}이 최종보고서 작업일 {final_date.isoformat()}과 다릅니다."
        )
    return selected


def japan_input_is_resolved(
    path: Path | None,
    profile: dict[str, Any] | None,
) -> bool:
    """Return whether the current context explicitly records Japan input state."""
    profile = profile or {}
    status = clean_text(profile.get("status"))
    has_evidence = any(clean_text(item) for item in profile.get("evidence", []))
    expected_status = "present_checked" if path else "not_provided"
    return status == expected_status and has_evidence


def resolve_job_date(
    final_report: Path,
    run_context: dict[str, Any],
    explicit: str | None,
) -> tuple[dt.date, list[str]]:
    warnings: list[str] = []
    context_value, context_evidence = context_job_date(run_context)
    document_value, document_evidence = document_job_date(final_report)
    explicit_value = dt.date.fromisoformat(explicit) if explicit else None
    if context_value and not context_evidence:
        raise ValueError("run_context.json의 작업일에 판단 근거가 없습니다.")
    selected = context_value or document_value or explicit_value
    if not selected:
        raise ValueError("작업일을 판정할 근거가 없습니다. 최종보고서 또는 run_context.json을 확인하세요.")
    candidates = [value for value in (context_value, document_value, explicit_value) if value]
    if any(value != selected for value in candidates):
        warnings.append(
            "작업일 근거 상충: "
            + ", ".join(value.isoformat() for value in candidates)
            + f"; 실행 컨텍스트 판단 {selected.isoformat()} 사용"
        )
    if context_value:
        warnings.append("작업일 판단 근거: " + "; ".join(context_evidence))
    elif document_evidence:
        warnings.append("작업일 자동 추출 근거: " + document_evidence)
    return selected, warnings


def apply_article_overrides(
    articles: list[Article],
    run_context: dict[str, Any],
) -> list[str]:
    warnings: list[str] = []
    allowed = {"category", "media", "date", "canonical_title"}
    for override in run_context.get("article_overrides", []):
        evidence = [clean_text(item) for item in override.get("evidence", []) if clean_text(item)]
        order = override.get("order")
        field_name = clean_text(override.get("field"))
        value = clean_text(override.get("value"))
        if not isinstance(order, int) or field_name not in allowed or not value:
            warnings.append(f"잘못된 기사별 실행 컨텍스트 항목: {override}")
            continue
        if not evidence:
            warnings.append(f"근거 없는 기사별 판단을 적용하지 않음: {order}/{field_name}")
            continue
        candidates = [article for article in articles if article.order == order]
        if len(candidates) == 1:
            target = candidates[0]
        elif len(candidates) > 1:
            ranked = sorted(
                (
                    (article_context_identity_score(override, article), index, article)
                    for index, article in enumerate(candidates)
                ),
                key=lambda item: (item[0], -item[1]),
                reverse=True,
            )
            target = (
                ranked[0][2]
                if ranked[0][0] > 0 and (len(ranked) == 1 or ranked[0][0] > ranked[1][0])
                else None
            )
        else:
            target = articles[order - 1] if 1 <= order <= len(articles) else None
        if target is None:
            warnings.append(f"기사별 판단 대상이 순번·제목·매체로 하나로 확정되지 않음: {order}/{field_name}")
            continue
        setattr(target, field_name, value)
    return warnings


def apply_confirmed_result_order(
    articles: list[Article],
    run_context: dict[str, Any],
) -> tuple[list[Article], list[str]]:
    confirmation = run_context.get("result_order")
    if not confirmation:
        return articles, []
    evidence = [clean_text(item) for item in confirmation.get("evidence", []) if clean_text(item)]
    reference_file = clean_text(confirmation.get("reference_file"))
    reference_sha256 = clean_text(confirmation.get("reference_sha256"))
    orders = confirmation.get("orders")
    if not evidence or not reference_file or not reference_sha256 or not isinstance(orders, list):
        return articles, ["결과 순서 확인값의 기준 파일·해시·근거·순번이 불완전함"]
    reference_path = Path(reference_file)
    if not reference_path.is_file():
        return articles, ["결과 순서 확인값의 기준 파일을 찾지 못함"]
    if sha256_file(reference_path) != reference_sha256:
        return articles, ["결과 순서 확인값의 기준 파일이 확인 이후 변경됨"]
    expected_orders = {article.order for article in articles}
    if len(orders) != len(articles) or set(orders) != expected_orders:
        return articles, ["결과 순서 확인값이 현재 최종 기사 순번의 완전한 순열이 아님"]
    by_order = {article.order: article for article in articles}
    return [by_order[order] for order in orders], []


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    args = parse_args()
    working_root = Path.cwd().resolve()
    config_path = Path(args.config).resolve() if args.config else default_config_path()
    config = json.loads(config_path.read_text(encoding="utf-8"))

    final_report = Path(args.final_report).resolve()
    morning_dir = Path(args.morning_dir).resolve()
    afternoon_dir = Path(args.afternoon_dir).resolve()
    source_json = Path(args.source_json).resolve()
    schedule_json = Path(args.schedule_json).resolve()
    run_context_path = Path(args.run_context).resolve()
    required = [final_report, morning_dir, afternoon_dir, source_json, schedule_json, config_path, run_context_path]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError("필수 입력 경로 없음: " + ", ".join(missing))
    if not morning_dir.is_dir():
        raise NotADirectoryError(f"오전 작업 폴더가 아님: {morning_dir}")
    if not afternoon_dir.is_dir():
        raise NotADirectoryError(f"오후 작업 폴더가 아님: {afternoon_dir}")
    for label, path in (
        ("최종보고서", final_report),
        ("정기 작업내역 JSON", source_json),
        ("동향 스케줄 JSON", schedule_json),
        ("실행 컨텍스트", run_context_path),
    ):
        if not path.is_file():
            raise FileNotFoundError(f"{label} 파일이 아님: {path}")

    run_context = json.loads(run_context_path.read_text(encoding="utf-8-sig"))
    morning_paths = list(iter_document_files(morning_dir))
    afternoon_paths = list(iter_document_files(afternoon_dir))
    work_paths = morning_paths + afternoon_paths
    comparison_stages = {
        **{str(path.resolve()): "morning" for path in morning_paths},
        **{str(path.resolve()): "afternoon" for path in afternoon_paths},
    }
    japan_path = resolve_japan_input(final_report, args.japan_input)
    fingerprint_paths = [final_report, source_json, schedule_json, *work_paths]
    if japan_path:
        fingerprint_paths.extend(iter_document_files(japan_path))
        if japan_path.is_file() and japan_path.suffix.lower() == ".json":
            fingerprint_paths.append(japan_path)
    actual_fingerprint = run_fingerprint(fingerprint_paths)
    expected_fingerprint = clean_text(run_context.get("input_fingerprint"))
    if config.get("inference", {}).get("require_fresh_context", True):
        if not expected_fingerprint:
            raise ValueError("run_context.json에 input_fingerprint가 없습니다.")
        if actual_fingerprint != expected_fingerprint:
            raise ValueError("입력 파일이 run_context.json 작성 이후 변경되었습니다. 현재 파일로 컨텍스트를 다시 판단하세요.")

    job_date, warnings = resolve_job_date(final_report, run_context, args.job_date)
    if japan_path:
        japan_job_date, japan_date_evidence = document_job_date(japan_path)
        if japan_job_date and japan_job_date != job_date:
            raise ValueError(
                f"일본언론동향 작업일 {japan_job_date.isoformat()}이 최종보고서 작업일 {job_date.isoformat()}과 다릅니다."
            )
        warnings.append(
            "일본언론동향 입력: "
            + str(japan_path)
            + (f"; {japan_date_evidence}" if japan_date_evidence else "")
        )
    else:
        warnings.append("일본언론동향 미제공(선택 입력): 일일일본동향 열은 공란으로 유지")
    schedule = json.loads(schedule_json.read_text(encoding="utf-8-sig"))
    context_schedule = run_context.get("schedule") or {}
    context_schedule_hash = clean_text(context_schedule.get("sha256"))
    if context_schedule_hash and context_schedule_hash != sha256_file(schedule_json):
        raise ValueError("run_context.json 작성 이후 동향 스케줄 근거가 변경되었습니다.")
    valid_schedule_refs = validate_schedule_evidence(schedule, job_date, config)
    require_schedule = bool(config.get("inference", {}).get("require_schedule_evidence_for_roles", True))
    warnings.append(
        "동향 스케줄 근거: "
        + f"{clean_text((schedule.get('source') or {}).get('sheet_name'))} 탭 "
        + f"{clean_text(schedule.get('weekday'))} 열, {len(valid_schedule_refs)}개 행"
    )
    target_date = job_date - dt.timedelta(days=1)
    output_dir = Path(args.output_dir).resolve() if args.output_dir else working_root / "output" / job_date.isoformat()
    output_dir.mkdir(parents=True, exist_ok=True)

    final_hash = sha256_file(final_report)
    final_articles = parse_document(final_report, require_category_alignment=True)
    if not final_articles:
        raise ValueError("최종보고서에서 기사 제목을 찾지 못했습니다.")
    warnings.extend(apply_article_overrides(final_articles, run_context))
    final_articles, order_warnings = apply_confirmed_result_order(final_articles, run_context)
    warnings.extend(order_warnings)

    source_profiles = run_context.get("sources", {})
    source_sheet_config = source_spreadsheet_config(config)
    runtime_source_id = clean_text(source_sheet_config.get("id")) or clean_text(
        (schedule.get("source") or {}).get("spreadsheet_id")
    )
    regular, regular_warnings = regular_candidates(
        source_json,
        target_date,
        source_profiles.get("regular"),
        valid_schedule_refs,
        require_schedule,
        bool(config.get("inference", {}).get("require_source_scan_audit", False)),
        runtime_source_id,
        clean_text(source_sheet_config.get("source_sheet")),
    )
    warnings.extend(regular_warnings)
    current_source_history = source_history_has_operational_fields(regular)
    workers, worker_files, worker_warnings = worker_candidates(
        work_paths,
        final_hash,
        run_context,
        comparison_stages,
        valid_schedule_refs,
        require_schedule,
    )
    warnings.extend(worker_warnings)
    apply_latest_group_titles(
        final_articles,
        workers,
        config["matching"]["review_threshold"],
    )
    align_group_child_titles(final_articles)
    apply_latest_aggregate_categories(
        final_articles,
        workers,
        config["matching"]["review_threshold"],
        config["matching"]["auto_threshold"],
    )
    apply_aggregate_front_category_labels(final_articles, workers)
    japan, japan_warnings = japan_candidates(
        japan_path,
        source_profiles.get("japan"),
        valid_schedule_refs,
        require_schedule,
    )
    warnings.extend(japan_warnings)
    japan = enrich_special_source_roles(
        japan,
        workers,
        config["matching"],
        job_date,
        qualify_same_day_auxiliary=current_source_history,
    )
    final_profile = run_context.get("final")
    final_profile_complete = profile_is_complete(
        final_profile,
        ("owner", "worker"),
        valid_schedule_refs,
        require_schedule,
    )
    if not final_profile_complete:
        warnings.append("최종 담당·작업자 판단 근거가 실행 컨텍스트에 없거나 불완전함")
    final_disposition = run_context.get("final_disposition") or {}
    disposition_complete = bool(
        clean_text(final_disposition.get("not_representative_owner"))
        and any(clean_text(item) for item in final_disposition.get("evidence", []))
    )
    japan_profile = source_profiles.get("japan") or {}
    japan_source_resolved = japan_input_is_resolved(japan_path, japan_profile)
    if not japan_source_resolved:
        if japan_path:
            warnings.append("일본언론동향 원본의 현재 실행 확인 근거가 불완전함")
        else:
            warnings.append("일본언론동향 미제공 상태의 현재 실행 근거가 불완전함")

    pools = {"regular": regular, "japan": japan, "worker": workers}
    representative_draft_lineage = infer_representative_draft_lineage(
        final_articles,
        workers,
        config["matching"],
    )
    confirmed_additions, addition_warnings = confirmed_article_additions(run_context, pools)
    warnings.extend(addition_warnings)
    automatic_additions = automatic_similar_additions(
        final_articles,
        workers,
        pools,
        config["matching"],
    )
    rows: list[list[Any]] = []
    reviews: list[dict[str, Any]] = []
    match_details: list[dict[str, Any]] = []
    for article in final_articles:
        origin, score, reasons, best_scores = choose_origin(
            article,
            pools,
            config["matching"],
            run_context.get("origin_policy"),
        )
        lineage_origin = representative_draft_lineage.get(article.order)
        if lineage_origin is not None and origin is not None and origin.source_type == "regular":
            origin = lineage_origin
            score = candidate_score(article, lineage_origin)
            reasons = [reason for reason in reasons if not reason.startswith("낮은 매칭 점수")]
        confirmation = article_scoped_confirmation(
            run_context.get("article_origin_confirmations", []),
            article,
        )
        if confirmation:
            confirmed, confirmed_score, confirmation_reasons = confirmed_origin(article, pools, confirmation)
            if confirmed:
                origin = confirmed
                score = confirmed_score
                reasons = [] if confirmed.extra.get("profile_complete", False) else ["유입 파일의 작업자·역할 근거 불완전"]
            reasons.extend(confirmation_reasons)
        role_confirmation = article_scoped_confirmation(
            run_context.get("article_role_confirmations", []),
            article,
        )
        japan_confirmation = article_scoped_confirmation(
            run_context.get("article_japan_confirmations", []),
            article,
        )
        japan_match, japan_score, japan_reasons = japan_membership(
            article,
            japan,
            config["matching"]["review_threshold"],
            japan_confirmation,
        )
        reasons.extend(japan_reasons)
        if (
            origin is not None
            and origin.source_type == "japan"
            and not (japan_confirmation and japan_confirmation.get("included") is False)
        ):
            japan_match = True
            japan_score = score
            reasons = [
                reason
                for reason in reasons
                if not reason.startswith("일본동향 동일 매체·인접 날짜 저점수 후보 직접 대조 필요")
            ]
        prior_origin = origin
        preferred_origin, preferred_score, preferred_reasons = confirmed_japan_origin(
            origin,
            article,
            japan,
            japan_confirmation,
        )
        reasons.extend(preferred_reasons)
        if preferred_origin is not origin:
            origin = preferred_origin
            score = preferred_score
            if prior_origin is None:
                reasons = [reason for reason in reasons if reason != "유입 경로를 확인하지 못함"]
            if not origin.extra.get("profile_complete", False):
                reasons.append("유입 파일의 작업자·역할 근거 불완전")
        origin = regular_reintroduced_in_morning_origin(
            article,
            origin,
            pools,
            config["matching"],
            target_date,
        )
        origin = legacy_late_morning_aggregate_origin(
            article,
            origin,
            pools,
            config["matching"],
            target_date,
            enabled=bool(regular) and not current_source_history,
        )
        if role_confirmation:
            origin, role_confirmation_reasons, role_confirmation_applied = (
                apply_confirmed_article_roles(
                    origin,
                    role_confirmation,
                    valid_schedule_refs,
                    require_schedule,
                )
            )
            if role_confirmation_applied:
                # The confirmation is bound to the current authoritative file
                # hash and is deliberately applied after every automatic origin
                # rewrite. Later category/final/Japan checks remain independent.
                reasons = []
            reasons.extend(role_confirmation_reasons)
        japan_value = "O" if japan_match else ""
        if not article.category:
            reasons.append("최종보고서 상위 카테고리 미확인")
        if not final_profile_complete:
            reasons.append("최종 담당·작업자 근거 불완전")
        if article.similar and not disposition_complete:
            reasons.append("비대표·미포함 최종 담당 표기 근거 불완전")
        if not japan_value and not japan_source_resolved:
            reasons.append("일본동향 자료 제공 여부 미확인")
        row = result_row(
            article,
            job_date,
            origin,
            japan_value,
            final_profile,
            final_disposition,
            reasons,
        )
        rows.append(row)
        detail = {
            "order": article.order,
            "title": row[10],
            "origin": origin.source_type if origin else None,
            "origin_file": origin.source_file if origin else None,
            "origin_source_kind": origin.extra.get("source_kind") if origin else None,
            "origin_actual_edit_source_kind": origin.extra.get("actual_edit_source_kind") if origin else None,
            "origin_comparison_stage": origin.extra.get("comparison_stage") if origin else None,
            "origin_priority": int(origin.extra.get("priority", 0)) if origin else None,
            "score": round(score, 4),
            "best_scores": best_scores,
            "reasons": reasons,
            "context_confirmation": (confirmation or {}).get("evidence", []),
            "role_confirmation": (role_confirmation or {}).get("evidence", []),
            "role_confirmed_from_reference": bool(
                origin and origin.extra.get("role_confirmed_from_reference")
            ),
            "japan_match_score": round(japan_score, 4),
            "japan_confirmation": (japan_confirmation or {}).get("evidence", []),
        }
        match_details.append(detail)
        if reasons:
            reviews.append({"row_number": article.order + 1, "row": row, **detail})

    rows, match_details = reorder_front_only_results(final_articles, rows, match_details)

    def addition_output(record: dict[str, Any]) -> tuple[list[Any], dict[str, Any], list[str]]:
        article = record["article"]
        candidate = record["candidate"]
        reasons: list[str] = list(record.get("origin_reasons", []))
        scoped_order = record.get("after_order") or article.order
        origin_confirmation = article_scoped_confirmation(
            run_context.get("article_origin_confirmations", []),
            article,
            order=scoped_order,
        )
        if origin_confirmation:
            confirmed_candidate, confirmed_score, confirmation_reasons = confirmed_origin(
                article,
                pools,
                origin_confirmation,
            )
            if confirmed_candidate:
                candidate = confirmed_candidate
                record["score"] = confirmed_score
                reasons = (
                    []
                    if candidate.extra.get("profile_complete", False)
                    else ["유입 파일의 작업자·역할 근거 불완전"]
                )
            reasons.extend(confirmation_reasons)
        role_confirmation = article_scoped_confirmation(
            run_context.get("article_role_confirmations", []),
            article,
            order=scoped_order,
        )
        role_confirmation_applied = False
        if role_confirmation:
            candidate, role_reasons, role_confirmation_applied = apply_confirmed_article_roles(
                candidate,
                role_confirmation,
                valid_schedule_refs,
                require_schedule,
            )
            if role_confirmation_applied:
                reasons = []
            reasons.extend(role_reasons)
        if not candidate.extra.get("profile_complete", False):
            reasons.append("유입 파일의 작업자·역할 근거 불완전")
        if not final_profile_complete:
            reasons.append("최종 담당·작업자 근거 불완전")
        if not disposition_complete:
            reasons.append("비대표·미포함 최종 담당 표기 근거 불완전")
        japan_confirmation = article_scoped_confirmation(
            run_context.get("article_japan_confirmations", []),
            article,
            order=scoped_order,
        )
        japan_match, japan_score, japan_reasons = japan_membership(
            article,
            japan,
            config["matching"]["review_threshold"],
            japan_confirmation,
        )
        reasons.extend(japan_reasons)
        japan_value = "O" if japan_match else ""
        if not japan_value and not japan_source_resolved:
            reasons.append("일본동향 자료 제공 여부 미확인")
        in_final_report = record["kind"] == "similar"
        row = result_row(
            article,
            job_date,
            candidate,
            japan_value,
            final_profile,
            final_disposition,
            reasons,
            in_final_report=in_final_report,
        )
        detail = {
            "order": record.get("after_order") or len(final_articles) + 1,
            "title": row[10],
            "origin": candidate.source_type,
            "origin_file": candidate.source_file,
            "origin_source_kind": candidate.extra.get("source_kind"),
            "origin_actual_edit_source_kind": candidate.extra.get("actual_edit_source_kind"),
            "origin_comparison_stage": candidate.extra.get("comparison_stage"),
            "score": round(float(record.get("score", 1.0)), 4),
            "best_scores": record.get("best_scores", {}),
            "reasons": reasons,
            "confirmed_addition": record["kind"] if not record.get("automatic") else None,
            "automatic_addition": record["kind"] if record.get("automatic") else None,
            "reference_file": record.get("reference_file"),
            "confirmation_evidence": record["evidence"],
            "context_confirmation": (origin_confirmation or {}).get("evidence", []),
            "role_confirmation": (role_confirmation or {}).get("evidence", []),
            "role_confirmed_from_reference": role_confirmation_applied,
            "japan_match_score": round(japan_score, 4),
            "japan_confirmation": (japan_confirmation or {}).get("evidence", []),
        }
        return row, detail, reasons

    inline_additions = sorted(
        (
            record
            for record in [*confirmed_additions, *automatic_additions]
            if record["kind"] == "similar"
        ),
        key=lambda record: int(record["after_order"]),
    )
    for record in inline_additions:
        row, detail, reasons = addition_output(record)
        preceding_index = next(
            (
                index
                for index, current_detail in enumerate(match_details)
                if current_detail.get("order") == int(record["after_order"])
            ),
            len(rows) - 1,
        )
        insert_index = min(preceding_index + 1, len(rows))
        rows.insert(insert_index, row)
        match_details.insert(insert_index, detail)
        if reasons:
            reviews.append({"row_number": insert_index + 2, "row": row, **detail})

    omitted = omitted_worker_candidates(final_articles, workers, config["matching"])
    confirmed_candidate_keys = {
        (
            record["candidate"].source_type,
            str(Path(record["candidate"].source_file).resolve()),
            normalize_key(record["candidate"].title),
        )
        for record in confirmed_additions
    }
    omitted = [
        candidate
        for candidate in omitted
        if (
            candidate.source_type,
            str(Path(candidate.source_file).resolve()),
            normalize_key(candidate.title),
        )
        not in confirmed_candidate_keys
    ]
    for offset, candidate in enumerate(omitted, start=1):
        order = len(final_articles) + offset
        category, category_mapped = omitted_candidate_category(
            candidate,
            final_articles,
            config["matching"],
        )
        reasons: list[str] = []
        if not category_mapped:
            reasons.append("미포함 기사 카테고리 최종 기준 확인 필요")
        if not candidate.extra.get("profile_complete", False):
            reasons.append("유입 파일의 작업자·역할 근거 불완전")
        if not final_profile_complete:
            reasons.append("최종 담당·작업자 근거 불완전")
        if not disposition_complete:
            reasons.append("비대표·미포함 최종 담당 표기 근거 불완전")
        japan_match, japan_score, japan_reasons = japan_membership(
            Article(
                source_file=candidate.source_file,
                order=order,
                category=category,
                media=candidate.media,
                date=candidate.date,
                body_title=candidate.title,
                canonical_title=candidate.title,
            ),
            japan,
            config["matching"]["review_threshold"],
        )
        reasons.extend(japan_reasons)
        japan_value = "O" if japan_match else ""
        if not japan_value and not japan_source_resolved:
            reasons.append("일본동향 자료 제공 여부 미확인")
        article = Article(
            source_file=candidate.source_file,
            order=order,
            category=category,
            media=candidate.media,
            date=candidate.date,
            body_title=candidate.title,
            canonical_title=candidate.title,
        )
        row = result_row(
            article,
            job_date,
            candidate,
            japan_value,
            final_profile,
            final_disposition,
            reasons,
            in_final_report=False,
        )
        rows.append(row)
        detail = {
            "order": order,
            "title": row[10],
            "origin": candidate.source_type,
            "origin_file": candidate.source_file,
            "origin_source_kind": candidate.extra.get("source_kind"),
            "origin_actual_edit_source_kind": candidate.extra.get("actual_edit_source_kind"),
            "origin_comparison_stage": candidate.extra.get("comparison_stage"),
            "score": 0.0,
            "best_scores": {},
            "reasons": reasons,
            "omitted_from_final": True,
            "role_confirmed_from_reference": False,
            "japan_match_score": round(japan_score, 4),
        }
        match_details.append(detail)
        if reasons:
            reviews.append({"row_number": len(rows) + 1, "row": row, **detail})

    for record in (item for item in confirmed_additions if item["kind"] == "omitted"):
        row, detail, reasons = addition_output(record)
        rows.append(row)
        match_details.append(detail)
        if reasons:
            reviews.append({"row_number": len(rows) + 1, "row": row, **detail})

    errors = validate_result(
        final_articles,
        rows,
        len(omitted) + len(confirmed_additions) + len(automatic_additions),
        match_details,
    )
    if errors:
        raise ValueError("; ".join(errors))

    for review_item in reviews:
        review_item["row_number"] = next(
            index
            for index, current_row in enumerate(rows, start=2)
            if current_row is review_item["row"]
        )

    final_info = {
        "path": str(final_report),
        "size": final_report.stat().st_size,
        "sha256": final_hash,
        "role": "final_report",
        "deduplicated": False,
    }
    source_info = {
        "path": str(source_json),
        "size": source_json.stat().st_size,
        "sha256": sha256_file(source_json),
        "role": "regular_history_json",
        "deduplicated": False,
    }
    schedule_info = {
        "path": str(schedule_json),
        "size": schedule_json.stat().st_size,
        "sha256": sha256_file(schedule_json),
        "role": "work_schedule_json",
        "deduplicated": False,
        "source": schedule.get("source", {}),
        "assignment_refs": sorted(valid_schedule_refs),
    }
    japan_info = None
    if japan_path:
        japan_info = {
            "path": str(japan_path),
            "size": japan_path.stat().st_size if japan_path.is_file() else 0,
            "sha256": sha256_file(japan_path) if japan_path.is_file() else "",
            "role": "japan_news_source",
            "deduplicated": False,
            "candidate_articles": len(japan),
        }
    manifest = {
        "job_date": job_date.isoformat(),
        "target_source_date": target_date.isoformat(),
        "created_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "run_context": {
            "path": str(run_context_path),
            "sha256": sha256_file(run_context_path),
            "input_fingerprint": actual_fingerprint,
        },
        "schedule": {
            "path": str(schedule_json),
            "sha256": sha256_file(schedule_json),
            "source": schedule.get("source", {}),
            "job_date": schedule.get("job_date"),
            "weekday": schedule.get("weekday"),
            "assignment_refs": sorted(valid_schedule_refs),
        },
        "japan_input": {
            "status": "present_checked" if japan_path else "not_provided",
            "path": str(japan_path) if japan_path else "",
            "candidate_articles": len(japan),
        },
        "files": [final_info, source_info, schedule_info, *([japan_info] if japan_info else []), *worker_files],
        "counts": {
            "final_articles": len(final_articles),
            "regular_candidates": len(regular),
            "japan_candidates": len(japan),
            "worker_candidates": len(workers),
            "omitted_workfile_articles": len(omitted),
            "confirmed_article_additions": len(confirmed_additions),
            "automatic_article_additions": len(automatic_additions),
            "result_rows": len(rows),
            "review_rows": len(reviews),
        },
        "warnings": warnings,
    }
    result = {
        "headers": RESULT_HEADERS,
        "rows": rows,
        "workbook": {
            "date_column_mode": workbook_date_column_mode(regular),
        },
        "articles": [asdict(article) for article in final_articles],
        "confirmed_additions": [
            {
                "kind": record["kind"],
                "after_order": record["after_order"],
                "article": asdict(record["article"]),
                "source_file": record["candidate"].source_file,
                "reference_file": record["reference_file"],
            }
            for record in confirmed_additions
        ],
        "automatic_additions": [
            {
                "kind": record["kind"],
                "after_order": record["after_order"],
                "article": asdict(record["article"]),
                "source_file": record["source_candidate"].source_file,
                "evidence": record["evidence"],
            }
            for record in automatic_additions
        ],
        "matches": match_details,
    }
    review = {"headers": RESULT_HEADERS, "count": len(reviews), "items": reviews, "warnings": warnings}
    google_payload = {
        "range": clean_text(result_spreadsheet_config(config).get("result_range")) or "A:O",
        "values": rows,
    }
    sheets_write_enabled = google_sheets_write_enabled(config)
    checkpoint = {
        "job_date": job_date.isoformat(),
        "phase": "local_processed",
        "intermediate_saved": False,
        "excel_finalized": False,
        "google_sheets_write_enabled": sheets_write_enabled,
        "upload_skipped": not sheets_write_enabled,
        "uploaded_verified": False,
        "result_rows": len(rows),
        "review_rows": len(reviews),
        "updated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
    }

    write_json(output_dir / "manifest.json", manifest)
    write_json(output_dir / "result.json", result)
    write_json(output_dir / "review.json", review)
    write_json(output_dir / "google_payload.json", google_payload)
    write_json(output_dir / "checkpoint.json", checkpoint)
    log_lines = [
        f"작업일: {job_date.isoformat()}",
        f"최종보고서: {final_report}",
        f"최종 기사/결과 행: {len(final_articles)}/{len(rows)}",
        f"정기/일본/작업자 후보: {len(regular)}/{len(japan)}/{len(workers)}",
        f"확인 필요: {len(reviews)}",
        f"Google Sheets 결과 쓰기: {'활성화' if sheets_write_enabled else '비활성화 (엑셀 전용)'}",
        *[f"경고: {warning}" for warning in warnings],
    ]
    (output_dir / "작업로그.txt").write_text("\n".join(log_lines) + "\n", encoding="utf-8")

    print(
        json.dumps(
            {
                "output_dir": str(output_dir),
                "final_articles": len(final_articles),
                "result_rows": len(rows),
                "review_rows": len(reviews),
                "checkpoint": "local_processed",
                "google_sheets_write_enabled": sheets_write_enabled,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise
