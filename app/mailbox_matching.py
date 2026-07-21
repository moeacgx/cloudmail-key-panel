from __future__ import annotations

import fnmatch
import html
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from app.code_extractor import extract_verification_codes

_TAG_PATTERN = re.compile(r"<[^>]+>")
_EMAIL_PATTERN = r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}"
_EMAIL_ADDRESS_PATTERN = re.compile(_EMAIL_PATTERN, re.IGNORECASE)
_ICLOUD_SENDER_DOMAINS = frozenset({"icloud.com", "me.com", "mac.com"})
_RECIPIENT_PATTERNS = (
    re.compile(
        rf"(?:收件人|收件邮箱|收件地址|Recipient|Original Recipient|Original-Recipient|"
        rf"Final-Recipient|Delivered-To|X-Original-To|To)\s*[:：]\s*[<\"']?({_EMAIL_PATTERN})",
        re.IGNORECASE,
    ),
)


@dataclass(slots=True, frozen=True)
class PlatformRule:
    """定制平台的邮件匹配规则；空规则表示不限制发件人与主题。"""

    sender_patterns: tuple[str, ...] = ()
    subject_keywords: tuple[str, ...] = ()
    code_patterns: tuple[str, ...] = ()
    extraction_mode: str = "rules"

    def matches(self, sender: str, subject: str) -> bool:
        normalized_sender = (sender or "").strip().casefold()
        normalized_subject = (subject or "").casefold()

        if self.sender_patterns:
            sender_matched = any(
                _match_sender_pattern(normalized_sender, pattern)
                for pattern in self.sender_patterns
                if pattern.strip()
            )
            if not sender_matched:
                return False

        if self.subject_keywords and not any(
            keyword.strip().casefold() in normalized_subject
            for keyword in self.subject_keywords
            if keyword.strip()
        ):
            return False
        return True


def build_platform_rule(
    name: str,
    *,
    sender_patterns: Iterable[str] = (),
    subject_keywords: Iterable[str] = (),
    code_patterns: Iterable[str] = (),
    extraction_mode: str = "rules",
    unrestricted: bool = False,
) -> PlatformRule:
    """根据后台标签规则构造平台过滤器；未知平台至少按标签名过滤主题。"""

    normalized_senders = tuple(str(value).strip().casefold() for value in sender_patterns if str(value).strip())
    normalized_subjects = tuple(str(value).strip().casefold() for value in subject_keywords if str(value).strip())
    extraction_policy = {
        "code_patterns": tuple(str(value).strip() for value in code_patterns if str(value).strip()),
        "extraction_mode": extraction_mode,
    }
    if unrestricted:
        return PlatformRule(**extraction_policy)
    if normalized_senders or normalized_subjects:
        return PlatformRule(normalized_senders, normalized_subjects, **extraction_policy)

    normalized_name = (name or "").strip().casefold()
    if any(keyword in normalized_name for keyword in ("gpt", "chatgpt", "openai")):
        return PlatformRule(
            sender_patterns=("@openai.com", "*@*.openai.com"),
            **extraction_policy,
        )
    if any(keyword in normalized_name for keyword in ("claude", "anthropic")):
        return PlatformRule(
            sender_patterns=("@claude.ai", "@anthropic.com", "*@*.anthropic.com"),
            **extraction_policy,
        )
    if any(keyword in normalized_name for keyword in ("gemini", "谷歌")):
        return PlatformRule(
            sender_patterns=("@google.com", "*@*.google.com"),
            subject_keywords=("gemini",),
            **extraction_policy,
        )
    if any(keyword in normalized_name for keyword in ("grok", "spacexai")):
        return PlatformRule(
            subject_keywords=("grok", "spacexai"),
            **extraction_policy,
        )
    return (
        PlatformRule(subject_keywords=(normalized_name,), **extraction_policy)
        if normalized_name
        else PlatformRule(**extraction_policy)
    )


@dataclass(slots=True, frozen=True)
class CodeMatch:
    email_id: int
    code: str
    sender: str
    subject: str
    create_time: str
    recipients: tuple[str, ...]
    matched_via_fallback: bool = False


def max_email_id(messages: Iterable[Any], *, baseline: int = 0) -> int:
    """返回邮件快照中的最大有效编号。

    CloudMail 的时间精度可能只有秒级，因此领取边界以单调递增的邮件编号为主，
    时间仅作为第二层保护。无效或缺失的编号不会破坏整次快照。
    """

    maximum = max(0, int(baseline))
    for message in messages:
        try:
            email_id = int(getattr(message, "email_id", 0) or 0)
        except (TypeError, ValueError):
            continue
        maximum = max(maximum, email_id)
    return maximum


