#!/usr/bin/env python3
"""Collect current-run evidence and apply the documented file-stage role rules.

Role labels come from the workflow prompt.  Worker names are never hard-coded: a
name is accepted only when the current filename and the same-day schedule row agree.
No previous-day personnel mapping is reused.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from process_job import (
    ARTICLE_HEADING,
    clean_text,
    document_job_date,
    extract_paragraphs,
    iter_document_files,
    parse_document,
    resolve_japan_input,
    run_fingerprint,
    sha256_file,
)


AGGREGATE_REVISION = re.compile(r"(?:^|[ _-])(?:\d+|[nN])차(?:$|[ _-])|최종")


def marker(value: Any) -> str:
    """Normalize a short filename/schedule marker for containment checks."""
    return re.sub(r"[^0-9A-Za-z가-힣]", "", clean_text(value)).casefold()


def schedule_assignment_matches(source_kind: str, assignment: dict[str, Any]) -> bool:
    report = marker(assignment.get("report"))
    division = marker(assignment.get("division"))
    if source_kind == "global_draft":
        return "글로벌" in report and division == "오후"
    if source_kind == "domestic_draft":
        return ("한국관련" in report or "국내" in report) and division == "오후"
    if source_kind in {"afternoon_aggregate", "morning_aggregate"}:
        return "일일동향" in report and division == "총괄"
    if source_kind == "morning_auxiliary":
        return "일일동향" in report and division in {"새벽", "보조"}
    return False


def infer_file_profile(
    path: Path,
    comparison_stage: str,
    schedule: dict[str, Any],
) -> dict[str, Any]:
    """Infer stage labels and validate the filename worker against today's schedule."""
    stem = clean_text(path.stem)
    stem_marker = marker(stem)
    evidence: list[str] = []
    if comparison_stage == "afternoon":
        if "글로벌이슈" in stem_marker or "글로벌동향" in stem_marker:
            source_kind, workgroup, owner, include_unmatched, priority = (
                "global_draft", "1조", "글로벌", True, 100
            )
            evidence.append("프롬프트 역할 기준: 오후 글로벌 이슈 작업본 → 1조/글로벌")
        elif "취합" in stem_marker:
            source_kind, workgroup, owner, include_unmatched, priority = (
                "afternoon_aggregate", "오후", "오후/총괄", False, 10
            )
            evidence.append("프롬프트 역할 기준: 오후 취합본 → 오후/오후/총괄")
        else:
            source_kind, workgroup, owner, include_unmatched, priority = (
                "domestic_draft", "1조", "국내", True, 100
            )
            evidence.append("프롬프트 역할 기준: 오후 개별 국내 초안 → 1조/국내")
    elif comparison_stage == "morning":
        if AGGREGATE_REVISION.search(stem):
            source_kind, workgroup, owner, include_unmatched, priority = (
                "morning_aggregate", "2조", "오전/총괄", False, 10
            )
            evidence.append("프롬프트 역할 기준: 오전 n차·최종 총괄본 → 2조/오전/총괄")
        else:
            source_kind, workgroup, owner, include_unmatched, priority = (
                "morning_auxiliary", "2조", "보조", True, 100
            )
            evidence.append("프롬프트 역할 기준: 오전 보조·초안 작업본 → 2조/보조")
    else:
        return {
            "source_kind": "",
            "workgroup": "",
            "owner": "",
            "worker": "",
            "priority": 0,
            "include_unmatched": False,
            "confidence": "unresolved",
            "evidence": [],
            "schedule_refs": [],
        }

    if "순방" in stem_marker:
        workgroup = "순방"
        evidence.append("프롬프트 특수 유입 기준: 파일명에 순방 업무가 명시됨 → 작업조 순방")

    matches: list[dict[str, Any]] = []
    for assignment in schedule.get("assignments", []):
        if not isinstance(assignment, dict) or not schedule_assignment_matches(source_kind, assignment):
            continue
        worker = clean_text(assignment.get("worker"))
        if worker and marker(worker) in stem_marker:
            matches.append(assignment)

    worker = ""
    schedule_refs: list[str] = []
    confidence = "unresolved"
    if len(matches) == 1:
        assignment = matches[0]
        worker = clean_text(assignment.get("worker"))
        ref = clean_text(assignment.get("ref"))
        schedule_refs = [ref] if ref else []
        evidence.extend([
            f"파일명에 작업자 '{worker}'가 명시됨: {path.name}",
            f"{ref}: {clean_text(assignment.get('report'))}/{clean_text(assignment.get('division'))} 담당자 {worker}",
        ])
        confidence = "confirmed" if schedule_refs else "unresolved"
    elif len(matches) > 1:
        evidence.append("파일명 작업자와 일치하는 당일 근무 행이 둘 이상이어서 작업자 확인 필요")
    else:
        evidence.append("파일명과 해당 역할의 당일 근무 행에서 같은 작업자를 확인하지 못함")

    return {
        "source_kind": source_kind,
        "workgroup": workgroup,
        "owner": owner,
        "worker": worker,
        "priority": priority,
        "include_unmatched": include_unmatched,
        "confidence": confidence,
        "evidence": evidence,
        "schedule_refs": schedule_refs,
    }


