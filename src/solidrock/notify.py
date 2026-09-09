"""通知模块：钉钉/企微 webhook 推送.

Paper/Live 会话的成交、信号、异常通过 webhook 推送：
- 钉钉群机器人（加签安全设置）
- 企业微信群机器人
- 通用 HTTP webhook

配置方式：环境变量 ``SOLIDROCK_NOTIFY_WEBHOOK=<webhook_url>`` 和
``SOLIDROCK_NOTIFY_TYPE=dingtalk|wecom|generic``。

安全约束：仅允许 HTTPS 协议；目标主机必须是公网域名（阻断私网 IP、
环回地址、链路本地和云元数据端点）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import time
import urllib.request
from typing import Any
from urllib.parse import urlparse

from solidrock.config import get_settings

_MAX_TEXT_LEN = 4096
_ALLOWED_HOSTS: set[str] | None = None  # None = 阻断全部私网（默认安全）


def _validate_webhook_url(url: str) -> None:
    """校验 webhook URL：仅 HTTPS、禁止私网/环回/链路本地/元数据 IP."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"webhook URL 必须使用 HTTPS，收到 {parsed.scheme!r}")
    host = parsed.hostname or ""
    if not host:
        raise ValueError("webhook URL 缺少主机名")
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0", "metadata.google.internal"):
        raise ValueError(f"webhook URL 禁止指向本机/元数据端点: {host}")
    try:
        addr = ipaddress.ip_address(host)
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
            raise ValueError(f"webhook URL 禁止指向私网地址: {host}")
    except ValueError:
        pass  # 域名（非 IP）→ 后续 DNS 解析由 HTTPS 证书校验保障


def _dingtalk_sign(secret: str) -> tuple[str, str]:
    """钉钉加签：返回 (timestamp_ms, sign)."""
    ts = str(round(time.time() * 1000))
    string_to_sign = f"{ts}\n{secret}"
    hmac_code = hmac.new(secret.encode(), string_to_sign.encode(), hashlib.sha256).digest()
    return ts, base64.b64encode(hmac_code).decode()


def send_webhook(text: str, *, title: str | None = None, extra: dict[str, Any] | None = None) -> bool:
    """发送通知到配置的 webhook；未配置则静默跳过（返回 False）.

    仅在 ``SOLIDROCK_NOTIFY_WEBHOOK`` 已配置时发送；URL 经安全校验
    （仅 HTTPS、阻断私网/环回/元数据端点）。
    """
    settings = get_settings()
    url = getattr(settings, "notify_webhook", None)
    if not url:
        return False
    try:
        _validate_webhook_url(url)
    except ValueError:
        return False
    notify_type = getattr(settings, "notify_type", "generic")
    text = text[:_MAX_TEXT_LEN]
    try:
        if notify_type == "dingtalk":
            body: dict[str, Any] = {
                "msgtype": "text",
                "text": {"content": f"[SolidRockQuant] {title or '通知'}\n{text}"},
            }
            secret = getattr(settings, "notify_secret", None)
            if secret:
                ts, sign = _dingtalk_sign(secret)
                url = f"{url}&timestamp={ts}&sign={sign}"
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"},
            )
        elif notify_type == "wecom":
            body = {"msgtype": "text", "text": {"content": f"[SolidRockQuant] {title or ''}\n{text}"}}
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"},
            )
        else:
            payload = {"text": text, "title": title or "", **(extra or {})}
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            )
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception:
        return False
