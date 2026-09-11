"""通知模块：钉钉/企微 webhook 推送.

Paper/Live 会话的成交、信号、异常通过 webhook 推送：

- 钉钉群机器人（加签安全设置）
- 企业微信群机器人
- 通用 HTTP webhook

配置方式：环境变量 ``SOLIDROCK_NOTIFY_WEBHOOK=<webhook_url>`` 和
``SOLIDROCK_NOTIFY_TYPE=dingtalk|wecom|generic``。

安全约束：仅允许 HTTPS 协议；目标主机必须是公网域名（阻断私网 IP、
环回地址、链路本地和云元数据端点）。

**可观测性**：通知是无人值守实盘唯一的告警通道，因此失败不能静默。
本模块区分三种"没发出去"的原因（``not_configured`` / ``invalid_url`` /
``send_failed``），可用 :func:`last_failure` 读取最近一次原因，并追加写入
``{data_dir}/live/notify_errors.log``（写入失败也不抛错）。重复事件可用
``dedupe_key`` 做进程内抑制（默认 60s），避免同一问题刷屏。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import time
import urllib.request
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse

from solidrock.config import get_settings

_MAX_TEXT_LEN = 4096
_ALLOWED_HOSTS: set[str] | None = None  # None = 阻断全部私网（默认安全）
_DEFAULT_DEDUPE_WINDOW = 60.0

# 最近一次失败原因（供测试与运维排查；进程级）
_last_failure: str | None = None
# dedupe_key -> 上次发送时间戳
_recent: dict[str, float] = {}


def last_failure() -> str | None:
    """最近一次通知未发出的原因（``None`` 表示无失败记录）."""
    return _last_failure


def reset_state() -> None:
    """清空失败记录与去重窗口（测试与长期运行进程的可选复位点）."""
    global _last_failure
    _last_failure = None
    _recent.clear()


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
    except ValueError:
        return  # 域名（非 IP）→ 后续 DNS 解析由 HTTPS 证书校验保障
    if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
        raise ValueError(f"webhook URL 禁止指向私网地址: {host}")


def _dingtalk_sign(secret: str) -> tuple[str, str]:
    """钉钉加签：返回 (timestamp_ms, sign)."""
    ts = str(round(time.time() * 1000))
    string_to_sign = f"{ts}\n{secret}"
    hmac_code = hmac.new(secret.encode(), string_to_sign.encode(), hashlib.sha256).digest()
    return ts, base64.b64encode(hmac_code).decode()


def _record_failure(reason: str, detail: str) -> None:
    """记录失败原因（内存 + 落盘），任何异常都不得外溢."""
    global _last_failure
    _last_failure = f"{reason}: {detail}"
    try:
        log_dir = get_settings().resolved_data_dir() / "live"
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "notify_errors.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {reason} {detail}\n")
    except Exception:
        pass  # 通知失败不能影响主干流程


def _deduped(key: str | None, window: float | None) -> bool:
    """key 命中抑制窗口则返回 True（本次不发）."""
    if not key:
        return False
    win = _DEFAULT_DEDUPE_WINDOW if window is None else float(window)
    if win <= 0:
        return False
    now = time.time()
    last = _recent.get(key)
    if last is not None and now - last < win:
        return True
    _recent[key] = now
    return False


def send_webhook(
    text: str,
    *,
    title: str | None = None,
    extra: dict[str, Any] | None = None,
    dedupe_key: str | None = None,
    dedupe_window: float | None = None,
) -> bool:
    """发送通知到配置的 webhook；未配置则跳过（返回 False，原因见 ``last_failure``）.

    仅在 ``SOLIDROCK_NOTIFY_WEBHOOK`` 已配置时发送；URL 经安全校验
    （仅 HTTPS、阻断私网/环回/元数据端点）。失败原因可经 :func:`last_failure` 读取，
    并落盘到 ``{data_dir}/live/notify_errors.log``。
    """
    global _last_failure
    settings = get_settings()
    url = getattr(settings, "notify_webhook", None)
    if not url:
        _record_failure("not_configured", "SOLIDROCK_NOTIFY_WEBHOOK 未配置")
        return False
    try:
        _validate_webhook_url(url)
    except ValueError as exc:
        _record_failure("invalid_url", str(exc))
        return False
    if _deduped(dedupe_key, dedupe_window):
        # 抑制不算失败：曾成功/曾尝试过的同一问题在窗口内不重复刷屏
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
        _last_failure = None
        return True
    except Exception as exc:
        _record_failure("send_failed", f"{type(exc).__name__}: {exc}")
        return False


# ---------------------------------------------------------------------- 事件
def notify_event(
    kind: str,
    message: str,
    *,
    fields: dict[str, Any] | None = None,
    dedupe_key: str | None = None,
    dedupe_window: float | None = None,
) -> bool:
    """发送一条结构化事件通知（永远不会抛错）.

    ``kind`` 用于标题与去重前缀，例如 ``live_order_submitted`` / ``live_order_blocked``
    / ``live_session_error`` / ``live_reconcile_diff``。
    """
    lines = [message]
    for key, value in (fields or {}).items():
        lines.append(f"{key}: {value}")
    return send_webhook(
        "\n".join(lines),
        title=kind,
        extra={"kind": kind, **(fields or {})},
        dedupe_key=f"{kind}|{dedupe_key}" if dedupe_key else None,
        dedupe_window=dedupe_window,
    )


def notify_lines(header: str, items: Iterable[str], *, kind: str, dedupe_key: str | None = None) -> bool:
    """把多行明细合成一条通知（明细为空则不发送）."""
    rows = [str(item) for item in items]
    if not rows:
        return False
    return notify_event(kind, "\n".join([header, *rows]), dedupe_key=dedupe_key)
