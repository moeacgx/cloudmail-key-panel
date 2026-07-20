from app.cloudmail import CloudMailMessage
from app.mailbox_matching import (
    PlatformRule,
    build_platform_rule,
    extract_original_recipients,
    find_latest_code,
)


def _message(
    email_id: int,
    recipient: str,
    code: str,
    *,
    sender: str = "noreply@openai.com",
    subject: str = "Your verification code",
    create_time: str = "2026-07-18 10:01:00",
    recipient_json: str = "",
) -> CloudMailMessage:
    return CloudMailMessage(
        email_id=email_id,
        send_email=sender,
        send_name="",
        subject=f"{subject} {code}",
        to_email=recipient,
        to_name="",
        create_time=create_time,
        type=0,
        content=f"<p>验证码 {code}</p>",
        text=f"验证码 {code}",
        is_del=0,
        recipient=recipient_json,
    )


def test_plus_addresses_are_matched_exactly_without_base_fallback() -> None:
    messages = [
        _message(3, "aa+2@icloud.com", "222222"),
        _message(2, "aa+1@icloud.com", "111111"),
        _message(1, "aa@icloud.com", "999999"),
    ]

    matched = find_latest_code(
        messages,
        actual_email="aa+1@icloud.com",
        claimed_at="2026-07-18 10:00:00",
    )

    assert matched is not None
    assert matched.code == "111111"
    assert matched.recipients == ("aa+1@icloud.com",)
    assert matched.matched_via_fallback is False


def test_normalized_alias_can_explicitly_fallback_to_its_root_recipient() -> None:
    message = _message(
        12,
        "shared@example.com",
        "121212",
        recipient_json='[{"address":"posies_radio.8z@icloud.com"}]',
    )

    matched = find_latest_code(
        [message],
        actual_email="posies_radio.8z+1@icloud.com",
        fallback_email="posies_radio.8z@icloud.com",
        allow_recipient_fallback=True,
        claimed_at="2026-07-18 10:00:00",
    )

    assert matched is not None
    assert matched.code == "121212"
    assert matched.recipients == ("posies_radio.8z@icloud.com",)
    assert matched.matched_via_fallback is True


def test_alias_root_fallback_is_disabled_by_default() -> None:
    message = _message(
        12,
        "shared@example.com",
        "121212",
        recipient_json='[{"address":"aa@icloud.com"}]',
    )

    assert (
        find_latest_code(
            [message],
            actual_email="aa+1@icloud.com",
            fallback_email="aa@icloud.com",
        )
        is None
    )


def test_alias_fallback_rejects_unrelated_or_non_alias_addresses() -> None:
    message = _message(
        12,
        "shared@example.com",
        "121212",
        recipient_json='[{"address":"other@icloud.com"}]',
    )

    assert (
        find_latest_code(
            [message],
            actual_email="aa+1@icloud.com",
            fallback_email="other@icloud.com",
            allow_recipient_fallback=True,
        )
        is None
    )
    root_match = find_latest_code(
        [message],
        actual_email="other@icloud.com",
        fallback_email="other@icloud.com",
        allow_recipient_fallback=True,
    )
    assert root_match is not None
    assert root_match.matched_via_fallback is False


def test_exact_alias_match_wins_over_newer_root_fallback() -> None:
    messages = [
        _message(
            20,
            "shared@example.com",
            "202020",
            create_time="2026-07-18 10:02:00",
            recipient_json='[{"address":"aa@icloud.com"}]',
        ),
        _message(
            19,
            "shared@example.com",
            "191919",
            create_time="2026-07-18 10:01:00",
            recipient_json='[{"address":"aa+1@icloud.com"}]',
        ),
    ]

    matched = find_latest_code(
        messages,
        actual_email="aa+1@icloud.com",
        fallback_email="aa@icloud.com",
        allow_recipient_fallback=True,
        claimed_at="2026-07-18 10:00:00",
    )

    assert matched is not None
    assert matched.code == "191919"
    assert matched.matched_via_fallback is False


