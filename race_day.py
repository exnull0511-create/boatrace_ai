# -*- coding: utf-8 -*-
"""
ボートレース予想AI - レース当日オーケストレーター

全場全レースを監視し、締切7-10分前に予想→通知を実行。
全レースのデータを蓄積して月末バックテスト用に保存。
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

NOTIFY_WINDOW_MIN = 7    # 締切の何分前から通知開始
NOTIFY_WINDOW_MAX = 10   # 締切の何分前まで通知対象
BUDGET_PER_RACE = 1000   # 1レースあたり予算


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
# スケジュール取得
# ============================================================

def get_today_schedule(scraper: BoatraceScraper, date_str: str) -> list:
    """本日開催中の場を検出"""
    active_venues = []
    for place_no in range(1, 25):
        venue = VENUE_MAP.get(place_no, f"?{place_no}")
        try:
            race = scraper.scrape_full_race(place_no, 1, date_str)
            if race and race.racers:
                active_venues.append((place_no, venue))
        except:
            pass
    return active_venues


# ============================================================
# メイン処理: 1回の実行サイクル
# ============================================================

def run_cycle(date_str: str = None, dry_run: bool = False):
    """
    1回の実行サイクル:
    - 全開催場の全レースを確認
    - 締切7-10分前のレースを予想+通知
    - 全レースデータを蓄積
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y%m%d")

    scraper = BoatraceScraper()
    now = datetime.now()
    processed = 0
    notified = 0

    print(f"\n[{now.strftime('%H:%M:%S')}] サイクル開始 ({date_str})")

    for place_no in range(1, 25):
        venue = VENUE_MAP.get(place_no, f"?{place_no}")

        for race_no in range(1, 13):
            # 既にログ済みならスキップ
            if already_logged(date_str, place_no, race_no):
                continue

            try:
                race = scraper.scrape_full_race(place_no, race_no, date_str)
                if not race or not race.racers:
                    continue

                # 締切時刻チェック
                deadline_str = race.deadline  # "HH:MM" 形式
                if not deadline_str or ":" not in deadline_str:
                    continue

                hh, mm = deadline_str.split(":")
                deadline_dt = now.replace(hour=int(hh), minute=int(mm), second=0)
                diff = (deadline_dt - now).total_seconds() / 60  # 分

                # 通知ウィンドウ外ならスキップ
                if diff < NOTIFY_WINDOW_MIN or diff > NOTIFY_WINDOW_MAX:
                    continue

                print(f"  {venue} {race_no}R (締切{deadline_str}, あと{diff:.0f}分)")

                # オッズ取得
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

                # レースデータ蓄積 (月末バックテスト用)
                scores = pred["scores"]
                sorted_scores = sorted(scores.values(), reverse=True)
                gap = sorted_scores[0] - sorted_scores[1] if len(sorted_scores) >= 2 else 0
                race_record = {
                    "place_no": place_no, "venue": venue, "race_no": race_no,
                    "deadline": deadline_str,
                    "race_type": pred["race_type_tag"],
                    "is_look": is_look, "look_reason": look_reason,
                    "score_gap": round(gap, 3),
                    "score_top": round(max(scores.values()), 3) if scores else 0,
                    "bets": bets_data,
                    "top5_prob": [
                        {"combo": c, "prob": round(p, 5)}
                        for c, p, _ in pred["top_combos"][:5]
                    ],
                    "racers": [
                        {"waku": r.waku, "name": r.name, "grade": r.grade,
                         "score": round(r.ev_score, 3),
                         "nige": round(r.course_nigeritsu, 3),
                         "display": round(r.tenji_time, 3)}
                        for r in race.racers
                    ],
                    "timestamp": datetime.now().isoformat(),
                }
                save_race_data(date_str, race_record)

                # ログ記録
                log_bet(date_str, place_no, race_no, venue,
                        pred["race_type_tag"], bets_data, is_look, look_reason)

                # Discord通知
                if not dry_run:
                    send_prediction(venue, race_no, pred["race_type_tag"],
                                    bets_data, deadline_str,
                                    is_look=is_look, look_reason=look_reason)
                    time.sleep(1)  # レート制限回避

                tag = pred["race_type_tag"]
                if is_look:
                    print(f"    → 👀 LOOK ({look_reason})")
                else:
                    total = sum(b["amount"] for b in bets_data)
                    print(f"    → 🏁 {tag} {len(bets_data)}点 {total}円")
                    notified += 1

                processed += 1

            except Exception as e:
                print(f"  {venue} {race_no}R: ERROR {e}")

    print(f"[{datetime.now().strftime('%H:%M:%S')}] サイクル完了: "
          f"{processed}R処理 / {notified}R通知")
    return processed


# ============================================================
# エントリーポイント
# ============================================================

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None, help="日付 (yyyymmdd)")
    parser.add_argument("--dry-run", action="store_true", help="Discord通知しない")
    parser.add_argument("--loop", action="store_true", help="5分間隔ループ")
    args = parser.parse_args()

    date_str = args.date or datetime.now().strftime("%Y%m%d")

    if args.loop:
        print(f"ループモード開始 ({date_str})")
        while True:
            run_cycle(date_str, dry_run=args.dry_run)
            print("  … 5分待機")
            time.sleep(300)
    else:
        run_cycle(date_str, dry_run=args.dry_run)
