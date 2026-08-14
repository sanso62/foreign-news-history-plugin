#!/usr/bin/env python3
"""Deterministic preflight and read-back verification for optional Sheets sync.

Connector calls stay in Codex.  This script decides whether the connector may
append, should do nothing, or must stop because existing rows conflict.  The
bundled config disables these commands during Excel-only trial operation.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Google Sheets 동기화 안전 검사")
    sub = parser.add_subparsers(dest="command", required=True)
    preflight = sub.add_parser("preflight")
    preflight.add_argument("--job-date", required=True)
    preflight.add_argument("--sheet-name", required=True)
    preflight.add_argument("--existing-json", required=True)
    preflight.add_argument("--result-json", required=True)
    preflight.add_argument("--output-dir", required=True)
    preflight.add_argument("--config")
    verify = sub.add_parser("verify")
    verify.add_argument("--readback-json", required=True)
    verify.add_argument("--result-json", required=True)
    verify.add_argument("--output-dir", required=True)
    verify.add_argument("--config")
    return parser.parse_args()


def load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def default_config_path() -> Path:
    return Path(__file__).resolve().parents[1] / "assets" / "harness.config.json"


def require_google_sheets_write_enabled(args: argparse.Namespace) -> Path:
    config_path = Path(getattr(args, "config", None) or default_config_path()).resolve()
    config = load_json(config_path)
    enabled = config.get("sync", {}).get("google_sheets_write_enabled") is True
    if not enabled:
        raise ValueError(
            "설정에서 Google Sheets 결과 쓰기가 비활성화되어 있습니다. "
            "현재 실행은 작업이력_최종.xlsx 생성까지만 완료해야 합니다."
        )
    return config_path


def matrix_from_payload(payload: Any) -> list[list[Any]]:
    if isinstance(payload, dict):
        for key in ("values", "rows", "data"):
            if key in payload and isinstance(payload[key], list):
                return payload[key]
        result = payload.get("result")
        if isinstance(result, dict):
            return matrix_from_payload(result)
    if isinstance(payload, list):
        return payload
    raise ValueError("시트 JSON에서 2차원 values 배열을 찾지 못했습니다.")


def normalize_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_row(row: list[Any], width: int = 15) -> list[str]:
    return [normalize_cell(value) for value in (row + [None] * width)[:width]]


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def load_result(path: str | Path) -> tuple[list[str], list[list[Any]]]:
    result = load_json(path)
    headers = result.get("headers")
    rows = result.get("rows")
    if not isinstance(headers, list) or len(headers) != 15 or not isinstance(rows, list):
        raise ValueError("result.json의 headers/rows 스키마가 올바르지 않습니다.")
    if any(not isinstance(row, list) or len(row) != 15 for row in rows):
        raise ValueError("result.json에 A:O 15열이 아닌 행이 있습니다.")
    return headers, rows


def update_checkpoint(output_dir: Path, **changes: Any) -> None:
    checkpoint_path = output_dir / "checkpoint.json"
    checkpoint = load_json(checkpoint_path)
    checkpoint.update(changes)
    checkpoint["updated_at"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    write_json(checkpoint_path, checkpoint)


def preflight(args: argparse.Namespace) -> int:
    require_google_sheets_write_enabled(args)
    output_dir = Path(args.output_dir).resolve()
    checkpoint = load_json(output_dir / "checkpoint.json")
    if not checkpoint.get("intermediate_saved"):
        raise ValueError("로컬 중간 엑셀 저장·검수가 완료되지 않아 Google Sheets 동기화를 시작할 수 없습니다.")
    try:
        review_rows = int(checkpoint.get("review_rows", 0))
    except (TypeError, ValueError):
        raise ValueError("checkpoint.json의 확인 필요 행 수가 올바르지 않습니다.")
    if review_rows:
        raise ValueError(
            f"확인 필요 {review_rows}행이 남아 있어 Google Sheets 동기화를 중단합니다. "
            "현재 원문 또는 해시 검증된 기준표로 확인값을 보완하고 결과를 다시 생성하세요."
        )
    headers, expected = load_result(args.result_json)
    existing = matrix_from_payload(load_json(args.existing_json))
    if not existing:
        raise ValueError("월별 결과 탭이 비어 있어 헤더를 검증할 수 없습니다.")
    if normalize_row(existing[0]) != normalize_row(headers):
        raise ValueError("월별 탭 A1:O1 헤더가 결과 스키마와 다릅니다.")

    data_rows = [list(row) for row in existing[1:] if isinstance(row, list)]
    job_label = normalize_cell(expected[0][1]) if expected else ""
    matching_indexes = [
        index for index, row in enumerate(data_rows) if normalize_cell((row + [None, None])[1]) == job_label
    ]
    existing_job_rows = [data_rows[index] for index in matching_indexes]
    expected_normalized = [normalize_row(row) for row in expected]
    existing_normalized = [normalize_row(row) for row in existing_job_rows]

    if not matching_indexes:
        start_row = len(data_rows) + 2
        end_row = start_row + len(expected) - 1
        action = "append"
        plan = {
            "action": action,
            "job_date": args.job_date,
            "sheet_name": args.sheet_name,
            "write_range": f"'{args.sheet_name}'!A{start_row}:O{end_row}",
            "start_row": start_row,
            "end_row": end_row,
            "values": expected,
            "reason": "같은 작업일의 기존 행 없음",
        }
        update_checkpoint(output_dir, sheet_preflight="append_ready", planned_range=plan["write_range"])
    elif existing_normalized == expected_normalized:
        action = "no_op"
        plan = {
            "action": action,
            "job_date": args.job_date,
            "sheet_name": args.sheet_name,
            "existing_rows": [index + 2 for index in matching_indexes],
            "reason": "같은 작업일의 기존 값이 결과와 완전히 같음",
        }
        update_checkpoint(
            output_dir,
            phase="uploaded_verified",
            uploaded_verified=True,
            sheet_preflight="no_op_verified",
        )
    else:
        action = "conflict"
        differences = []
        maximum = max(len(existing_normalized), len(expected_normalized))
        for index in range(maximum):
            old = existing_normalized[index] if index < len(existing_normalized) else None
            new = expected_normalized[index] if index < len(expected_normalized) else None
            if old != new:
                differences.append({"offset": index, "existing": old, "new": new})
        plan = {
            "action": action,
            "job_date": args.job_date,
            "sheet_name": args.sheet_name,
            "existing_rows": [index + 2 for index in matching_indexes],
            "reason": "같은 작업일에 서로 다른 기존 데이터가 있어 교체 승인 필요",
            "differences": differences,
        }
        update_checkpoint(output_dir, sheet_preflight="conflict", uploaded_verified=False)

    write_json(output_dir / "sheet_sync_plan.json", plan)
    print(json.dumps({"action": action, "plan": str(output_dir / "sheet_sync_plan.json")}, ensure_ascii=False))
    return 3 if action == "conflict" else 0


def verify(args: argparse.Namespace) -> int:
    require_google_sheets_write_enabled(args)
    output_dir = Path(args.output_dir).resolve()
    headers, expected = load_result(args.result_json)
    readback = matrix_from_payload(load_json(args.readback_json))
    if readback and normalize_row(readback[0]) == normalize_row(headers):
        readback = readback[1:]
    exact = [normalize_row(row) for row in readback] == [normalize_row(row) for row in expected]
    report = {
        "verified": exact,
        "expected_rows": len(expected),
        "readback_rows": len(readback),
    }
    write_json(output_dir / "sheet_readback_verification.json", report)
    if not exact:
        update_checkpoint(output_dir, uploaded_verified=False, sheet_verification="mismatch")
        raise ValueError("시트 재조회 값이 로컬 결과와 일치하지 않습니다.")
    update_checkpoint(
        output_dir,
        phase="uploaded_verified",
        uploaded_verified=True,
        sheet_verification="exact_match",
    )
    print(json.dumps(report, ensure_ascii=False))
    return 0


def main() -> int:
    args = parse_args()
    return preflight(args) if args.command == "preflight" else verify(args)


if __name__ == "__main__":
    raise SystemExit(main())
