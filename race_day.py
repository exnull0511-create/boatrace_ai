# -*- coding: utf-8 -*-
"""
ボートレース予想AI - レース当日オーケストレーター

方式: 締切時刻ベースのスケジューラ
  1. 起動時に全場の全レース締切時刻を取得
  2. 締切7分前のレースを計算してキューに入れる
  3. 次のレースの実行時刻まで sleep
  4. ピンポイントで予想→通知
  5. 全レース完了まで繰り返し
"""
import sys
import io
import os
import csv
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from scraper import BoatraceScraper
from engine import predict_race
from strategy import allocate_portfolio, should_look
from send_discord import send_prediction, send_daily_summary
from config import VENUE_MAP, BET_BASE

# ============================================================
# 設定
# ============================================================
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

NOTIFY_BEFORE_MIN = 7     # 締切の何分前に予想実行
BUDGET_PER_RACE = 1000    # 1レースあたり予算


# ============================================================
# ベットログ (重複防止 + 精算用)
# ============================================================

def _log_path(date_str: str) -> Path:
    return DATA_DIR / f"bets_{date_str}.csv"


def _race_data_path(date_str: str) -> Path:
    return DATA_DIR / f"races_{date_str}.json"


def already_logged(date_str: str, place_no: int, race_no: int) -> bool:
    path = _log_path(date_str)
    if not path.exists():
        return False
    with open(path, "r", encoding="utf-8") as f:
        for row in csv.reader(f):
            if len(row) >= 3 and row[0] == str(place_no) and row[1] == str(race_no):
                return True
    return False


def log_bet(date_str: str, place_no: int, race_no: int, venue: str,
            race_type: str, bets: list, is_look: bool, look_reason: str = ""):
    path = _log_path(date_str)
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        bet_str = json.dumps(bets, ensure_ascii=False) if bets else "[]"
        total = sum(b["amount"] for b in bets)
        w.writerow([
            place_no, race_no, venue, race_type,
            "LOOK" if is_look else "BET",
            total, bet_str, look_reason,
            datetime.now().strftime("%H:%M:%S")
        ])


def save_race_data(date_str: str, race_record: dict):
    """全レースのデータを蓄積 (月末バックテスト用)"""
    path = _race_data_path(date_str)
    records = []
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            records = json.load(f)
    records.append(race_record)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=1)


# ============================================================
# スケジュール取得: 全場の締切時刻を一括取得
# ============================================================

def build_schedule(scraper: BoatraceScraper, date_str: str) -> list:
    """
    全24場をスキャンして開催場を検出し、全レースの締切時刻を取得。
    Returns: [(exec_time, place_no, race_no, venue, deadline_str), ...]
             exec_time = 締切 - NOTIFY_BEFORE_MIN 分
    """
    schedule = []
    today = datetime.now().date()

    print(f"スケジュール取得中...")
    for place_no in range(1, 25):
        venue = VENUE_MAP.get(place_no, f"?{place_no}")
        try:
            race = scraper.scrape_full_race(place_no, 1, date_str)
            if not race or not race.racers:
                continue

            print(f"  {venue}: ", end="")

            for race_no in range(1, 13):
                if race_no > 1:
                    race = scraper.scrape_full_race(place_no, race_no, date_str)
                    if not race or not race.racers:
                        continue

                deadline_str = race.deadline
                if not deadline_str or ":" not in deadline_str:
                    continue

                hh, mm = deadline_str.split(":")
                deadline_dt = datetime.combine(today, datetime.strptime(f"{hh}:{mm}", "%H:%M").time())
                exec_time = deadline_dt - timedelta(minutes=NOTIFY_BEFORE_MIN)

                schedule.append((exec_time, place_no, race_no, venue, deadline_str))

            print(f"{len([s for s in schedule if s[1]==place_no])}R")

        except Exception as e:
            print(f"  {venue}: SKIP ({e})")

    # 実行時刻順にソート
    schedule.sort(key=lambda x: x[0])
    return schedule


# ============================================================
# 1レースの予想実行
# ============================================================

