#!/usr/bin/env python3
"""Select the current job date's weekday assignments from a live schedule export.

The script does not map people to fixed roles.  It only preserves the labels and
worker cells found in the connected Google Sheet so Codex can interpret them
together with the current input files.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any


WEEKDAY_LABELS = ("월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="동향 스케줄의 작업일 담당자 선택")
    parser.add_argument("--input", required=True, help="Google Sheets 범위 조회 결과 JSON")
    parser.add_argument("--job-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--output", required=True)
    parser.add_argument("--spreadsheet-id")
    parser.add_argument("--sheet-name")
    parser.add_argument("--range")
    return parser.parse_args()


def clean(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def matrix_from_payload(payload: Any) -> list[list[Any]]:
    if isinstance(payload, dict):
        values = payload.get("values")
        if isinstance(values, list):
            return values
        result = payload.get("result")
        if isinstance(result, dict):
            return matrix_from_payload(result)
    raise ValueError("스케줄 JSON에서 values 배열을 찾지 못했습니다.")


def column_letter(index: int) -> str:
    value = index + 1
    result = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result


def source_value(payload: dict[str, Any], cli_value: str | None, key: str) -> str:
    return clean(cli_value) or clean(payload.get(key))


def select_schedule(
    payload: dict[str, Any],
    job_date: dt.date,
    *,
    spreadsheet_id: str = "",
    sheet_name: str = "",
    source_range: str = "",
) -> dict[str, Any]:
    values = matrix_from_payload(payload)
    normalized = [[clean(cell) for cell in row] for row in values]
    header_index = next(
        (
            index
            for index, row in enumerate(normalized)
            if "보고서" in row and "구분" in row and sum(label in row for label in WEEKDAY_LABELS) >= 5
        ),
        None,
    )
    if header_index is None:
        raise ValueError("보고서·구분·요일 열을 가진 동향 스케줄 헤더를 찾지 못했습니다.")

    header = normalized[header_index]
    weekday = WEEKDAY_LABELS[job_date.weekday()]
    weekday_index = header.index(weekday)
    report_index = header.index("보고서")
    division_index = header.index("구분")
    publication_index = header.index("발행 요일") if "발행 요일" in header else None
    resolved_sheet = source_value(payload, sheet_name, "sheet_name")
    assignments: list[dict[str, Any]] = []

    for row_index, row in enumerate(normalized[header_index + 1 :], start=header_index + 2):
        padded = row + [""] * (len(header) - len(row))
        report = padded[report_index] if report_index < len(padded) else ""
        division = padded[division_index] if division_index < len(padded) else ""
        worker = padded[weekday_index] if weekday_index < len(padded) else ""
        if not worker or not (report or division):
            continue
        assignment = {
            "ref": f"{resolved_sheet}!{row_index}" if resolved_sheet else str(row_index),
            "sheet_row": row_index,
            "report": report,
            "division": division,
            "publication_schedule": (
                padded[publication_index]
                if publication_index is not None and publication_index < len(padded)
                else ""
            ),
            "worker": worker,
            "worker_cell": f"{column_letter(weekday_index)}{row_index}",
        }
        assignments.append(assignment)

    if not assignments:
        raise ValueError(f"{weekday} 열에서 동향 스케줄 담당자를 찾지 못했습니다.")

    heading = next((row[0] for row in normalized[:header_index] if row and row[0]), "")
    return {
        "schema_version": 1,
        "job_date": job_date.isoformat(),
        "weekday": weekday,
        "heading": heading,
        "header_row": header_index + 1,
        "weekday_column": column_letter(weekday_index),
        "source": {
            "spreadsheet_id": source_value(payload, spreadsheet_id, "spreadsheet_id"),
            "spreadsheet_title": clean(payload.get("spreadsheet_title")),
            "sheet_name": resolved_sheet,
            "range": source_value(payload, source_range, "range"),
        },
        "assignments": assignments,
    }


def main() -> int:
    args = parse_args()
    input_path = Path(args.input).resolve()
    output_path = Path(args.output).resolve()
    payload = json.loads(input_path.read_text(encoding="utf-8-sig"))
    job_date = dt.date.fromisoformat(args.job_date)
    selected = select_schedule(
        payload,
        job_date,
        spreadsheet_id=args.spreadsheet_id or "",
        sheet_name=args.sheet_name or "",
        source_range=args.range or "",
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(selected, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "output": str(output_path),
        "job_date": selected["job_date"],
        "weekday": selected["weekday"],
        "assignments": len(selected["assignments"]),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
