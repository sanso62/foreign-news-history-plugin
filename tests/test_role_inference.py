import importlib.util
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = (
    ROOT
    / "plugins"
    / "foreign-news-history"
    / "skills"
    / "run-foreign-news-history"
    / "scripts"
)
PLUGIN_DIR = ROOT / "plugins" / "foreign-news-history"
CONFIG_PATH = (
    PLUGIN_DIR
    / "skills"
    / "run-foreign-news-history"
    / "assets"
    / "harness.config.json"
)


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


process_job = load_module("process_job", "process_job.py")
discover_context = load_module("discover_context", "discover_context.py")
select_schedule = load_module("select_schedule", "select_schedule.py")


SCHEDULE = {
    "assignments": [
        {"report": "글로벌 이슈", "division": "오후", "worker": "작업자갑", "ref": "근무!월요일:R3"},
        {"report": "한국 관련 보도", "division": "오후", "worker": "작업자을", "ref": "근무!월요일:R4"},
        {"report": "외신 일일동향", "division": "총괄", "worker": "작업자병", "ref": "근무!월요일:R5"},
        {"report": "외신 일일동향", "division": "새벽", "worker": "작업자정", "ref": "근무!월요일:R6"},
    ]
}


class FileRoleInferenceTests(unittest.TestCase):
    def assert_profile(self, filename, stage, workgroup, owner, worker, source_kind):
        profile = discover_context.infer_file_profile(Path(filename), stage, SCHEDULE)
        self.assertEqual(workgroup, profile["workgroup"])
        self.assertEqual(owner, profile["owner"])
        self.assertEqual(worker, profile["worker"])
        self.assertEqual(source_kind, profile["source_kind"])
        self.assertEqual("confirmed", profile["confidence"])
        self.assertTrue(profile["schedule_refs"])
        return profile

    def test_afternoon_domestic_draft(self):
        profile = self.assert_profile(
            "260803 국내 초안 작업자을.hwp", "afternoon", "1조", "국내", "작업자을", "domestic_draft"
        )
        self.assertTrue(profile["include_unmatched"])

    def test_afternoon_global_draft(self):
        self.assert_profile(
            "260803 글로벌 이슈 작업자갑.hwp", "afternoon", "1조", "글로벌", "작업자갑", "global_draft"
        )

    def test_afternoon_aggregate(self):
        profile = self.assert_profile(
            "260803 작업자병 취합.hwp", "afternoon", "1조", "오후/총괄", "작업자병", "afternoon_aggregate"
        )
        self.assertFalse(profile["include_unmatched"])

    def test_morning_auxiliary(self):
        self.assert_profile(
            "260804 작업자정 보조.hwp", "morning", "2조", "보조", "작업자정", "morning_auxiliary"
        )

    def test_morning_aggregate(self):
        self.assert_profile(
            "260804 2차 작업자병.hwp", "morning", "2조", "오전/총괄", "작업자병", "morning_aggregate"
        )

    def test_worker_is_unresolved_without_schedule_match(self):
        profile = discover_context.infer_file_profile(
            Path("260803 국내 초안 미확인작업자.hwp"), "afternoon", SCHEDULE
        )
        self.assertEqual("", profile["worker"])
        self.assertEqual("unresolved", profile["confidence"])

    def test_aggregate_profiles_reuse_current_file_worker(self):
        files = [
            {
                "source_kind": "afternoon_aggregate",
                "worker": "작업자병",
                "filename": "260803 작업자병 취합.hwp",
                "schedule_refs": ["근무!월요일:R5"],
            }
        ]
        profile = discover_context.aggregate_profile(
            files, "afternoon_aggregate", "정기", "오후/총괄", "역할 근거"
        )
        self.assertEqual(("정기", "오후/총괄", "작업자병"), (
            profile["workgroup"], profile["owner"], profile["worker"]
        ))


