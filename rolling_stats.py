# -*- coding: utf-8 -*-
"""
ローリング統計: rawデータから決まり手分布等の統計を動的に算出

使い方:
  from rolling_stats import RollingStats
  stats = RollingStats()
  stats.load(lookback_days=21)
  kimarite = stats.kimarite_dist  # {"逃げ": 0.52, ...}
"""
import json
import glob
import os
from datetime import datetime, timedelta
from typing import Dict, Optional
from pathlib import Path

RAW_DIR = Path(__file__).parent / "data" / "raw"
CACHE_PATH = Path(__file__).parent / "data" / "rolling_stats_cache.json"

# 634Rのデフォルト値 (rawデータ不足時のフォールバック)
DEFAULT_KIMARITE = {
    "逃げ": 0.54, "差し": 0.13, "まくり": 0.17,
    "まくり差し": 0.11, "抜き": 0.05,
}

# ローリング統計に必要な最低レース数
MIN_RACES = 100


class RollingStats:
    """rawデータからローリング統計を算出"""

    def __init__(self, raw_dir: str = None):
        self.raw_dir = Path(raw_dir) if raw_dir else RAW_DIR
        self.kimarite_dist: Dict[str, float] = DEFAULT_KIMARITE.copy()
        self.total_races: int = 0
        self.dates_used: list = []

    def load(self, lookback_days: int = 21, end_date: str = None) -> bool:
        """
        直近N日のrawデータから統計を算出。

        Args:
            lookback_days: 何日分遡るか
            end_date: 終了日 (yyyymmdd)。Noneなら今日。

        Returns:
            True: 十分なデータで算出できた
            False: データ不足でデフォルト値を使用
        """
        if end_date:
            end_dt = datetime.strptime(end_date, "%Y%m%d")
        else:
            end_dt = datetime.now()

        start_dt = end_dt - timedelta(days=lookback_days)

        # 対象日の生データを読み込み
        kimarite_counts = {}
        total = 0
        dates_used = []

        files = sorted(glob.glob(str(self.raw_dir / "*.json")))
        for fpath in files:
            date_str = os.path.basename(fpath).replace(".json", "")
            try:
                file_dt = datetime.strptime(date_str, "%Y%m%d")
            except ValueError:
                continue

            if file_dt < start_dt or file_dt > end_dt:
                continue

            try:
                races = json.load(open(fpath, encoding="utf-8"))
            except Exception:
                continue

            for raw in races:
                km = raw.get("result_kimarite", "")
                if km:
                    # 正規化: "逃げ", "差し", "まくり", "まくり差し", "抜き", "恵まれ" 等
                    km_normalized = _normalize_kimarite(km)
                    kimarite_counts[km_normalized] = kimarite_counts.get(km_normalized, 0) + 1
                    total += 1

            dates_used.append(date_str)

        self.total_races = total
        self.dates_used = dates_used

        if total < MIN_RACES:
            # データ不足: デフォルト値を使用
            return False

        # 確率分布に変換
        self.kimarite_dist = {
            km: count / total
            for km, count in kimarite_counts.items()
        }

        # 主要5パターン以外は「抜き」に吸収
        main_patterns = {"逃げ", "差し", "まくり", "まくり差し", "抜き"}
        other_total = sum(v for k, v in self.kimarite_dist.items() if k not in main_patterns)
        self.kimarite_dist = {
            k: v for k, v in self.kimarite_dist.items() if k in main_patterns
        }
        self.kimarite_dist["抜き"] = self.kimarite_dist.get("抜き", 0) + other_total

        # 正規化
        total_prob = sum(self.kimarite_dist.values())
        if total_prob > 0:
            self.kimarite_dist = {k: v / total_prob for k, v in self.kimarite_dist.items()}

        return True

    def save_cache(self):
        """統計をキャッシュファイルに保存"""
        cache = {
            "kimarite_dist": self.kimarite_dist,
            "total_races": self.total_races,
            "dates_used": self.dates_used,
            "updated_at": datetime.now().isoformat(),
        }
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)

    def load_cache(self) -> bool:
        """キャッシュから統計を読み込み"""
        if not CACHE_PATH.exists():
            return False
        try:
            cache = json.load(open(CACHE_PATH, encoding="utf-8"))
            self.kimarite_dist = cache["kimarite_dist"]
            self.total_races = cache["total_races"]
            self.dates_used = cache.get("dates_used", [])
            return True
        except Exception:
            return False


def _normalize_kimarite(km: str) -> str:
    """決まり手の表記を正規化"""
    km = km.strip()
    # 主要パターンにマッチング
    if "逃" in km:
        return "逃げ"
    elif "まくり差" in km or "捲り差" in km:
        return "まくり差し"
    elif "まくり" in km or "捲り" in km:
        return "まくり"
    elif "差し" in km or "差" in km:
        return "差し"
    elif "抜" in km:
        return "抜き"
    elif "恵" in km:
        return "抜き"  # 恵まれは抜きに統合
    else:
        return "抜き"  # 不明は抜きに統合


# ============================================================
# CLI テスト
# ============================================================

if __name__ == "__main__":
    stats = RollingStats()
    ok = stats.load(lookback_days=30)

    print(f"データ: {stats.total_races}R ({len(stats.dates_used)}日)")
    if stats.dates_used:
        print(f"期間: {stats.dates_used[0]} ~ {stats.dates_used[-1]}")

    if ok:
        print("\nローリング統計:")
    else:
        print("\nデフォルト値 (データ不足):")

    for km, prob in sorted(stats.kimarite_dist.items(), key=lambda x: -x[1]):
        bar = "#" * int(prob * 50)
        print(f"  {km:8} {prob:>5.1%} {bar}")

    default = DEFAULT_KIMARITE
    if ok:
        print("\n差分 (ローリング - デフォルト):")
        for km in sorted(default.keys()):
            diff = stats.kimarite_dist.get(km, 0) - default[km]
            print(f"  {km:8} {diff:>+5.1%}")
