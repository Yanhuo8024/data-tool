#!/usr/bin/env python3
"""
一站式数据采集分析工具 — 爬虫 → 清洗 → 分析 → 存储，单文件开箱即用。

用法:
    python scraper.py scrape -u URL -s ".card" -f title,price,link -p 3
    python scraper.py clean -i raw.csv
    python scraper.py analyze -i clean.csv
    python scraper.py full -u URL -s ".card" -f title,price,link

依赖: selenium webdriver-manager bs4 pandas numpy seaborn
"""

import argparse
import csv
import json
import logging
import os
import random
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

# ── 输出目录 ───────────────────────────────────────────
# DATATOOL_OUT_DIR 优先（npx/便携部署时指向用户数据目录，避免写进包缓存）
_env_out = os.environ.get("DATATOOL_OUT_DIR")
if _env_out:
    OUT_DIR = Path(_env_out)
else:
    try:
        from core_config import OUT_DIR  # 统一入口，避免多处定义分叉
    except ImportError:
        OUT_DIR = Path(__file__).parent / "output"  # core_config 不可用时兜底
OUT_DIR.mkdir(parents=True, exist_ok=True)


# ═══════════════════════════════════════════════════════
#  日志
# ═══════════════════════════════════════════════════════

def setup_logging(name: str = "scraper") -> logging.Logger:
    """配置双通道日志：控制台 + 文件。"""
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fmtr = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")

    fh = logging.FileHandler(OUT_DIR / f"{name}_{datetime.now():%Y%m%d_%H%M%S}.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmtr)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmtr)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


# ═══════════════════════════════════════════════════════
#  爬虫引擎
# ═══════════════════════════════════════════════════════

class Scraper:
    """无头 Chrome 爬虫：列表分页 + 详情深度抓取 + 反爬策略。"""

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    def __init__(self, fields: list[str], logger: logging.Logger | None = None):
        self.fields = [f.strip() for f in fields]
        self.log = logger or logging.getLogger("scraper")
        self.driver = None
        self.results: list[dict] = []
        self.stats = {"pages_ok": 0, "pages_fail": 0, "detail_ok": 0, "detail_fail": 0}
        self.start_time = None

    # ── 浏览器 ──

    def _init_driver(self):
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from webdriver_manager.chrome import ChromeDriverManager

        opts = Options()
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--window-size=1920,1080")
        for k, v in self.HEADERS.items():
            opts.add_argument(f"--{k}={v}")

        # webdriver-manager 自动下载匹配的 ChromeDriver
        svc = Service(ChromeDriverManager().install())
        self.driver = webdriver.Chrome(service=svc, options=opts)
        self.driver.set_page_load_timeout(30)
        self.log.info("浏览器已启动（无头 Chrome）")

    def _quit_driver(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.log.info("浏览器已关闭")

    # ── 等待 ──

    def _wait_for(self, selector: str, timeout: int = 10):
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC

        return WebDriverWait(self.driver, timeout).until(
            EC.presence_of_all_elements_located((By.CSS_SELECTOR, selector))
        )

    # ── 页面抓取 ──

    def _fetch_page(self, url: str) -> bool:
        try:
            self.driver.get(url)
            time.sleep(random.uniform(2, 5))  # ponytail: 模拟人类浏览间隔
            return True
        except Exception as e:
            self.log.warning("页面加载失败 %s: %s", url, e)
            return False

    # ── 列表提取 ──

    def _extract_list(self, item_selector: str) -> list[dict]:
        """从当前列表页提取字段 + 详情链接。"""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(self.driver.page_source, "html.parser")
        items = soup.select(item_selector)
        self.log.info("  列表项: %d 个", len(items))

        rows = []
        for el in items:
            row = {}
            for f in self.fields:
                if f == "link" or f == "url":
                    a = el.select_one("a[href]")
                    href = a.get("href", "") if a else ""
                    row[f] = urljoin(self.driver.current_url, href) if href else ""
                else:
                    # 按 class / tag / data-attr 匹配字段
                    val = (
                        el.select_one(f".{f}")
                        or el.select_one(f"[data-{f}]")
                        or el.select_one(f)
                    )
                    row[f] = val.get_text(strip=True) if val else ""
            rows.append(row)
        return rows

    # ── 详情抓取 ──

    def _fetch_detail(self, url: str, parent: dict) -> dict:
        """打开详情页，补充缺失字段。"""
        if not url or not self._fetch_page(url):
            self.stats["detail_fail"] += 1
            return parent

        soup = None
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(self.driver.page_source, "html.parser")
        except Exception:
            pass

        if not soup:
            self.stats["detail_fail"] += 1
            return parent

        for f in self.fields:
            if parent.get(f, "").strip():
                continue
            el = soup.select_one(f".{f}") or soup.select_one(f"[data-{f}]")
            if el:
                parent[f] = el.get_text(strip=True)

        self.stats["detail_ok"] += 1
        return parent

    # ── 分页循环 ──

    def _next_page_url(self, base: str, page: int, next_selector: str | None) -> str | None:
        """
        智能翻页：优先用 next_selector 点击；兜底用 page 参数拼接。
        返回 None 表示无更多页。
        """
        if next_selector:
            from selenium.webdriver.common.by import By
            try:
                btn = self.driver.find_element(By.CSS_SELECTOR, next_selector)
                if btn.is_enabled():
                    return btn.get_attribute("href") or None
            except Exception:
                pass

        # ponytail: 常见分页 URL 模式
        if "?" in base:
            sep = "&"
        else:
            sep = "?"
        return f"{base}{sep}page={page}" if page > 1 else base

    # ── 主流程 ──

    def run(
        self,
        url: str,
        item_selector: str,
        max_pages: int = 1,
        detail: bool = False,
        next_selector: str | None = None,
    ) -> list[dict]:
        """
        Args:
            url: 起始 URL
            item_selector: CSS 选择器，选中列表每一项
            max_pages: 最大翻页数
            detail: 是否进入详情页补充字段
            next_selector: 翻页按钮 CSS 选择器（可选）
        """
        self.start_time = time.time()
        self._init_driver()

        page = 1
        while page <= max_pages:
            page_url = self._next_page_url(url, page, next_selector if page == 1 else None)
            if page_url is None and page > 1:
                break

            self.log.info("抓取第 %d 页: %s", page, page_url[:80])
            if not self._fetch_page(page_url):
                self.stats["pages_fail"] += 1
                page += 1
                continue

            self.stats["pages_ok"] += 1
            rows = self._extract_list(item_selector)

            if detail:
                for r in rows:
                    detail_url = r.get("link") or r.get("url", "")
                    if detail_url:
                        self._fetch_detail(detail_url, r)
                        time.sleep(random.uniform(1, 3))

            self.results.extend(rows)
            page += 1

        self._quit_driver()
        elapsed = time.time() - self.start_time
        self.stats["elapsed"] = round(elapsed, 1)
        self.stats["total"] = len(self.results)
        self._log_summary()
        return self.results

    def _log_summary(self):
        s = self.stats
        self.log.info(
            "Done | pages: %d OK / %d fail | detail: %d OK / %d fail | data: %d rows | time: %.1fs",
            s["pages_ok"], s["pages_fail"], s["detail_ok"], s["detail_fail"],
            s["total"], s["elapsed"],
        )


# ═══════════════════════════════════════════════════════
#  数据清洗流水线
# ═══════════════════════════════════════════════════════

def load_raw(path: str) -> "pd.DataFrame":
    import pandas as pd
    df = pd.read_csv(path)
    print(f"加载: {len(df)} 行 × {len(df.columns)} 列")
    return df


def diagnose(df: "pd.DataFrame") -> dict:
    """诊断：重复行、缺失值、异常值。"""
    dupes = int(df.duplicated().sum())
    missing = df.isnull().sum()
    missing = missing[missing > 0].to_dict()

    print("\n==== 诊断 ====")
    print(f"  重复行: {dupes}")
    print(f"  缺失列: {missing if missing else '无'}")
    return {"duplicates": dupes, "missing": missing}


def clean(
    df: "pd.DataFrame",
    drop_dupes: bool = True,
    fill_mode: str = "none",    # none / mean / median / drop
    outlier_std: float = 0,     # 0 = skip, >0 = 剔除超出 N 个标准差的数值
    strip_text: bool = True,
) -> "pd.DataFrame":
    """
    清洗入口。
      fill_mode: none(不填) | drop(删行) | mean | median
      outlier_std: 剔除数值列中超过 N 倍标准差的极端值
    """
    import numpy as np
    import pandas as pd

    df = df.copy()

    if drop_dupes:
        before = len(df)
        df = df.drop_duplicates()
        print(f"  去重: {before} → {len(df)}")

    if strip_text:
        for c in df.select_dtypes(include="object").columns:
            df[c] = df[c].astype(str).str.strip().replace({"nan": "", "None": ""})

    num_cols = df.select_dtypes(include=np.number).columns

    if fill_mode == "drop":
        before = len(df)
        df = df.dropna()
        print(f"  删空值: {before} → {len(df)}")
    elif fill_mode == "mean" and len(num_cols):
        df[num_cols] = df[num_cols].fillna(df[num_cols].mean())
        print(f"  均值填充: {list(num_cols)}")
    elif fill_mode == "median" and len(num_cols):
        df[num_cols] = df[num_cols].fillna(df[num_cols].median())
        print(f"  中位数填充: {list(num_cols)}")

    if outlier_std > 0 and len(num_cols):
        before = len(df)
        for c in num_cols:
            mu, sigma = df[c].mean(), df[c].std()
            if pd.isna(sigma) or sigma == 0:
                continue
            df = df[(df[c] - mu).abs() <= outlier_std * sigma]
        print(f"  异常值剔除 (±{outlier_std}σ): {before} → {len(df)}")

    return df


# ═══════════════════════════════════════════════════════
#  统计与可视化
# ═══════════════════════════════════════════════════════

def analyze(
    df: "pd.DataFrame",
    group_col: str | None = None,
    charts: bool = True,
    out_dir: str | None = None,
) -> dict:
    """
    描述性统计 + 可选分组 + 可视化。
    图表保存为高清 PNG 到 out_dir。
    """
    import numpy as np
    import pandas as pd

    out = Path(out_dir) if out_dir else OUT_DIR / "charts"
    out.mkdir(parents=True, exist_ok=True)

    num_cols = df.select_dtypes(include=np.number).columns
    stats = {}

    # ── 描述性统计 ──
    if len(num_cols):
        desc = df[num_cols].describe().T
        desc["median"] = df[num_cols].median()
        desc["skew"] = df[num_cols].skew()
        stats["numeric"] = desc
        print("\n📊 数值字段统计")
        print(desc.to_string())

    # ── 分组聚合 ──
    if group_col and group_col in df.columns:
        if len(num_cols):
            grp = df.groupby(group_col)[list(num_cols)].agg(["mean", "sum", "count"])
            stats["grouped"] = grp
            print(f"\n==== 按 [{group_col}] 分组聚合")
            print(grp.to_string())

    # ── 图表 ──
    if charts and len(num_cols) >= 1:
        _make_charts(df, num_cols, group_col, out)

    return stats


def _make_charts(df, num_cols, group_col, out_dir: Path):
    import matplotlib
    matplotlib.use("Agg")  # 无 GUI 后端
    import matplotlib.pyplot as plt
    import seaborn as sns

    sns.set_theme(style="whitegrid", font="SimHei" if _has_font("SimHei") else None)
    # ponytail: 中文字体回退，不影响英文图表

    # 分布直方图
    n = len(num_cols)
    cols = min(3, n)
    rows = (n + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(cols * 5, rows * 4))
    axes = axes.flatten() if n > 1 else [axes]
    for i, c in enumerate(num_cols):
        sns.histplot(df[c].dropna(), kde=True, ax=axes[i])
        axes[i].set_title(c)
    for j in range(len(num_cols), len(axes)):
        axes[j].set_visible(False)
    fig.tight_layout()
    fig.savefig(out_dir / "histogram.png", dpi=150)
    plt.close(fig)
    print(f"  直方图 → {out_dir / 'histogram.png'}")

    # 趋势图（用 index 做 x 轴）
    if len(num_cols) and len(df) > 1:
        fig, ax = plt.subplots(figsize=(10, 4))
        for c in num_cols[:5]:  # ponytail: 最多 5 条线，避免拥挤
            ax.plot(df.index, df[c], marker=".", markersize=2, linewidth=0.8, label=c)
        ax.legend(fontsize=8)
        ax.set_title("趋势图")
        fig.tight_layout()
        fig.savefig(out_dir / "trend.png", dpi=150)
        plt.close(fig)
        print(f"  趋势图 → {out_dir / 'trend.png'}")

    # 分组箱线图
    if group_col and group_col in df.columns and len(num_cols):
        for c in num_cols[:6]:
            fig, ax = plt.subplots(figsize=(8, 4))
            df.boxplot(column=c, by=group_col, ax=ax, rot=45)
            ax.set_title(f"{c} by {group_col}")
            fig.tight_layout()
            fig.savefig(out_dir / f"box_{c}_by_{group_col}.png", dpi=150)
            plt.close(fig)
        print(f"  箱线图 → {out_dir}")


def _has_font(name: str) -> bool:
    from matplotlib.font_manager import fontManager
    return any(name.lower() in f.name.lower() for f in fontManager.ttflist)


# ═══════════════════════════════════════════════════════
#  持久化存储
# ═══════════════════════════════════════════════════════

class Storage:
    """SQLite 存储 + CSV 导出。增量爬取自动去重。"""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or str(OUT_DIR / "data.db")
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("PRAGMA journal_mode=WAL")

    @staticmethod
    def _safe_table(table: str) -> str:
        """校验表名仅含字母/数字/下划线（防 SQL 注入）"""
        import re as _re
        if not table or not _re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', table):
            raise ValueError(f"非法表名: {table!r}（仅允许字母/数字/下划线）")
        return table

    def save(
        self,
        df: "pd.DataFrame",
        table: str = "scraped",
        unique_cols: list[str] | None = None,
    ) -> int:
        """
        写入 SQLite，unique_cols 指定去重列（如 ["link"]）。
        返回新增行数。
        """
        import pandas as pd
        table = self._safe_table(table)

        exist = pd.DataFrame()
        try:
            exist = pd.read_sql(f"SELECT * FROM {table}", self.conn)
        except Exception:
            pass

        if not exist.empty and unique_cols:
            mask = ~df.set_index(unique_cols).index.isin(
                exist.set_index(unique_cols).index
            )
            df = df[mask].reset_index(drop=True)

        df.to_sql(table, self.conn, if_exists="append", index=False)
        self.conn.commit()
        print(f"  SQLite → {self.db_path} [{table}] +{len(df)} 行")
        return len(df)

    def export_csv(self, table: str = "scraped", name: str = "data") -> str:
        import pandas as pd
        table = self._safe_table(table)
        df = pd.read_sql(f"SELECT * FROM {table}", self.conn)
        path = str(OUT_DIR / f"{name}.csv")
        df.to_csv(path, index=False, encoding="utf-8-sig")
        print(f"  CSV → {path}")
        return path

    def close(self):
        self.conn.close()


# ═══════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════

def cmd_scrape(args):
    """抓取 → 存为 raw.csv"""
    log = setup_logging("scraper")
    scraper = Scraper(fields=args.fields.split(","), logger=log)

    rows = scraper.run(
        url=args.url,
        item_selector=args.selector,
        max_pages=args.pages,
        detail=args.detail,
        next_selector=args.next or None,
    )

    if not rows:
        log.warning("未抓取到数据")
        return

    # 保存原始 CSV
    import pandas as pd
    df = pd.DataFrame(rows)
    raw_path = OUT_DIR / "raw.csv"
    df.to_csv(raw_path, index=False, encoding="utf-8-sig")
    log.info("原始数据 → %s (%d 行)", raw_path, len(df))


def cmd_clean(args):
    """清洗 → 输出 clean.csv"""
    df = load_raw(args.input)
    diagnose(df)
    df = clean(
        df,
        drop_dupes=not args.keep_dupes,
        fill_mode=args.fill,
        outlier_std=args.outlier,
    )
    path = OUT_DIR / "clean.csv"
    df.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"清洗数据 → {path}")


def cmd_analyze(args):
    """分析 + 图表"""
    df = load_raw(args.input)
    analyze(df, group_col=args.group, charts=not args.no_chart)


def cmd_store(args):
    """存储：SQLite 入库 + CSV 导出"""
    df = load_raw(args.input)
    st = Storage(args.db)
    st.save(df, table=args.table, unique_cols=args.unique.split(",") if args.unique else None)
    st.export_csv(table=args.table, name=args.export_name or "export")
    st.close()


def cmd_full(args):
    """全流程一键执行"""
    log = setup_logging("full")

    # 1. 抓取
    log.info("═══ 阶段 1: 爬取 ═══")
    scraper = Scraper(fields=args.fields.split(","), logger=log)
    rows = scraper.run(
        url=args.url,
        item_selector=args.selector,
        max_pages=args.pages,
        detail=args.detail,
        next_selector=args.next or None,
    )
    if not rows:
        log.error("无数据，终止")
        return
    import pandas as pd
    df_raw = pd.DataFrame(rows)
    raw_path = OUT_DIR / "raw.csv"
    df_raw.to_csv(raw_path, index=False, encoding="utf-8-sig")

    # 2. 清洗
    log.info("═══ 阶段 2: 清洗 ═══")
    diagnose(df_raw)
    df_clean = clean(df_raw, fill_mode=args.fill, outlier_std=args.outlier)
    clean_path = OUT_DIR / "clean.csv"
    df_clean.to_csv(clean_path, index=False, encoding="utf-8-sig")

    # 3. 分析
    log.info("═══ 阶段 3: 分析 ═══")
    analyze(df_clean, group_col=args.group, charts=True)

    # 4. 存储
    log.info("═══ 阶段 4: 存储 ═══")
    st = Storage(args.db)
    st.save(df_clean, table="scraped", unique_cols=["link"] if "link" in df_clean.columns else None)
    st.export_csv(table="scraped", name="final")
    st.close()

    log.info("═══ 全流程完成 ═══")


def main():
    parser = argparse.ArgumentParser(description="数据采集分析工具")
    sub = parser.add_subparsers(dest="cmd")

    # scrape
    p = sub.add_parser("scrape", help="爬取数据")
    p.add_argument("-u", "--url", required=True)
    p.add_argument("-s", "--selector", required=True, help="列表项 CSS 选择器，例: .product-item")
    p.add_argument("-f", "--fields", required=True, help="字段名，逗号分隔，例: title,price,link")
    p.add_argument("-p", "--pages", type=int, default=1)
    p.add_argument("-d", "--detail", action="store_true", help="进入详情页补充字段")
    p.add_argument("--next", help="翻页按钮 CSS 选择器，例: .next-page a")
    p.add_argument("--delay-min", type=float, default=1.0)
    p.add_argument("--delay-max", type=float, default=3.0)

    # clean
    p = sub.add_parser("clean", help="清洗数据")
    p.add_argument("-i", "--input", required=True)
    p.add_argument("--keep-dupes", action="store_true")
    p.add_argument("--fill", default="none", choices=["none", "drop", "mean", "median"])
    p.add_argument("--outlier", type=float, default=0, help="异常值剔除阈值（标准差倍数）")

    # analyze
    p = sub.add_parser("analyze", help="统计与可视化")
    p.add_argument("-i", "--input", required=True)
    p.add_argument("-g", "--group", help="分组聚合列名")
    p.add_argument("--no-chart", action="store_true")

    # store
    p = sub.add_parser("store", help="存入 SQLite + 导出 CSV")
    p.add_argument("-i", "--input", required=True)
    p.add_argument("--db", help="SQLite 路径，默认 output/data.db")
    p.add_argument("--table", default="scraped")
    p.add_argument("--unique", help="去重列，逗号分隔，例: link")
    p.add_argument("--export-name", help="导出 CSV 名称")

    # full
    p = sub.add_parser("full", help="全流程一键执行")
    p.add_argument("-u", "--url", required=True)
    p.add_argument("-s", "--selector", required=True)
    p.add_argument("-f", "--fields", required=True)
    p.add_argument("-p", "--pages", type=int, default=1)
    p.add_argument("-d", "--detail", action="store_true")
    p.add_argument("--next")
    p.add_argument("-g", "--group")
    p.add_argument("--fill", default="none", choices=["none", "drop", "mean", "median"])
    p.add_argument("--outlier", type=float, default=0)
    p.add_argument("--db")

    args = parser.parse_args()

    if args.cmd == "scrape":
        cmd_scrape(args)
    elif args.cmd == "clean":
        cmd_clean(args)
    elif args.cmd == "analyze":
        cmd_analyze(args)
    elif args.cmd == "store":
        cmd_store(args)
    elif args.cmd == "full":
        cmd_full(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
