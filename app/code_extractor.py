from __future__ import annotations

import html
import re
from typing import Iterable

_CODE_HINT_PATTERNS = [
    re.compile(r"(?:code|验证码|驗證碼|otp|password)\s*(?:is|:|：|-)?\s*([A-Z0-9]{4,10})", re.IGNORECASE),
    re.compile(r"(?:enter\s+this\s+code|login\s+code|verification\s+code)\s*(?:is|:|：|-)?\s*([A-Z0-9]{4,10})", re.IGNORECASE),
]
_GENERIC_CODE_PATTERN = re.compile(r"(?<![A-Z0-9])(\d{4,8})(?![A-Z0-9])", re.IGNORECASE)
_TAG_PATTERN = re.compile(r"<[^>]+>")
_WHITESPACE_PATTERN = re.compile(r"\s+")


def extract_verification_codes(subject: str = "", text: str = "", html_content: str = "", **kwargs: str) -> list[str]:
    html_source = html_content or kwargs.get("html", "")
    sources = [subject or "", text or "", _normalize_html(html_source)]
    return _deduplicate(_extract_from_sources(sources))


def _extract_from_sources(sources: Iterable[str]) -> list[str]:
    candidates: list[str] = []

    for source in sources:
        for pattern in _CODE_HINT_PATTERNS:
            candidates.extend(match.group(1) for match in pattern.finditer(source))

    for source in sources:
        candidates.extend(match.group(1) for match in _GENERIC_CODE_PATTERN.finditer(source))

    return candidates


def _deduplicate(codes: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []

    for code in codes:
        normalized = code.strip().upper()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)

    return ordered


def _normalize_html(value: str) -> str:
    if not value:
        return ""

    text = html.unescape(value)
    text = _TAG_PATTERN.sub(" ", text)
    return _WHITESPACE_PATTERN.sub(" ", text).strip()
