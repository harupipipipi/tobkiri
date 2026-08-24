from __future__ import annotations

import copy
import json
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


class FakeBridge:
    def __init__(self, *, search_results=None, judge_result=None):
        self.search_results = [dict(item) for item in (search_results or [])]
        self.judge_result = dict(judge_result or {"status": "error"})
        self.search_calls: list[dict[str, object]] = []
        self.judge_calls: list[dict[str, object]] = []

    def web_search(self, query, *, limit=8, context=None, **kwargs):
        self.search_calls.append({"query": query, "limit": limit, "context": context})
        return [dict(item) for item in self.search_results]

    def judge_search_targets(self, user_query, candidates, *, context=None, **kwargs):
        self.judge_calls.append(
            {
                "user_query": user_query,
                "candidates": copy.deepcopy(candidates),
                "context": context,
                "kwargs": dict(kwargs),
            }
        )
        return dict(self.judge_result)


def _fresh_route_state(**values: object) -> dict[str, object]:
    issued_at = datetime.now(timezone.utc)
    return {
        "state_id": "0123456789abcdef0123456789abcdef",
        "issued_at": issued_at.isoformat(),
        "expires_at": (issued_at + timedelta(minutes=5)).isoformat(),
        **values,
    }


def _probe(candidate):
    url = str(candidate.get("url") or "")
    return {
        "final_url": url,
        "status": 200,
        "title": candidate.get("title") or url,
        "meta_description": candidate.get("snippet") or "",
        "canonical_url": url,
        "extracted_text": f"Primary source for {candidate.get('title') or url}",
        "content_type": "text/html",
        "redirected": False,
        "looks_like_login": False,
        "looks_like_paywall": False,
        "looks_like_404": False,
        "looks_like_ad_heavy": False,
        "is_search_results": False,
    }


def test_youtube_shortcut_goes_to_home_without_google_search():
    from ecosystem.search_home_pack.domain.search_target_resolver import SearchTargetResolver

    bridge = FakeBridge()
    resolver = SearchTargetResolver(bridge=bridge, probe_fn=_probe)

    decision = resolver.resolve("youtube")

    assert decision.target_url == "https://www.youtube.com/"
    assert decision.fallback_url == "https://www.google.com/search?q=youtube"
    assert decision.selected_index == 0
    assert bridge.search_calls == []


def test_youtube_query_becomes_youtube_internal_search():
    from ecosystem.search_home_pack.domain.search_target_resolver import SearchTargetResolver

    resolver = SearchTargetResolver(bridge=FakeBridge(), probe_fn=_probe)

    decision = resolver.resolve("youtube 米津玄師")

    assert decision.target_url == "https://www.youtube.com/results?search_query=%E7%B1%B3%E6%B4%A5%E7%8E%84%E5%B8%AB"
    assert decision.target_candidates[0]["final_url"] == decision.target_url


def test_bang_g_query_still_runs_best_url_resolution():
    from ecosystem.search_home_pack.domain.search_target_resolver import SearchTargetResolver

    bridge = FakeBridge(
        search_results=[
            {"url": "https://example.com/deepseek-v4", "title": "DeepSeek V4 analysis", "summary": "Best match"},
        ],
        judge_result={
            "status": "ok",
            "best_index": 0,
            "confidence": 0.92,
            "reason": "Best direct match",
            "ordered_indexes": [0],
            "reject_reasons": {},
        },
    )
    resolver = SearchTargetResolver(bridge=bridge, probe_fn=_probe)

    decision = resolver.resolve("!g deepseek v4")

    assert decision.target_url == "https://example.com/deepseek-v4"
    assert decision.fallback_url == "https://www.google.com/search?q=deepseek+v4"
    assert bridge.search_calls[0]["query"] == "deepseek v4"


def test_question_like_query_routes_to_defaultspack_ai_answer():
    from ecosystem.search_home_pack.domain.route_decision import ASK_AI_WITH_SEARCH
    from ecosystem.search_home_pack.domain.search_target_resolver import SearchTargetResolver

    resolver = SearchTargetResolver(bridge=FakeBridge(), probe_fn=_probe)

    decision = resolver.resolve("今日のニュースを教えて")

    assert decision.route_type == ASK_AI_WITH_SEARCH
    assert decision.target_url == ""
    assert decision.fallback_url == "https://www.google.com/search?q=%E4%BB%8A%E6%97%A5%E3%81%AE%E3%83%8B%E3%83%A5%E3%83%BC%E3%82%B9%E3%82%92%E6%95%99%E3%81%88%E3%81%A6"
    assert decision.metadata["defaultspack_node"] == "blocks.chat.send"
    assert decision.metadata["selected_tools"] == ["web_search"]


