#!/usr/bin/env python3
"""Turn a same-day authoritative result comparison into current-run confirmations."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from process_job import RESULT_HEADERS, clean_text, media_similarity, sha256_file, text_similarity


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
    return (index, score) if score >= 0.68 else (None, score)


def merge_by_order(existing: list[dict[str, Any]], generated: list[dict[str, Any]]) -> list[dict[str, Any]]:
    replacements = {item["order"]: item for item in generated}
    kept = [item for item in existing if item.get("order") not in replacements]
    return sorted([*kept, *generated], key=lambda item: int(item["order"]))


def main() -> int:
    parser = argparse.ArgumentParser(description="권위 있는 같은 작업일 기준표 피드백을 실행 컨텍스트에 반영")
    parser.add_argument("--run-context", required=True)
    parser.add_argument("--result-json", required=True)
    parser.add_argument("--reference-json", required=True)
    parser.add_argument("--reference-file", required=True)
    parser.add_argument("--schedule-json", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--audit-output", required=True)
    args = parser.parse_args()

    context_path = Path(args.run_context).resolve()
    result_path = Path(args.result_json).resolve()
    reference_json_path = Path(args.reference_json).resolve()
    reference_file = Path(args.reference_file).resolve()
    schedule_path = Path(args.schedule_json).resolve()
    for path in (context_path, result_path, reference_json_path, reference_file, schedule_path):
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
    unused = set(range(len(references)))
    role_confirmations: list[dict[str, Any]] = []
    origin_confirmations: list[dict[str, Any]] = []
    overrides: list[dict[str, Any]] = []
    matches: list[dict[str, Any]] = []
    matched_orders: list[tuple[int, int]] = []
    profile_votes: dict[tuple[str, str], list[tuple[str, str, str, str, int]]] = defaultdict(list)
    final_votes: list[tuple[str, str, str, int]] = []
    disposition_votes: list[tuple[str, int]] = []

    for row, detail in zip(result.get("rows", []), result.get("matches", [])):
        if detail.get("omitted_from_final") or detail.get("confirmed_addition"):
            continue
        order = detail.get("order")
        if not isinstance(order, int):
            continue
        reference_index, score = best_reference_index(row, references, unused)
        if reference_index is None:
            continue
        unused.remove(reference_index)
        reference = references[reference_index]
        worker = clean_text(reference[4])
        schedule_ref = worker_refs.get(worker)
        evidence = [f"같은 작업일 권위 기준표 {reference_index + 2}행과 현재 결과·원본을 대조"]
        if schedule_ref:
            role_confirmations.append(
                {
                    "order": order,
                    "workgroup": clean_text(reference[2]),
                    "owner": clean_text(reference[3]),
                    "worker": worker,
                    "schedule_refs": [schedule_ref],
                    "reference_file": str(reference_file),
                    "reference_sha256": reference_hash,
                    "evidence": evidence,
                }
            )
            profile_votes[(clean_text(detail.get("origin")), clean_text(detail.get("origin_file")))].append(
                (clean_text(reference[2]), clean_text(reference[3]), worker, schedule_ref, reference_index + 2)
            )
        if clean_text(reference[11]) == "O":
            final_worker = clean_text(reference[6])
            final_ref = worker_refs.get(final_worker)
            if final_ref:
                final_votes.append((clean_text(reference[5]), final_worker, final_ref, reference_index + 2))
        elif clean_text(reference[5]):
            disposition_votes.append((clean_text(reference[5]), reference_index + 2))
        if clean_text(detail.get("origin")) and clean_text(detail.get("origin_file")):
            origin_confirmations.append(
                {
                    "order": order,
                    "source_type": clean_text(detail.get("origin")),
                    "source_file": clean_text(detail.get("origin_file")),
                    "source_title": clean_text(row[10]),
                    "evidence": evidence,
                }
            )
        for field_name, column in (("category", 7), ("media", 8), ("date", 9), ("canonical_title", 10)):
            expected = clean_text(reference[column])
            if expected and clean_text(row[column]) != expected:
                overrides.append(
                    {
                        "order": order,
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

    context["article_role_confirmations"] = merge_by_order(
        context.get("article_role_confirmations", []),
        role_confirmations,
    )
    context["article_origin_confirmations"] = merge_by_order(
        context.get("article_origin_confirmations", []),
        origin_confirmations,
    )
    replaced_override_keys = {(item["order"], item["field"]) for item in overrides}
    context["article_overrides"] = [
        item
        for item in context.get("article_overrides", [])
        if (item.get("order"), item.get("field")) not in replaced_override_keys
    ] + overrides
    if len(matched_orders) == len(result.get("articles", [])):
        context["result_order"] = {
            "orders": [order for _, order in sorted(matched_orders)],
            "reference_file": str(reference_file),
            "reference_sha256": reference_hash,
            "evidence": ["같은 작업일 권위 기준표의 기사 행 순서를 현재 최종 기사와 제목·매체로 대조"],
        }

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
        if source_type in {"regular", "japan"}:
            current = (context.get("sources") or {}).get(source_type) or {}
            current.update(profile)
            context.setdefault("sources", {})[source_type] = current
        elif source_type == "worker" and source_file:
            expected_path = Path(source_file).resolve()
            for current in context.get("files", []):
                if current.get("path") and Path(current["path"]).resolve() == expected_path:
                    current.update(profile)
                    break
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
        "article_overrides": len(overrides),
    }
    audit_output.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), **{key: audit[key] for key in ("role_confirmations", "origin_confirmations", "article_overrides")}, "unmatched": len(unused)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
