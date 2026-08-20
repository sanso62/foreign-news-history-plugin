#!/usr/bin/env python3
"""Turn a same-day authoritative result comparison into current-run confirmations."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from process_job import (
    RESULT_HEADERS,
    clean_text,
    media_similarity,
    normalize_key,
    parse_date,
    parse_document,
    profile_fields,
    rows_from_json,
    sha256_file,
    text_similarity,
)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def values_from_reference(payload: dict[str, Any]) -> list[list[Any]]:
    values = payload.get("values")
    if not isinstance(values, list) or not values:
        raise ValueError("기준표 JSON에 values 배열이 없습니다.")
    headers = [clean_text(value) for value in values[0]]
    if headers != RESULT_HEADERS:
        raise ValueError("기준표 JSON의 A:O 헤더가 결과 스키마와 다릅니다.")
    return [list(row) + [None] * (len(RESULT_HEADERS) - len(row)) for row in values[1:]]


def best_reference_index(row: list[Any], references: list[list[Any]], unused: set[int]) -> tuple[int | None, float]:
    ranked: list[tuple[float, int]] = []
    for index in unused:
        reference = references[index]
        title_score = text_similarity(clean_text(row[10]), clean_text(reference[10]))
        media_score = media_similarity(clean_text(row[8]), clean_text(reference[8]))
        flags_score = 1.0 if [clean_text(value) for value in row[11:13]] == [clean_text(value) for value in reference[11:13]] else 0.0
        ranked.append((0.86 * title_score + 0.09 * media_score + 0.05 * flags_score, index))
    if not ranked:
        return None, 0.0
    score, index = max(ranked)
    if score >= 0.68:
        return index, score

    # A final title can be completely rewritten.  In that case accept a single
    # structural anchor only when category, outlet, date, and inclusion flags all
    # agree.  This prevents the low title score from leaving the exact row unused.
    row_media_key = normalize_key(row[8])
    anchored: list[int] = []
    for candidate_index in unused:
        reference = references[candidate_index]
        reference_media_key = normalize_key(reference[8])
        same_media = bool(
            row_media_key
            and reference_media_key
            and (
                row_media_key in reference_media_key
                or reference_media_key in row_media_key
            )
        )
        same_category = normalize_key(row[7]) == normalize_key(reference[7])
        same_date = normalize_key(row[9]) == normalize_key(reference[9])
        same_flags = [clean_text(value) for value in row[11:13]] == [
            clean_text(value) for value in reference[11:13]
        ]
        if same_media and same_category and same_date and same_flags:
            anchored.append(candidate_index)
    return (anchored[0], score) if len(anchored) == 1 else (None, score)


IDENTITY_FIELDS = ("article_title", "article_media", "reference_title", "reference_media")


def article_identity_fields(row: list[Any], reference: list[Any]) -> dict[str, str]:
    """Bind a judgment to an article even when several rows share one order."""
    return {
        "article_title": clean_text(row[10]),
        "article_media": clean_text(row[8]),
        "reference_title": clean_text(reference[10]),
        "reference_media": clean_text(reference[8]),
    }


def context_item_identity(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        item.get("order"),
        *(normalize_key(item.get(field)) for field in IDENTITY_FIELDS),
    )


def merge_by_order(existing: list[dict[str, Any]], generated: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge article judgments without collapsing distinct rows with one order."""
    generated_keys = {context_item_identity(item) for item in generated}
    generated_orders = {item.get("order") for item in generated}
    kept = []
    for item in existing:
        has_identity = any(clean_text(item.get(field)) for field in IDENTITY_FIELDS)
        if context_item_identity(item) in generated_keys:
            continue
        if not has_identity and item.get("order") in generated_orders:
            # Replace an older order-only judgment with current article-scoped ones.
            continue
        kept.append(item)
    return sorted(
        [*kept, *generated],
        key=lambda item: (int(item["order"]), *context_item_identity(item)[1:]),
    )