def process_race(scraper: BoatraceScraper, date_str: str,
                 place_no: int, race_no: int, venue: str,
                 deadline_str: str, dry_run: bool = False) -> bool:
    """1レースの予想→通知→データ蓄積"""
    if already_logged(date_str, place_no, race_no):
        print(f"  → 既にログ済み SKIP")
        return False

    try:
        # データ取得
        race = scraper.scrape_full_race(place_no, race_no, date_str)
        if not race or not race.racers:
            print(f"  → データ取得失敗")
            return False

        odds = scraper.scrape_odds(place_no, race_no, date_str)
        race.trifecta_odds = odds

        # 予測
        pred = predict_race(race)
        is_look, look_reason = should_look(pred, race)

        # ベッティング
        if is_look:
            bets_data = []
        else:
            bets = allocate_portfolio(pred, budget=BUDGET_PER_RACE)
            bets_data = [{"combo": b.combo, "odds": b.odds,
                          "prob": b.prob, "amount": b.bet_amount}
                         for b in bets]

        # データ蓄積
        scores = pred["scores"]
        sorted_scores = sorted(scores.values(), reverse=True)
        gap = sorted_scores[0] - sorted_scores[1] if len(sorted_scores) >= 2 else 0
        race_record = {
            "place_no": place_no, "venue": venue, "race_no": race_no,
            "deadline": deadline_str,
            "race_type": pred["race_type_tag"],
            "is_look": is_look, "look_reason": look_reason,
            "score_gap": round(gap, 3),
            "bets": bets_data,
            "top5_prob": [{"combo": c, "prob": round(p, 5)}
                          for c, p, _ in pred["top_combos"][:5]],
            "racers": [{"waku": r.waku, "name": r.name, "grade": r.grade,
                         "score": round(r.ev_score, 3)}
                        for r in race.racers],
            "timestamp": datetime.now().isoformat(),
        }
        save_race_data(date_str, race_record)

        # ログ記録
        log_bet(date_str, place_no, race_no, venue,
                pred["race_type_tag"], bets_data, is_look, look_reason)

        # Discord通知
        tag = pred["race_type_tag"]
        if is_look:
            print(f"  → 👀 LOOK ({look_reason})")
            if not dry_run:
                send_prediction(venue, race_no, tag, bets_data,
                                deadline_str, is_look=True,
                                look_reason=look_reason)
        else:
            total = sum(b["amount"] for b in bets_data)
            print(f"  → 🏁 [{tag}] {len(bets_data)}点 {total}円")
            if not dry_run:
                send_prediction(venue, race_no, tag, bets_data, deadline_str)

        return True

    except Exception as e:
        print(f"  → ERROR: {e}")
        return False


# ============================================================
# メイン: 締切時刻ベースのスケジューラ
# ============================================================

def run_scheduler(date_str: str = None, dry_run: bool = False,
                  until: str = None):
    if date_str is None:
        date_str = datetime.now().strftime("%Y%m%d")

    scraper = BoatraceScraper()

    # Step 1: 全レースの締切時刻を取得
    schedule = build_schedule(scraper, date_str)
    if not schedule:
        print("本日の開催なし")
        return

    now = datetime.now()
    today = now.date()

    # --until で終了時刻を制限
    until_dt = None
    if until:
        hh, mm = until.split(":")
        until_dt = datetime.combine(today, datetime.strptime(f"{hh}:{mm}", "%H:%M").time())

    # 未来の実行のみフィルタ + until制限
    future = []
    for t, p, r, v, d in schedule:
        if t < now - timedelta(minutes=3):
            continue
        if until_dt and t > until_dt:
            continue
        future.append((t, p, r, v, d))

    total = len(schedule)
    remaining = len(future)

    print(f"\n{'='*55}")
    print(f"  スケジュール: {total}R (今回対象{remaining}R)")
    if until:
        print(f"  終了時刻: {until}")
    print(f"  最初: {future[0][3]} {future[0][2]}R ({future[0][4]})" if future else "")
    print(f"  最後: {future[-1][3]} {future[-1][2]}R ({future[-1][4]})" if future else "")
    print(f"{'='*55}\n")

    processed = 0
    for exec_time, place_no, race_no, venue, deadline_str in future:
        now = datetime.now()
        wait_sec = (exec_time - now).total_seconds()

        if wait_sec > 0:
            print(f"⏳ 次: {venue} {race_no}R (締切{deadline_str}) "
                  f"→ {exec_time.strftime('%H:%M')}に実行 "
                  f"(あと{wait_sec/60:.0f}分)")
            time.sleep(max(0, wait_sec))

        print(f"\n🏁 {venue} {race_no}R (締切{deadline_str})")
        ok = process_race(scraper, date_str, place_no, race_no,
                          venue, deadline_str, dry_run)
        if ok:
            processed += 1
        time.sleep(2)  # レート制限

    print(f"\n{'='*55}")
    print(f"  完了: {processed}R処理")
    print(f"{'='*55}")


# ============================================================
# エントリーポイント
# ============================================================

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None, help="日付 (yyyymmdd)")
    parser.add_argument("--dry-run", action="store_true", help="Discord通知しない")
    parser.add_argument("--until", default=None, help="終了時刻 (HH:MM)")
    args = parser.parse_args()
    run_scheduler(args.date, args.dry_run, args.until)