def test_alias_root_fallback_keeps_time_id_and_platform_guards() -> None:
    messages = [
        _message(
            30,
            "shared@example.com",
            "303030",
            sender="mail@claude.ai",
            create_time="2026-07-18 10:03:00",
            recipient_json='[{"address":"aa@icloud.com"}]',
        ),
        _message(
            29,
            "shared@example.com",
            "292929",
            create_time="2026-07-18 09:59:00",
            recipient_json='[{"address":"aa@icloud.com"}]',
        ),
        _message(
            28,
            "shared@example.com",
            "282828",
            create_time="2026-07-18 10:02:00",
            recipient_json='[{"address":"aa@icloud.com"}]',
        ),
    ]

    assert (
        find_latest_code(
            messages,
            actual_email="aa+1@icloud.com",
            fallback_email="aa@icloud.com",
            allow_recipient_fallback=True,
            claimed_at="2026-07-18 10:00:00",
            baseline_email_id=28,
            platform_rule=PlatformRule(sender_patterns=("@openai.com",)),
        )
        is None
    )


def test_old_messages_and_baseline_email_ids_are_rejected() -> None:
    messages = [
        _message(10, "aa+1@icloud.com", "101010", create_time="2026-07-18 09:59:59"),
        _message(9, "aa+1@icloud.com", "090909", create_time="2026-07-18 10:02:00"),
    ]

    assert (
        find_latest_code(
            messages,
            actual_email="aa+1@icloud.com",
            claimed_at="2026-07-18 10:00:00",
            baseline_email_id=9,
        )
        is None
    )


def test_snapshot_baseline_distinguishes_messages_arriving_in_the_same_second() -> None:
    """CloudMail 时间只有秒级精度，同秒新旧邮件必须由邮件编号基线区分。"""

    messages = [
        _message(102, "aa+1@icloud.com", "102102", create_time="2026-07-18 10:00:00"),
        _message(101, "aa+1@icloud.com", "101101", create_time="2026-07-18 10:00:00"),
    ]

    matched = find_latest_code(
        messages,
        actual_email="aa+1@icloud.com",
        claimed_at="2026-07-18 10:00:00",
        baseline_email_id=101,
    )

    assert matched is not None
    assert matched.email_id == 102
    assert matched.code == "102102"


def test_snapshot_baseline_accepts_newer_email_id_when_time_is_missing() -> None:
    message = _message(102, "aa+1@icloud.com", "102102", create_time="")

    matched = find_latest_code(
        [message],
        actual_email="aa+1@icloud.com",
        claimed_at="2026-07-18 10:00:00",
        baseline_email_id=101,
    )

    assert matched is not None
    assert matched.email_id == 102


def test_missing_time_without_snapshot_baseline_is_rejected() -> None:
    message = _message(102, "aa+1@icloud.com", "102102", create_time="")

    assert (
        find_latest_code(
            [message],
            actual_email="aa+1@icloud.com",
            claimed_at="2026-07-18 10:00:00",
        )
        is None
    )


def test_timezone_aware_old_message_is_not_treated_as_new() -> None:
    message = _message(
        11,
        "aa+1@icloud.com",
        "121212",
        create_time="2026-07-18T09:59:59+08:00",
    )

    assert (
        find_latest_code(
            [message],
            actual_email="aa+1@icloud.com",
            claimed_at="2026-07-18 02:00:00",
        )
        is None
    )


def test_platform_rule_filters_sender_and_subject() -> None:
    messages = [
        _message(3, "aa+1@icloud.com", "333333", sender="mail@claude.ai", subject="Claude code"),
        _message(2, "aa+1@icloud.com", "222222", sender="noreply@openai.com", subject="GPT code"),
    ]

    matched = find_latest_code(
        messages,
        actual_email="aa+1@icloud.com",
        claimed_at="2026-07-18 10:00:00",
        platform_rule=PlatformRule(sender_patterns=("@openai.com",), subject_keywords=("GPT",)),
    )

    assert matched is not None
    assert matched.code == "222222"


