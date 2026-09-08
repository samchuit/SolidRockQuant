"""CLI 冒烟测试（typer CliRunner，本地临时数据目录）."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from solidrock.cli.main import app
from solidrock.config import get_settings
from solidrock.data.store import DataStore
from tests.conftest import make_bars

runner = CliRunner()


def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SOLIDROCK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SOLIDROCK_TUSHARE_TOKEN", "")
    get_settings.cache_clear()  # 防止上个测试的缓存配置跨测试泄漏


class TestCli:
    def test_help(self) -> None:
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "srq" in result.output

    def test_init_creates_data_dir(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _isolated_env(monkeypatch, tmp_path)
        result = runner.invoke(app, ["init"])
        assert result.exit_code == 0
        assert (tmp_path / "data").exists()

    def test_stats_empty(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _isolated_env(monkeypatch, tmp_path)
        result = runner.invoke(app, ["data", "stats"])
        assert result.exit_code == 0
        assert "暂无缓存" in result.output

    def test_peek_missing_symbol_friendly_error(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _isolated_env(monkeypatch, tmp_path)
        result = runner.invoke(app, ["data", "peek", "000001.SZ"])
        assert result.exit_code == 1
        assert "NO_DATA" in result.output
        assert "srq data update" in result.output  # 错误必须带可执行 hint

    def test_snapshot_lifecycle(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _isolated_env(monkeypatch, tmp_path)
        store = DataStore(tmp_path / "data")
        store.save_bars(make_bars())
        assert runner.invoke(app, ["data", "snapshot", "create", "snap-x"]).exit_code == 0
        listing = runner.invoke(app, ["data", "snapshot", "list"])
        assert listing.exit_code == 0
        assert "snap-x" in listing.output
        # 重复创建 → 友好报错（非 traceback）
        dup = runner.invoke(app, ["data", "snapshot", "create", "snap-x"])
        assert dup.exit_code == 1
        assert "SNAPSHOT_EXISTS" in dup.output
        assert runner.invoke(app, ["data", "snapshot", "delete", "snap-x", "--force"]).exit_code == 0

    def test_calendar_not_loaded(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _isolated_env(monkeypatch, tmp_path)
        result = runner.invoke(app, ["data", "calendar"])
        assert result.exit_code == 0
        assert "暂无交易日历" in result.output

    def test_config_masks_token(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _isolated_env(monkeypatch, tmp_path)
        monkeypatch.setenv("SOLIDROCK_TUSHARE_TOKEN", "abcdefghijklmn")
        result = runner.invoke(app, ["config"])
        assert result.exit_code == 0
        assert "abcdefghijklmn" not in result.output  # token 必须脱敏
        assert "abcd" in result.output

    def test_sources_listing(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _isolated_env(monkeypatch, tmp_path)
        result = runner.invoke(app, ["data", "sources"])
        assert result.exit_code == 0
        assert "akshare" in result.output
        assert "tushare" in result.output