def aggregate_profile(
    files: list[dict[str, Any]],
    source_kind: str,
    workgroup: str,
    owner: str,
    rule_evidence: str,
) -> dict[str, Any]:
    """Build a regular/final profile from uniquely identified aggregate workers."""
    matches = [
        item for item in files
        if item.get("source_kind") == source_kind
        and clean_text(item.get("worker"))
        and item.get("schedule_refs")
    ]
    workers = {clean_text(item.get("worker")) for item in matches}
    if len(workers) != 1:
        return {
            "workgroup": workgroup,
            "owner": owner,
            "worker": "",
            "priority": 0,
            "confidence": "unresolved",
            "evidence": [rule_evidence, "해당 총괄 파일의 당일 작업자를 하나로 확정하지 못함"],
            "schedule_refs": [],
        }
    worker = next(iter(workers))
    refs = list(dict.fromkeys(
        clean_text(ref)
        for item in matches
        for ref in item.get("schedule_refs", [])
        if clean_text(ref)
    ))
    filenames = ", ".join(item.get("filename", "") for item in matches)
    return {
        "workgroup": workgroup,
        "owner": owner,
        "worker": worker,
        "priority": 0,
        "confidence": "confirmed" if refs else "unresolved",
        "evidence": [rule_evidence, f"총괄 파일과 당일 근무표 대조: {filenames}"],
        "schedule_refs": refs,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="현재 입력 파일의 실행 컨텍스트 근거 수집")
    parser.add_argument("--morning-dir", required=True)
    parser.add_argument("--afternoon-dir", required=True)
    parser.add_argument("--final-report", required=True)
    parser.add_argument("--source-json", required=True)
    parser.add_argument("--schedule-json", required=True, help="작업일 요일을 선택한 동향 스케줄 근거 JSON")
    parser.add_argument(
        "--japan-input",
        help="선택 입력. 제공하는 경우 현재 작업일의 일본언론동향 원본 정확한 경로",
    )
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def document_signal(
    path: Path,
    root: Path | None = None,
    comparison_stage: str = "",
    schedule: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        paragraphs = extract_paragraphs(path)
        preview = paragraphs[:20]
        article_count = sum(1 for paragraph in paragraphs if ARTICLE_HEADING.match(paragraph))
    except Exception as exc:
        preview = []
        article_count = 0
        error = str(exc)
    else:
        error = ""
    relative = str(path.relative_to(root)) if root and path.is_relative_to(root) else path.name
    signal = {
        "path": str(path.resolve()),
        "relative_path": relative,
        "filename": path.name,
        "filename_tokens": [token for token in re.split(r"[_\-\s()]+", path.stem) if token],
        "sha256": sha256_file(path),
        "size": path.stat().st_size,
        "article_heading_count": article_count,
        "document_preview": preview,
        "read_error": error,
        "comparison_stage": comparison_stage,
        "source_kind": "",
        "workgroup": "",
        "owner": "",
        "worker": "",
        "priority": 0,
        "include_unmatched": False,
        "confidence": "unresolved",
        "evidence": [],
        "schedule_refs": [],
    }
    if comparison_stage and schedule:
        signal.update(infer_file_profile(path, comparison_stage, schedule))
    return signal


def raise_for_unreadable_documents(
    signals: list[dict[str, Any]],
    label: str,
    require_articles: bool = True,
) -> None:
    """Stop before a partial context can turn a missing source into a wrong role."""
    failures: list[str] = []
    for signal in signals:
        filename = clean_text(signal.get("filename")) or clean_text(signal.get("path"))
        read_error = clean_text(signal.get("read_error"))
        if read_error:
            failures.append(f"{filename}: {read_error}")
            continue
        if require_articles and int(signal.get("article_heading_count") or 0) <= 0:
            failures.append(f"{filename}: 기사 제목 0건")
    if failures:
        raise ValueError(f"{label} 파싱 실패: " + "; ".join(failures))


def main() -> int:
    args = parse_args()
    morning = Path(args.morning_dir).resolve()
    afternoon = Path(args.afternoon_dir).resolve()
    final_report = Path(args.final_report).resolve()
    source_json = Path(args.source_json).resolve()
    schedule_json = Path(args.schedule_json).resolve()
    japan = resolve_japan_input(final_report, args.japan_input)
    output = Path(args.output).resolve()

    if not morning.is_dir():
        raise NotADirectoryError(f"오전 작업 폴더 없음: {morning}")
    if not afternoon.is_dir():
        raise NotADirectoryError(f"오후 작업 폴더 없음: {afternoon}")
    if not final_report.is_file():
        raise FileNotFoundError(f"최종보고서 파일 없음: {final_report}")
    if not source_json.is_file():
        raise FileNotFoundError(f"정기 작업내역 JSON 없음: {source_json}")
    if not schedule_json.is_file():
        raise FileNotFoundError(f"동향 스케줄 JSON 없음: {schedule_json}")

    work_files = list(iter_document_files(morning)) + list(iter_document_files(afternoon))
    japan_files = list(iter_document_files(japan)) if japan else []
    all_inputs = [final_report, source_json, schedule_json, *work_files]
    all_inputs.extend(japan_files)
    if japan and japan.suffix.lower() == ".json":
        all_inputs.append(japan)

    job_date, job_evidence = document_job_date(final_report)
    schedule = json.loads(schedule_json.read_text(encoding="utf-8-sig"))
    schedule_job_date = clean_text(schedule.get("job_date"))
    if job_date and schedule_job_date != job_date.isoformat():
        raise ValueError("동향 스케줄 근거의 작업일이 최종보고서 작업일과 다릅니다.")
    if not schedule.get("assignments"):
        raise ValueError("동향 스케줄 근거에 작업일 담당자 행이 없습니다.")
    files = [
        document_signal(path, morning, "morning", schedule)
        if path.is_relative_to(morning)
        else document_signal(path, afternoon, "afternoon", schedule)
        for path in work_files
    ]
    final_report_signal = document_signal(final_report, final_report.parent)
    japan_signals = [document_signal(path, japan.parent) for path in japan_files] if japan else []
    raise_for_unreadable_documents([final_report_signal], "최종보고서")
    raise_for_unreadable_documents(files, "작업자 파일")
    if japan_files:
        raise_for_unreadable_documents(japan_signals, "일본언론동향")
    final_articles = [
        {
            "order": article.order,
            "category": article.category,
            "media": article.media,
            "date": article.date,
            "title": article.canonical_title or article.body_title,
            "similar": article.similar,
        }
        for article in parse_document(final_report)
    ]
    regular_profile = aggregate_profile(
        files,
        "afternoon_aggregate",
        "정기",
        "오후/총괄",
        "프롬프트 역할 기준: 정기 유입 → 정기/오후/총괄, 작업자는 당일 오후 취합 작업자",
    )
    final_profile = aggregate_profile(
        files,
        "morning_aggregate",
        "",
        "오전/총괄",
        "프롬프트 역할 기준: 최종 대표기사 → 오전/총괄, 작업자는 당일 오전 총괄 작업자",
    )
    if japan:
        japan_status = "present_checked"
        japan_evidence = "사용자가 명시한 같은 작업일의 일본언론동향 원본을 확인함: " + str(japan)
        japan_workgroup = "일본문화원"
        japan_confidence = "unresolved"
        japan_profile_evidence = [
            japan_evidence,
            "프롬프트 특수 유입 기준: 일본언론동향 → 작업조 일본문화원",
        ]
        japan_decision_note = (
            "일본언론동향 원본 수록 기사만 일일일본동향 O로 판정하며, 제목이 크게 바뀐 "
            "동일 기사는 article_japan_confirmations에 현재 원문 대조 근거를 기록한다."
        )
    else:
        japan_status = "not_provided"
        japan_evidence = "일본언론동향은 선택 입력이며 이번 실행에는 제공되지 않음"
        japan_workgroup = ""
        japan_confidence = "confirmed"
        japan_profile_evidence = [japan_evidence]
        japan_decision_note = (
            "일본언론동향이 제공되지 않아 일본동향 비교를 건너뛰고 일일일본동향 열을 공란으로 유지한다."
        )
    draft = {
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "input_fingerprint": run_fingerprint(all_inputs),
        "job_date": {
            "value": job_date.isoformat() if job_date else "",
            "confidence": "document-derived" if job_date else "unresolved",
            "evidence": [job_evidence] if job_evidence else [],
        },
        "schedule": {
            **schedule,
            "local_path": str(schedule_json),
            "sha256": sha256_file(schedule_json),
        },
        "final": final_profile,
        "final_disposition": {
            "not_representative_owner": "최종 보고서 미포함",
            "confidence": "confirmed",
            "evidence": ["프롬프트 역할 기준: 유사보도·완전 미포함 기사의 최종 담당 표기"],
        },
        "sources": {
            "regular": regular_profile,
            "japan": {
                "status": japan_status,
                "workgroup": japan_workgroup,
                "owner": "",
                "worker": "",
                "priority": 0,
                "confidence": japan_confidence,
                "evidence": japan_profile_evidence,
                "schedule_refs": [],
            },
        },
        "comparison_order": ["regular_and_japan", "afternoon", "morning"],
        "final_report_signal": final_report_signal,
        "japan_input": {
            "status": japan_status,
            "path": str(japan) if japan else "",
            "files": japan_signals,
        },
        "final_articles": final_articles,
        "files": files,
        "article_overrides": [],
        "article_japan_confirmations": [],
        "decision_notes": [
            "프롬프트에 명시된 파일 단계별 역할 표기를 적용하고, 작업자는 현재 파일명과 당일 근무표가 일치할 때만 확정한다.",
            "작업조·초벌 담당·초벌 작업자·최종 담당·최종 작업자는 schedule_refs로 근무 시트의 당일 요일 행을 인용한다.",
            "이전 실행의 사람·조 매핑을 복사하지 않는다.",
            japan_decision_note,
            "최종보고서 기사 유입 경로는 정기 작업내역·일본동향, 오후폴더, 오전폴더 순서로 비교한다. 정기와 일본동향이 겹치면 정기를 선택하고 일본동향 O는 유지한다.",
            "근거가 없으면 빈 값과 unresolved를 유지한다.",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "files": len(work_files), "job_date": draft["job_date"]["value"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
