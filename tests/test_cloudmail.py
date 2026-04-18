import httpx

from app.cloudmail import CloudMailClient


def test_cloudmail_client_generates_token_and_queries_recent_emails() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)

        if request.url.path == "/api/public/genToken":
            assert request.method == "POST"
            assert request.read() == b'{"email":"admin@example.com","password":"secret"}'
            return httpx.Response(
                200,
                json={"code": 200, "message": "success", "data": {"token": "token-123"}},
            )

        if request.url.path == "/api/public/emailList":
            assert request.headers["Authorization"] == "token-123"
            assert request.read() == b'{"toEmail":"cranes_solute.1o@icloud.com","timeSort":"desc","type":0,"isDel":0,"num":1,"size":5}'
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "message": "success",
                    "data": [
                        {
                            "emailId": 1,
                            "sendEmail": "noreply@tm.openai.com",
                            "sendName": "OpenAI",
                            "subject": "Your ChatGPT code is 330119",
                            "toEmail": "cranes_solute.1o@icloud.com",
                            "toName": "buyer",
                            "createTime": "2026-04-18 14:59:00",
                            "type": 0,
                            "content": "<div>330119</div>",
                            "text": "330119",
                            "isDel": 0,
                        }
                    ],
                },
            )

        raise AssertionError(f"unexpected path: {request.url.path}")

    client = CloudMailClient(
        base_url="https://mail.example.com",
        admin_email="admin@example.com",
        admin_password="secret",
        transport=httpx.MockTransport(handler),
    )

    emails = client.fetch_recent_emails("cranes_solute.1o@icloud.com", limit=5)

    assert len(requests) == 2
    assert emails[0].subject == "Your ChatGPT code is 330119"
    assert emails[0].to_email == "cranes_solute.1o@icloud.com"
    assert emails[0].text == "330119"


def test_cloudmail_client_uses_fixed_api_token_without_calling_gen_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/public/emailList"
        assert request.headers["Authorization"] == "fixed-token"
        return httpx.Response(200, json={"code": 200, "message": "success", "data": []})

    client = CloudMailClient(
        base_url="https://mail.example.com",
        api_token="fixed-token",
        transport=httpx.MockTransport(handler),
    )

    emails = client.fetch_recent_emails("buyer@example.com")

    assert emails == []
