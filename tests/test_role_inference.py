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
            "260803 작업자병 취합.hwp", "afternoon", "오후", "오후/총괄", "작업자병", "afternoon_aggregate"
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
    MATCHING = {"review_threshold": 0.68, "auto_threshold": 0.82, "ambiguity_margin": 0.035}

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

    def test_japan_packet_uses_file_covering_complete_set(self):
        japan = [
            process_job.Candidate("japan", "첫 기사", "첫 매체", "8.4", workgroup="일본문화원"),
            process_job.Candidate("japan", "둘째 기사", "둘째 매체", "8.4", workgroup="일본문화원"),
        ]
        auxiliary = process_job.Candidate(
            "worker", "첫 기사", "첫 매체", "8.4", source_file="aux.hwp",
            owner="보조", worker="작업자정", extra={
                "comparison_stage": "morning", "profile_complete": True,
                "source_kind": "morning_auxiliary", "schedule_refs": ["근무!6"],
            },
        )
        aggregate = [
            process_job.Candidate(
                "worker", title, media, "8.4", source_file="aggregate.hwp",
                owner="오전/총괄", worker="작업자병", extra={
                    "comparison_stage": "morning", "profile_complete": True,
                    "source_kind": "morning_aggregate", "schedule_refs": ["근무!5"],
                },
            )
            for title, media in (("첫 기사", "첫 매체"), ("둘째 기사", "둘째 매체"))
        ]
        result = process_job.enrich_special_source_roles(
            japan, [auxiliary, *aggregate], {"review_threshold": 0.5}
        )
        self.assertEqual({("오전/총괄", "작업자병")}, {(item.owner, item.worker) for item in result})

    def test_slash_group_sets_representative_title(self):
        articles = [
            process_job.Article("sample.hwp", 1, "분류", "첫 매체", "8.3", "첫 제목", "첫 제목"),
            process_job.Article("sample.hwp", 2, "분류", "둘째 매체", "", "둘째 제목", "둘째 제목"),
        ]
        process_job.apply_front_titles(
            articles, [{"category": "분류", "title": "첫 제목을 편집한 묶음 제목", "media": "첫 매체/둘째 매체"}]
        )
        self.assertEqual("첫 제목을 편집한 묶음 제목", articles[0].canonical_title)
        self.assertTrue(articles[1].similar)

    def test_materially_stronger_individual_draft_beats_looser_regular_match(self):
        article = process_job.Article(
            "final.hwp", 1, "분류", "현재 매체", "8.10",
            "현재 최종 기사 제목", "현재 최종 기사 제목",
        )
        regular = process_job.Candidate(
            "regular", "과거의 다른 기사 제목", "현재 매체", "8.10",
            extra={"profile_complete": True},
        )
        draft = process_job.Candidate(
            "worker", "현재 최종 기사 제목", "현재 매체", "",
            workgroup="1조", owner="국내", worker="작업자을",
            extra={
                "source_kind": "domestic_draft", "comparison_stage": "afternoon",
                "profile_complete": True, "priority": 100,
            },
        )
        chosen, _, reasons, _ = process_job.choose_origin(
            article, {"regular": [regular], "japan": [], "worker": [draft]}, self.MATCHING
        )
        self.assertIs(draft, chosen)
        self.assertEqual([], reasons)

    def test_near_equal_individual_draft_keeps_regular_origin(self):
        article = process_job.Article(
            "final.hwp", 1, "분류", "현재 매체", "8.10", "동일 기사", "동일 기사"
        )
        regular = process_job.Candidate(
            "regular", "동일 기사", "현재 매체", "8.10",
            extra={"profile_complete": True},
        )
        draft = process_job.Candidate(
            "worker", "동일 기사", "현재 매체", "",
            extra={
                "source_kind": "domestic_draft", "comparison_stage": "afternoon",
                "profile_complete": True, "priority": 100,
            },
        )
        chosen, _, reasons, _ = process_job.choose_origin(
            article, {"regular": [regular], "japan": [], "worker": [draft]}, self.MATCHING
        )
        self.assertIs(regular, chosen)
        self.assertEqual([], reasons)

    def test_clear_review_threshold_draft_has_no_low_score_review(self):
        article = process_job.Article(
            "final.hwp", 1, "분류", "Bloomberg", "7.22",
            "코스피, 레버리지 청산 마무리 단계에 급반등",
            "코스피, 레버리지 청산 마무리 단계에 급반등",
        )
        regular = process_job.Candidate(
            "regular", "마진거래 청산 임박... 한국 증시 급등", "Bloomberg", "7.22",
            extra={"profile_complete": True},
        )
        draft = process_job.Candidate(
            "worker", "한국 증시, 레버리지 투자 청산 마무리 단계라는 증권사 분석에 급반등",
            "Bloomberg", "7.22", workgroup="1조", owner="국내", worker="작업자을",
            extra={
                "source_kind": "domestic_draft", "comparison_stage": "afternoon",
                "profile_complete": True, "priority": 100,
            },
        )
        chosen, score, reasons, _ = process_job.choose_origin(
            article, {"regular": [regular], "japan": [], "worker": [draft]}, self.MATCHING
        )
        self.assertIs(draft, chosen)
        self.assertGreaterEqual(score, self.MATCHING["review_threshold"])
        self.assertEqual([], reasons)

    def test_direct_japan_confirmation_overrides_regular_rewrite(self):
        article = process_job.Article(
            "final.hwp", 1, "분류", "교도통신", "7.23",
            "북한 핵개발 대응 공조 강화", "북한 핵개발 대응 공조 강화",
        )
        regular = process_job.Candidate("regular", article.canonical_title, "교도통신", "7.23")
        japan = process_job.Candidate(
            "japan", "중국 미사일 우려 및 대북 연대 공유", "교도통신", "7.23",
            source_file="japan.hwp",
        )
        chosen, score, reasons = process_job.confirmed_japan_origin(
            regular, article, [japan], {
                "included": True, "source_file": "japan.hwp", "source_title": japan.title,
                "evidence": ["현재 일본 원문과 최종 본문 직접 대조"],
            }
        )
        self.assertIs(japan, chosen)
        self.assertGreater(score, 0)
        self.assertEqual([], reasons)

    def test_latest_group_title_and_implicit_child_subject_are_preserved(self):
        representative = process_job.Article(
            "final.hwp", 1, "분류", "대표 매체", "7.22",
            "이 대통령, 젠슨 황과 회동", "이 대통령, 젠슨 황과 회동",
        )
        explicit_child = process_job.Article(
            "final.hwp", 2, "분류", "첫 매체", "7.22",
            "이재명 대통령, 첫 일정", "이재명 대통령, 첫 일정",
            starred=True, similar=True,
        )
        implicit_child = process_job.Article(
            "final.hwp", 3, "분류", "둘째 매체", "7.22",
            "이재명 대통령, 둘째 일정", "이재명 대통령, 둘째 일정",
            similar=True,
        )
        candidate = process_job.Candidate(
            "worker", "이 대통령, 빅테크 수장 연쇄 회동 예정", "대표 매체", "7.22",
            source_file="오전 2차.hwp", extra={
                "comparison_stage": "morning", "source_kind": "morning_aggregate",
                "group_representative": True, "body_title": representative.body_title,
                "article_order": 1,
            },
        )
        process_job.apply_latest_group_titles(
            [representative, explicit_child, implicit_child], [candidate], 0.68
        )
        process_job.align_group_child_titles([representative, explicit_child, implicit_child])
        self.assertEqual(candidate.title, representative.canonical_title)
        self.assertEqual("이재명 대통령, 첫 일정", explicit_child.canonical_title)
        self.assertEqual("이 대통령, 둘째 일정", implicit_child.canonical_title)

    def test_similar_representative_inherits_sandwiched_draft_lineage(self):
        representative = process_job.Article(
            "final.hwp", 1, "정치", "대표 매체", "7.22", "대표 기사", "대표 기사"
        )
        similar = process_job.Article(
            "final.hwp", 2, "정치", "유사 매체", "7.22", "유사 기사", "유사 기사",
            starred=True, similar=True,
        )
        draft_common = {
            "source_type": "worker", "source_file": "domestic.hwp",
            "workgroup": "1조", "owner": "국내", "worker": "작업자을",
            "extra": {
                "source_kind": "domestic_draft", "comparison_stage": "afternoon",
                "profile_complete": True, "schedule_refs": ["근무!4"],
            },
        }
        direct = [
            process_job.Candidate(title="앞 기사", media="앞 매체", date="7.22", **draft_common),
            process_job.Candidate(title="뒤 기사", media="뒤 매체", date="7.22", **draft_common),
        ]
        aggregate = [
            process_job.Candidate(
                "worker", title, media, "7.22", "aggregate.hwp",
                extra={
                    "source_kind": "afternoon_aggregate", "comparison_stage": "afternoon",
                    "article_order": order, "body_title": title,
                },
            )
            for order, title, media in (
                (10, "앞 기사", "앞 매체"), (11, "대표 기사", "대표 매체"),
                (12, "유사 기사", "유사 매체"), (13, "뒤 기사", "뒤 매체"),
            )
        ]
        inferred = process_job.infer_representative_draft_lineage(
            [representative, similar], [*direct, *aggregate], self.MATCHING
        )
        self.assertEqual(("1조", "국내", "작업자을"), (
            inferred[1].workgroup, inferred[1].owner, inferred[1].worker
        ))


