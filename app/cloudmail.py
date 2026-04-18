from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


class CloudMailError(RuntimeError):
    pass


@dataclass(slots=True)
class CloudMailMessage:
    email_id: int
    send_email: str
    send_name: str
    subject: str
    to_email: str
    to_name: str
    create_time: str
    type: int
    content: str
    text: str
    is_del: int
    recipient: str = ""


class CloudMailClient:
    def __init__(
        self,
        base_url: str,
        admin_email: str | None = None,
        admin_password: str | None = None,
        api_token: str | None = None,
        internal_admin_email: str | None = None,
        internal_admin_password: str | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.admin_email = admin_email
        self.admin_password = admin_password
        self.api_token = api_token
        self.internal_admin_email = internal_admin_email
        self.internal_admin_password = internal_admin_password
        self.transport = transport
        self.timeout = timeout
        self._session_token: str | None = None
        self._internal_token: str | None = None

    def fetch_recent_emails(self, recipient_email: str, limit: int = 10) -> list[CloudMailMessage]:
        if self.internal_admin_email and self.internal_admin_password:
            response = self._request(
                "GET",
                "/api/allEmail/list",
                params={"accountEmail": recipient_email, "type": "receive", "size": limit},
                headers={"Authorization": self._get_internal_token()},
            )
            items = response.get("data", {}).get("list", [])
            return [self._parse_message(item) for item in items]

        payload = {
            "toEmail": recipient_email,
            "timeSort": "desc",
            "type": 0,
            "isDel": 0,
            "num": 1,
            "size": limit,
        }
        token = self._get_token()
        response = self._request("POST", "/api/public/emailList", json=payload, headers={"Authorization": token})
        return [self._parse_message(item) for item in response.get("data", [])]

    def _get_token(self) -> str:
        if self.api_token:
            return self.api_token
        if self._session_token:
            return self._session_token
        if not self.admin_email or not self.admin_password:
            raise CloudMailError("CloudMail public credentials are not configured")

        response = self._request(
            "POST",
            "/api/public/genToken",
            json={"email": self.admin_email, "password": self.admin_password},
        )
        token = response.get("data", {}).get("token")
        if not token:
            raise CloudMailError("CloudMail token response did not include a token")
        self._session_token = token
        return token

    def _get_internal_token(self) -> str:
        if self._internal_token:
            return self._internal_token
        if not self.internal_admin_email or not self.internal_admin_password:
            raise CloudMailError("CloudMail internal admin credentials are not configured")

        response = self._request(
            "POST",
            "/api/login",
            json={"email": self.internal_admin_email, "password": self.internal_admin_password},
        )
        token = response.get("data", {}).get("token")
        if not token:
            raise CloudMailError("CloudMail internal login response did not include a token")
        self._internal_token = token
        return token

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        with httpx.Client(base_url=self.base_url, transport=self.transport, timeout=self.timeout) as client:
            response = client.request(method, path, **kwargs)

        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 200:
            raise CloudMailError(payload.get("message") or "CloudMail request failed")
        return payload

    @staticmethod
    def _parse_message(item: dict[str, Any]) -> CloudMailMessage:
        return CloudMailMessage(
            email_id=int(item.get("emailId", 0)),
            send_email=item.get("sendEmail", ""),
            send_name=item.get("sendName") or item.get("name", ""),
            subject=item.get("subject", ""),
            to_email=item.get("toEmail", ""),
            to_name=item.get("toName", ""),
            create_time=item.get("createTime", ""),
            type=int(item.get("type", 0)),
            content=item.get("content", ""),
            text=item.get("text", ""),
            is_del=int(item.get("isDel", 0)),
            recipient=item.get("recipient", ""),
        )
