#!/usr/bin/env python3
"""Filter a bounded Google Sheets range scan to the required regular-work window.

The connector must read FORMATTED_VALUE cells.  This script parses those visible
values locally so a guessed search string can never silently produce a partial
regular-history set.

The ordinary window is the target report date. One narrow carry-over window is
also retained: a row worked on the target date whose report date is exactly one
day earlier. That is the auditable shape produced when an older regular item is
held from the afternoon aggregate and reintroduced in the following morning
aggregate.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any

from process_job import SOURCE_HEADERS, clean_text, parse_date


def canonical_header(value: Any) -> str:
    compact = "".join(clean_text(value).replace("TINY URL", "").split()).replace("*", "")
    for expected in SOURCE_HEADERS:
        if compact == "".join(expected.split()).replace("*", ""):
            return expected
    return clean_text(value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="작업내역 표시값 범위 조회 결과를 대상일로 필터링")
    parser.add_argument("--input", required=True, help="Google Sheets bounded range 응답 JSON")
    parser.add_argument("--target-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--spreadsheet-id", required=True)
    parser.add_argument("--sheet-name", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_path = Path(args.input).resolve()
    output_path = Path(args.output).resolve()
    target_date = dt.date.fromisoformat(args.target_date)
    raw = json.loads(input_path.read_text(encoding="utf-8-sig"))
    chunks = raw.get("chunks") if isinstance(raw, dict) else None
    if not isinstance(chunks, list):
        chunks = [raw]
    scan_ranges: list[str] = []
    values: list[list[Any]] = []
    for chunk in chunks:
        chunk_values = chunk.get("values") if isinstance(chunk, dict) else None
        chunk_range = clean_text(chunk.get("range")) if isinstance(chunk, dict) else ""
        if not isinstance(chunk_values, list):
            raise ValueError("작업내역 범위 조회 JSON에 values 2차원 배열이 없습니다.")
        if not chunk_range:
            raise ValueError("작업내역 범위 조회 JSON에 실제 scan range가 없습니다.")
        scan_ranges.append(chunk_range)
        # Google Sheets may return an empty values array for a bounded trailing
        # grid chunk whose cells are all blank. Keep that queried range in the
        # source audit, but do not require a duplicate header from the empty area.
        if not chunk_values:
            continue
        if not isinstance(chunk_values[0], list):
            raise ValueError("작업내역 범위 조회 JSON에 values 2차원 배열이 없습니다.")
        if not values:
            values.extend(chunk_values)
        else:
            # Later row chunks may repeat the header. Remove it only when the
            # normalized cells actually match the first chunk's header.
            header = [canonical_header(value) for value in values[0]]
            incoming_header = [canonical_header(value) for value in chunk_values[0]]
            values.extend(chunk_values[1:] if incoming_header == header else chunk_values)
    if not values or not isinstance(values[0], list):
        raise ValueError("작업내역 범위 조회 JSON에 비어 있지 않은 헤더 행이 없습니다.")
    headers = [canonical_header(value) for value in values[0]]
    if "보도일" not in headers or "제목 (한글)" not in headers:
        raise ValueError("작업내역 범위 조회에 필수 열 보도일/제목 (한글)이 없습니다.")
    date_index = headers.index("보도일")
    work_date_index = headers.index("작업날짜") if "작업날짜" in headers else None
    previous_date = target_date - dt.timedelta(days=1)
    filtered: list[list[Any]] = []
    report_date_matches = 0
    work_date_carryovers = 0
    for row in values[1:]:
        report_date = parse_date(row[date_index]) if date_index < len(row) else None
        work_date = (
            parse_date(row[work_date_index])
            if work_date_index is not None and work_date_index < len(row)
            else None
        )
        report_date_match = report_date == target_date
        work_date_carryover = work_date == target_date and report_date == previous_date
        if not (report_date_match or work_date_carryover):
            continue
        filtered.append(row)
        report_date_matches += int(report_date_match)
        work_date_carryovers += int(work_date_carryover and not report_date_match)
    if not filtered:
        raise ValueError(f"작업내역 전체 범위에서 {target_date.isoformat()} 행을 찾지 못했습니다.")
    payload = {
        "source_audit": {
            "schema_version": 2,
            "retrieval_method": "bounded_range_scan",
            "value_render_option": "FORMATTED_VALUE",
            "spreadsheet_id": clean_text(args.spreadsheet_id),
            "sheet_name": clean_text(args.sheet_name),
            "scan_range": scan_ranges[0] if len(scan_ranges) == 1 else "",
            "scan_ranges": scan_ranges,
            "target_date": target_date.isoformat(),
            "selection_rule": "report_date_or_one_day_work_date_carryover",
            "scanned_row_count": max(0, len(values) - 1),
            "matched_row_count": len(filtered),
            "matched_by_report_date_count": report_date_matches,
            "matched_by_work_date_carryover_count": work_date_carryovers,
            "source_file": str(input_path),
        },
        "values": [headers, *filtered],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output_path), "matched_rows": len(filtered)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
