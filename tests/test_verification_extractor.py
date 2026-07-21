import json

import httpx
import pytest

from app.verification_extractor import (
    OpenAICompatibleCodeExtractor,
    VerificationCodeExtractor,
    VerificationExtractionError,
)


def _ai_extractor(handler) -> OpenAICompatibleCodeExtractor:
    return OpenAICompatibleCodeExtractor(
        base_url="https://ai.example.com/v1",
        api_key="secret-key",
        model="extract-model",
        transport=httpx.MockTransport(handler),
    )


def test_rules_mode_uses_custom_patterns_before_defaults() -> None:
    extractor = VerificationCodeExtractor(
        mode="rules",
        custom_patterns=(r"ticket=(?P<code>[A-Z]{2}/\d{4})",),
    )

    assert extractor.extract("", "ticket=AB/7788; backup 112233", "") == ["AB/7788", "112233"]


def test_ai_fallback_does_not_call_api_when_rules_match() -> None:
    def unexpected_request(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("规则命中时不应调用 AI")

    extractor = VerificationCodeExtractor(
        mode="ai_fallback",
        ai_extractor=_ai_extractor(unexpected_request),
    )

    assert extractor.extract("Your code is 112233", "", "") == ["112233"]


def test_ai_fallback_calls_openai_compatible_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://ai.example.com/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer secret-key"
        payload = json.loads(request.content)
        assert payload["model"] == "extract-model"
        assert "ZX-91QK" in payload["messages"][1]["content"]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"code":"ZX-91QK"}'}}]},
        )

    extractor = VerificationCodeExtractor(
        mode="ai_fallback",
        ai_extractor=_ai_extractor(handler),
    )

    assert extractor.extract("Security token ZX-91QK", "", "") == ["ZX-91QK"]


def test_ai_only_skips_deterministic_code() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"code":"ABC-123"}'}}]},
        )

    extractor = VerificationCodeExtractor(
        mode="ai_only",
        ai_extractor=_ai_extractor(handler),
    )

    assert extractor.extract("Codes: 112233 and ABC-123", "", "") == ["ABC-123"]


def test_ai_hallucinated_code_is_rejected() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"code":"NOT-REAL"}'}}]},
        )

    extractor = VerificationCodeExtractor(
        mode="ai_only",
        ai_extractor=_ai_extractor(handler),
    )

    with pytest.raises(VerificationExtractionError, match="不在原邮件"):
        extractor.extract("No verification token here", "", "")


def test_ai_http_error_is_reported() -> None:
    extractor = VerificationCodeExtractor(
        mode="ai_only",
        ai_extractor=_ai_extractor(lambda _request: httpx.Response(503)),
    )

    with pytest.raises(VerificationExtractionError, match="请求失败"):
        extractor.extract("Code pending", "", "")


def test_ai_fallback_without_configuration_is_not_treated_as_no_code() -> None:
    extractor = VerificationCodeExtractor(mode="ai_fallback")

    with pytest.raises(VerificationExtractionError, match="尚未配置"):
        extractor.extract("Unknown token format: AB/CD", "", "")
