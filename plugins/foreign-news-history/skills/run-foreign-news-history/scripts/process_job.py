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
    raw_heading: str = ""

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
    parser.add_argument("--japan-input", required=True, help="현재 작업일의 일본언론동향 원본 정확한 경로")
    parser.add_argument("--output-dir")
    parser.add_argument("--config")
    return parser.parse_args()


def skill_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_config_path() -> Path:
    return skill_root() / "assets" / "harness.config.json"


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


def extract_hwp(path: Path) -> list[str]:
    try:
        import olefile  # type: ignore
    except ImportError as exc:  # pragma: no cover - environment error
        raise RuntimeError("HWP 읽기에 olefile 패키지가 필요합니다.") from exc

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
        and re.search(r"[A-Za-z]", remainder)
    ):
        return remainder
    return media


def display_title(value: str) -> str:
    """Apply only presentation cleanup confirmed by the authoritative result format."""
    title = clean_text(value)
    title = re.sub(r"^\[영상\]\s*", "", title)
    return re.sub(r"…\s+", "…", title)


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
    if re.search(r"\d", text) or re.search(r"[,，.!?。?!]$", text):
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


def body_category_map(paragraphs: list[str], entries: list[dict[str, str]]) -> dict[int, str]:
    first_heading = next((index for index, text in enumerate(paragraphs) if ARTICLE_HEADING.match(text)), None)
    if first_heading is None:
        return {}
    front_categories: list[str] = []
    for entry in entries:
        category = clean_text(entry.get("category"))
        if category and category not in front_categories:
            front_categories.append(category)
    last_front_article = max(
        (index for index, text in enumerate(paragraphs[:first_heading]) if FRONT_ARTICLE.match(text)),
        default=-1,
    )
    candidates = [
        index
        for index in range(last_front_article + 1, len(paragraphs))
        if is_body_category_candidate(paragraphs, index)
    ]
    mapping: dict[int, str] = {}
    for position, index in enumerate(candidates):
        raw = clean_text(paragraphs[index]).strip("[] ")
        mapping[index] = front_categories[position] if position < len(front_categories) else raw
    return mapping


def parse_document(path: Path) -> list[Article]:
    paragraphs = extract_paragraphs(path)
    entries = front_entries(paragraphs)
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

    for article_index, article in enumerate(articles):
        start = positions[article_index] + 1
        end = positions[article_index + 1] if article_index + 1 < len(positions) else len(paragraphs)
        content = []
        for paragraph in paragraphs[start:end]:
            if paragraph.startswith("[") and paragraph.endswith("]"):
                break
            cleaned = paragraph.lstrip("- ").strip()
            if cleaned:
                content.append(cleaned)
        article.body_present = bool(content)

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
            article.canonical_title = entries[best_index]["title"]
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