def test_site_name_query_still_resolves_to_best_url():
    from ecosystem.search_home_pack.domain.search_target_resolver import SearchTargetResolver

    bridge = FakeBridge(
        search_results=[
            {"url": "https://www.nikkei.com/", "title": "日本経済新聞", "summary": "Nikkei official site"},
        ],
        judge_result={
            "status": "ok",
            "best_index": 0,
            "confidence": 0.9,
            "reason": "Official site",
            "ordered_indexes": [0],
            "reject_reasons": {},
        },
    )
    resolver = SearchTargetResolver(bridge=bridge, probe_fn=_probe)

    decision = resolver.resolve("nikkei新聞")

    assert decision.target_url == "https://www.nikkei.com/"
    assert decision.route_type == "GOOGLE_REDIRECT"
    assert bridge.search_calls[0]["query"] == "nikkei新聞"


def test_site_like_query_uses_safe_brand_guess_instead_of_duckduckgo_result_page():
    from ecosystem.search_home_pack.domain.search_target_resolver import SearchTargetResolver

    bridge = FakeBridge(
        search_results=[
            {"url": "https://html.duckduckgo.com/html/?q=nikkei%E6%96%B0%E8%81%9E", "title": "DuckDuckGo", "summary": "Search results"},
        ],
    )
    resolver = SearchTargetResolver(bridge=bridge, probe_fn=_probe)

    decision = resolver.resolve("nikkei新聞")

    assert decision.target_url == "https://www.nikkei.com/"
    assert decision.target_candidates[0]["source"] == "site_guess"
    assert all(candidate["domain"] != "html.duckduckgo.com" for candidate in decision.target_candidates)
    assert decision.fallback_url == "https://www.google.com/search?q=nikkei%E6%96%B0%E8%81%9E"


def test_general_search_engine_pages_are_filtered_from_web_search_candidates():
    from ecosystem.search_home_pack.domain.search_target_resolver import SearchTargetResolver

    bridge = FakeBridge(
        search_results=[
            {"url": "https://duckduckgo.com/?q=deepseek+v4", "title": "DuckDuckGo", "summary": "Search page"},
            {"url": "https://semianalysis.com/deepseek-v4", "title": "DeepSeek V4 SemiAnalysis", "summary": "Article"},
        ],
    )
    resolver = SearchTargetResolver(bridge=bridge, probe_fn=_probe)

    decision = resolver.resolve("deepseek v4")

    assert decision.target_url == "https://semianalysis.com/deepseek-v4"
    assert [candidate["domain"] for candidate in decision.target_candidates] == ["semianalysis.com"]


def test_selected_model_is_passed_to_ai_target_judge():
    from ecosystem.search_home_pack.domain.search_target_resolver import SearchTargetResolver

    bridge = FakeBridge(
        search_results=[
            {"url": "https://example.com/a", "title": "A", "summary": "A"},
        ],
        judge_result={
            "status": "ok",
            "best_index": 0,
            "confidence": 0.9,
            "reason": "Selected",
            "ordered_indexes": [0],
            "reject_reasons": {},
        },
    )
    resolver = SearchTargetResolver(bridge=bridge, probe_fn=_probe)

    resolver.resolve("example a", context={"preferred_model": "demo/model"})

    assert bridge.judge_calls[0]["kwargs"]["preferred_model"] == "demo/model"


def test_search_failure_returns_google_fallback():
    from ecosystem.search_home_pack.domain.search_target_resolver import SearchTargetResolver

    resolver = SearchTargetResolver(bridge=FakeBridge(), probe_fn=_probe)

    decision = resolver.resolve("obscure query with no hits")

    assert decision.target_url == "https://www.google.com/search?q=obscure+query+with+no+hits"
    assert decision.fallback_url == decision.target_url
    assert decision.selected_index == -1


def test_ai_judge_best_index_controls_target_url():
    from ecosystem.search_home_pack.domain.search_target_resolver import SearchTargetResolver

    bridge = FakeBridge(
        search_results=[
            {"url": "https://example.com/a", "title": "Candidate A", "summary": "First"},
            {"url": "https://example.com/b", "title": "Candidate B", "summary": "Second"},
        ],
        judge_result={
            "status": "ok",
            "best_index": 1,
            "confidence": 0.88,
            "reason": "Candidate B is the better destination",
            "ordered_indexes": [1, 0],
            "reject_reasons": {0: "Too generic"},
        },
    )
    resolver = SearchTargetResolver(bridge=bridge, probe_fn=_probe)

    decision = resolver.resolve("candidate b")

    assert decision.target_url == "https://example.com/b"
    assert decision.selected_index == 1
    assert decision.used_ai_judge is True


