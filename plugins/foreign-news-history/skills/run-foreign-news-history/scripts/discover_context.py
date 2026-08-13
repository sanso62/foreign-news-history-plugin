#!/usr/bin/env python3
"""Collect current-run evidence without assigning people, teams, or categories.

Codex reviews this draft together with schedule/source evidence and writes the final
run_context.json.  No previous-day personnel mapping is reused.
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="현재 입력 파일의 실행 컨텍스트 근거 수집")
    parser.add_argument("--morning-dir", required=True)
    parser.add_argument("--afternoon-dir", required=True)
    parser.add_argument("--final-report", required=True)
    parser.add_argument("--source-json", required=True)
    parser.add_argument("--schedule-json", required=True, help="작업일 요일을 선택한 동향 스케줄 근거 JSON")
    parser.add_argument("--japan-input", required=True, help="현재 작업일의 일본언론동향 원본 정확한 경로")
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def document_signal(path: Path, root: Path | None = None) -> dict[str, Any]:
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
    return {
        "path": str(path.resolve()),
        "relative_path": relative,
        "filename": path.name,
        "filename_tokens": [token for token in re.split(r"[_\-\s()]+", path.stem) if token],
        "sha256": sha256_file(path),
        "size": path.stat().st_size,
        "article_heading_count": article_count,
        "document_preview": preview,
        "read_error": error,
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
    japan_files = list(iter_document_files(japan))
    all_inputs = [final_report, source_json, schedule_json, *work_files]
    all_inputs.extend(japan_files)
    if japan.suffix.lower() == ".json":
        all_inputs.append(japan)

    job_date, job_evidence = document_job_date(final_report)
    schedule = json.loads(schedule_json.read_text(encoding="utf-8-sig"))
    schedule_job_date = clean_text(schedule.get("job_date"))
    if job_date and schedule_job_date != job_date.isoformat():
        raise ValueError("동향 스케줄 근거의 작업일이 최종보고서 작업일과 다릅니다.")
    if not schedule.get("assignments"):
        raise ValueError("동향 스케줄 근거에 작업일 담당자 행이 없습니다.")
    try:
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
    except Exception:
        final_articles = []
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
        "final": {
            "workgroup": "",
            "owner": "",
            "worker": "",
            "confidence": "unresolved",
            "evidence": [],
            "schedule_refs": [],
        },
        "final_disposition": {
            "not_representative_owner": "",
            "confidence": "unresolved",
            "evidence": [],
        },
        "sources": {
            "regular": {
                "workgroup": "",
                "owner": "",
                "worker": "",
                "priority": 0,
                "confidence": "unresolved",
                "evidence": [],
                "schedule_refs": [],
            },
            "japan": {
                "status": "present_checked",
                "workgroup": "",
                "owner": "",
                "worker": "",
                "priority": 0,
                "confidence": "unresolved",
                "evidence": [
                    "사용자가 명시한 같은 작업일의 일본언론동향 원본을 확인함: " + str(japan)
                ],
                "schedule_refs": [],
            },
        },
        "origin_policy": {
            "source_order": [],
            "confidence": "unresolved",
            "evidence": [],
        },
        "final_report_signal": document_signal(final_report, final_report.parent),
        "japan_input": {
            "status": "present_checked",
            "path": str(japan),
            "files": [document_signal(path, japan.parent) for path in japan_files],
        },
        "final_articles": final_articles,
        "files": [document_signal(path, morning if path.is_relative_to(morning) else afternoon) for path in work_files],
        "article_overrides": [],
        "article_japan_confirmations": [],
        "decision_notes": [
            "Codex가 현재 실행의 파일·문서·스케줄 근거만 사용해 빈 필드를 채운다.",
            "작업조·초벌 담당·초벌 작업자·최종 담당·최종 작업자는 schedule_refs로 근무 시트의 당일 요일 행을 인용한다.",
            "이전 실행의 사람·조 매핑을 복사하지 않는다.",
            "일본언론동향 원본 수록 기사만 일일일본동향 O로 판정하며, 제목이 크게 바뀐 동일 기사는 article_japan_confirmations에 현재 원문 대조 근거를 기록한다.",
            "근거가 없으면 빈 값과 unresolved를 유지한다.",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "files": len(work_files), "job_date": draft["job_date"]["value"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
