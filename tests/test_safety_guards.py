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


class SafetyGuardTests(unittest.TestCase):
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
            )
            with self.assertRaisesRegex(ValueError, "확인 필요 1행"):
                sheet_sync_guard.preflight(args)


if __name__ == "__main__":
    unittest.main()
