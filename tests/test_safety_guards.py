import argparse
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = (
    ROOT
    / "plugins"
    / "foreign-news-history"
    / "skills"
    / "run-foreign-news-history"
    / "scripts"
)


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


process_job = sys.modules.get("process_job") or load_module("process_job", "process_job.py")
discover_context = load_module("safety_discover_context", "discover_context.py")
reference_feedback = load_module("safety_reference_feedback", "apply_reference_feedback.py")
sheet_sync_guard = load_module("safety_sheet_sync_guard", "sheet_sync_guard.py")
prepare_source_history = load_module("safety_prepare_source_history", "prepare_source_history.py")


class SafetyGuardTests(unittest.TestCase):
    def test_source_history_filters_formatted_dates_and_records_audit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.json"
            output = Path(temp_dir) / "output.json"
            source.write_text(json.dumps({
                "range": "'1. 작업 내역'!A1:O4",
                "values": [
                    ["작업날짜", "작업 조", "보도일", "보도시각 (KST)", "제목 (한글)"],
                    ["2026. 7. 23", "1조", "2026. 7. 22", "9:00", "첫 기사"],
                    ["2026. 7. 23", "1조", "2026-07-22", "10:00", "둘째 기사"],
                    ["2026. 7. 22", "2조", "2026. 7. 21", "11:00", "제외 기사"],
                ],
            }, ensure_ascii=False), encoding="utf-8")
            with mock.patch.object(sys, "argv", [
                "prepare_source_history.py", "--input", str(source),
                "--target-date", "2026-07-22", "--spreadsheet-id", "current",
                "--sheet-name", "1. 작업 내역", "--output", str(output),
            ]):
                self.assertEqual(0, prepare_source_history.main())
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(3, payload["source_audit"]["matched_row_count"])
            self.assertEqual(2, payload["source_audit"]["matched_by_report_date_count"])
            self.assertEqual(1, payload["source_audit"]["matched_by_work_date_carryover_count"])
            self.assertEqual([], process_job.source_scan_audit_errors(
                output, process_job.dt.date(2026, 7, 22)
            ))

    def test_source_history_rejects_row_search_audit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.json"
            source.write_text(json.dumps({
                "source_audit": {
                    "schema_version": 2, "retrieval_method": "row_search",
                    "value_render_option": "FORMATTED_VALUE", "target_date": "2026-07-22",
                    "scan_range": "'작업내역'!A1:K1000", "scanned_row_count": 1,
                    "matched_row_count": 1,
                },
                "values": [["보도일", "제목 (한글)"], ["2026. 7. 22", "기사"]],
            }, ensure_ascii=False), encoding="utf-8")
            errors = process_job.source_scan_audit_errors(
                source, process_job.dt.date(2026, 7, 22)
            )
            self.assertTrue(any("bounded range scan" in error for error in errors))

    def test_source_history_rejects_a_different_input_workbook_or_tab(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.json"
            source.write_text(json.dumps({
                "source_audit": {
                    "schema_version": 2,
                    "retrieval_method": "bounded_range_scan",
                    "value_render_option": "FORMATTED_VALUE",
                    "spreadsheet_id": "wrong-sheet",
                    "sheet_name": "wrong-tab",
                    "target_date": "2026-07-22",
                    "scan_range": "'wrong-tab'!A1:O2",
                    "scanned_row_count": 1,
                    "matched_row_count": 1,
                },
                "values": [["보도일", "제목 (한글)"], ["2026. 7. 22", "기사"]],
            }, ensure_ascii=False), encoding="utf-8")
            errors = process_job.source_scan_audit_errors(
                source,
                process_job.dt.date(2026, 7, 22),
                expected_spreadsheet_id="source-sheet",
                expected_sheet_name="1. 작업 내역",
            )
            self.assertTrue(any("입력 스프레드시트" in error for error in errors))
            self.assertTrue(any("입력 탭" in error for error in errors))

    def test_output_cleanup_handles_compact_prefix_and_unbalanced_quote(self):
        self.assertEqual(process_job.display_media("중 가상매체"), "가상매체")
        self.assertEqual(process_job.display_media("New Example News"), "New Example News")
        self.assertEqual(
            process_job.display_title("장관, 노력 “전적으로 이해“"),
            "장관, 노력 “전적으로 이해”",
        )

    def test_numeric_first_category_matches_by_name_without_shifting(self):
        paragraphs = [
            "□ 미래성장동력 7대 시드 보고회",
            "ㅇ 첫 기사",
            "□ 외교·안보",
            "ㅇ 둘째 기사",
            "미래성장동력 7대 시드 보고회",
            "<첫 매체 8.10> 첫 기사",
            "- 첫 기사 본문",
            "외교 ‧ 안보",
            "<둘째 매체 8.10> 둘째 기사",
            "- 둘째 기사 본문",
        ]
        entries = process_job.front_entries(paragraphs)
        self.assertTrue(process_job.is_body_category_candidate(paragraphs, 4))
        mapping = process_job.body_category_map(paragraphs, entries)
        self.assertEqual(
            list(mapping.values()),
            ["미래성장동력 7대 시드 보고회", "외교·안보"],
        )
        self.assertEqual(
            process_job.final_category_alignment_errors(paragraphs, entries),
            [],
        )

    def test_missing_body_category_cannot_shift_the_following_category(self):
        paragraphs = [
            "□ 미래성장동력 7대 시드 보고회",
            "ㅇ 첫 기사",
            "□ 외교·안보",
            "ㅇ 둘째 기사",
            "외교 ‧ 안보",
            "<둘째 매체 8.10> 둘째 기사",
            "- 둘째 기사 본문",
        ]
        entries = process_job.front_entries(paragraphs)
        mapping = process_job.body_category_map(paragraphs, entries)
        self.assertEqual(list(mapping.values()), ["외교·안보"])
        errors = process_job.final_category_alignment_errors(paragraphs, entries)
        self.assertTrue(any("미래성장동력 7대 시드 보고회" in error for error in errors))

    def test_duplicate_body_category_fails_exact_sequence_check(self):
        paragraphs = [
            "□ 첫 분류",
            "ㅇ 첫 기사",
            "□ 둘째 분류",
            "ㅇ 둘째 기사",
            "첫 분류",
            "<첫 매체 8.10> 첫 기사",
            "- 첫 기사 본문",
            "둘째 분류",
            "<둘째 매체 8.10> 둘째 기사",
            "- 둘째 기사 본문",
            "둘째 분류",
            "<셋째 매체 8.10> 셋째 기사",
        ]
        entries = process_job.front_entries(paragraphs)
        errors = process_job.final_category_alignment_errors(paragraphs, entries)
        self.assertTrue(any("개수·순서 불일치" in error for error in errors))

    def test_final_report_rejects_category_sequence_mismatch(self):
        paragraphs = [
            "□ 첫 분류",
            "ㅇ 첫 기사",
            "□ 둘째 분류",
            "ㅇ 둘째 기사",
            "둘째 분류",
            "<둘째 매체 8.10> 둘째 기사",
        ]
        with mock.patch.object(process_job, "extract_paragraphs", return_value=paragraphs):
            with self.assertRaisesRegex(ValueError, "카테고리 구조 불일치"):
                process_job.parse_document(
                    Path("final.hwpx"),
                    require_category_alignment=True,
                )

    def test_reference_feedback_matches_unique_anchor_after_full_title_rewrite(self):
        row = [
            "", "8월 4일", "2조", "보조", "작업자갑", "", "",
            "현재 분류", "Example Finance", "8.3", "원문과 크게 다른 제목",
            "O", "X", "", "",
        ]
        references = [[
            "", "8월 4일", "2조", "보조", "작업자갑", "", "",
            "현재 분류", "Example Finance", "8.3", "완전히 새로 쓴 최종 제목",
            "O", "X", "", "",
        ], [
            "", "8월 4일", "1조", "국내", "작업자을", "", "",
            "다른 분류", "Other News", "8.3", "다른 기사",
            "O", "X", "", "",
        ]]
        index, _ = reference_feedback.best_reference_index(row, references, {0, 1})
        self.assertEqual(index, 0)

    def test_reference_feedback_re_resolves_origin_from_authoritative_roles(self):
        reference = [
            "", "8월 4일", "1조", "국내", "작업자을", "", "",
            "경제", "현재 매체", "8.3", "현재 기사 제목", "O", "X", "", "",
        ]
        candidates = [
            {
                "source_type": "regular",
                "source_file": "regular.json",
                "source_title": "현재 기사 제목",
                "media": "현재 매체",
                "date": "8.3",
                "workgroup": "정기",
                "owner": "오후/총괄",
                "worker": "작업자병",
            },
            {
                "source_type": "worker",
                "source_file": "domestic.hwpx",
                "source_title": "현재 기사 제목",
                "media": "현재 매체",
                "date": "8.3",
                "workgroup": "1조",
                "owner": "국내",
                "worker": "작업자을",
            },
        ]
        selected = reference_feedback.reference_origin_candidate(reference, candidates)
        self.assertEqual(selected["source_file"], "domestic.hwpx")

    def test_reference_feedback_accepts_unique_media_after_title_and_date_edit(self):
        reference = [
            "", "8월 4일", "특수 경로", "오전/총괄", "작업자갑", "", "",
            "외교", "Current Wire", "8.3", "Leaders announce expanded cooperation", "O", "X", "O", "",
        ]
        candidates = [
            {
                "source_type": "japan",
                "source_file": "special.hwpx",
                "source_title": "Leaders discuss cooperation framework",
                "media": "Current Wire",
                "date": "8.4",
                "workgroup": "특수 경로",
                "owner": "",
                "worker": "",
            },
            {
                "source_type": "japan",
                "source_file": "special.hwpx",
                "source_title": "별개의 기사",
                "media": "Other Broadcast",
                "date": "8.3",
                "workgroup": "특수 경로",
                "owner": "",
                "worker": "",
            },
        ]
        selected = reference_feedback.reference_origin_candidate(reference, candidates)
        self.assertEqual(selected["source_title"], "Leaders discuss cooperation framework")

    def test_hashed_authoritative_japan_exclusion_overrides_automatic_match(self):
        article = process_job.Article(
            source_file="final.hwpx",
            order=1,
            category="외교",
            media="현재 매체",
            date="8.3",
            body_title="현재 기사 제목",
            canonical_title="현재 기사 제목",
        )
        japan = process_job.Candidate(
            source_type="japan",
            title="현재 기사 제목",
            media="현재 매체",
            date="8.3",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            reference = Path(temp_dir) / "reference.xlsx"
            reference.write_bytes(b"current-reference")
            matched, score, reasons = process_job.japan_membership(
                article,
                [japan],
                0.68,
                {
                    "included": False,
                    "reference_file": str(reference),
                    "reference_sha256": process_job.sha256_file(reference),
                    "evidence": ["같은 작업일 기준표의 일본동향 열 확인"],
                },
            )
        self.assertFalse(matched)
        self.assertGreaterEqual(score, 0.68)
        self.assertEqual(reasons, [])

    def test_japan_cli_argument_is_optional_for_both_stages(self):
        common = [
            "--morning-dir", "morning",
            "--afternoon-dir", "afternoon",
            "--final-report", "final.hwpx",
            "--source-json", "source.json",
            "--schedule-json", "schedule.json",
        ]
        with mock.patch.object(
            sys,
            "argv",
            ["discover_context.py", *common, "--output", "context.json"],
        ):
            self.assertIsNone(discover_context.parse_args().japan_input)
        with mock.patch.object(
            sys,
            "argv",
            ["process_job.py", *common, "--run-context", "context.json"],
        ):
            self.assertIsNone(process_job.parse_args().japan_input)

    def test_omitted_japan_path_is_optional(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            final_report = Path(temp_dir) / "260723(목) 일일외신보도동향.hwpx"
            final_report.write_bytes(b"synthetic")
            self.assertIsNone(process_job.resolve_japan_input(final_report))

    def test_explicit_missing_japan_path_still_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            final_report = root / "260723(목) 일일외신보도동향.hwpx"
            final_report.write_bytes(b"synthetic")
            with self.assertRaisesRegex(FileNotFoundError, "입력 파일 없음"):
                process_job.resolve_japan_input(final_report, root / "missing.hwpx")

    def test_not_provided_context_resolves_optional_japan_input(self):
        self.assertTrue(process_job.japan_input_is_resolved(
            None,
            {
                "status": "not_provided",
                "evidence": ["이번 실행에는 일본언론동향이 제공되지 않음"],
            },
        ))
        self.assertFalse(process_job.japan_input_is_resolved(
            None,
            {"status": "present_checked", "evidence": ["과거 입력 근거"]},
        ))

    def test_missing_japan_source_keeps_membership_blank(self):
        candidates, warnings = process_job.japan_candidates(
            None,
            {"status": "not_provided", "evidence": ["이번 실행 미제공"]},
        )
        self.assertEqual(candidates, [])
        self.assertTrue(any("공란으로 유지" in warning for warning in warnings))

    def test_context_discovery_records_omitted_japan_input(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            morning = root / "morning"
            afternoon = root / "afternoon"
            morning.mkdir()
            afternoon.mkdir()
            final_report = root / "260723(목) 일일외신보도동향.hwpx"
            source_json = root / "source.json"
            schedule_json = root / "schedule.json"
            output = root / "context.json"
            final_report.write_bytes(b"synthetic")
            source_json.write_text("{}", encoding="utf-8")
            schedule_json.write_text(json.dumps({
                "job_date": "2026-07-23",
                "assignments": [{"ref": "0. 근무 일정!1", "worker": "현재 작업자"}],
            }, ensure_ascii=False), encoding="utf-8")
            final_signal = {
                "path": str(final_report),
                "filename": final_report.name,
                "read_error": "",
                "article_heading_count": 1,
            }
            article = process_job.Article(
                str(final_report), 1, "현재 분류", "현재 매체", "7.22", "현재 기사", "현재 기사"
            )
            argv = [
                "discover_context.py",
                "--morning-dir", str(morning),
                "--afternoon-dir", str(afternoon),
                "--final-report", str(final_report),
                "--source-json", str(source_json),
                "--schedule-json", str(schedule_json),
                "--output", str(output),
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(discover_context, "document_signal", return_value=final_signal),
                mock.patch.object(discover_context, "parse_document", return_value=[article]),
            ):
                self.assertEqual(discover_context.main(), 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["japan_input"], {
                "status": "not_provided",
                "path": "",
                "files": [],
            })
            self.assertEqual(payload["sources"]["japan"]["status"], "not_provided")

    def test_hwp_reader_falls_back_to_bundled_module(self):
        with mock.patch.object(
            process_job.importlib, "import_module", side_effect=ImportError("not installed")
        ):
            module = process_job.load_olefile_module()
        self.assertTrue(hasattr(module, "OleFileIO"))
        self.assertEqual(Path(module.__file__).name, "olefile.py")
        self.assertEqual(Path(module.__file__).parent.name, "_vendor")

    def test_worker_parse_failure_stops_the_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "invalid.hwp"
            path.write_bytes(b"not-an-ole-document")
            digest = process_job.sha256_file(path)
            context = {
                "files": [
                    {
                        "path": str(path),
                        "sha256": digest,
                        "source_kind": "global_draft",
                        "workgroup": "1조",
                        "owner": "글로벌",
                        "worker": "작업자갑",
                        "evidence": ["검사용 가상 근거"],
                    }
                ]
            }
            with self.assertRaisesRegex(ValueError, "작업자 파일 파싱 실패"):
                process_job.worker_candidates(
                    [path],
                    final_hash="different",
                    run_context=context,
                    comparison_stages={str(path.resolve()): "morning"},
                )

    def test_context_discovery_rejects_unreadable_or_empty_documents(self):
        signals = [
            {
                "filename": "broken.hwp",
                "read_error": "읽기 실패",
                "article_heading_count": 0,
            },
            {
                "filename": "empty.hwpx",
                "read_error": "",
                "article_heading_count": 0,
            },
        ]
        with self.assertRaisesRegex(ValueError, "broken.hwp.*empty.hwpx"):
            discover_context.raise_for_unreadable_documents(signals, "작업본")

    def test_worker_role_contract_rejects_wrong_owner_label(self):
        errors = process_job.role_semantic_errors(
            "worker", "global_draft", "1조", "국내"
        )
        self.assertTrue(any("글로벌" in error for error in errors))

    def test_result_validation_rejects_wrong_role_combination(self):
        article = process_job.Article(
            source_file="final.hwpx",
            order=1,
            category="경제",
            media="가상매체",
            date="8.4",
            body_title="가상 기사",
        )
        row = ["", "8월 4일", "2조", "국내", "작업자갑", "", "", "경제", "가상매체", "8.4", "가상 기사", "O", "X", "X", ""]
        detail = {
            "origin": "worker",
            "origin_source_kind": "global_draft",
            "origin_actual_edit_source_kind": "",
        }
        errors = process_job.validate_result([article], [row], match_details=[detail])
        self.assertTrue(any("역할 의미 불일치" in error for error in errors))

    def test_hashed_reference_confirmation_can_override_automatic_role(self):
        article = process_job.Article(
            source_file="final.hwpx",
            order=1,
            category="경제",
            media="가상매체",
            date="8.4",
            body_title="가상 기사",
        )
        row = ["", "8월 4일", "2조", "국내", "작업자갑", "", "", "경제", "가상매체", "8.4", "가상 기사", "O", "X", "X", ""]
        detail = {
            "origin": "worker",
            "origin_source_kind": "global_draft",
            "role_confirmed_from_reference": True,
        }
        self.assertEqual(
            process_job.validate_result([article], [row], match_details=[detail]),
            [],
        )

    def test_reference_feedback_restores_source_backed_missing_row(self):
        reference = [
            "",
            "8월 4일",
            "1조",
            "글로벌",
            "작업자갑",
            "",
            "",
            "경제",
            "가상매체",
            "8.4",
            "누락된 가상 기사",
            "X",
            "X",
            "X",
            "",
        ]
        candidates = [
            {
                "source_type": "worker",
                "source_file": "worker.hwpx",
                "source_title": "누락된 가상 기사",
                "category": "경제",
                "media": "가상매체",
                "date": "8.4",
                "workgroup": "1조",
                "owner": "글로벌",
                "worker": "작업자갑",
            }
        ]
        additions, consumed = reference_feedback.missing_reference_additions(
            [reference],
            {0},
            [],
            candidates,
            Path("reference.xlsx"),
            "f" * 64,
        )
        self.assertEqual(len(additions), 1)
        self.assertEqual(additions[0]["kind"], "omitted")
        self.assertEqual(consumed, {0})

    def test_sheet_preflight_rejects_unresolved_review_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            enabled_config = output_dir / "enabled-config.json"
            enabled_config.write_text(
                json.dumps({"sync": {"google_sheets_write_enabled": True}}),
                encoding="utf-8",
            )
            (output_dir / "checkpoint.json").write_text(
                json.dumps({"intermediate_saved": True, "review_rows": 1}),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                output_dir=str(output_dir),
                result_json=str(output_dir / "result.json"),
                existing_json=str(output_dir / "existing.json"),
                job_date="2026-08-04",
                sheet_name="8월",
                config=str(enabled_config),
            )
            with self.assertRaisesRegex(ValueError, "확인 필요 1행"):
                sheet_sync_guard.preflight(args)

    def test_sheet_preflight_rejects_bundled_excel_only_config(self):
        args = argparse.Namespace(
            output_dir="unused",
            result_json="unused",
            existing_json="unused",
            job_date="2026-08-04",
            sheet_name="8월",
            config=str(sheet_sync_guard.default_config_path()),
        )
        with self.assertRaisesRegex(ValueError, "비활성화"):
            sheet_sync_guard.preflight(args)


class ReferenceFeedbackRegressionTests(unittest.TestCase):
    def test_source_history_accepts_empty_trailing_grid_chunk(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.json"
            output = Path(temp_dir) / "output.json"
            source.write_text(json.dumps({
                "chunks": [
                    {
                        "range": "'1. 작업 내역'!A1:O3333",
                        "values": [
                            ["작업날짜", "작업 조", "보도일", "제목 (한글)"],
                            ["2026. 7. 22", "1조", "2026. 7. 22", "현재 기사"],
                        ],
                    },
                    {"range": "'1. 작업 내역'!A3334:O4780", "values": []},
                ],
            }, ensure_ascii=False), encoding="utf-8")
            with mock.patch.object(sys, "argv", [
                "prepare_source_history.py", "--input", str(source),
                "--target-date", "2026-07-22", "--spreadsheet-id", "current",
                "--sheet-name", "1. 작업 내역", "--output", str(output),
            ]):
                self.assertEqual(0, prepare_source_history.main())
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(payload["source_audit"]["scanned_row_count"], 1)
        self.assertEqual(
            payload["source_audit"]["scan_ranges"],
            ["'1. 작업 내역'!A1:O3333", "'1. 작업 내역'!A3334:O4780"],
        )

    def test_merge_preserves_distinct_articles_with_one_order(self):
        generated = [
            {"order": 10, "article_title": "첫 기사", "article_media": "First News"},
            {"order": 10, "article_title": "둘째 기사", "article_media": "Second News"},
        ]

        merged = reference_feedback.merge_by_order([], generated)

        self.assertEqual(len(merged), 2)
        self.assertEqual({item["article_title"] for item in merged}, {"첫 기사", "둘째 기사"})

    def test_consumes_existing_omitted_row_without_duplicate_addition(self):
        row = [
            "2026년 08월", "8월 4일", "1조", "국내", "작업자",
            "오전/총괄", "", "경제", "Example News", "8.3",
            "이미 작업본에 있던 미포함 기사", "X", "X", "", "",
        ]
        references = [list(row)]
        unused = {0}
        matches = []

        consumed = reference_feedback.consume_existing_omitted_reference(
            row,
            {"order": 99, "omitted_from_final": True},
            references,
            unused,
            matches,
        )

        self.assertEqual(consumed, 0)
        self.assertEqual(unused, set())
        self.assertTrue(matches[0]["existing_omitted"])

    def test_rebuilds_existing_omitted_rows_when_reference_order_differs(self):
        first = [
            "2026년 08월", "8월 13일", "1조", "국내", "작업자",
            "오전/총괄", "", "경제", "First News", "8.12",
            "첫 번째 미포함 기사", "X", "X", "", "",
        ]
        second = [
            "2026년 08월", "8월 13일", "2조", "보조", "다른 작업자",
            "오전/총괄", "", "외교", "Second News", "8.12",
            "두 번째 미포함 기사", "X", "X", "", "",
        ]
        unused = {0, 1}
        matches = []

        consumed = reference_feedback.consume_existing_omitted_rows(
            [
                (second, {"order": 99, "omitted_from_final": True}),
                (first, {"order": 100, "omitted_from_final": True}),
            ],
            [first, second],
            unused,
            matches,
        )

        self.assertFalse(consumed)
        self.assertEqual(unused, {0, 1})
        self.assertEqual(matches, [])


if __name__ == "__main__":
    unittest.main()
