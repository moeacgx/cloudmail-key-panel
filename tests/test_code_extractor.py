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