class OriginOrderTests(unittest.TestCase):
    def test_afternoon_precedes_morning(self):
        self.assertEqual(("reference", "afternoon", "morning"), process_job.FIXED_COMPARISON_ORDER)
        article = process_job.Article(
            source_file="final.hwp", order=1, category="", media="매체", date="8.4",
            body_title="동일 기사", canonical_title="동일 기사"
        )
        afternoon = process_job.Candidate(
            source_type="worker", title="동일 기사", media="매체", date="8.4",
            source_file="afternoon.hwp", workgroup="1조", owner="국내", worker="작업자을",
            extra={"comparison_stage": "afternoon", "priority": 1, "profile_complete": True}
        )
        morning = process_job.Candidate(
            source_type="worker", title="동일 기사", media="매체", date="8.4",
            source_file="morning.hwp", workgroup="2조", owner="보조", worker="작업자정",
            extra={"comparison_stage": "morning", "priority": 999, "profile_complete": True}
        )
        chosen, _, _, _ = process_job.choose_origin(
            article,
            {"regular": [], "japan": [], "worker": [morning, afternoon]},
            {"review_threshold": 0.5, "auto_threshold": 0.8, "ambiguity_margin": 0.02},
        )
        self.assertIs(afternoon, chosen)

    def test_japan_workgroup_keeps_actual_edit_role(self):
        japan = process_job.Candidate(
            source_type="japan", title="일본 기사", source_file="japan.docx",
            workgroup="일본문화원", extra={"comparison_stage": "reference", "profile_complete": False}
        )
        worker = process_job.Candidate(
            source_type="worker", title="일본 기사", source_file="afternoon.hwp",
            workgroup="1조", owner="글로벌", worker="작업자갑",
            extra={
                "comparison_stage": "afternoon", "profile_complete": True,
                "schedule_refs": ["근무!월요일:R3"], "profile_evidence": ["현재 작업 근거"]
            },
        )
        result = process_job.enrich_special_source_roles(
            [japan], [worker], {"review_threshold": 0.5}
        )[0]
        self.assertEqual("일본문화원", result.workgroup)
        self.assertEqual(("글로벌", "작업자갑"), (result.owner, result.worker))
        self.assertTrue(result.extra["profile_complete"])


