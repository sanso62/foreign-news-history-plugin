import copy
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/foreign-news-history/skills/run-foreign-news-history/scripts"
sys.path.insert(0, str(SCRIPTS))
import process_job
import prepare_source_history as prepare


class XlsxHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        node = os.environ.get("NODE_EXECUTABLE") or shutil.which("node")
        if not node:
            raise RuntimeError("XLSX integration tests require the workspace Node runtime and NODE_PATH")
        subprocess.run([node, str(ROOT / "tests/make_source_history_fixtures.mjs"), str(cls.root)],
                       check=True, capture_output=True, text=True, timeout=120)
        cls.day = dt.date(2029, 3, 14)

    def payload(self, name="valid"):
        return prepare.prepare_xlsx_history(self.root / f"{name}.xlsx", self.day)

    def audit_errors(self, payload, expected=True):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.json"
            source.write_text(json.dumps(payload), encoding="utf-8")
            return process_job.source_scan_audit_errors(
                source, self.day, "schedule-document", "1. 작업 내역",
                self.root / "valid.xlsx" if expected else None,
            )

    def test_xlsx_without_schedule_preserves_window_and_order(self):
        payload = self.payload()
        self.assertEqual(["First story", "Carryover story"], [row[12] for row in payload["values"][1:]])
        audit = payload["source_audit"]
        self.assertEqual("local_xlsx", audit["retrieval_method"])
        self.assertEqual("Exported history", audit["sheet_name"])
        self.assertEqual(3, audit["scanned_row_count"])
        self.assertEqual(1, audit["matched_by_work_date_carryover_count"])
        self.assertEqual([], self.audit_errors(payload))

    def test_raw_google_and_xlsx_candidates_are_identical(self):
        payload = self.payload()
        google = prepare.prepare_history({"range": "'1. 작업 내역'!A1:O3", "values": payload["values"]}, self.day, {})
        with tempfile.TemporaryDirectory() as directory:
            results = []
            for index, item in enumerate([payload, google]):
                file = Path(directory) / f"{index}.json"
                file.write_text(json.dumps(item), encoding="utf-8")
                candidates, _ = process_job.regular_candidates(file, self.day, {})
                results.append([(c.title, c.url, c.media, c.date, c.extra) for c in candidates])
            self.assertEqual(*results)

    def test_dates_saved_as_excel_dates_are_parsed(self):
        payload = self.payload("date_cells")
        self.assertEqual(2, payload["source_audit"]["matched_row_count"])
        self.assertEqual("2029-03-14", payload["values"][1][2][:10])

    def test_hyperlink_formula_uses_target_not_label(self):
        self.assertEqual("https://example.test/linked", self.payload("hyperlink_formula")["values"][1][5])

    def test_ambiguous_tables_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "정확히 하나"):
            self.payload("ambiguous")

    def test_result_schema_is_not_a_source_history_schema(self):
        with self.assertRaisesRegex(ValueError, "필수 11개"):
            self.payload("wrong_headers")

    def test_no_matching_rows_is_error(self):
        with self.assertRaisesRegex(ValueError, "찾지 못"):
            self.payload("empty")

    def test_missing_explicit_file_never_falls_back(self):
        with self.assertRaisesRegex(ValueError, "대체하지"):
            self.payload("missing")

    def test_corrupt_file_is_rejected(self):
        path = self.root / "corrupt.xlsx"
        path.write_bytes(b"not an xlsx")
        with self.assertRaisesRegex(ValueError, "읽을 수 없습니다"):
            prepare.prepare_xlsx_history(path, self.day)

    def test_wrong_extension_is_rejected(self):
        path = self.root / "other.csv"
        path.write_text("not xlsx", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, ".xlsx 파일"):
            prepare.prepare_xlsx_history(path, self.day)

    def test_tampered_rows_and_audit_are_rejected(self):
        original = self.payload()
        for kind in ["cell", "order", "delete", "hash", "path"]:
            with self.subTest(kind=kind):
                payload = copy.deepcopy(original)
                if kind == "cell": payload["values"][1][12] = "Changed"
                if kind == "order": payload["values"][1:] = reversed(payload["values"][1:])
                if kind == "delete": payload["values"].pop()
                if kind == "hash": payload["source_audit"]["source_sha256"] = "0" * 64
                if kind == "path": payload["source_audit"]["source_file"] = "different.xlsx"
                self.assertTrue(self.audit_errors(payload))

    def test_xlsx_requires_explicit_original_path(self):
        self.assertTrue(self.audit_errors(self.payload(), expected=False))

    def test_explicit_xlsx_cannot_use_google_audit(self):
        payload = self.payload()
        payload["source_audit"]["retrieval_method"] = "bounded_range_scan"
        self.assertTrue(self.audit_errors(payload))

    def test_explicit_xlsx_cannot_disable_original_verification(self):
        payload = self.payload()
        payload["values"][1][12] = "Altered"
        source = self.root / "altered.json"
        source.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "원본 XLSX"):
            process_job.regular_candidates(source, self.day, {}, require_scan_audit=False,
                                           expected_source_xlsx=self.root / "valid.xlsx")

    def test_changed_original_is_rejected(self):
        path = self.root / "changing.xlsx"
        shutil.copyfile(self.root / "valid.xlsx", path)
        payload = prepare.prepare_xlsx_history(path, self.day)
        shutil.copyfile(self.root / "date_cells.xlsx", path)
        source = self.root / "changing.json"
        source.write_text(json.dumps(payload), encoding="utf-8")
        self.assertTrue(process_job.source_scan_audit_errors(source, self.day, expected_source_xlsx=path))

    def test_cli_source_modes_are_mutually_exclusive(self):
        with mock.patch.object(sys, "argv", ["prepare", "--input", "a.json", "--xlsx", "b.xlsx", "--target-date", str(self.day), "--output", "out.json"]):
            with self.assertRaises(SystemExit):
                prepare.parse_args()

    def test_cli_cannot_overwrite_input(self):
        path = str(self.root / "valid.xlsx")
        with mock.patch.object(sys, "argv", ["prepare", "--xlsx", path, "--target-date", str(self.day), "--output", path]):
            with self.assertRaisesRegex(ValueError, "덮어쓸"):
                prepare.main()

    def test_hyperlink_relationship_is_read_without_network(self):
        cell = SimpleNamespace(value="Read", hyperlink=SimpleNamespace(target="https://example.test/target"), data_type="s", coordinate="F2")
        self.assertEqual("https://example.test/target", prepare.xlsx_cell_value(cell, cell, "온라인 기사 URL", dt.datetime(1899, 12, 30)))

    def test_nonliteral_formula_url_fails_closed(self):
        cell = SimpleNamespace(value='=HYPERLINK(A2,"Read")', hyperlink=None, data_type="f", coordinate="F2")
        cached = SimpleNamespace(value="Read", data_type="s")
        with self.assertRaisesRegex(ValueError, "값으로 저장"):
            prepare.xlsx_cell_value(cell, cached, "온라인 기사 URL", dt.datetime(1899, 12, 30))

    def test_numeric_date_respects_workbook_epoch(self):
        cell = SimpleNamespace(value=1, hyperlink=None, data_type="n", coordinate="A2")
        self.assertEqual("1904-01-02T00:00:00", prepare.xlsx_cell_value(cell, cell, "보도일", dt.datetime(1904, 1, 1)))


if __name__ == "__main__":
    unittest.main()