def test_screenshot_failure_continues_with_text_only_judge():
    from ecosystem.search_home_pack.domain.search_target_resolver import SearchTargetResolver

    bridge = FakeBridge(
        search_results=[
            {"url": "https://example.com/a", "title": "Candidate A", "summary": "First"},
            {"url": "https://example.com/b", "title": "Candidate B", "summary": "Second"},
        ],
        judge_result={
            "status": "ok",
            "best_index": 0,
            "confidence": 0.83,
            "reason": "Text-only signal is enough",
            "ordered_indexes": [0, 1],
            "reject_reasons": {},
        },
    )
    resolver = SearchTargetResolver(
        bridge=bridge,
        probe_fn=_probe,
        screenshot_fn=lambda candidate: (_ for _ in ()).throw(RuntimeError("capture failed")),
    )

    decision = resolver.resolve("candidate a")

    assert decision.target_url == "https://example.com/a"
    assert bridge.judge_calls
    first_candidate = bridge.judge_calls[0]["candidates"][0]
    assert "screenshot_data_url" not in first_candidate


def test_unsafe_candidate_urls_are_filtered_out():
    from ecosystem.search_home_pack.domain.search_target_resolver import SearchTargetResolver

    bridge = FakeBridge(
        search_results=[
            {"url": "javascript:alert(1)", "title": "bad", "summary": "bad"},
            {"url": "http://127.0.0.1:3000/private", "title": "local", "summary": "local"},
            {"url": "https://safe.example.com/article", "title": "safe", "summary": "safe"},
        ],
        judge_result={
            "status": "ok",
            "best_index": 0,
            "confidence": 0.91,
            "reason": "Only safe candidate remained",
            "ordered_indexes": [0],
            "reject_reasons": {},
        },
    )
    resolver = SearchTargetResolver(bridge=bridge, probe_fn=_probe)

    decision = resolver.resolve("safe example")

    assert decision.target_url == "https://safe.example.com/article"
    assert len(decision.target_candidates) == 1
    assert decision.target_candidates[0]["final_url"] == "https://safe.example.com/article"


def test_explicit_local_url_never_becomes_a_routed_destination():
    from ecosystem.search_home_pack.domain.search_target_resolver import SearchTargetResolver

    resolver = SearchTargetResolver(
        bridge=FakeBridge(search_results=[]),
        probe_fn=_probe,
    )

    decision = resolver.resolve("localhost:3000/admin")

    assert urllib.parse.urlparse(decision.target_url).hostname == "www.google.com"
    assert decision.target_candidates == []
    assert decision.target_url.startswith("https://www.google.com/search?")


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("javascript:alert(1)", "unsafe_scheme"),
        ("data:text/html,fake", "unsafe_scheme"),
        ("file:///tmp/fake-secret", "unsafe_scheme"),
        ("custom://example.com/path", "unsupported_scheme"),
        ("/relative/path", "unsupported_scheme"),
        ("https://[::1", "malformed_url"),
        ("https://fake-user:fake-password@example.com/", "embedded_credentials"),
        ("https://example.com/%0d%0aheader", "control_characters"),
        ("https:\\example.com\\path", "ambiguous_url_syntax"),
        ("http://127.0.0.1/private", "private_or_local_host"),
        ("http://2130706433/private", "private_or_local_host"),
        ("http://0177.0.0.1/private", "private_or_local_host"),
        ("http://0x7f.0.0.1/private", "private_or_local_host"),
        ("http://127.1/private", "private_or_local_host"),
        ("http://0x7f000001/private", "private_or_local_host"),
        ("http://169.254.169.254/latest/meta-data/", "private_or_local_host"),
        ("http://100.64.0.1/", "private_or_local_host"),
        ("http://service.lan/", "private_or_local_host"),
        ("http://service.home/", "private_or_local_host"),
        ("http://[::1]/private", "private_or_local_host"),
        ("http://[ff02::1]/private", "private_or_local_host"),
        ("http://[fec0::1]/private", "private_or_local_host"),
        ("http://service.local/private", "private_or_local_host"),
    ],
)
def test_candidate_url_policy_rejects_unsafe_destinations(url, reason):
    from ecosystem.search_home_pack.domain.safe_url import validate_candidate_url

    result = validate_candidate_url(url, allow_localhost=False)

    assert result.ok is False
    assert result.reason == reason


def test_candidate_url_policy_normalizes_idn_host_to_punycode():
    from ecosystem.search_home_pack.domain.safe_url import validate_candidate_url

    result = validate_candidate_url("https://例え.テスト/path")

    assert result.ok is True
    assert result.normalized_url == "https://xn--r8jz45g.xn--zckzah/path"