def consume_existing_omitted_reference(
    row: list[Any],
    detail: dict[str, Any],
    references: list[list[Any]],
    unused: set[int],
    matches: list[dict[str, Any]],
) -> int | None:
    """Consume an omitted row already emitted from current inputs.

    These rows are not ordinary final-article matches and must not affect result
    ordering, but leaving their reference rows unused would create duplicate
    confirmed additions at the end of the result.
    """
    reference_index, score = best_reference_index(row, references, unused)
    if reference_index is None:
        return None
    unused.remove(reference_index)
    matches.append(
        {
            "order": detail.get("order"),
            "result_title": clean_text(row[10]),
            "reference_row": reference_index + 2,
            "reference_title": clean_text(references[reference_index][10]),
            "score": round(score, 4),
            "existing_omitted": True,
        }
    )
    return reference_index


def consume_existing_omitted_rows(
    rows_and_details: list[tuple[list[Any], dict[str, Any]]],
    references: list[list[Any]],
    unused: set[int],
    matches: list[dict[str, Any]],
) -> bool:
    """Keep automatic omitted rows only when their complete order is authoritative."""
    if not rows_and_details:
        return True
    proposed_unused = set(unused)
    proposed_matches: list[dict[str, Any]] = []
    reference_indices: list[int] = []
    for row, detail in rows_and_details:
        reference_index = consume_existing_omitted_reference(
            row,
            detail,
            references,
            proposed_unused,
            proposed_matches,
        )
        if reference_index is None:
            return False
        reference_indices.append(reference_index)
    if reference_indices != sorted(reference_indices):
        return False
    unused.clear()
    unused.update(proposed_unused)
    matches.extend(proposed_matches)
    return True


def current_source_articles(context: dict[str, Any], source_json: Path) -> list[dict[str, Any]]:
    """Collect exact current-run candidates for reference-only missing rows."""
    job_value = clean_text((context.get("job_date") or {}).get("value"))
    target_date = dt.date.fromisoformat(job_value) - dt.timedelta(days=1)
    records: list[dict[str, Any]] = []
    regular_profile = (context.get("sources") or {}).get("regular") or {}
    regular_group, regular_owner, regular_worker = profile_fields(regular_profile)
    for row in rows_from_json(source_json):
        if parse_date(row.get("보도일")) != target_date:
            continue
        title = clean_text(row.get("제목 (한글)"))
        if title:
            records.append({
                "source_type": "regular",
                "source_file": str(source_json.resolve()),
                "source_title": title,
                "media": clean_text(row.get("매체명 (원어)")) or clean_text(row.get("매체명 (한글)")),
                "date": f"{target_date.month}.{target_date.day}",
                "category": "",
                "workgroup": regular_group,
                "owner": regular_owner,
                "worker": regular_worker,
            })

    for profile in context.get("files", []):
        path_value = clean_text(profile.get("path"))
        if not path_value:
            continue
        path = Path(path_value)
        if not path.is_file():
            continue
        workgroup, owner, worker = profile_fields(profile)
        for article in parse_document(path):
            records.append({
                "source_type": "worker",
                "source_file": str(path.resolve()),
                "source_title": article.canonical_title or article.body_title,
                "media": article.media,
                "date": article.date,
                "category": article.category,
                "workgroup": workgroup,
                "owner": owner,
                "worker": worker,
            })

    japan_profile = (context.get("sources") or {}).get("japan") or {}
    japan_group, japan_owner, japan_worker = profile_fields(japan_profile)
    japan_input = context.get("japan_input") or {}
    japan_items = list(japan_input.get("files", []))
    if clean_text(japan_input.get("path")):
        japan_items.append({"path": japan_input.get("path")})
    seen_japan_paths: set[str] = set()
    for item in japan_items:
        path_value = clean_text(item.get("path"))
        if not path_value or not Path(path_value).is_file():
            continue
        path = Path(path_value)
        resolved_path = str(path.resolve())
        if resolved_path in seen_japan_paths:
            continue
        seen_japan_paths.add(resolved_path)
        for article in parse_document(path):
            records.append({
                "source_type": "japan",
                "source_file": resolved_path,
                "source_title": article.canonical_title or article.body_title,
                "media": article.media,
                "date": article.date,
                "category": article.category,
                "workgroup": japan_group,
                "owner": japan_owner,
                "worker": japan_worker,
            })
    return records