def profile_fields(profile: dict[str, Any] | None) -> tuple[str, str, str]:
    profile = profile or {}
    return (
        clean_text(profile.get("workgroup")),
        clean_text(profile.get("owner")),
        clean_text(profile.get("worker")),
    )


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
    if clean_text(schedule.get("job_date")) != job_date.isoformat():
        raise ValueError("동향 스케줄 근거의 작업일이 최종보고서 작업일과 다릅니다.")
    expected_weekday = ("월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일")[job_date.weekday()]
    if clean_text(schedule.get("weekday")) != expected_weekday:
        raise ValueError("동향 스케줄 근거의 요일이 작업일과 다릅니다.")
    source = schedule.get("source") or {}
    sheet_config = config.get("spreadsheet", {})
    expected_spreadsheet = clean_text(sheet_config.get("id"))
    expected_sheet = clean_text(sheet_config.get("schedule_sheet"))
    if expected_spreadsheet and clean_text(source.get("spreadsheet_id")) != expected_spreadsheet:
        raise ValueError("동향 스케줄 근거가 설정된 외신 일일동향 스프레드시트에서 오지 않았습니다.")
    if expected_sheet and clean_text(source.get("sheet_name")) != expected_sheet:
        raise ValueError("동향 스케줄 근거의 탭이 설정된 근무 탭과 다릅니다.")
    refs = {
        clean_text(item.get("ref")): clean_text(item.get("worker"))
        for item in schedule.get("assignments", [])
        if isinstance(item, dict) and clean_text(item.get("ref")) and clean_text(item.get("worker"))
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
) -> tuple[list[Candidate], list[str]]:
    rows = rows_from_json(path)
    headers = set(rows[0]) if rows else set()
    warnings: list[str] = []
    missing = [header for header in ("보도일", "제목 (한글)") if header not in headers]
    if missing:
        warnings.append("정기 작업내역 필수 열 누락: " + ", ".join(missing))
    workgroup, owner, worker = profile_fields(profile)
    profile_complete = profile_is_complete(profile, valid_schedule_refs=valid_schedule_refs, require_schedule=require_schedule)
    if not profile_complete:
        warnings.append("정기 작업내역 역할 근거가 실행 컨텍스트에 없거나 불완전함")
    candidates: list[Candidate] = []
    for row in rows:
        row_date = parse_date(row.get("보도일"))
        if row_date != target_date:
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
                    "priority": int((profile or {}).get("priority", 0)),
                    "profile_complete": profile_complete,
                    "profile_evidence": (profile or {}).get("evidence", []),
                    "schedule_refs": (profile or {}).get("schedule_refs", []),
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
        profile_complete = profile_is_complete(
            profile,
            valid_schedule_refs=valid_schedule_refs,
            require_schedule=require_schedule,
        )
        file_info = {
            "path": resolved,
            "size": path.stat().st_size,
            "sha256": digest,
            "role": "final_report_duplicate" if digest == final_hash else clean_text((profile or {}).get("source_kind")) or "unresolved",
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
        try:
            articles = parse_document(path)
        except Exception as exc:  # retain the run and expose the file for review
            warnings.append(f"작업자 파일 파싱 실패: {path.name}: {exc}")
            continue
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
                        "priority": int((profile or {}).get("priority", 0)),
                        "source_kind": clean_text((profile or {}).get("source_kind")),
                        "include_unmatched": bool((profile or {}).get("include_unmatched", False)),
                        "profile_complete": profile_complete,
                        "profile_evidence": (profile or {}).get("evidence", []),
                        "schedule_refs": (profile or {}).get("schedule_refs", []),
                        "category": article.category,
                        "article_order": article.order,
                    },
                )
            )
    return candidates, files, warnings


def japan_candidates(
    path: Path | None,
    profile: dict[str, Any] | None,
    valid_schedule_refs: dict[str, str] | set[str] | None = None,
    require_schedule: bool = False,
) -> tuple[list[Candidate], list[str]]:
    if path is None:
        return [], ["일본동향 입력 미제공: 일일일본동향 여부는 자동 확정하지 않음"]
    workgroup, owner, worker = profile_fields(profile)
    candidates: list[Candidate] = []
    warnings: list[str] = []
    profile_complete = profile_is_complete(profile, valid_schedule_refs=valid_schedule_refs, require_schedule=require_schedule)
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
                                "priority": int((profile or {}).get("priority", 0)),
                                "profile_complete": profile_complete,
                                "profile_evidence": (profile or {}).get("evidence", []),
                                "schedule_refs": (profile or {}).get("schedule_refs", []),
                            },
                        )
                    )
        except Exception as exc:
            warnings.append(f"일본동향 JSON 파싱 실패: {exc}")
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
                            "priority": int((profile or {}).get("priority", 0)),
                            "profile_complete": profile_complete,
                            "profile_evidence": (profile or {}).get("evidence", []),
                            "schedule_refs": (profile or {}).get("schedule_refs", []),
                        },
                    )
                )
        except Exception as exc:
            warnings.append(f"일본동향 파일 파싱 실패: {document.name}: {exc}")
    if not candidates:
        warnings.append("일본동향 입력에서 기사를 찾지 못했습니다.")
    return candidates, warnings


def candidate_score(article: Article, candidate: Candidate) -> float:
    title_score = max(text_similarity(title, candidate.title) for title in article.match_titles)
    media_score = media_similarity(article.media, candidate.media)
    date_score = 1.0 if article.date and candidate.date and normalize_key(article.date) == normalize_key(candidate.date) else 0.0
    return min(1.0, 0.88 * title_score + 0.08 * media_score + 0.04 * date_score)


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


