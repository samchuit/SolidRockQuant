"""通知模块测试（不发起真实网络请求）.

覆盖重点是**失败可观测性**——通知是无人值守实盘唯一的告警通道，
"发不出去且无人知晓"是最危险的状态。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from solidrock import notify


@pytest.fixture(autouse=True)
def _clean_notify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """每个用例：干净状态 + 数据目录指向 tmp（失败日志不污染真实目录）."""
    monkeypatch.setenv("SOLIDROCK_DATA_DIR", str(tmp_path))
    from solidrock.config import get_settings

    get_settings.cache_clear()
    notify.reset_state()
    yield
    notify.reset_state()
    get_settings.cache_clear()


class _FakeResponse:
    def __init__(self) -> None:
        self.status = 200

    def read(self) -> bytes:
        return b"{}"

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class TestUrlSafety:
    def test_http_scheme_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOLIDROCK_NOTIFY_WEBHOOK", "http://example.com/hook")
        from solidrock.config import get_settings

        get_settings.cache_clear()
        assert notify.send_webhook("hi") is False
        assert notify.last_failure() is not None
        assert notify.last_failure().startswith("invalid_url")

    def test_private_ip_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOLIDROCK_NOTIFY_WEBHOOK", "https://192.168.1.10/hook")
        from solidrock.config import get_settings

        get_settings.cache_clear()
        assert notify.send_webhook("hi") is False
        assert "invalid_url" in (notify.last_failure() or "")

    def test_localhost_and_metadata_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for host in ("localhost", "127.0.0.1", "metadata.google.internal"):
            monkeypatch.setenv("SOLIDROCK_NOTIFY_WEBHOOK", f"https://{host}/hook")
            from solidrock.config import get_settings

            get_settings.cache_clear()
            assert notify.send_webhook("hi") is False, host


class TestNotConfigured:
    def test_missing_webhook_is_observable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SOLIDROCK_NOTIFY_WEBHOOK", raising=False)
        from solidrock.config import get_settings

        get_settings.cache_clear()
        assert notify.send_webhook("hi") is False
        assert "not_configured" in (notify.last_failure() or "")


class TestDelivery:
    def _capture(self, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
        sent: list[dict] = []

        def fake_urlopen(req, timeout=10):
            sent.append({"url": req.full_url, "body": json.loads(req.data.decode())})
            return _FakeResponse()

        monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
        return sent

    def _set(self, monkeypatch: pytest.MonkeyPatch, url: str, kind: str = "generic") -> None:
        monkeypatch.setenv("SOLIDROCK_NOTIFY_WEBHOOK", url)
        monkeypatch.setenv("SOLIDROCK_NOTIFY_TYPE", kind)
        from solidrock.config import get_settings

        get_settings.cache_clear()

    def test_generic_payload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent = self._capture(monkeypatch)
        self._set(monkeypatch, "https://example.com/hook")
        assert notify.send_webhook("hello", title="T", extra={"k": 1}) is True
        assert sent[0]["body"] == {"text": "hello", "title": "T", "k": 1}
        assert notify.last_failure() is None

    def test_wecom_payload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent = self._capture(monkeypatch)
        self._set(monkeypatch, "https://example.com/hook", "wecom")
        assert notify.send_webhook("hello", title="T") is True
        assert sent[0]["body"]["msgtype"] == "text"
        assert "hello" in sent[0]["body"]["text"]["content"]

    def test_dingtalk_signature_appended(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent = self._capture(monkeypatch)
        self._set(monkeypatch, "https://example.com/hook?access_token=x", "dingtalk")
        monkeypatch.setenv("SOLIDROCK_NOTIFY_SECRET", "SEC")
        from solidrock.config import get_settings

        get_settings.cache_clear()
        assert notify.send_webhook("hello") is True
        assert "timestamp=" in sent[0]["url"]
        assert "sign=" in sent[0]["url"]

    def test_send_failure_is_recorded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """HTTP 异常必须可观测，而不是静默 False."""
        self._set(monkeypatch, "https://example.com/hook")

        def boom(req, timeout=10):
            raise OSError("connection refused")

        monkeypatch.setattr(notify.urllib.request, "urlopen", boom)
        assert notify.send_webhook("hi") is False
        assert "send_failed" in (notify.last_failure() or "")
        assert "connection refused" in (notify.last_failure() or "")

    def test_failure_written_to_log_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._set(monkeypatch, "https://example.com/hook")

        def boom(req, timeout=10):
            raise OSError("down")

        monkeypatch.setattr(notify.urllib.request, "urlopen", boom)
        notify.send_webhook("hi")
        log = tmp_path / "live" / "notify_errors.log"
        assert log.exists()
        assert "send_failed" in log.read_text(encoding="utf-8")


class TestDedupe:
    def test_same_key_suppressed_within_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent: list[str] = []

        def fake_urlopen(req, timeout=10):
            sent.append(req.full_url)
            return _FakeResponse()

        monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setenv("SOLIDROCK_NOTIFY_WEBHOOK", "https://example.com/hook")
        from solidrock.config import get_settings

        get_settings.cache_clear()
        assert notify.send_webhook("a", dedupe_key="same", dedupe_window=60) is True
        assert notify.send_webhook("a", dedupe_key="same", dedupe_window=60) is False  # 被抑制
        assert len(sent) == 1
        # 不同 key 不受影响
        assert notify.send_webhook("b", dedupe_key="other", dedupe_window=60) is True
        assert len(sent) == 2

    def test_zero_window_disables_dedupe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent: list[str] = []

        def fake_urlopen(req, timeout=10):
            sent.append(req.full_url)
            return _FakeResponse()

        monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setenv("SOLIDROCK_NOTIFY_WEBHOOK", "https://example.com/hook")
        from solidrock.config import get_settings

        get_settings.cache_clear()
        assert notify.send_webhook("a", dedupe_key="k", dedupe_window=0) is True
        assert notify.send_webhook("a", dedupe_key="k", dedupe_window=0) is True
        assert len(sent) == 2


class TestEventHelper:
    def test_notify_event_includes_fields(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent: list[dict] = []

        def fake_urlopen(req, timeout=10):
            sent.append(json.loads(req.data.decode()))
            return _FakeResponse()

        monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setenv("SOLIDROCK_NOTIFY_WEBHOOK", "https://example.com/hook")
        from solidrock.config import get_settings

        get_settings.cache_clear()
        assert notify.notify_event("live_order_submitted", "已提交", fields={"symbol": "510300.SH"}) is True
        body = sent[0]
        assert body["kind"] == "live_order_submitted"
        assert body["symbol"] == "510300.SH"
        assert "已提交" in body["text"]

    def test_notify_lines_skips_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert notify.notify_lines("h", [], kind="k") is False

    def test_notify_event_never_raises_on_invalid_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SOLIDROCK_NOTIFY_WEBHOOK", "ftp://bad/hook")
        from solidrock.config import get_settings

        get_settings.cache_clear()
        assert notify.notify_event("k", "m") is False  # 不抛错
