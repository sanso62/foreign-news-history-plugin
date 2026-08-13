import importlib.util
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


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


process_job = load_module("process_job", "process_job.py")
discover_context = load_module("discover_context", "discover_context.py")


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


if __name__ == "__main__":
    unittest.main()