def choose_origin(
    article: Article,
    pools: dict[str, list[Candidate]],
    matching: dict[str, float],
    origin_policy: dict[str, Any] | None = None,
) -> tuple[Candidate | None, float, list[str], dict[str, float]]:
    reasons: list[str] = []
    best_scores: dict[str, float] = {}
    chosen: Candidate | None = None
    chosen_score = 0.0
    ranked_by_source: dict[str, list[tuple[float, Candidate]]] = {}
    for source_type, candidates in pools.items():
        score_ranked = ranked_matches(article, candidates)
        ranked = ranked_origin_matches(article, candidates, matching["review_threshold"])
        ranked_by_source[source_type] = ranked
        best_scores[source_type] = round(score_ranked[0][0], 4) if score_ranked else 0.0

    policy = origin_policy or {}
    policy_evidence = [clean_text(item) for item in policy.get("evidence", []) if clean_text(item)]
    selection = clean_text(policy.get("selection"))
    source_order = [
        clean_text(item)
        for item in policy.get("source_order", [])
        if clean_text(item) in pools
    ]
    if selection == "priority_then_score" and policy_evidence:
        combined = sorted(
            (pair for ranked in ranked_by_source.values() for pair in ranked),
            key=lambda pair: (int(pair[1].extra.get("priority", 0)), pair[0]),
            reverse=True,
        )
        if combined:
            chosen_score, chosen = combined[0]
    elif source_order and policy_evidence:
        ordered_sources = source_order + [key for key in pools if key not in source_order]
        for source_type in ordered_sources:
            ranked = ranked_by_source[source_type]
            if ranked:
                chosen_score, chosen = ranked[0]
                break
    else:
        combined = sorted(
            (pair for ranked in ranked_by_source.values() for pair in ranked),
            key=lambda pair: (pair[0], int(pair[1].extra.get("priority", 0))),
            reverse=True,
        )
        if combined:
            chosen_score, chosen = combined[0]
        if sum(bool(ranked) for ranked in ranked_by_source.values()) > 1:
            reasons.append("유입 경로 우선순위의 현재 실행 근거 없음")
    if chosen is None:
        reasons.append("유입 경로를 확인하지 못함")
        return None, 0.0, reasons, best_scores
    if chosen_score < matching["auto_threshold"]:
        reasons.append(f"낮은 매칭 점수 {chosen_score:.3f}")
    if not chosen.extra.get("profile_complete", False):
        reasons.append("유입 파일의 작업자·역할 근거 불완전")
    chosen_provenance = (chosen.workgroup, chosen.owner, chosen.worker)
    chosen_priority = int(chosen.extra.get("priority", 0))
    all_ranked = sorted(
        (pair for ranked in ranked_by_source.values() for pair in ranked),
        key=lambda pair: (pair[0], int(pair[1].extra.get("priority", 0))),
        reverse=True,
    )
    alternative = next(
        (
            pair
            for pair in all_ranked
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


def japan_membership(
    article: Article,
    candidates: list[Candidate],
    threshold: float,
    confirmation: dict[str, Any] | None = None,
) -> tuple[bool, float, list[str]]:
    ranked = ranked_matches(article, candidates)
    automatic_score = ranked[0][0] if ranked else 0.0
    if automatic_score >= threshold:
        return True, automatic_score, []
    confirmed, reasons = confirmed_japan_candidate(candidates, confirmation)
    if confirmed:
        return True, candidate_score(article, confirmed), []
    return False, automatic_score, reasons


def omitted_worker_candidates(
    final_articles: list[Article],
    candidates: list[Candidate],
    matching: dict[str, float],
) -> list[Candidate]:
    eligible = [candidate for candidate in candidates if candidate.extra.get("include_unmatched", False)]
    unmatched = [
        candidate
        for candidate in eligible
        if not final_articles
        or max(candidate_score(article, candidate) for article in final_articles) < matching["review_threshold"]
    ]
    unmatched.sort(key=lambda item: int(item.extra.get("priority", 0)), reverse=True)
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
            unique.append(candidate)
    return unique


def closest_current_category(
    value: str,
    final_articles: list[Article],
    threshold: float,
) -> tuple[str, bool]:
    raw = clean_text(value)
    categories = list(dict.fromkeys(article.category for article in final_articles if article.category))
    if not raw or not categories:
        return raw, False
    exact = next((category for category in categories if normalize_key(category) == normalize_key(raw)), None)
    if exact:
        return exact, True
    ranked = sorted(((text_similarity(raw, category), category) for category in categories), reverse=True)
    if ranked and ranked[0][0] >= threshold:
        return ranked[0][1], True
    return raw, False


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


def validate_result(final_articles: list[Article], rows: list[list[Any]], omitted_count: int = 0) -> list[str]:
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


def resolve_japan_input(final_report: Path, explicit: str | Path | None = None) -> Path:
    if not explicit:
        raise ValueError("일본언론동향 원본의 정확한 경로를 입력해야 합니다.")
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
        if not isinstance(order, int) or not 1 <= order <= len(articles) or field_name not in allowed or not value:
            warnings.append(f"잘못된 기사별 실행 컨텍스트 항목: {override}")
            continue
        if not evidence:
            warnings.append(f"근거 없는 기사별 판단을 적용하지 않음: {order}/{field_name}")
            continue
        setattr(articles[order - 1], field_name, value)
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
    work_paths = list(iter_document_files(morning_dir)) + list(iter_document_files(afternoon_dir))
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
        warnings.append("같은 작업일의 일본언론동향 원본을 찾지 못함")
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
    final_articles = parse_document(final_report)
    if not final_articles:
        raise ValueError("최종보고서에서 기사 제목을 찾지 못했습니다.")
    warnings.extend(apply_article_overrides(final_articles, run_context))
    final_articles, order_warnings = apply_confirmed_result_order(final_articles, run_context)
    warnings.extend(order_warnings)

    source_profiles = run_context.get("sources", {})
    regular, regular_warnings = regular_candidates(
        source_json,
        target_date,
        source_profiles.get("regular"),
        valid_schedule_refs,
        require_schedule,
    )
    warnings.extend(regular_warnings)
    workers, worker_files, worker_warnings = worker_candidates(
        work_paths,
        final_hash,
        run_context,
        valid_schedule_refs,
        require_schedule,
    )
    warnings.extend(worker_warnings)
    japan, japan_warnings = japan_candidates(
        japan_path,
        source_profiles.get("japan"),
        valid_schedule_refs,
        require_schedule,
    )
    warnings.extend(japan_warnings)
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
    japan_status = clean_text(japan_profile.get("status"))
    japan_status_evidence = any(clean_text(item) for item in japan_profile.get("evidence", []))
    japan_source_confirmed = japan_status == "present_checked" and japan_status_evidence and bool(japan_path)
    if japan_path and not japan_source_confirmed:
        warnings.append("일본언론동향 원본의 현재 실행 확인 근거가 불완전함")

    pools = {"regular": regular, "japan": japan, "worker": workers}
    confirmed_additions, addition_warnings = confirmed_article_additions(run_context, pools)
    warnings.extend(addition_warnings)
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
        confirmation = next(
            (
                item
                for item in run_context.get("article_origin_confirmations", [])
                if item.get("order") == article.order
            ),
            None,
        )
        if confirmation:
            confirmed, confirmed_score, confirmation_reasons = confirmed_origin(article, pools, confirmation)
            if confirmed:
                origin = confirmed
                score = confirmed_score
                reasons = [] if confirmed.extra.get("profile_complete", False) else ["유입 파일의 작업자·역할 근거 불완전"]
            reasons.extend(confirmation_reasons)
        role_confirmation = next(
            (
                item
                for item in run_context.get("article_role_confirmations", [])
                if item.get("order") == article.order
            ),
            None,
        )
        if role_confirmation:
            role_profile, role_confirmation_reasons = confirmed_article_roles(
                role_confirmation,
                valid_schedule_refs,
                require_schedule,
            )
            if role_profile and origin:
                origin = replace(
                    origin,
                    workgroup=clean_text(role_profile.get("workgroup")),
                    owner=clean_text(role_profile.get("owner")),
                    worker=clean_text(role_profile.get("worker")),
                )
            elif role_profile and not origin:
                role_confirmation_reasons.append("기사별 역할 확인값을 적용할 유입 후보가 없음")
            reasons.extend(role_confirmation_reasons)
        japan_confirmation = next(
            (
                item
                for item in run_context.get("article_japan_confirmations", [])
                if item.get("order") == article.order
            ),
            None,
        )
        japan_match, japan_score, japan_reasons = japan_membership(
            article,
            japan,
            config["matching"]["review_threshold"],
            japan_confirmation,
        )
        reasons.extend(japan_reasons)
        japan_value = "O" if japan_match else ""
        if not article.category:
            reasons.append("최종보고서 상위 카테고리 미확인")
        if not final_profile_complete:
            reasons.append("최종 담당·작업자 근거 불완전")
        if article.similar and not disposition_complete:
            reasons.append("비대표·미포함 최종 담당 표기 근거 불완전")
        if not japan_value and not japan_source_confirmed:
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
            "origin_priority": int(origin.extra.get("priority", 0)) if origin else None,
            "score": round(score, 4),
            "best_scores": best_scores,
            "reasons": reasons,
            "context_confirmation": (confirmation or {}).get("evidence", []),
            "role_confirmation": (role_confirmation or {}).get("evidence", []),
            "japan_match_score": round(japan_score, 4),
            "japan_confirmation": (japan_confirmation or {}).get("evidence", []),
        }
        match_details.append(detail)
        if reasons:
            reviews.append({"row_number": article.order + 1, "row": row, **detail})

    def addition_output(record: dict[str, Any]) -> tuple[list[Any], dict[str, Any], list[str]]:
        article = record["article"]
        candidate = record["candidate"]
        reasons: list[str] = []
        if not candidate.extra.get("profile_complete", False):
            reasons.append("유입 파일의 작업자·역할 근거 불완전")
        if not final_profile_complete:
            reasons.append("최종 담당·작업자 근거 불완전")
        if not disposition_complete:
            reasons.append("비대표·미포함 최종 담당 표기 근거 불완전")
        japan_match, japan_score, japan_reasons = japan_membership(
            article,
            japan,
            config["matching"]["review_threshold"],
        )
        reasons.extend(japan_reasons)
        japan_value = "O" if japan_match else ""
        if not japan_value and not japan_source_confirmed:
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
            "score": 1.0,
            "best_scores": {},
            "reasons": reasons,
            "confirmed_addition": record["kind"],
            "reference_file": record["reference_file"],
            "confirmation_evidence": record["evidence"],
            "japan_match_score": round(japan_score, 4),
        }
        return row, detail, reasons

    inline_additions = sorted(
        (record for record in confirmed_additions if record["kind"] == "similar"),
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
        category, category_mapped = closest_current_category(
            clean_text(candidate.extra.get("category")),
            final_articles,
            config["matching"]["review_threshold"],
        )
        reasons = ["작업본에는 있으나 최종보고서에 대표·유사보도로 확인되지 않음"]
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
        if not japan_value and not japan_source_confirmed:
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
            "score": 0.0,
            "best_scores": {},
            "reasons": reasons,
            "omitted_from_final": True,
            "japan_match_score": round(japan_score, 4),
        }
        match_details.append(detail)
        reviews.append({"row_number": len(rows) + 1, "row": row, **detail})

    for record in (item for item in confirmed_additions if item["kind"] == "omitted"):
        row, detail, reasons = addition_output(record)
        rows.append(row)
        match_details.append(detail)
        if reasons:
            reviews.append({"row_number": len(rows) + 1, "row": row, **detail})

    errors = validate_result(final_articles, rows, len(omitted) + len(confirmed_additions))
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
        "files": [final_info, source_info, schedule_info, *([japan_info] if japan_info else []), *worker_files],
        "counts": {
            "final_articles": len(final_articles),
            "regular_candidates": len(regular),
            "japan_candidates": len(japan),
            "worker_candidates": len(workers),
            "omitted_workfile_articles": len(omitted),
            "confirmed_article_additions": len(confirmed_additions),
            "result_rows": len(rows),
            "review_rows": len(reviews),
        },
        "warnings": warnings,
    }
    result = {
        "headers": RESULT_HEADERS,
        "rows": rows,
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
        "matches": match_details,
    }
    review = {"headers": RESULT_HEADERS, "count": len(reviews), "items": reviews, "warnings": warnings}
    google_payload = {"range": config["spreadsheet"]["result_range"], "values": rows}
    checkpoint = {
        "job_date": job_date.isoformat(),
        "phase": "local_processed",
        "intermediate_saved": False,
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
