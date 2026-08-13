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
SCHEDULE_HEADING = "동향 스케줄"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="동향 스케줄의 작업일 담당자 선택")
    parser.add_argument("--input", required=True, help="Google Sheets 범위 조회 결과 JSON")
    parser.add_argument("--job-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--output", required=True)
    parser.add_argument("--spreadsheet-id")
    parser.add_argument("--sheet-name")
    parser.add_argument("--range")
    parser.add_argument("--heading", default=SCHEDULE_HEADING, help="찾을 동향 스케줄 표 제목")
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


def payload_value(payload: dict[str, Any], key: str) -> Any:
    if key in payload:
        return payload.get(key)
    result = payload.get("result")
    if isinstance(result, dict):
        return payload_value(result, key)
    return None


def column_letter(index: int) -> str:
    value = index + 1
    result = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result


def column_index(label: str) -> int:
    value = 0
    for character in label.upper():
        if not "A" <= character <= "Z":
            raise ValueError(f"잘못된 열 이름입니다: {label}")
        value = value * 26 + ord(character) - ord("A") + 1
    return value - 1


def split_a1_range(source_range: str) -> tuple[str, str]:
    value = clean(source_range)
    if "!" not in value:
        return "", value
    sheet_name, grid_range = value.rsplit("!", 1)
    sheet_name = sheet_name.strip()
    if len(sheet_name) >= 2 and sheet_name[0] == sheet_name[-1] == "'":
        sheet_name = sheet_name[1:-1].replace("''", "'")
    return sheet_name, grid_range


def range_origin(source_range: str) -> tuple[int, int]:
    _, grid_range = split_a1_range(source_range)
    first_cell = grid_range.split(":", 1)[0].replace("$", "").strip()
    match = re.fullmatch(r"([A-Za-z]*)(\d*)", first_cell)
    if not match:
        raise ValueError(f"Google Sheets 조회 범위의 시작 셀을 해석하지 못했습니다: {source_range}")
    column_label, row_label = match.groups()
    start_column = column_index(column_label) if column_label else 0
    start_row = int(row_label) - 1 if row_label else 0
    if start_row < 0:
        raise ValueError(f"Google Sheets 조회 범위의 시작 행이 잘못되었습니다: {source_range}")
    return start_row, start_column


def source_value(payload: dict[str, Any], cli_value: str | None, key: str) -> str:
    return clean(cli_value) or clean(payload_value(payload, key))


def find_schedule_table(
    normalized: list[list[str]],
    weekday: str,
    schedule_heading: str = SCHEDULE_HEADING,
) -> tuple[tuple[int, int], int]:
    headings = [
        (row_index, column_index)
        for row_index, row in enumerate(normalized)
        for column_index, value in enumerate(row)
        if value == schedule_heading
    ]
    if not headings:
        raise ValueError(f"'{schedule_heading}' 표 제목을 찾지 못했습니다.")

    headers = [
        row_index
        for row_index, row in enumerate(normalized)
        if "보고서" in row
        and "구분" in row
        and weekday in row
        and sum(label in row for label in WEEKDAY_LABELS) >= 5
    ]
    matches: list[tuple[tuple[int, int], int]] = []
    for heading in headings:
        next_heading_row = next(
            (row_index for row_index, _ in headings if row_index > heading[0]),
            len(normalized),
        )
        candidates = [
            header_index
            for header_index in headers
            if heading[0] < header_index < next_heading_row
        ]
        if candidates:
            matches.append((heading, candidates[0]))

    if not matches:
        raise ValueError(
            f"'{schedule_heading}' 아래에서 보고서·구분·{weekday} 열을 가진 헤더를 찾지 못했습니다."
        )
    if len(matches) > 1:
        locations = ", ".join(str(heading[0] + 1) for heading, _ in matches)
        raise ValueError(
            f"'{schedule_heading}' 표가 여러 개 발견되었습니다(조회 결과 기준 행: {locations}). "
            "현재 사용할 표를 하나로 특정할 수 없습니다."
        )
    return matches[0]


def select_schedule(
    payload: dict[str, Any],
    job_date: dt.date,
    *,
    spreadsheet_id: str = "",
    sheet_name: str = "",
    source_range: str = "",
    schedule_heading: str = SCHEDULE_HEADING,
) -> dict[str, Any]:
    values = matrix_from_payload(payload)
    normalized = [[clean(cell) for cell in row] for row in values]
    weekday = WEEKDAY_LABELS[job_date.weekday()]
    resolved_heading = clean(schedule_heading) or SCHEDULE_HEADING
    heading_position, header_index = find_schedule_table(normalized, weekday, resolved_heading)
    header = normalized[header_index]
    weekday_index = header.index(weekday)
    report_index = header.index("보고서")
    division_index = header.index("구분")
    publication_index = header.index("발행 요일") if "발행 요일" in header else None
    resolved_range = source_value(payload, source_range, "range")
    range_sheet, _ = split_a1_range(resolved_range)
    resolved_sheet = source_value(payload, sheet_name, "sheet_name") or range_sheet
    start_row, start_column = range_origin(resolved_range)
    absolute_header_row = start_row + header_index + 1
    absolute_heading_row = start_row + heading_position[0] + 1
    absolute_heading_column = start_column + heading_position[1]
    assignments: list[dict[str, Any]] = []

    for matrix_row_index, row in enumerate(normalized[header_index + 1 :], start=header_index + 1):
        if resolved_heading in row:
            break
        if (
            "보고서" in row
            and "구분" in row
            and sum(label in row for label in WEEKDAY_LABELS) >= 5
        ):
            break
        padded = row + [""] * (len(header) - len(row))
        report = padded[report_index] if report_index < len(padded) else ""
        division = padded[division_index] if division_index < len(padded) else ""
        worker = padded[weekday_index] if weekday_index < len(padded) else ""
        if not worker or not (report or division):
            continue
        sheet_row = start_row + matrix_row_index + 1
        assignment = {
            "ref": f"{resolved_sheet}!{sheet_row}" if resolved_sheet else str(sheet_row),
            "sheet_row": sheet_row,
            "report": report,
            "report_cell": f"{column_letter(start_column + report_index)}{sheet_row}",
            "division": division,
            "division_cell": f"{column_letter(start_column + division_index)}{sheet_row}",
            "publication_schedule": (
                padded[publication_index]
                if publication_index is not None and publication_index < len(padded)
                else ""
            ),
            "publication_schedule_cell": (
                f"{column_letter(start_column + publication_index)}{sheet_row}"
                if publication_index is not None
                else ""
            ),
            "worker": worker,
            "worker_cell": f"{column_letter(start_column + weekday_index)}{sheet_row}",
        }
        assignments.append(assignment)

    if not assignments:
        raise ValueError(f"{weekday} 열에서 동향 스케줄 담당자를 찾지 못했습니다.")

    return {
        "schema_version": 2,
        "job_date": job_date.isoformat(),
        "weekday": weekday,
        "heading": resolved_heading,
        "heading_cell": f"{column_letter(absolute_heading_column)}{absolute_heading_row}",
        "header_row": absolute_header_row,
        "header_cell": f"{column_letter(start_column + report_index)}{absolute_header_row}",
        "weekday_column": column_letter(start_column + weekday_index),
        "source": {
            "spreadsheet_id": source_value(payload, spreadsheet_id, "spreadsheet_id"),
            "spreadsheet_title": clean(payload_value(payload, "spreadsheet_title")),
            "sheet_name": resolved_sheet,
            "range": resolved_range,
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
        schedule_heading=args.heading or SCHEDULE_HEADING,
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