def extract_original_recipients(message: Any) -> list[str]:
    """提取真实收件地址；强来源存在时不混入共享查询邮箱。

    CloudMail 的内部查询接口会把 ``to_email`` 返回为被查询的共享邮箱，
    而 ``recipient`` 或邮件中的原始收件头才是投递目标。若把两者简单合并，
    主邮箱领取就可能误命中发给 ``+tag`` 裂变地址的验证码。
    """

    direct_candidates: set[str] = set()
    to_email = str(getattr(message, "to_email", "") or "").strip().casefold()
    if to_email:
        direct_candidates.add(to_email)

    metadata_candidates: set[str] = set()
    raw_recipient = getattr(message, "recipient", "") or ""
    recipient_items: Any = raw_recipient
    if isinstance(raw_recipient, str) and raw_recipient.strip():
        try:
            recipient_items = json.loads(raw_recipient)
        except json.JSONDecodeError:
            recipient_items = (
                [raw_recipient.strip()]
                if re.fullmatch(_EMAIL_PATTERN, raw_recipient.strip(), re.IGNORECASE)
                else []
            )

    if isinstance(recipient_items, dict):
        recipient_items = [recipient_items]
    if isinstance(recipient_items, list):
        for item in recipient_items:
            if isinstance(item, dict):
                address = str(item.get("address") or item.get("email") or "").strip().casefold()
                if address:
                    metadata_candidates.add(address)
            elif isinstance(item, str) and re.fullmatch(_EMAIL_PATTERN, item.strip(), re.IGNORECASE):
                metadata_candidates.add(item.strip().casefold())

    # recipient 字段来自 CloudMail 的原始信封信息，优先级最高。
    if metadata_candidates:
        return sorted(_prefer_specific_aliases(metadata_candidates))

    header_candidates: set[str] = set()
    searchable = "\n".join(
        (
            str(getattr(message, "subject", "") or ""),
            str(getattr(message, "text", "") or ""),
            _TAG_PATTERN.sub(" ", html.unescape(str(getattr(message, "content", "") or ""))),
        )
    )
    for pattern in _RECIPIENT_PATTERNS:
        for match in pattern.finditer(searchable):
            header_candidates.add(match.group(1).strip().casefold())
    if header_candidates:
        return sorted(_prefer_specific_aliases(header_candidates))
    return sorted(direct_candidates)


def find_latest_code(
    messages: Iterable[Any],
    *,
    actual_email: str,
    claimed_at: str = "",
    baseline_email_id: int = 0,
    platform_rule: PlatformRule | None = None,
    fallback_email: str = "",
    allow_recipient_fallback: bool = False,
    code_extractor: Callable[[str, str, str, PlatformRule], list[str]] | None = None,
) -> CodeMatch | None:
    """返回本次领取后的最新验证码。

    默认只接受 ``actual_email`` 的严格收件人匹配。部分 CloudMail 上游会把
    iCloud ``+tag`` 裂变地址归一化为主邮箱；调用方持有该主邮箱的独占实时
    租约时，可显式开启 ``allow_recipient_fallback`` 并传入 ``fallback_email``。

    回退仅对同一邮箱族的 ``+tag`` 地址生效，并且严格匹配在全局优先。时间、
    邮件 ID 基线与平台规则在两种匹配方式下完全一致，避免把旧邮件或其它平台
    的验证码误判为本次结果。
    """

    target = (actual_email or "").strip().casefold()
    if not target:
        return None

    ordered = sorted(
        messages,
        key=lambda message: (
            _parse_timestamp(str(getattr(message, "create_time", "") or "")) or datetime.min,
            int(getattr(message, "email_id", 0) or 0),
        ),
        reverse=True,
    )
    rule = platform_rule or PlatformRule()
    exact_match = _find_latest_code_for_recipient(
        ordered,
        target=target,
        claimed_at=claimed_at,
        baseline_email_id=baseline_email_id,
        platform_rule=rule,
        matched_via_fallback=False,
        code_extractor=code_extractor,
    )
    if exact_match is not None:
        return exact_match

    fallback_target = _validated_alias_fallback(
        target,
        fallback_email,
        enabled=allow_recipient_fallback,
    )
    if not fallback_target:
        return None
    return _find_latest_code_for_recipient(
        ordered,
        target=fallback_target,
        claimed_at=claimed_at,
        baseline_email_id=baseline_email_id,
        platform_rule=rule,
        matched_via_fallback=True,
        code_extractor=code_extractor,
    )


