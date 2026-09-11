"""拉取中证1000 月度成分权重（Tushare index_weight，2014-12~2026-08）→ PIT 宇宙.

输出: research/tushare/index_weight_000852.parquet 长表（trade_date, con_code, weight）
Token 从环境变量读取（.env），不写入代码。
"""

import os
import sys
import time
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
ROOT = ENV_FILE.parent
for line in open(ENV_FILE, encoding="utf-8"):
    if line.startswith("SOLIDROCK_TUSHARE_TOKEN="):
        os.environ["SOLIDROCK_TUSHARE_TOKEN"] = line.split("=", 1)[1].strip()

sys.path.insert(0, str(ROOT))

from solidrock.config import get_settings  # noqa: E402

OUT = ROOT / "research" / "tushare"
OUT.mkdir(parents=True, exist_ok=True)


def main() -> None:
    import pandas as pd
    import tushare as ts

    pro = ts.pro_api(get_settings().tushare_token)
    months = pd.period_range("2014-12", "2026-08", freq="M")
    frames, failed = [], []
    for p in months:
        d = p.end_time.strftime("%Y%m%d")
        for attempt in range(3):
            try:
                df = pro.index_weight(index_code="000852.SH", start_date=d, end_date=d)
                if df is None or df.empty:
                    failed.append(d)
                    break
                frames.append(df)
                break
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if "每分钟" in msg or "权限" in msg or "积分" in msg:
                    print(f"{d}: {msg[:80]}")
                    failed.append(d)
                    break
                time.sleep(3)
        else:
            continue
        time.sleep(0.3)
    import pandas as pd

    allf = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    allf.to_parquet(OUT / "index_weight_000852.parquet", index=False)
    got = allf["trade_date"].nunique() if len(allf) else 0
    print(f"月度快照: {got}/{len(months)} 个月, 总行数 {len(allf)}, 缺失月 {len(set(failed))}: {failed[:6]}")


if __name__ == "__main__":
    import pandas as pd  # noqa: F401

    main()