class AggregateRecoveryTests(unittest.TestCase):
    MATCHING = {"review_threshold": 0.68, "auto_threshold": 0.82, "ambiguity_margin": 0.035}

    def test_unrelated_slash_title_keeps_first_body_title(self):
        articles = [
            process_job.Article("sample.hwp", 1, "분류", "첫 매체", "8.3", "첫 기사", "첫 기사"),
            process_job.Article("sample.hwp", 2, "분류", "둘째 매체", "8.3", "둘째 기사", "둘째 기사"),
        ]
        process_job.apply_front_titles(
            articles,
            [{"category": "분류", "title": "전혀 다른 주제", "media": "첫 매체/둘째 매체"}],
        )
        self.assertEqual("첫 기사", articles[0].canonical_title)
        self.assertTrue(articles[0].group_representative)
        self.assertTrue(articles[1].similar)

    def test_lineage_requires_immediate_cluster_boundaries(self):
        representative = process_job.Article(
            "final.hwp", 1, "분류", "대표 매체", "7.22", "대표 기사", "대표 기사"
        )
        similar = process_job.Article(
            "final.hwp", 2, "분류", "유사 매체", "7.22", "유사 기사", "유사 기사",
            similar=True,
        )
        draft_common = {
            "source_type": "worker", "source_file": "draft.hwp",
            "workgroup": "1조", "owner": "국내", "worker": "작업자을",
            "extra": {
                "source_kind": "domestic_draft", "comparison_stage": "afternoon",
                "profile_complete": True,
            },
        }
        direct = [
            process_job.Candidate(title="앞 기사", media="앞 매체", date="7.22", **draft_common),
            process_job.Candidate(title="뒤 기사", media="뒤 매체", date="7.22", **draft_common),
        ]
        def aggregate(title, media, order):
            return process_job.Candidate(
                "worker", title, media, "7.22", "aggregate.hwp",
                extra={
                    "source_kind": "afternoon_aggregate", "comparison_stage": "afternoon",
                    "article_order": order, "body_title": title,
                },
            )
        sequence = [
            aggregate("앞 기사", "앞 매체", 1),
            aggregate("무관 기사", "무관 매체", 2),
            aggregate("대표 기사", "대표 매체", 3),
            aggregate("유사 기사", "유사 매체", 4),
            aggregate("뒤 기사", "뒤 매체", 5),
        ]
        inferred = process_job.infer_representative_draft_lineage(
            [representative, similar], [*direct, *sequence], self.MATCHING
        )
        self.assertEqual({}, inferred)

    def test_carried_aggregate_omission_gets_late_role(self):
        final = process_job.Article(
            "final.hwp", 1, "분류", "Other Wire", "8.3", "국제 정세 분석", "국제 정세 분석"
        )
        afternoon = process_job.Candidate(
            "worker", "반도체 주가 급락", "Current Finance", "8.3", source_file="afternoon.hwp",
            workgroup="오후", owner="오후/총괄", worker="작업자병",
            extra={"source_kind": "afternoon_aggregate", "comparison_stage": "afternoon"},
        )
        morning = process_job.Candidate(
            "worker", afternoon.title, afternoon.media, afternoon.date, source_file="morning 1차.hwp",
            extra={"source_kind": "morning_aggregate", "comparison_stage": "morning"},
        )
        omitted = process_job.omitted_worker_candidates(
            [final], [afternoon, morning], self.MATCHING
        )
        self.assertEqual(1, len(omitted))
        self.assertEqual(("1조", "오후/총괄"), (omitted[0].workgroup, omitted[0].owner))
        self.assertEqual("afternoon_aggregate_omitted", omitted[0].extra["source_kind"])

    def test_latest_category_only_extends_a_compatible_final_category(self):
        social = process_job.Article(
            "final.hwp", 1, "사회", "BBC", "8.3", "폭염 기사", "폭염 기사"
        )
        culture = process_job.Article(
            "final.hwp", 2, "문화", "도쿄신문", "8.3", "문화 기사", "문화 기사"
        )
        candidates = [
            process_job.Candidate(
                "worker", "폭염 기사", "BBC", "8.3", source_file="morning 2차.hwp",
                extra={"source_kind": "morning_aggregate", "category": "사회‧문화"},
            ),
            process_job.Candidate(
                "worker", "문화 기사", "도쿄신문", "8.3", source_file="morning 2차.hwp",
                extra={"source_kind": "morning_aggregate", "category": "중동 전쟁"},
            ),
        ]
        process_job.apply_latest_aggregate_categories(
            [social, culture], candidates, 0.68, 0.82
        )
        self.assertEqual("사회‧문화", social.category)
        self.assertEqual("문화", culture.category)

    def test_latest_explicit_similar_row_is_recovered(self):
        final = process_job.Article(
            "final.hwp", 1, "경제·기업", "Bloomberg", "8.3", "대표 기사", "대표 기사",
            body_present=True,
        )
        representative = process_job.Candidate(
            "worker", "대표 기사", "Bloomberg", "8.3", source_file="morning 2차.hwp",
            extra={
                "source_kind": "morning_aggregate", "comparison_stage": "morning",
                "article_order": 1, "similar": False, "category": "경제·기업",
            },
        )
        similar = process_job.Candidate(
            "worker", "명시된 유사 기사", "Reuters", "8.3", source_file="morning 2차.hwp",
            extra={
                "source_kind": "morning_aggregate", "comparison_stage": "morning",
                "article_order": 2, "similar": True, "starred": True, "category": "경제·기업",
            },
        )
        regular = process_job.Candidate(
            "regular", similar.title, similar.media, similar.date,
            workgroup="정기", owner="오후/총괄", worker="작업자병",
            extra={"profile_complete": True},
        )
        additions = process_job.automatic_similar_additions(
            [final], [representative, similar],
            {"regular": [regular], "japan": [], "worker": [representative, similar]},
            self.MATCHING,
        )
        self.assertEqual(1, len(additions))
        self.assertIs(regular, additions[0]["candidate"])

    def test_latest_target_date_article_uses_late_morning_role(self):
        article = process_job.Article(
            "final.hwp", 1, "증시", "현재 매체", "8.3", "새 기사", "새 기사"
        )
        origin = process_job.Candidate(
            "worker", "새 기사", "현재 매체", "8.3", source_file="morning 2차.hwp",
            workgroup="2조", owner="오전/총괄", worker="작업자병",
            extra={"source_kind": "morning_aggregate", "comparison_stage": "morning"},
        )
        changed = process_job.late_morning_aggregate_origin(
            article, origin, {"regular": [], "japan": [], "worker": [origin]},
            self.MATCHING, process_job.dt.date(2026, 8, 3),
        )
        self.assertEqual(("1조", "오후/총괄"), (changed.workgroup, changed.owner))
        self.assertEqual("late_morning_aggregate", changed.extra["source_kind"])

    def test_front_only_same_file_packet_stays_together(self):
        articles = [
            process_job.Article("f", 1, "분류", "A", "8.3", "첫째", "첫째"),
            process_job.Article("f", 2, "분류", "B", "8.3", "둘째", "둘째"),
            process_job.Article("f", 3, "분류", "C", "8.3", "셋째", "셋째"),
        ]
        rows = [["첫째"], ["둘째"], ["셋째"]]
        details = [
            {"origin_source_kind": "domestic_draft", "origin_file": "draft.hwp", "origin_comparison_stage": "afternoon"},
            {"origin_source_kind": "morning_auxiliary", "origin_file": "aux.hwp", "origin_comparison_stage": "morning"},
            {"origin_source_kind": "domestic_draft", "origin_file": "draft.hwp", "origin_comparison_stage": "afternoon"},
        ]
        reordered, _ = process_job.reorder_front_only_results(articles, rows, details)
        self.assertEqual([["첫째"], ["셋째"], ["둘째"]], reordered)


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