def reference_origin_candidate(
    reference: list[Any],
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Re-resolve provenance from the authoritative role columns and current inputs.

    The first-pass result can contain the very provenance error that the reference
    is meant to correct.  Therefore its selected origin is not reused here.  Role
    fields narrow the live candidates first, then title/media/date identify the
    article without a person, outlet, or country lookup table.
    """
    expected_group = clean_text(reference[2])
    expected_owner = clean_text(reference[3])
    expected_worker = clean_text(reference[4])
    expected_media = clean_text(reference[8])
    expected_date = clean_text(reference[9])
    expected_title = clean_text(reference[10])

    selected = list(candidates)
    if expected_group:
        group_matches = [
            item for item in selected
            if normalize_key(item.get("workgroup")) == normalize_key(expected_group)
        ]
        if not group_matches:
            # Older run_context files left the special-source workgroup blank.
            # The reference's Japan-membership flag permits only the current
            # Japan input as a compatibility fallback; it never relabels a
            # regular/worker candidate or overrides an existing group match.
            group_matches = [
                item for item in selected
                if clean_text(reference[13]) == "O"
                and clean_text(item.get("source_type")) == "japan"
                and not clean_text(item.get("workgroup"))
            ]
        if not group_matches:
            return None
        selected = group_matches
    for key, expected in (("owner", expected_owner), ("worker", expected_worker)):
        if not expected:
            continue
        role_matches = [
            item for item in selected
            if normalize_key(item.get(key)) == normalize_key(expected)
        ]
        if role_matches:
            selected = role_matches
    if not selected:
        return None

    ranked: list[tuple[float, float, float, bool, bool, dict[str, Any]]] = []
    for item in selected:
        title_score = text_similarity(expected_title, clean_text(item.get("source_title")))
        candidate_media = clean_text(item.get("media"))
        expected_media_key = normalize_key(expected_media)
        candidate_media_key = normalize_key(candidate_media)
        strong_media = bool(
            expected_media_key
            and candidate_media_key
            and (
                expected_media_key in candidate_media_key
                or candidate_media_key in expected_media_key
            )
        )
        media_score = 1.0 if strong_media else media_similarity(expected_media, candidate_media)
        same_date = bool(
            expected_date
            and clean_text(item.get("date"))
            and normalize_key(expected_date) == normalize_key(item.get("date"))
        )
        score = 0.72 * title_score + 0.20 * media_score + 0.08 * float(same_date)
        ranked.append((score, title_score, media_score, same_date, strong_media, item))
    ranked.sort(key=lambda value: (value[0], value[1], value[2]), reverse=True)
    best_score, title_score, media_score, same_date, strong_media, best = ranked[0]

    if title_score == 1.0:
        return best
    strong_media_evidence = [item for item in ranked if item[4]]
    if len(strong_media_evidence) == 1:
        media_anchor = strong_media_evidence[0]
        if media_anchor[3] or media_anchor[1] >= 0.25:
            return media_anchor[5]
    if best_score >= 0.68 and (
        len(ranked) == 1 or best_score - ranked[1][0] >= 0.05
    ):
        return best
    return None


def missing_reference_additions(
    references: list[list[Any]],
    unused: set[int],
    matched_orders: list[tuple[int, int]],
    candidates: list[dict[str, Any]],
    reference_file: Path,
    reference_hash: str,
) -> tuple[list[dict[str, Any]], set[int]]:
    additions: list[dict[str, Any]] = []
    consumed: set[int] = set()
    for reference_index in sorted(unused):
        reference = references[reference_index]
        included = clean_text(reference[11])
        similar = clean_text(reference[12])
        kind = "similar" if included == "X" and similar == "O" else "omitted" if included == "X" and similar == "X" else ""
        if not kind:
            continue
        title_key = normalize_key(reference[10])
        exact = [item for item in candidates if normalize_key(item.get("source_title")) == title_key]
        if len(exact) > 1:
            role_matches = [
                item for item in exact
                if clean_text(item.get("workgroup")) == clean_text(reference[2])
                and clean_text(item.get("owner")) == clean_text(reference[3])
                and clean_text(item.get("worker")) == clean_text(reference[4])
            ]
            exact = role_matches or exact
        if len(exact) > 1:
            media_matches = [
                item for item in exact
                if media_similarity(clean_text(item.get("media")), clean_text(reference[8])) >= 0.68
            ]
            exact = media_matches or exact
        if len(exact) != 1:
            continue
        candidate = exact[0]
        after_order = None
        if kind == "similar":
            prior = [order for matched_index, order in matched_orders if matched_index < reference_index]
            if not prior:
                continue
            after_order = prior[-1]
        additions.append({
            "kind": kind,
            "after_order": after_order,
            "source_type": candidate["source_type"],
            "source_file": candidate["source_file"],
            "source_title": candidate["source_title"],
            "category": clean_text(reference[7]) or clean_text(candidate.get("category")),
            "media": clean_text(reference[8]) or clean_text(candidate.get("media")),
            "date": clean_text(reference[9]) or clean_text(candidate.get("date")),
            "canonical_title": clean_text(reference[10]),
            "reference_file": str(reference_file),
            "reference_sha256": reference_hash,
            "evidence": [
                f"같은 작업일 권위 기준표 {reference_index + 2}행의 누락 기사와 현재 원본 제목을 정확히 대조"
            ],
        })
        consumed.add(reference_index)
    return additions, consumed


def main() -> int:
    parser = argparse.ArgumentParser(description="권위 있는 같은 작업일 기준표 피드백을 실행 컨텍스트에 반영")
    parser.add_argument("--run-context", required=True)
    parser.add_argument("--result-json", required=True)
    parser.add_argument("--reference-json", required=True)
    parser.add_argument("--reference-file", required=True)
    parser.add_argument("--source-json", required=True)
    parser.add_argument("--schedule-json", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--audit-output", required=True)
    args = parser.parse_args()

    context_path = Path(args.run_context).resolve()
    result_path = Path(args.result_json).resolve()
    reference_json_path = Path(args.reference_json).resolve()
    reference_file = Path(args.reference_file).resolve()
    source_json = Path(args.source_json).resolve()
    schedule_path = Path(args.schedule_json).resolve()
    for path in (context_path, result_path, reference_json_path, reference_file, source_json, schedule_path):
        if not path.is_file():
            raise FileNotFoundError(path)

    context = read_json(context_path)
    result = read_json(result_path)
    references = values_from_reference(read_json(reference_json_path))
    schedule = read_json(schedule_path)
    worker_refs = {
        clean_text(item.get("worker")): clean_text(item.get("ref"))
        for item in schedule.get("assignments", [])
        if clean_text(item.get("worker")) and clean_text(item.get("ref"))
    }
    reference_hash = sha256_file(reference_file)
    source_articles = current_source_articles(context, source_json)
    unused = set(range(len(references)))
    role_confirmations: list[dict[str, Any]] = []
    origin_confirmations: list[dict[str, Any]] = []
    japan_confirmations: list[dict[str, Any]] = []
    overrides: list[dict[str, Any]] = []
    matches: list[dict[str, Any]] = []
    matched_orders: list[tuple[int, int]] = []
    profile_votes: dict[tuple[str, str], list[tuple[str, str, str, str, int]]] = defaultdict(list)
    final_votes: list[tuple[str, str, str, int]] = []
    disposition_votes: list[tuple[str, int]] = []
    existing_omitted_rows: list[tuple[list[Any], dict[str, Any]]] = []

    for row, detail in zip(result.get("rows", []), result.get("matches", [])):
        if detail.get("omitted_from_final"):
            existing_omitted_rows.append((row, detail))
            continue
        if detail.get("confirmed_addition"):
            reference_index, score = best_reference_index(row, references, unused)
            if reference_index is not None:
                unused.remove(reference_index)
                matches.append({
                    "order": detail.get("order"),
                    "result_title": clean_text(row[10]),
                    "reference_row": reference_index + 2,
                    "reference_title": clean_text(references[reference_index][10]),
                    "score": round(score, 4),
                })
            continue
        order = detail.get("order")
        if not isinstance(order, int):
            continue
        reference_index, score = best_reference_index(row, references, unused)
        if reference_index is None:
            continue
        unused.remove(reference_index)
        reference = references[reference_index]
        identity = article_identity_fields(row, reference)
        worker = clean_text(reference[4])
        schedule_ref = worker_refs.get(worker)
        evidence = [f"같은 작업일 권위 기준표 {reference_index + 2}행과 현재 결과·원본을 대조"]
        resolved_origin = reference_origin_candidate(reference, source_articles)
        expected_japan = clean_text(reference[13]) == "O"
        japan_candidate = None
        if expected_japan:
            japan_reference = list(reference)
            japan_reference[2:5] = ["", "", ""]
            japan_candidate = reference_origin_candidate(
                japan_reference,
                [
                    item for item in source_articles
                    if clean_text(item.get("source_type")) == "japan"
                ],
            )
        japan_confirmation = {
            "order": order,
            **identity,
            "included": expected_japan,
            "reference_file": str(reference_file),
            "reference_sha256": reference_hash,
            "evidence": [
                f"같은 작업일 권위 기준표 {reference_index + 2}행의 일일일본동향 열과 현재 일본동향 원본을 대조"
            ],
        }
        if japan_candidate:
            japan_confirmation.update(
                {
                    "source_file": clean_text(japan_candidate.get("source_file")),
                    "source_title": clean_text(japan_candidate.get("source_title")),
                }
            )
        if not expected_japan or japan_candidate:
            japan_confirmations.append(japan_confirmation)
        if schedule_ref:
            role_confirmations.append(
                {
                    "order": order,
                    **identity,
                    "workgroup": clean_text(reference[2]),
                    "owner": clean_text(reference[3]),
                    "worker": worker,
                    "schedule_refs": [schedule_ref],
                    "reference_file": str(reference_file),
                    "reference_sha256": reference_hash,
                    "evidence": evidence,
                }
            )
            if resolved_origin:
                profile_votes[(
                    clean_text(resolved_origin.get("source_type")),
                    clean_text(resolved_origin.get("source_file")),
                )].append(
                    (clean_text(reference[2]), clean_text(reference[3]), worker, schedule_ref, reference_index + 2)
                )
        if clean_text(reference[11]) == "O":
            final_worker = clean_text(reference[6])
            final_ref = worker_refs.get(final_worker)
            if final_ref:
                final_votes.append((clean_text(reference[5]), final_worker, final_ref, reference_index + 2))
        elif clean_text(reference[5]):
            disposition_votes.append((clean_text(reference[5]), reference_index + 2))
        if resolved_origin:
            origin_confirmations.append(
                {
                    "order": order,
                    **identity,
                    "source_type": clean_text(resolved_origin.get("source_type")),
                    "source_file": clean_text(resolved_origin.get("source_file")),
                    "source_title": clean_text(resolved_origin.get("source_title")),
                    "evidence": [
                        *evidence,
                        "권위 기준표의 작업조·초벌 담당·초벌 작업자와 현재 입력 후보를 다시 대조",
                    ],
                }
            )
        for field_name, column in (("category", 7), ("media", 8), ("date", 9), ("canonical_title", 10)):
            expected = clean_text(reference[column])
            if expected and clean_text(row[column]) != expected:
                overrides.append(
                    {
                        "order": order,
                        **identity,
                        "field": field_name,
                        "value": expected,
                        "evidence": evidence,
                    }
                )
        matches.append(
            {
                "order": order,
                "result_title": clean_text(row[10]),
                "reference_row": reference_index + 2,
                "reference_title": clean_text(reference[10]),
                "score": round(score, 4),
            }
        )
        matched_orders.append((reference_index, order))

    consume_existing_omitted_rows(
        existing_omitted_rows,
        references,
        unused,
        matches,
    )

    context["article_role_confirmations"] = merge_by_order(
        context.get("article_role_confirmations", []),
        role_confirmations,
    )
    context["article_origin_confirmations"] = merge_by_order(
        context.get("article_origin_confirmations", []),
        origin_confirmations,
    )
    context["article_japan_confirmations"] = merge_by_order(
        context.get("article_japan_confirmations", []),
        japan_confirmations,
    )
    replaced_override_keys = {
        (item["order"], item["field"], *context_item_identity(item)[1:])
        for item in overrides
    }
    generated_override_orders = {
        (item["order"], item["field"])
        for item in overrides
    }
    context["article_overrides"] = [
        item
        for item in context.get("article_overrides", [])
        if (
            (
                item.get("order"),
                item.get("field"),
                *context_item_identity(item)[1:],
            ) not in replaced_override_keys
            and not (
                not any(clean_text(item.get(field)) for field in IDENTITY_FIELDS)
                and (item.get("order"), item.get("field")) in generated_override_orders
            )
        )
    ] + overrides
    if len(matched_orders) == len(result.get("articles", [])):
        context["result_order"] = {
            "orders": [order for _, order in sorted(matched_orders)],
            "reference_file": str(reference_file),
            "reference_sha256": reference_hash,
            "evidence": ["같은 작업일 권위 기준표의 기사 행 순서를 현재 최종 기사와 제목·매체로 대조"],
        }

    additions, consumed_additions = missing_reference_additions(
        references,
        unused,
        matched_orders,
        source_articles,
        reference_file,
        reference_hash,
    )
    unused -= consumed_additions
    existing_additions = [
        item for item in context.get("article_additions", [])
        if not (
            clean_text(item.get("reference_file")) == str(reference_file)
            and normalize_key(item.get("canonical_title")) in {
                normalize_key(generated.get("canonical_title")) for generated in additions
            }
        )
    ]
    context["article_additions"] = [*existing_additions, *additions]

    def winning_profile(votes: list[tuple[str, str, str, str, int]]) -> tuple[str, str, str, str, int] | None:
        if not votes:
            return None
        triple = Counter((item[0], item[1], item[2], item[3]) for item in votes).most_common(1)[0][0]
        return next(item for item in votes if item[:4] == triple)

    for (source_type, source_file), votes in profile_votes.items():
        winner = winning_profile(votes)
        if not winner:
            continue
        workgroup, owner, worker, schedule_ref, reference_row = winner
        profile = {
            "workgroup": workgroup,
            "owner": owner,
            "worker": worker,
            "confidence": "confirmed",
            "evidence": [f"같은 작업일 권위 기준표 {reference_row}행과 현재 유입 원본을 대조"],
            "schedule_refs": [schedule_ref],
        }
        if source_type == "regular":
            current = (context.get("sources") or {}).get(source_type) or {}
            current.update(profile)
            context.setdefault("sources", {})[source_type] = current
    if final_votes:
        final_key = Counter(item[:3] for item in final_votes).most_common(1)[0][0]
        final_owner, final_worker, final_ref, reference_row = next(
            item for item in final_votes if item[:3] == final_key
        )
        current_final = context.get("final") or {}
        current_final.update(
            {
                "owner": final_owner,
                "worker": final_worker,
                "confidence": "confirmed",
                "evidence": [f"같은 작업일 권위 기준표 {reference_row}행의 대표 기사 최종 단계와 현재 총괄 원본을 대조"],
                "schedule_refs": [final_ref],
            }
        )
        context["final"] = current_final
    if disposition_votes:
        disposition = Counter(item[0] for item in disposition_votes).most_common(1)[0][0]
        reference_row = next(item[1] for item in disposition_votes if item[0] == disposition)
        context["final_disposition"] = {
            "not_representative_owner": disposition,
            "confidence": "confirmed",
            "evidence": [f"같은 작업일 권위 기준표 {reference_row}행의 비대표·미포함 표기를 확인"],
        }

    output = Path(args.output).resolve()
    audit_output = Path(args.audit_output).resolve()
    output.write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8")
    audit = {
        "reference_file": str(reference_file),
        "reference_sha256": reference_hash,
        "matched_rows": matches,
        "unmatched_reference_rows": [
            {"row": index + 2, "values": references[index]}
            for index in sorted(unused)
        ],
        "role_confirmations": len(role_confirmations),
        "origin_confirmations": len(origin_confirmations),
        "japan_confirmations": len(japan_confirmations),
        "article_overrides": len(overrides),
        "article_additions": len(additions),
    }
    audit_output.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), **{key: audit[key] for key in ("role_confirmations", "origin_confirmations", "japan_confirmations", "article_overrides", "article_additions")}, "unmatched": len(unused)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