class DynamicScheduleTests(unittest.TestCase):
    def setUp(self):
        self.payload = {
            "spreadsheet_id": "sheet-current",
            "sheet_name": "근무",
            "range": "A1:J20",
            "values": [
                ["동향 스케줄"],
                ["보고서", "구분", "발행 요일", "월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"],
                ["현재 보고서", "현재 구분", "현재 시간", "월 담당", "화 담당", "수 담당", "목 담당", "금 담당", "토 담당", "일 담당"],
            ],
        }

    def test_selects_worker_from_job_date_weekday(self):
        selected = select_schedule.select_schedule(
            self.payload,
            process_job.dt.date(2026, 7, 23),
        )
        assignment = selected["assignments"][0]
        self.assertEqual(selected["schema_version"], 2)
        self.assertEqual(selected["weekday"], "목요일")
        self.assertEqual(selected["heading_cell"], "A1")
        self.assertEqual(selected["header_cell"], "A2")
        self.assertEqual(assignment["ref"], "근무!3")
        self.assertEqual(assignment["report_cell"], "A3")
        self.assertEqual(assignment["worker_cell"], "G3")

    def test_finds_moved_table_and_preserves_absolute_cells(self):
        payload = {
            "spreadsheet_id": "sheet-current",
            "range": "'근무'!D20:M40",
            "values": [
                ["메모"],
                [],
                ["", "", "동향 스케줄"],
                ["", "", "보고서", "구분", "발행 요일", "월요일", "화요일", "수요일", "목요일", "금요일"],
                ["", "", "현재 보고서", "현재 구분", "현재 시간", "월 담당", "화 담당", "수 담당", "목 담당", "금 담당"],
            ],
        }
        selected = select_schedule.select_schedule(
            payload,
            process_job.dt.date(2026, 7, 23),
        )
        assignment = selected["assignments"][0]
        self.assertEqual(selected["source"]["sheet_name"], "근무")
        self.assertEqual(selected["heading_cell"], "F22")
        self.assertEqual(selected["header_cell"], "F23")
        self.assertEqual(selected["weekday_column"], "L")
        self.assertEqual(assignment["ref"], "근무!24")
        self.assertEqual(assignment["report_cell"], "F24")
        self.assertEqual(assignment["division_cell"], "G24")
        self.assertEqual(assignment["publication_schedule_cell"], "H24")
        self.assertEqual(assignment["worker_cell"], "L24")

    def test_reads_range_metadata_from_nested_connector_result(self):
        payload = {
            "result": {
                "spreadsheet_id": "sheet-current",
                "sheet_name": "근무",
                "range": "C10:L30",
                "values": [
                    ["동향 스케줄"],
                    ["보고서", "구분", "발행 요일", "월요일", "화요일", "수요일", "목요일", "금요일"],
                    ["현재 보고서", "현재 구분", "현재 시간", "월 담당", "화 담당", "수 담당", "목 담당", "금 담당"],
                ],
            }
        }
        selected = select_schedule.select_schedule(
            payload,
            process_job.dt.date(2026, 7, 23),
        )
        self.assertEqual(selected["heading_cell"], "C10")
        self.assertEqual(selected["assignments"][0]["worker_cell"], "I12")

    def test_requires_schedule_heading(self):
        payload = dict(self.payload)
        payload["values"] = payload["values"][1:]
        with self.assertRaisesRegex(ValueError, "표 제목"):
            select_schedule.select_schedule(
                payload,
                process_job.dt.date(2026, 7, 23),
            )

    def test_rejects_multiple_schedule_tables(self):
        payload = dict(self.payload)
        payload["values"] = self.payload["values"] + [
            [],
            ["동향 스케줄"],
            ["보고서", "구분", "발행 요일", "월요일", "화요일", "수요일", "목요일", "금요일"],
            ["다른 보고서", "다른 구분", "다른 시간", "월 담당2", "화 담당2", "수 담당2", "목 담당2", "금 담당2"],
        ]
        with self.assertRaisesRegex(ValueError, "여러 개"):
            select_schedule.select_schedule(
                payload,
                process_job.dt.date(2026, 7, 23),
            )

    def test_rejects_duplicate_schedule_titles_on_the_same_row(self):
        payload = dict(self.payload)
        payload["values"] = [
            ["동향 스케줄", "동향 스케줄"],
            *self.payload["values"][1:],
        ]
        with self.assertRaisesRegex(ValueError, "여러 개"):
            select_schedule.select_schedule(
                payload,
                process_job.dt.date(2026, 7, 23),
            )

    def test_uses_configured_heading_and_validates_current_provenance(self):
        payload = dict(self.payload)
        payload["values"] = [["현재 동향 배정표"], *self.payload["values"][1:]]
        selected = select_schedule.select_schedule(
            payload,
            process_job.dt.date(2026, 7, 23),
            schedule_heading="현재 동향 배정표",
        )
        refs = process_job.validate_schedule_evidence(
            selected,
            process_job.dt.date(2026, 7, 23),
            {
                "spreadsheet": {
                    "id": "sheet-current",
                    "schedule_sheet": "근무",
                    "schedule_heading": "현재 동향 배정표",
                }
            },
        )
        self.assertEqual(refs, {"근무!3": "목 담당"})

    def test_rejects_legacy_fixed_range_schedule(self):
        legacy = {
            "schema_version": 1,
            "job_date": "2026-07-23",
            "weekday": "목요일",
            "source": {
                "spreadsheet_id": "sheet-current",
                "sheet_name": "근무",
                "range": "A1:L100",
            },
            "assignments": [
                {"ref": "근무!3", "worker": "현재 작업자", "worker_cell": "G3"}
            ],
        }
        with self.assertRaisesRegex(ValueError, "동적으로 확인"):
            process_job.validate_schedule_evidence(
                legacy,
                process_job.dt.date(2026, 7, 23),
                {
                    "spreadsheet": {
                        "id": "sheet-current",
                        "schedule_sheet": "근무",
                        "schedule_heading": "동향 스케줄",
                    }
                },
            )


class PublicPackageSafetyTests(unittest.TestCase):
    def test_public_config_keeps_connection_values_blank(self):
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        spreadsheet = config["spreadsheet"]
        self.assertEqual(spreadsheet["url"], "")
        self.assertEqual(spreadsheet["id"], "")
        self.assertNotIn("schedule_range", spreadsheet)
        self.assertEqual(spreadsheet["schedule_heading"], "동향 스케줄")
        self.assertLessEqual(spreadsheet["schedule_scan_max_cells"], 50000)


if __name__ == "__main__":
    unittest.main()
