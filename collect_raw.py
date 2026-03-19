# -*- coding: utf-8 -*-
"""
生データ収集: 選手情報 + オッズ + 結果だけを高速収集
エラー耐性を強化: 1場ごとに中間保存
"""
import sys, io, time, json, os, traceback
from dataclasses import asdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.path.insert(0, r'c:\money plus\boatrace_ai')

from scraper import BoatraceScraper
from config import VENUE_MAP

DATES = [
    "20260312", "20260313", "20260314", "20260315",
    "20260316", "20260317", "20260318",
]

OUTPUT_DIR = r"c:\money plus\boatrace_ai\data\raw"


def collect_raw_day(scraper, hiduke):
    """1日分の生データ(選手/オッズ/結果)を収集。"""
    races = []

    for place_no in range(1, 25):
        venue = VENUE_MAP.get(place_no, f"?{place_no}")

        try:
            test_race = scraper.scrape_full_race(place_no, 1, hiduke, include_result=True)
            if not test_race or not test_race.racers:
                continue
            if not test_race.result_order:
                continue
        except Exception as e:
            continue

        print(f"  {venue}", end="", flush=True)
        count = 0

        for race_no in range(1, 13):
            try:
                if race_no == 1:
                    race = test_race
                else:
                    race = scraper.scrape_full_race(place_no, race_no, hiduke, include_result=True)

                if not race or not race.racers or not race.result_order:
                    continue

                odds = scraper.scrape_odds(place_no, race_no, hiduke)

                racers_data = [asdict(r) for r in race.racers]

                race_data = {
                    "place_no": place_no,
                    "venue": venue,
                    "race_no": race_no,
                    "deadline": race.deadline,
                    "racers": racers_data,
                    "trifecta_odds": odds,
                    "result_order": race.result_order,
                    "result_kimarite": race.result_kimarite,
                    "result_payout": race.result_payout,
                    "seibi_list": race.seibi_list,
                    "flying_info": race.flying_info,
                }
                races.append(race_data)
                count += 1

            except Exception as e:
                print(f"[E:{race_no}R]", end="", flush=True)

        print(f"({count})", end=" ", flush=True)

    print()
    return races


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    scraper = BoatraceScraper()

    print("=" * 60)
    print("  📦 7日分 生データ収集")
    print(f"  保存先: {OUTPUT_DIR}")
    print("=" * 60)

    for hiduke in DATES:
        fname = os.path.join(OUTPUT_DIR, f"{hiduke}.json")

        if os.path.exists(fname):
            d = json.load(open(fname, encoding='utf-8'))
            print(f"✅ {hiduke}: 既存 ({len(d)}R) → SKIP")
            continue

        print(f"\n📅 {hiduke} 収集中...")
        start = time.time()

        try:
            races = collect_raw_day(scraper, hiduke)
        except Exception as e:
            print(f"\n  ⚠️ エラー: {e}")
            traceback.print_exc()
            races = []

        # 0Rでもファイルは保存（次回SKIPのため）
        if races:
            with open(fname, "w", encoding="utf-8") as f:
                json.dump(races, f, ensure_ascii=False, indent=1)
            elapsed = time.time() - start
            print(f"  ✅ {len(races)}R / {elapsed:.0f}秒")
        else:
            print(f"  ⚠️ {hiduke}: 0R (開催なし?)")

    # サマリー
    print(f"\n{'='*60}")
    total = 0
    for hiduke in DATES:
        fname = os.path.join(OUTPUT_DIR, f"{hiduke}.json")
        if os.path.exists(fname):
            d = json.load(open(fname, encoding='utf-8'))
            total += len(d)
            print(f"  {hiduke}: {len(d):>3}R")
        else:
            print(f"  {hiduke}: なし")
    print(f"  合計: {total}R")
    print(f"\n→ python fast_backtest.py でバックテスト")


if __name__ == "__main__":
    main()
