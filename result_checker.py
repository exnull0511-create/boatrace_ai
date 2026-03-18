# -*- coding: utf-8 -*-
"""
ボートレース予想AI - 結果精算

全レース終了後に結果を取得し、的中/不的中を判定。
日次サマリーをDiscordに送信。
"""
import sys
import io
import csv
import json
import time
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from scraper import BoatraceScraper
from send_discord import send_result, send_daily_summary
from config import VENUE_MAP

DATA_DIR = Path(__file__).parent / "data"


def check_results(date_str: str = None, dry_run: bool = False):
    """全ベットの結果を精算"""
    if date_str is None:
        date_str = datetime.now().strftime("%Y%m%d")

    log_path = DATA_DIR / f"bets_{date_str}.csv"
    if not log_path.exists():
        print(f"ログなし: {log_path}")
        return

    # ベットログ読み込み
    rows = []
    with open(log_path, "r", encoding="utf-8") as f:
        for row in csv.reader(f):
            if len(row) >= 8:
                rows.append(row)

    if not rows:
        print("ベットなし")
        return

    scraper = BoatraceScraper()
    total_bet = 0
    total_payout = 0
    hits = 0
    look_count = 0
    total_races = 0
    results_log = []

    for row in rows:
        place_no = int(row[0])
        race_no = int(row[1])
        venue = row[2]
        race_type = row[3]
        status = row[4]  # BET or LOOK
        bet_amount = int(row[5])
        bets_json = row[6]

        if status == "LOOK":
            look_count += 1
            continue

        total_races += 1
        bets = json.loads(bets_json)

        try:
            # 結果取得
            race = scraper.scrape_full_race(place_no, race_no, date_str,
                                            include_result=True)
            if not race or not race.result_order:
                print(f"  {venue} {race_no}R: 結果未確定")
                continue

            actual = f"{race.result_order[0]}-{race.result_order[1]}-{race.result_order[2]}"
            kimarite = race.result_kimarite or ""
            payout_odds = race.result_payout or 0

            # 的中判定
            hit_bet = None
            for b in bets:
                if b["combo"] == actual:
                    hit_bet = b
                    break

            win_amount = 0
            if hit_bet:
                win_amount = int(payout_odds * hit_bet["amount"] / 100)
                hits += 1

            total_bet += bet_amount
            total_payout += win_amount

            pnl = win_amount - bet_amount
            mark = "✅" if hit_bet else "❌"
            print(f"  {mark} {venue} {race_no}R: {actual} ({kimarite}) "
                  f"{payout_odds}円 → {pnl:+,}円")

            # Discord通知
            if not dry_run:
                send_result(venue, race_no, actual, kimarite,
                            payout_odds, hit_bet is not None,
                            bet_amount, win_amount)
                time.sleep(1)

            results_log.append({
                "venue": venue, "race_no": race_no,
                "actual": actual, "kimarite": kimarite,
                "payout_odds": payout_odds,
                "hit": hit_bet is not None,
                "bet_amount": bet_amount, "win_amount": win_amount,
                "race_type": race_type,
            })

        except Exception as e:
            print(f"  {venue} {race_no}R: ERROR {e}")

    # サマリー
    roi = total_payout / total_bet * 100 if total_bet > 0 else 0
    pnl = total_payout - total_bet
    print(f"\n{'='*50}")
    print(f"日次サマリー ({date_str})")
    print(f"{'='*50}")
    print(f"  対象: {total_races}R (LOOK: {look_count}R)")
    print(f"  的中: {hits}R")
    print(f"  投資: {total_bet:,}円")
    print(f"  回収: {total_payout:,}円")
    print(f"  ROI:  {roi:.1f}%")
    print(f"  収支: {pnl:+,}円")

    # Discord日次サマリー
    if not dry_run and total_races > 0:
        send_daily_summary(date_str, total_races, hits,
                           total_bet, total_payout, look_count)

    # 結果をJSON保存
    result_path = DATA_DIR / f"results_{date_str}.json"
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump({
            "date": date_str,
            "total_races": total_races, "hits": hits,
            "total_bet": total_bet, "total_payout": total_payout,
            "roi": roi, "look_count": look_count,
            "results": results_log,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n保存: {result_path}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    check_results(args.date, args.dry_run)
