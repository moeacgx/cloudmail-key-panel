from __future__ import annotations

import html
import re
from typing import Iterable

_HYPHENATED_CODE_PATTERN = r"[A-Z0-9]{3}-[A-Z0-9]{3}"
_HINTED_CODE_PATTERN = rf"(?=[A-Z0-9-]*\d)(?:{_HYPHENATED_CODE_PATTERN}|[A-Z0-9]{{4,10}})"
_CODE_HINT_PATTERNS = [
    re.compile(
        rf"(?:code|验证码|驗證碼|otp|password)\s*(?:is|:|：|-)?\s*({_HINTED_CODE_PATTERN})",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?:enter\s+this\s+code|login\s+code|verification\s+code)\s*(?:is|:|：|-)?\s*({_HINTED_CODE_PATTERN})",
        re.IGNORECASE,
    ),
]
_GENERIC_CODE_PATTERN = re.compile(
    rf"(?<![A-Z0-9])((?=[A-Z0-9-]*[A-Z])(?=[A-Z0-9-]*\d){_HYPHENATED_CODE_PATTERN}|\d{{4,8}})(?![A-Z0-9])",
    re.IGNORECASE,
)
_TAG_PATTERN = re.compile(r"<[^>]+>")
_WHITESPACE_PATTERN = re.compile(r"\s+")


def extract_verification_codes(
    subject: str = "",
    text: str = "",
    html_content: str = "",
    *,
    custom_patterns: Iterable[str] = (),
    include_defaults: bool = True,
    **kwargs: str,
) -> list[str]:
    html_source = html_content or kwargs.get("html", "")
    sources = [subject or "", text or "", _normalize_html(html_source)]
    candidates = _extract_custom_patterns(sources, custom_patterns)
    if include_defaults:
        custom_values = tuple(value.casefold() for value in candidates if value)
        candidates.extend(
            value
            for value in _extract_from_sources(sources)
            if not any(value.casefold() in custom_value for custom_value in custom_values)
        )
    return _deduplicate(candidates)


def _extract_custom_patterns(sources: Iterable[str], patterns: Iterable[str]) -> list[str]:
    candidates: list[str] = []
    for raw_pattern in patterns:
        pattern = re.compile(str(raw_pattern), re.IGNORECASE)
        for source in sources:
            for match in pattern.finditer(source):
                if "code" in match.re.groupindex:
                    value = match.group("code")
                elif match.lastindex:
                    value = match.group(1)
                else:
                    value = match.group(0)
                if value:
                    candidates.append(value)
    return candidates


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
