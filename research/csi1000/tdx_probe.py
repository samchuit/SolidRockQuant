"""pytdx 接口极限与边界探针.

输出: research/csi1000/tdx_limits_report.txt
覆盖: K线历史深度、单次数量上限、K线类别、品种覆盖(深/沪/创/科/北/ETF/指数)、
分钟线深度、分笔成交、实时行情批量上限、财务字段、频率耐受。
"""

from __future__ import annotations

import time
from pathlib import Path

from pytdx.hq import TdxHq_API

OUT = Path(__file__).parent / "tdx_limits_report.txt"
SERVER = ("59.36.5.11", 7709)
LINES: list[str] = []


def emit(s: str = "") -> None:
    print(s)
    LINES.append(s)


def api_connect():
    api = TdxHq_API()
    for _ in range(3):
        if api.connect(*SERVER, time_out=6):
            return api
        time.sleep(1)
    raise RuntimeError("无法连接")


def probe_bars_depth(api: TdxHq_API, market: int, code: str, label: str) -> None:
    """二分向前翻页找历史第一根."""
    all_n, offset = 0, 0
    first_dt = None
    while True:
        bars = api.get_security_bars(9, market, code, offset, 800)
        if not bars:
            break
        all_n = len(bars)
        first_dt = bars[0]["datetime"]
        offset += 800
        if offset > 40000:
            break
    emit(f"[深度] {label}({market},{code}): 总拉取 {offset and all_n + offset - 800 or 0}+根, 首根 {first_dt}, 翻页offset={offset}")


def main() -> None:
    emit("=" * 70)
    emit(f"pytdx 极限探针 @ {SERVER}  {time.now():%Y-%m-%d %H:%M:%S}" if hasattr(time, "now") else "")
    emit("=" * 70)
    api = api_connect()

    # 1) 单次数量上限
    emit("\n## 1. 单次请求数量上限 (get_security_bars)")
    for cnt in (800, 801, 1000, 2000):
        bars = api.get_security_bars(9, 0, "000001", 0, cnt)
        emit(f"  count={cnt}: 实际返回 {len(bars) if bars else 0}")

    # 2) K线类别覆盖
    emit("\n## 2. K线类别 (category 0-12, 000001)")
    names = {0: "5分钟", 1: "15分钟", 2: "30分钟", 3: "1小时", 4: "2小时", 5: "日",
             6: "周", 7: "月", 8: "1分钟?", 9: "日?", 10: "季?", 11: "年?", 12: "年?"}
    for cat in range(13):
        bars = api.get_security_bars(cat, 0, "000001", 0, 3)
        n = len(bars) if bars else 0
        last = bars[-1]["datetime"] if bars else "-"
        emit(f"  category={cat:2d} ({names.get(cat,'?'):>4}): {n}根, 末根 {last}")

    # 3) 历史深度
    emit("\n## 3. 历史深度（向前翻页至空）")
    probe_bars_depth(api, 0, "000001", "平安银行(1991上市)")  # type: ignore[arg-type]

    # 4) 品种覆盖
    emit("\n## 4. 品种覆盖 (日线3根)")
    cases = [
        (0, "000001", "深主板"), (0, "300750", "创业板"), (0, "301269", "注册创"),
        (0, "003816", "深主板003"), (1, "600519", "沪主板"), (1, "688981", "科创板"),
        (1, "510300", "沪ETF"), (0, "159915", "深ETF"), (1, "511010", "沪债ETF"),
        (1, "000852", "中证1000指数(沪)"), (0, "399303", "国证2000指数(深)"),
        (1, "513100", "纳指ETF"), (0, "128136", "深可转债"),
    ]
    for market, code, label in cases:
        try:
            bars = api.get_security_bars(9, market, code, 0, 3)
            n = len(bars) if bars else 0
            px = bars[-1]["close"] if bars else "-"
            emit(f"  {label:14s} (m{market},{code}): {n}根, 末收 {px}")
        except Exception as e:  # noqa: BLE001
            emit(f"  {label:14s} (m{market},{code}): 异常 {type(e).__name__}")

    # 5) 分钟线深度 (5分钟 与 1分钟)
    emit("\n## 5. 分钟线深度")
    for cat, label in ((0, "5分钟"), (8, "1分钟(类别8)"), (9, "1分钟(类别9)")):
        total, offset, first = 0, 0, "-"
        while True:
            bars = api.get_security_bars(cat, 0, "000001", offset, 800)
            if not bars:
                break
            total += len(bars)
            first = bars[0]["datetime"]
            offset += 800
            if offset > 40000:
                break
        emit(f"  {label}: ~{total}根, 首根 {first}")

    # 6) 分笔成交
    emit("\n## 6. 分笔成交")
    try:
        tb = api.get_transaction_data(0, "000001", 0, 30)
        emit(f"  当日分笔: {len(tb) if tb else 0} 条")
    except Exception as e:  # noqa: BLE001
        emit(f"  当日分笔: 异常 {type(e).__name__}")
    try:
        htb = api.get_history_transaction_data(0, "000001", 20260908, 0, 30)
        emit(f"  历史分笔(20260908): {len(htb) if htb else 0} 条")
    except Exception as e:  # noqa: BLE001
        emit(f"  历史分笔: 异常 {type(e).__name__}")

    # 7) 实时行情批量上限
    emit("\n## 7. 实时行情批量 (get_security_quotes)")
    codes = [f"00000{i}" for i in range(10)] * 10  # 100个
    quotes = api.get_security_quotes([(0, c) for c in codes])
    emit(f"  请求100个 → 返回 {len(quotes) if quotes else 0} 个")
    codes80 = [(0, f"00000{i}") for i in range(8)] * 10
    quotes = api.get_security_quotes(codes80)
    emit(f"  请求80个 → 返回 {len(quotes) if quotes else 0} 个")
    if quotes:
        q = quotes[0]
        emit(f"  字段: {list(q.keys())[:14]}")

    # 8) 财务信息
    emit("\n## 8. 财务信息 (get_finance_info)")
    fi = api.get_finance_info(0, "000001")
    if fi:
        d = fi._asdict() if hasattr(fi, "_asdict") else vars(fi)
        emit(f"  字段({len(d)}): {list(d.keys())}")
        emit(f"  样例: liutongguben={d.get('liutongguben')} zongguben={d.get('zongguben')}")

    # 9) 股票列表分页
    emit("\n## 9. 股票列表 (get_security_list)")
    lst = api.get_security_list(0, 0)
    emit(f"  深市第一页: {len(lst) if lst else 0} 条")

    # 10) 频率耐受: 连续 300 次快速调用
    emit("\n## 10. 频率耐受（连续300次 get_security_bars × 800）")
    ok = fail = 0
    t0 = time.time()
    for i in range(300):
        try:
            bars = api.get_security_bars(9, 0, "000001", (i % 5) * 800, 800)
            if bars:
                ok += 1
            else:
                fail += 1
        except Exception:  # noqa: BLE001
            fail += 1
            try:
                api.disconnect()
            except Exception:  # noqa: BLE001
                pass
            api.connect(*SERVER, time_out=6)
    dt = time.time() - t0
    emit(f"  成功 {ok} / 失败 {fail}, 耗时 {dt:.1f}s, 吞吐 {300/dt:.1f} 请求/s")

    api.disconnect()
    OUT.write_text("\n".join(LINES), encoding="utf-8")
    print(f"\n已写出: {OUT}")


if __name__ == "__main__":
    main()