def test_persistence_rejects_key_only_credential_query():
    from ecosystem.search_home_pack.domain.safe_url import url_safe_for_persistence

    assert url_safe_for_persistence("https://example.com/?access_token") == ""


def test_dns_resolution_rejects_any_private_answer(monkeypatch):
    from ecosystem.search_home_pack.domain import safe_url

    monkeypatch.setattr(
        safe_url.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (safe_url.socket.AF_INET, safe_url.socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (safe_url.socket.AF_INET, safe_url.socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ],
    )

    with pytest.raises(ValueError, match="dns_resolved_private_or_local_host"):
        safe_url.resolve_public_addresses("fake-public.example", 443)


def test_dns_resolution_rejects_cgnat_answer(monkeypatch):
    from ecosystem.search_home_pack.domain import safe_url

    monkeypatch.setattr(
        safe_url.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (
                safe_url.socket.AF_INET,
                safe_url.socket.SOCK_STREAM,
                6,
                "",
                ("100.64.0.1", 443),
            )
        ],
    )

    with pytest.raises(ValueError, match="dns_resolved_private_or_local_host"):
        safe_url.resolve_public_addresses("fake-cgnat.example", 443)


def test_unsafe_redirect_target_removes_entire_candidate():
    from ecosystem.search_home_pack.domain.search_target_resolver import SearchTargetResolver

    bridge = FakeBridge(search_results=[{"url": "https://safe.example.com/start", "title": "start"}])
    resolver = SearchTargetResolver(
        bridge=bridge,
        probe_fn=lambda candidate: {
            **candidate,
            "final_url": "http://127.0.0.1/private",
            "redirected": True,
        },
    )

    decision = resolver.resolve("redirect candidate")

    assert decision.target_candidates == []
    assert decision.target_url.startswith("https://www.google.com/search?")
    assert decision.resolution_reason == "no_viable_target_fallback"


def test_desktop_route_state_round_trip(tmp_path):
    from ecosystem.search_home_pack import desktop_app

    payload = _fresh_route_state(
        query="openai pricing latest",
        target_url="https://platform.openai.com/docs/overview",
    )
    desktop_app.persist_route_state(payload, root=tmp_path)

    assert desktop_app.load_route_state(root=tmp_path) == payload

    desktop_app.clear_route_state(root=tmp_path)
    assert desktop_app.load_route_state(root=tmp_path) == {}


def test_desktop_route_state_does_not_persist_secret_bearing_urls(tmp_path):
    from ecosystem.search_home_pack import desktop_app

    fake_secret = "fake-secret-do-not-store"
    desktop_app.persist_route_state(
        _fresh_route_state(
            query=f"https://example.com/callback?access_token={fake_secret}",
            target_url=f"https://example.com/callback?access_token={fake_secret}",
            target_candidates=[
                {"url": f"https://example.com/path#{fake_secret}"}
            ],
        ),
        root=tmp_path,
    )

    serialized = desktop_app.route_state_path(root=tmp_path).read_text(encoding="utf-8")
    assert fake_secret not in serialized
    assert desktop_app.load_route_state(root=tmp_path)["target_url"] == ""


def test_desktop_route_state_does_not_persist_local_destinations(tmp_path):
    from ecosystem.search_home_pack import desktop_app

    desktop_app.persist_route_state(
        _fresh_route_state(
            query="http://127.0.0.1/admin",
            target_url="http://100.64.0.1/admin",
            fallback_url="https://www.google.com/search?q=safe",
            target_candidates=[
                {
                    "url": "https://example.com/path",
                    "domain": "forged.invalid",
                }
            ],
        ),
        root=tmp_path,
    )

    restored = desktop_app.load_route_state(root=tmp_path)
    assert restored["query"] == ""
    assert restored["target_url"] == ""
    assert restored["fallback_url"].startswith("https://www.google.com/")
    assert restored["target_candidates"][0]["domain"] == "example.com"


def test_desktop_route_state_rejects_stale_or_tampered_restore(tmp_path):
    from ecosystem.search_home_pack import desktop_app

    stale = _fresh_route_state(
        query="stale",
        target_url="https://example.com/",
        expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    )
    desktop_app.route_state_path(root=tmp_path).parent.mkdir(parents=True, exist_ok=True)
    desktop_app.route_state_path(root=tmp_path).write_text(
        json.dumps(stale),
        encoding="utf-8",
    )
    assert desktop_app.load_route_state(root=tmp_path) == {}

    tampered = _fresh_route_state(
        query="tampered",
        target_url="https://example.com/",
        state_id="invalid state id",
    )
    desktop_app.route_state_path(root=tmp_path).write_text(
        json.dumps(tampered),
        encoding="utf-8",
    )
    assert desktop_app.load_route_state(root=tmp_path) == {}
