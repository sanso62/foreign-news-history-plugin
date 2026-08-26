import base64
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
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
PLUGIN_DIR = ROOT / "plugins" / "foreign-news-history"


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


process_job = sys.modules.get("process_job") or load_module("process_job", "process_job.py")


def hwp_record(tag_id: int, payload: bytes, level: int = 0) -> bytes:
    if len(payload) >= 0xFFF:
        header = tag_id | (level << 10) | (0xFFF << 20)
        return header.to_bytes(4, "little") + len(payload).to_bytes(4, "little") + payload
    header = tag_id | (level << 10) | (len(payload) << 20)
    return header.to_bytes(4, "little") + payload


class HyperlinkExtractionTests(unittest.TestCase):
    def test_url_extraction_removes_only_a_trailing_path_slash(self):
        self.assertEqual(
            "https://example.test/story",
            process_job.clean_hyperlink_url("https://example.test/story/"),
        )

    def test_hwpx_heading_hyperlink_is_attached_to_article(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <section>
          <p><run><t>&lt;Example News 8.4&gt; 서로 다른 제목</t></run>
            <fieldBegin type="HYPERLINK"><parameters>
              <stringParam name="Path">https://Example.test/story?id=7</stringParam>
            </parameters></fieldBegin>
          </p>
        </section>"""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sample.hwpx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("Contents/section0.xml", xml)
            records = process_job.extract_hwpx_records(path)
            articles = process_job.parse_document(path)
        self.assertEqual("https://Example.test/story?id=7", records[0].url)
        self.assertEqual("https://Example.test/story?id=7", articles[0].url)

    def test_hwp_control_header_hyperlink_is_attached_to_paragraph(self):
        heading = "<Example News 8.4> 기사 제목".encode("utf-16le")
        hyperlink = b"\x01" + "https\\://Example.test/story;1;0;0;".encode("utf-16le")
        stream = b"".join(
            [
                hwp_record(66, b""),
                hwp_record(67, heading),
                hwp_record(71, hyperlink),
            ]
        )
        records = process_job.extract_hwp_records_from_stream(stream)
        self.assertEqual(1, len(records))
        self.assertEqual("<Example News 8.4> 기사 제목", records[0].text)
        self.assertEqual("https://Example.test/story", records[0].url)


class UrlFirstMatchingTests(unittest.TestCase):
    MATCHING = {
        "review_threshold": 0.68,
        "auto_threshold": 0.82,
        "ambiguity_margin": 0.035,
    }

    def article(self, title="최종 제목", normalized_url=""):
        return process_job.Article(
            source_file="final.hwpx",
            order=1,
            category="분류",
            media="Final Media",
            date="8.4",
            body_title=title,
            canonical_title=title,
            normalized_url=normalized_url,
        )

    def test_same_normalized_url_overrides_title_media_and_date(self):
        article = self.article("전혀 다른 최종 제목", "https://example.test/a")
        candidate = process_job.Candidate(
            source_type="regular",
            title="무관한 정기 제목",
            media="Other Media",
            date="7.1",
            normalized_url="https://example.test/a",
        )
        self.assertEqual(1.0, process_job.candidate_score(article, candidate))
        self.assertTrue(process_job.candidate_identity_matches(article, candidate))

    def test_different_normalized_urls_are_inconclusive_and_use_legacy_fallback(self):
        article = self.article("같은 제목", "https://example.test/a")
        candidate = process_job.Candidate(
            source_type="regular",
            title="같은 제목",
            media="Final Media",
            date="8.4",
            normalized_url="https://example.test/b",
        )
        self.assertGreaterEqual(process_job.candidate_score(article, candidate), 0.99)
        self.assertTrue(process_job.candidate_identity_matches(article, candidate))

    def test_missing_url_uses_legacy_title_media_date_fallback(self):
        article = self.article("같은 제목")
        candidate = process_job.Candidate(
            source_type="regular",
            title="같은 제목",
            media="Final Media",
            date="8.4",
            normalized_url="https://example.test/a",
        )
        self.assertGreaterEqual(process_job.candidate_score(article, candidate), 0.99)

    def test_direct_draft_wins_when_it_shares_regular_url(self):
        article = self.article("정기 제목")
        regular = process_job.Candidate(
            source_type="regular",
            title="정기 제목",
            media="Final Media",
            date="8.4",
            normalized_url="https://example.test/a",
            extra={"comparison_stage": "reference"},
        )
        draft = process_job.Candidate(
            source_type="worker",
            title="제목이 크게 바뀐 개별 초안",
            media="Other Media",
            date="8.3",
            normalized_url="https://example.test/a",
            extra={
                "comparison_stage": "afternoon",
                "source_kind": "domestic_draft",
                "priority": 1,
                "profile_complete": True,
                "body_content_count": 2,
                "similar": False,
            },
        )
        chosen, score, reasons, _ = process_job.choose_origin(
            article,
            {"regular": [regular], "japan": [], "worker": [draft]},
            self.MATCHING,
        )
        self.assertIs(draft, chosen)
        self.assertGreaterEqual(score, self.MATCHING["review_threshold"])
        self.assertFalse(any("낮은 매칭" in reason for reason in reasons))

    def test_title_only_similar_line_is_not_an_actual_direct_draft(self):
        article = self.article("정기 제목", "https://example.test/a")
        regular = process_job.Candidate(
            source_type="regular",
            title="정기 제목",
            media="Final Media",
            date="8.4",
            normalized_url="https://example.test/a",
        )
        title_only = process_job.Candidate(
            source_type="worker",
            title="유사보도 제목줄",
            media="Final Media",
            date="8.4",
            normalized_url="https://example.test/a",
            extra={
                "source_kind": "domestic_draft",
                "body_content_count": 0,
                "similar": True,
            },
        )
        self.assertIsNone(
            process_job.url_linked_initial_draft_origin(
                article,
                [regular],
                [title_only],
                self.MATCHING,
            )
        )

    def test_different_final_url_cannot_be_owned_by_nearby_url_pair(self):
        article = self.article("서로 비슷한 최종 제목", "https://example.test/final")
        regular = process_job.Candidate(
            source_type="regular",
            title="서로 비슷한 정기 제목",
            media="Final Media",
            date="8.4",
            normalized_url="https://example.test/pair",
        )
        draft = process_job.Candidate(
            source_type="worker",
            title="서로 비슷한 개별 초안",
            media="Final Media",
            date="8.4",
            normalized_url="https://example.test/pair",
            extra={
                "source_kind": "domestic_draft",
                "body_content_count": 2,
                "similar": False,
            },
        )
        self.assertIsNone(
            process_job.url_linked_initial_draft_origin(
                article,
                [regular],
                [draft],
                self.MATCHING,
            )
        )

    def test_private_normalizer_trailing_slash_output_has_one_comparison_identity(self):
        article = self.article("최종 제목")
        article.url = "https://publisher.test/story/7"
        candidate = process_job.Candidate(
            source_type="worker",
            title="작업본 제목",
            url="https://publisher.test/story/7?tracking=1",
        )
        normalized = {
            article.url: "https://publisher.test/story/7",
            candidate.url: "https://publisher.test/story/7/",
        }
        with mock.patch.object(
            process_job,
            "normalize_article_urls_for_run",
            return_value=normalized,
        ):
            process_job.apply_normalized_article_urls(
                [article],
                [candidate],
                "unused-in-mocked-test",
            )
        self.assertEqual(1.0, process_job.candidate_score(article, candidate))


class PrivateNormalizerBoundaryTests(unittest.TestCase):
    def private_env(self, directory: str, token: str = "test-secret") -> Path:
        path = Path(directory) / "private.env"
        api_host = ".".join(("api", "github", "com"))
        path.write_text(
            "FOREIGN_NEWS_RULES_TOKEN=" + token + "\n"
            "FOREIGN_NEWS_URL_NORMALIZER_API_URL=https://"
            + api_host
            + "/repos/private/project/contents/module.js?ref=main\n",
            encoding="utf-8",
        )
        return path

    def test_github_api_module_is_decoded_in_memory(self):
        source = "export function normalizeArticleUrl(value) { return value; }"
        payload = json.dumps(
            {
                "encoding": "base64",
                "content": base64.b64encode(source.encode("utf-8")).decode("ascii"),
            }
        ).encode("utf-8")
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = payload
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = self.private_env(temp_dir)
            with mock.patch.object(process_job.urllib.request, "urlopen", return_value=response):
                loaded = process_job.fetch_private_url_normalizer_source(env_path)
        self.assertEqual(source, loaded)

    def test_fetch_failure_does_not_expose_token_or_api_setting(self):
        token = "do-not-print-this-token"
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = self.private_env(temp_dir, token)
            api_setting = process_job.private_env_values(env_path)[
                "FOREIGN_NEWS_URL_NORMALIZER_API_URL"
            ]
            with mock.patch.object(
                process_job.urllib.request,
                "urlopen",
                side_effect=RuntimeError(token + api_setting),
            ):
                with self.assertRaises(RuntimeError) as raised:
                    process_job.fetch_private_url_normalizer_source(env_path)
        message = str(raised.exception)
        self.assertNotIn(token, message)
        self.assertNotIn(api_setting, message)

    def test_helper_protocol_returns_only_normalized_values(self):
        source = "export function normalizeArticleUrl(value) { return value; }"
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"ok": True, "normalized": ["https://example.test/a"]}),
            stderr="private failure details must be ignored",
        )
        with mock.patch.object(
            process_job,
            "fetch_private_url_normalizer_source",
            return_value=source,
        ), mock.patch.object(process_job.subprocess, "run", return_value=completed):
            with tempfile.NamedTemporaryFile() as node:
                result = process_job.normalize_article_urls_for_run(
                    ["https://EXAMPLE.test/a"],
                    node.name,
                )
        self.assertEqual(
            {"https://EXAMPLE.test/a": "https://example.test/a"},
            result,
        )

    def test_public_plugin_does_not_embed_repository_endpoint(self):
        forbidden = ("api.github.com/repos/",)
        text = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in PLUGIN_DIR.rglob("*")
            if path.is_file() and path.suffix.lower() in {".py", ".mjs", ".md", ".json"}
        )
        for value in forbidden:
            self.assertNotIn(value, text)


if __name__ == "__main__":
    unittest.main()
