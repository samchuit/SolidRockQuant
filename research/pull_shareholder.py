"""拉取中证1000成分股股东户数明细（东财，含公告日期）→ research/shareholder/."""

import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "research" / "shareholder"
OUT.mkdir(parents=True, exist_ok=True)


def main() -> None:
    import akshare as ak

    cons = pd.read_csv(ROOT / "research" / "csi1000" / "cons_current.csv", dtype={"code": str})
    cons["code"] = cons["code"].str.zfill(6)
    todo = [c for c in cons["code"] if not (OUT / f"gdhs_{c}.parquet").exists()]
    print(f"待拉: {len(todo)}")

    def work(code: str):
        for attempt in range(2):
            try:
                df = ak.stock_zh_a_gdhs_detail_em(symbol=code)
                if df is None or df.empty:
                    return code, "empty"
                df.to_parquet(OUT / f"gdhs_{code}.parquet", index=False)
                return code, f"ok {len(df)}"
            except Exception as e:  # noqa: BLE001
                if attempt == 1:
                    return code, f"FAIL {type(e).__name__}: {str(e)[:30]}"
                time.sleep(1)

    done, failed = 0, []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(work, c): c for c in todo}
        for fut in as_completed(futs):
            code, status = fut.result()
            done += 1
            if status.startswith("FAIL"):
                failed.append(code)
            if done % 200 == 0:
                print(f"[{done}/{len(todo)}] failed={len(failed)}")
    print(f"完成 失败 {len(failed)}: {failed[:10]}")


if __name__ == "__main__":
    main()
