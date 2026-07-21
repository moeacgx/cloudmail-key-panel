from app.code_extractor import extract_verification_codes


def test_extract_verification_codes_returns_unique_candidates_in_source_order() -> None:
    codes = extract_verification_codes(
        subject="Your ChatGPT code is 330119",
        text="Enter this temporary verification code to continue: 330119. Backup code 998877.",
        html="<div>Use code <strong>330119</strong></div><div>备用验证码：998877</div>",
    )

    assert codes == ["330119", "998877"]


def test_extract_verification_codes_reads_codes_from_html_when_text_missing() -> None:
    codes = extract_verification_codes(
        subject="OpenAI security alert",
        text="",
        html="<html><body><p>ChatGPT Log-in Code</p><p>If that was you, enter this code:</p><h1>445566</h1></body></html>",
    )

    assert codes == ["445566"]


def test_extract_verification_codes_reads_spacexai_alphanumeric_code() -> None:
    codes = extract_verification_codes(
        subject="Verify your SpaceXAI account",
        text="Your confirmation code is 6NQ-Y60. This code expires in 10 minutes.",
        html="<p>SpaceXAI verification code</p><strong>6nq-y60</strong>",
    )

    assert codes == ["6NQ-Y60"]


def test_extract_verification_codes_keeps_generic_codes_in_source_order() -> None:
    codes = extract_verification_codes(text="6NQ-Y60 appeared before 445566 and 6nq-y60.")

    assert codes == ["6NQ-Y60", "445566"]