def _find_latest_code_for_recipient(
    messages: Iterable[Any],
    *,
    target: str,
    claimed_at: str,
    baseline_email_id: int,
    platform_rule: PlatformRule,
    matched_via_fallback: bool,
    code_extractor: Callable[[str, str, str, PlatformRule], list[str]] | None,
) -> CodeMatch | None:
    """在已经确定的收件地址上执行完整的验证码筛选。"""

    for message in messages:
        email_id = int(getattr(message, "email_id", 0) or 0)
        if baseline_email_id > 0 and email_id <= baseline_email_id:
            continue
        time_match = _is_after_claim(
            str(getattr(message, "create_time", "") or ""),
            claimed_at,
        )
        if time_match is False or (time_match is None and baseline_email_id <= 0):
            continue

        recipients = extract_original_recipients(message)
        if target not in recipients:
            continue

        sender = str(getattr(message, "send_email", "") or "")
        subject = str(getattr(message, "subject", "") or "")
        if not platform_rule.matches(sender, subject):
            continue

        message_text = str(getattr(message, "text", "") or "")
        message_html = str(getattr(message, "content", "") or "")
        if code_extractor is None:
            codes = (
                []
                if platform_rule.extraction_mode == "ai_only"
                else extract_verification_codes(
                    subject,
                    message_text,
                    message_html,
                    custom_patterns=platform_rule.code_patterns,
                )
            )
        else:
            codes = code_extractor(subject, message_text, message_html, platform_rule)
        code = next((candidate.strip() for candidate in codes if candidate.strip()), "")
        if code:
            return CodeMatch(
                email_id=email_id,
                code=code,
                sender=sender,
                subject=subject,
                create_time=str(getattr(message, "create_time", "") or ""),
                recipients=tuple(recipients),
                matched_via_fallback=matched_via_fallback,
            )
    return None


def _validated_alias_fallback(actual_email: str, fallback_email: str, *, enabled: bool) -> str:
    """只允许 ``别名 -> 同族主邮箱`` 的显式回退。"""

    if not enabled:
        return ""
    fallback = (fallback_email or "").strip().casefold()
    local, separator, domain = actual_email.partition("@")
    if not separator or "+" not in local:
        return ""
    root_local = local.split("+", 1)[0]
    if not root_local:
        return ""
    expected_root = f"{root_local}@{domain}"
    return fallback if fallback == expected_root else ""


def _match_sender_pattern(sender: str, raw_pattern: str) -> bool:
    pattern = raw_pattern.strip().casefold()
    if not pattern:
        return False
    for candidate in _sender_match_candidates(sender):
        if pattern.startswith("@") and candidate.endswith(pattern):
            return True
        if ("*" in pattern or "?" in pattern) and fnmatch.fnmatchcase(candidate, pattern):
            return True
        if candidate == pattern:
            return True
    return False


def _sender_match_candidates(sender: str) -> set[str]:
    """返回可用于平台规则匹配的发件人候选。

    部分 iCloud 转发链会把原始发件人改写成类似
    ``noreply_at_tm_openai_com_<随机串>@icloud.com`` 的地址。CloudMail
    只能看到改写后的信封发件人，直接按 ``@openai.com`` 匹配就会漏掉
    已经实际到达的验证码。

    这里只解析 iCloud 自有域名，并保留原地址候选。改写地址中 ``_at_``
    后的域名边界不可从随机后缀中完全恢复，因此逐级产生域名前缀；最终仍需
    通过管理员配置的平台规则、领取时间、邮件编号和收件人校验。
    """

    normalized = (sender or "").strip().casefold()
    candidates = {normalized} if normalized else set()
    addresses = {
        match.group(0).strip().casefold()
        for match in _EMAIL_ADDRESS_PATTERN.finditer(normalized)
    }
    candidates.update(addresses)

    for address in addresses:
        local, separator, domain = address.rpartition("@")
        if not separator or domain not in _ICLOUD_SENDER_DOMAINS:
            continue
        original_local, marker, encoded_domain = local.partition("_at_")
        if not marker or not original_local or not encoded_domain:
            continue
        labels = [label for label in encoded_domain.split("_") if label]
        # 合法互联网域名至少包含两级；后续随机串会形成更多候选，但只有与
        # 明确平台规则匹配的候选才会被接受。
        for end in range(2, len(labels) + 1):
            candidates.add(f"{original_local}@{'.'.join(labels[:end])}")
    return candidates


def _prefer_specific_aliases(candidates: set[str]) -> set[str]:
    """同一邮箱族同时出现主地址与 +tag 时，只信任更具体的裂变地址。"""

    alias_roots: set[str] = set()
    for candidate in candidates:
        local, separator, domain = candidate.partition("@")
        if not separator or "+" not in local:
            continue
        root_local = local.split("+", 1)[0]
        if root_local:
            alias_roots.add(f"{root_local}@{domain}")
    return {candidate for candidate in candidates if candidate not in alias_roots}


def _is_after_claim(create_time: str, claimed_at: str) -> bool | None:
    if not claimed_at.strip():
        return True
    created = _parse_timestamp(create_time)
    claimed = _parse_timestamp(claimed_at)
    if created is None or claimed is None:
        # 时间不可比较时交给调用方决定是否由可靠的 email_id 快照基线兜底。
        return None
    return created >= claimed


def _parse_timestamp(value: str) -> datetime | None:
    normalized = (value or "").strip()
    if not normalized:
        return None
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except ValueError:
        try:
            return datetime.strptime(normalized, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
