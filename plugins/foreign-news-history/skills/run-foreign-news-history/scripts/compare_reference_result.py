#!/usr/bin/env python3
"""Compare generated A:O rows with an authoritative workbook-export JSON."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def normalized(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:g}"
    return str(value).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="생성 결과와 권위 기준표 A:O 비교")
    parser.add_argument("--result-json", required=True)
    parser.add_argument("--reference-json", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    result = read_json(Path(args.result_json).resolve())
    reference = read_json(Path(args.reference_json).resolve())
    headers = list(result.get("headers", []))
    actual_rows = list(result.get("rows", []))
    values = reference.get("values") or []
    reference_headers = list(values[0]) if values else []
    expected_rows = list(values[1:]) if values else []
    if [normalized(value) for value in headers] != [normalized(value) for value in reference_headers]:
        raise ValueError("결과와 기준표의 A:O 헤더가 다릅니다.")

    differences: list[dict[str, Any]] = []
    maximum_rows = max(len(expected_rows), len(actual_rows))
    for row_index in range(maximum_rows):
        expected = expected_rows[row_index] if row_index < len(expected_rows) else []
        actual = actual_rows[row_index] if row_index < len(actual_rows) else []
        for column_index in range(len(headers)):
            expected_value = normalized(expected[column_index] if column_index < len(expected) else None)
            actual_value = normalized(actual[column_index] if column_index < len(actual) else None)
            if expected_value != actual_value:
                differences.append(
                    {
                        "row": row_index + 2,
                        "column": column_index + 1,
                        "header": normalized(headers[column_index]),
                        "expected": expected_value,
                        "actual": actual_value,
                    }
                )

    audit = {
        "expected_rows": len(expected_rows),
        "actual_rows": len(actual_rows),
        "different_cells": len(differences),
        "exact_match": len(expected_rows) == len(actual_rows) and not differences,
        "differences": differences,
    }
    output = Path(args.output).resolve()
    output.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False))
    return 0 if audit["exact_match"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