def test_recipient_json_and_original_recipient_headers_are_supported() -> None:
    json_message = _message(
        2,
        "shared@example.com",
        "222222",
        recipient_json='[{"address":"AA+JSON@icloud.com"}]',
    )
    header_message = _message(1, "shared@example.com", "111111")
    header_message.text += "\nOriginal Recipient: aa+header@icloud.com"

    assert "aa+json@icloud.com" in extract_original_recipients(json_message)
    assert "aa+header@icloud.com" in extract_original_recipients(header_message)


def test_authoritative_recipient_does_not_leak_shared_query_address() -> None:
    message = _message(
        1,
        "aa@icloud.com",
        "112233",
        recipient_json='[{"address":"aa+private@icloud.com"}]',
    )

    assert extract_original_recipients(message) == ["aa+private@icloud.com"]
    assert find_latest_code([message], actual_email="aa@icloud.com") is None

    alias_match = find_latest_code([message], actual_email="aa+private@icloud.com")
    assert alias_match is not None
    assert alias_match.code == "112233"


def test_plain_recipient_field_is_treated_as_authoritative() -> None:
    message = _message(
        1,
        "aa@icloud.com",
        "443322",
        recipient_json="aa+plain@icloud.com",
    )

    assert extract_original_recipients(message) == ["aa+plain@icloud.com"]
    assert find_latest_code([message], actual_email="aa@icloud.com") is None
    assert find_latest_code([message], actual_email="aa+plain@icloud.com") is not None


def test_alias_wins_when_envelope_contains_both_root_and_alias() -> None:
    message = _message(
        1,
        "aa@icloud.com",
        "665544",
        recipient_json='[{"address":"aa@icloud.com"},{"address":"aa+exact@icloud.com"}]',
    )

    assert extract_original_recipients(message) == ["aa+exact@icloud.com"]
    assert find_latest_code([message], actual_email="aa@icloud.com") is None


def test_inferred_platform_rules_reject_other_services() -> None:
    rule = build_platform_rule("GPT")

    assert rule.matches("noreply@tm.openai.com", "Your code")
    assert not rule.matches("mail@anthropic.com", "Claude verification code")


def test_inferred_platform_rule_accepts_icloud_rewritten_openai_sender() -> None:
    """iCloud 转发后的随机信封地址仍应识别出原始 OpenAI 域名。"""

    rule = build_platform_rule("Chatgpt")

    assert rule.matches(
        "noreply_at_tm_openai_com_tgby4dfekkd9d0_09sb3712@icloud.com",
        "你的 ChatGPT 临时验证码",
    )
    assert rule.matches(
        "ChatGPT <noreply_at_tm_openai_com_tgby4dfekkd9d0_09sb3712@icloud.com>",
        "你的 ChatGPT 临时验证码",
    )


def test_icloud_rewritten_sender_still_honors_platform_domain() -> None:
    openai_rule = build_platform_rule("Chatgpt")

    assert not openai_rule.matches(
        "noreply_at_mail_anthropic_com_random@icloud.com",
        "Claude verification code",
    )
    # 仅解析 iCloud 自有域名，避免把任意第三方地址中的 _at_ 文本当成原始发件人。
    assert not openai_rule.matches(
        "noreply_at_tm_openai_com_random@example.com",
        "你的 ChatGPT 临时验证码",
    )


def test_code_match_accepts_actual_icloud_rewritten_sender_from_cloudmail() -> None:
    message = _message(
        88,
        "noise.molding-7f@icloud.com",
        "183830",
        sender="noreply_at_tm_openai_com_tgby4dfekkd9d0_09sb3712@icloud.com",
        subject="你的 ChatGPT 临时验证码",
        create_time="2026-07-18 20:26:00",
    )

    matched = find_latest_code(
        [message],
        actual_email="noise.molding-7f@icloud.com",
        claimed_at="2026-07-18 20:20:00",
        baseline_email_id=87,
        platform_rule=build_platform_rule("Chatgpt"),
    )

    assert matched is not None
    assert matched.code == "183830"
    assert matched.email_id == 88
