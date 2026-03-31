# -*- coding: utf-8 -*-
"""
ボートレース予想AI - レース当日オーケストレーター

方式: 締切時刻ベースのスケジューラ
  1. 起動時に全場の全レース締切時刻を取得
  2. --from / --until で実行範囲を指定可能
  3. 締切7分前のレースを順に処理 (sleepで待機)
  4. 完了後に終了
"""
import sys
import io
import os
import csv
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)

from scraper import BoatraceScraper
from engine_v6 import (predict_race_v6, should_look_v6,
                       select_ev_top_bets, V6_BUDGET,
                       LOOK_TENKAI_MIN, LOOK_AGREE_MIN)
from strategy import allocate_portfolio, should_look, CHUUANA_BUDGET
from send_discord import send_prediction, send_daily_summary
from config import VENUE_MAP, BET_BASE

# ============================================================
# 設定
# ============================================================
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

NOTIFY_BEFORE_MIN = 7     # 締切の何分前に予想実行
BUDGET_PER_RACE = 1000

# 厳選モード設定
# 23日間バックテスト: ag<0.40 top5/日 → ROI 129%, DD 30,650円
GENSEN_MAX_RACES = 5      # 厳選時の最大レース数 (低合意top5)
DAILY_BUDGET_MAX = 7000   # 1日の最大投資額 (円)


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

def build_schedule(scraper: BoatraceScraper, date_str: str,
                   from_time: str = None, until_time: str = None) -> list:
    """
    全場の全レース締切時刻を取得してキューを作る。

    Args:
        from_time:  "HH:MM" - この時刻以降のレースのみ
        until_time: "HH:MM" - この時刻以前のレースのみ
    """
    schedule = []
    today = datetime.now().date()

    # 時刻範囲
    from_dt = None
    until_dt = None
    if from_time:
        h, m = from_time.split(":")
        from_dt = datetime.combine(today, datetime.strptime(f"{h}:{m}", "%H:%M").time())
    if until_time:
        h, m = until_time.split(":")
        until_dt = datetime.combine(today, datetime.strptime(f"{h}:{m}", "%H:%M").time())

    print(f"スケジュール取得中...")
    for place_no in range(1, 25):
        venue = VENUE_MAP.get(place_no, f"?{place_no}")
        venue_count = 0
        try:
            # まず1Rで開催有無チェック
            race = scraper.scrape_full_race(place_no, 1, date_str)
            if not race or not race.racers:
                continue

            for race_no in range(1, 13):
                if race_no > 1:
                    race = scraper.scrape_full_race(place_no, race_no, date_str)
                    if not race or not race.racers:
                        continue

                deadline_str = race.deadline
                if not deadline_str or ":" not in deadline_str:
                    continue

                hh, mm = deadline_str.split(":")
                deadline_dt = datetime.combine(
                    today, datetime.strptime(f"{hh}:{mm}", "%H:%M").time())
                exec_time = deadline_dt - timedelta(minutes=NOTIFY_BEFORE_MIN)

                # 範囲フィルタ
                if from_dt and deadline_dt < from_dt:
                    continue
                if until_dt and deadline_dt > until_dt:
                    continue

                schedule.append((exec_time, place_no, race_no, venue, deadline_str))
                venue_count += 1

            if venue_count > 0:
                print(f"  {venue}: {venue_count}R")

        except Exception as e:
            print(f"  {venue}: SKIP ({e})")

    schedule.sort(key=lambda x: x[0])
    return schedule


# ============================================================
# 1レースの予想実行
# ============================================================

def process_race(scraper: BoatraceScraper, date_str: str,
                 place_no: int, race_no: int, venue: str,
                 deadline_str: str, dry_run: bool = False,
                 gensen: bool = False) -> bool:
    if already_logged(date_str, place_no, race_no):
        print(f"  → 既にログ済み SKIP")
        return False

    try:
        race = scraper.scrape_full_race(place_no, race_no, date_str)
        if not race or not race.racers:
            print(f"  → データ取得失敗")
            return False

        odds = scraper.scrape_odds(place_no, race_no, date_str)
        race.trifecta_odds = odds

        # v6ハイブリッド予測
        pred = predict_race_v6(race)

        # ルックフィルタ: v6フィルタ + ガチガチ除外 (strategy.py)
        is_look, look_reason = should_look_v6(pred, race)
        if not is_look:
            # ガチガチは常にスキップ (strategy.py の should_look)
            is_look_v4, look_reason_v4 = should_look(pred, race)
            if is_look_v4 and "ガチガチ" in look_reason_v4:
                is_look = True
                look_reason = look_reason_v4

        if is_look:
            bets_data = []
        else:
            # 確率Top5配分 (v6確率分布ベース)
            # 4日間OOSテスト: 確率Top5 ROI 99% vs EV Top5 ROI 64%
            prob_bets = allocate_portfolio(pred, budget=BUDGET_PER_RACE, max_bets=5)
            if prob_bets:
                bets_data = [{"combo": b.combo, "odds": b.odds,
                              "prob": b.prob, "ev": round(b.ev, 4) if b.ev else 0,
                              "amount": b.bet_amount}
                             for b in prob_bets]
            else:
                bets_data = []
                is_look = True
                look_reason = "確率買い目なし"

        # データ蓄積 (v6メタデータ含む)
        scores = pred["scores"]
        sorted_scores = sorted(scores.values(), reverse=True)
        gap = sorted_scores[0] - sorted_scores[1] if len(sorted_scores) >= 2 else 0
        race_record = {
            "place_no": place_no, "venue": venue, "race_no": race_no,
            "deadline": deadline_str,
            "race_type": pred["race_type_tag"],
            "is_look": is_look, "look_reason": look_reason,
            "score_gap": round(gap, 3),
            "tenkai_max": round(pred.get("tenkai_max", 0), 3),
            "agreement": round(pred.get("agreement", 0), 3),
            "bets": bets_data,
            "top5_prob": [{"combo": c, "prob": round(p, 5)}
                          for c, p, _ in pred["top_combos"][:5]],
            "racers": [{"waku": r.waku, "name": r.name, "grade": r.grade,
                         "score": round(r.ev_score, 3)}
                        for r in race.racers],
            "timestamp": datetime.now().isoformat(),
        }
        save_race_data(date_str, race_record)

        log_bet(date_str, place_no, race_no, venue,
                pred["race_type_tag"], bets_data, is_look, look_reason)

        tag = pred["race_type_tag"]
        tk = pred.get("tenkai_max", 0)
        ag = pred.get("agreement", 0)
        if is_look:
            print(f"  → 👀 LOOK ({look_reason})")
            if not dry_run:
                send_prediction(venue, race_no, tag, bets_data,
                                deadline_str, is_look=True,
                                look_reason=look_reason)
        else:
            total = sum(b["amount"] for b in bets_data)
            badge = "🎯" if gensen else "🏁"
            print(f"  → {badge} [{tag}] {len(bets_data)}点 {total}円 "
                  f"tk={tk:.2f} ag={ag:.2f}")
            if not dry_run:
                send_prediction(venue, race_no, tag, bets_data,
                                deadline_str, gensen=gensen)

        return True

    except Exception as e:
        print(f"  → ERROR: {e}")
        return False


# ============================================================
# 厳選モード: 2パス方式
# ============================================================

def run_gensen_mode(date_str: str = None, dry_run: bool = False,
                    from_time: str = None, until_time: str = None):
    """
    厳選モード: 全レースをスキャンし、フィルター通過の上位N件のみに集中投資。

    Pass 1: 全レースの予測を実行 → 厳選フィルターで候補を絞る
    Pass 2: 候補を優先度順にソート → 上位5Rの締切を待って順に実行
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y%m%d")

    scraper = BoatraceScraper()
    today = datetime.now().date()

    # 時刻範囲
    from_dt = None
    until_dt = None
    if from_time:
        h, m = from_time.split(":")
        from_dt = datetime.combine(today, datetime.strptime(f"{h}:{m}", "%H:%M").time())
    if until_time:
        h, m = until_time.split(":")
        until_dt = datetime.combine(today, datetime.strptime(f"{h}:{m}", "%H:%M").time())

    # === 対策1: 開催場のみに絞る ===
    print(f"\n{'='*55}")
    print(f"  🎯 厳選モード: 開催場チェック中...")
    print(f"{'='*55}")

    # まず scrape_today_schedule を試す
    schedule = scraper.scrape_today_schedule(date_str)
    active_venues_sched = [v for v in schedule.venues if v.status == "開催中"]

    # race_list も全場分取得 (対策2+3で後で使う + 開催判定のフォールバック)
    venue_race_lists = {}  # place_no -> race_info_list
    for place_no in range(1, 25):
        venue = VENUE_MAP.get(place_no, f"?{place_no}")
        try:
            race_info = scraper.scrape_race_list(place_no, date_str)
            if race_info:
                venue_race_lists[place_no] = race_info
        except Exception:
            pass

    # 開催場決定: schedule が使えればそれ、なければ race_list の有無で判断
    if active_venues_sched:
        active_place_nos = [v.place_no for v in active_venues_sched]
        print(f"  スケジュールAPI: {len(active_venues_sched)}場 開催中")
    else:
        active_place_nos = list(venue_race_lists.keys())
        print(f"  フォールバック: race_list から{len(active_place_nos)}場を検出")

    if not active_place_nos:
        print("開催中の場がありません")
        return

    venue_names = [VENUE_MAP.get(p, f"?{p}") for p in active_place_nos]
    print(f"  対象: {', '.join(venue_names)}")

    # === Pass 1: 開催場のみスキャン ===
    print(f"\n{'='*55}")
    print(f"  🎯 厳選モード: {len(active_place_nos)}場スキャン中...")
    print(f"{'='*55}")

    candidates = []  # (priority, place_no, race_no, venue, deadline_str, deadline_dt)
    scanned = 0
    skipped_reasons = {}

    for place_no in active_place_nos:
        venue = VENUE_MAP.get(place_no, f"?{place_no}")
        try:
            # === 対策2+3: 事前取得済みの race_list で締切フィルタ + キャッシュ ===
            race_info_list = venue_race_lists.get(place_no)
            if not race_info_list:
                race_info_list = scraper.scrape_race_list(place_no, date_str)
            if not race_info_list:
                print(f"  {venue}: レース一覧なし SKIP")
                continue

            # 対象レースを締切時刻で事前フィルタ
            target_races = []
            for ri in race_info_list:
                deadline_str = ri.get("deadline", "")
                if not deadline_str or ":" not in deadline_str:
                    continue
                hh, mm = deadline_str.split(":")
                deadline_dt = datetime.combine(
                    today, datetime.strptime(f"{hh}:{mm}", "%H:%M").time())

                # 時刻範囲フィルタ (対策2: スクレイピング前にスキップ)
                if from_dt and deadline_dt < from_dt:
                    continue
                if until_dt and deadline_dt > until_dt:
                    continue
                # まだ締切前かチェック
                if deadline_dt < datetime.now() - timedelta(minutes=3):
                    continue

                target_races.append((ri["race_no"], deadline_str, deadline_dt))

            if not target_races:
                print(f"  {venue}: 対象レースなし (時刻範囲外)")
                continue

            print(f"  {venue}: {len(target_races)}R 対象 "
                  f"(全{len(race_info_list)}R中)")

            venue_count = 0
            for race_no, deadline_str, deadline_dt in target_races:
                # 対策3: キャッシュした race_info_list を渡す
                race = scraper.scrape_full_race(
                    place_no, race_no, date_str,
                    race_info_cache=race_info_list)
                if not race or not race.racers:
                    continue

                scanned += 1

                # オッズ取得して予測
                odds = scraper.scrape_odds(place_no, race_no, date_str)
                race.trifecta_odds = odds

                pred = predict_race_v6(race)

                # v6ルックフィルタ
                is_look, look_reason = should_look_v6(pred, race)
                if not is_look and pred.get("race_type_tag") == "ガチガチ":
                    is_look = True
                    look_reason = "ガチガチ(控除率負け)"

                if not is_look:
                    tk = pred.get("tenkai_max", 0)
                    ag = pred.get("agreement", 0)
                    # 優先度 = 低合意ほど高い (ag<0.5がROI 140%)
                    # 1.0 - ag でスコア化 (ag=0.1 → 0.9, ag=0.4 → 0.6)
                    priority = 1.0 - ag
                    candidates.append((
                        priority, place_no, race_no, venue,
                        deadline_str, deadline_dt
                    ))
                    print(f"  ✅ {venue} {race_no}R [{pred['race_type_tag']}] "
                          f"tk={tk:.2f} ag={ag:.2f} (締切{deadline_str})")
                    venue_count += 1
                else:
                    key = look_reason.split("(")[0] if "(" in look_reason else look_reason
                    skipped_reasons[key] = skipped_reasons.get(key, 0) + 1

            if venue_count > 0:
                print(f"  → {venue}: {venue_count}R 候補")

        except Exception as e:
            print(f"  {venue}: SKIP ({e})")

    # スキップ理由サマリー
    print(f"\nスキャン: {scanned}R → 候補: {len(candidates)}R")
    if skipped_reasons:
        print("除外理由:")
        for reason, count in sorted(skipped_reasons.items(), key=lambda x: -x[1]):
            print(f"  {reason}: {count}R")

    if not candidates:
        print("\n🎯 厳選対象レースなし")
        return

    # === Pass 2: 優先度でソート → 上位N件を時刻順に実行 ===
    candidates.sort(key=lambda x: x[0], reverse=True)  # 優先度降順
    selected = candidates[:GENSEN_MAX_RACES]
    selected.sort(key=lambda x: x[5])  # 締切時刻順に並べ替え

    bet_per_race = V6_BUDGET
    # 日次予算上限チェック
    max_possible = len(selected) * bet_per_race
    if max_possible > DAILY_BUDGET_MAX:
        # 予算内に収まるようレース数を制限
        max_races = DAILY_BUDGET_MAX // bet_per_race
        selected = selected[:max_races]

    print(f"\n{'='*55}")
    print(f"  🎯 v6厳選 {len(selected)}R (確率Top5 {bet_per_race}円/R)")
    print(f"  フィルタ: tk≥{LOOK_TENKAI_MIN} ag≥{LOOK_AGREE_MIN}")
    print(f"  日次予算上限: {DAILY_BUDGET_MAX:,}円")
    print(f"{'='*55}")
    for pri, pn, rn, v, dl, dt in selected:
        print(f"  {v} {rn}R 優先度={pri:.3f} (締切{dl})")

    # 実行 (安全なタイムアウト制御付き)
    processed = 0
    daily_spent = 0
    for _, place_no, race_no, venue, deadline_str, deadline_dt in selected:
        # 日次予算チェック
        if daily_spent + bet_per_race > DAILY_BUDGET_MAX:
            print(f"\n💰 日次予算上限 ({DAILY_BUDGET_MAX:,}円) に到達")
            break
        # 安全策1: --until を過ぎたら終了
        if until_dt and datetime.now() > until_dt:
            print(f"\n⏰ 終了時刻 ({until_time}) を過ぎたため終了")
            break

        exec_time = deadline_dt - timedelta(minutes=NOTIFY_BEFORE_MIN)
        now = datetime.now()
        wait_sec = (exec_time - now).total_seconds()

        # 安全策2: 既に締切を3分以上過ぎたレースはスキップ
        if (deadline_dt - now).total_seconds() < -180:
            print(f"  {venue} {race_no}R: 締切済み SKIP")
            continue

        # 安全策3: 待機時間が5時間超なら異常としてスキップ
        if wait_sec > 18000:
            print(f"  {venue} {race_no}R: 待機{wait_sec/60:.0f}分は異常 SKIP")
            continue

        if wait_sec > 0:
            print(f"\n⏳ 次: {venue} {race_no}R (締切{deadline_str}) "
                  f"→ {exec_time.strftime('%H:%M')}に実行 "
                  f"(あと{wait_sec/60:.0f}分)")
            time.sleep(max(0, wait_sec))

        print(f"\n🎯 {venue} {race_no}R (締切{deadline_str})")
        ok = process_race(scraper, date_str, place_no, race_no,
                          venue, deadline_str, dry_run, gensen=True)
        if ok:
            processed += 1
            daily_spent += bet_per_race
        time.sleep(2)

    print(f"\n{'='*55}")
    print(f"  🎯 v6厳選完了: {processed}/{len(selected)}R処理")
    print(f"  投資額: {processed * bet_per_race:,}円")
    print(f"{'='*55}")


# ============================================================
# メイン: 締切時刻ベースのスケジューラ
# ============================================================

def run_scheduler(date_str: str = None, dry_run: bool = False,
                  from_time: str = None, until_time: str = None):
    if date_str is None:
        date_str = datetime.now().strftime("%Y%m%d")

    scraper = BoatraceScraper()

    schedule = build_schedule(scraper, date_str, from_time, until_time)
    if not schedule:
        print("対象レースなし")
        return

    now = datetime.now()
    future = [(t, p, r, v, d) for t, p, r, v, d in schedule
              if t > now - timedelta(minutes=3)]
    past_count = len(schedule) - len(future)

    print(f"\n{'='*55}")
    print(f"  スケジュール: {len(schedule)}R (未処理{len(future)}R, 済{past_count}R)")
    if from_time:
        print(f"  開始: {from_time}以降")
    if until_time:
        print(f"  終了: {until_time}まで")
    if future:
        print(f"  最初: {future[0][3]} {future[0][2]}R (締切{future[0][4]})")
        print(f"  最後: {future[-1][3]} {future[-1][2]}R (締切{future[-1][4]})")
    print(f"{'='*55}\n")

    processed = 0
    for exec_time, place_no, race_no, venue, deadline_str in future:
        now = datetime.now()
        wait_sec = (exec_time - now).total_seconds()

        # 安全策: 既に締切を3分以上過ぎたレースはスキップ
        if wait_sec < -180:
            continue

        # 安全策: 待機時間が60分超なら異常としてスキップ
        if wait_sec > 3600:
            print(f"  {venue} {race_no}R: 待機{wait_sec/60:.0f}分は異常 SKIP")
            continue

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
        time.sleep(2)

    print(f"\n{'='*55}")
    print(f"  完了: {processed}R処理")
    print(f"{'='*55}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None, help="日付 (yyyymmdd)")
    parser.add_argument("--dry-run", action="store_true", help="Discord通知しない")
    parser.add_argument("--from", dest="from_time", default=None,
                        help="開始時刻 (HH:MM) 例: 13:00")
    parser.add_argument("--until", default=None,
                        help="終了時刻 (HH:MM) 例: 13:30")
    parser.add_argument("--gensen", action="store_true",
                        help="厳選モード (1日3-5Rに絞り込み)")
    args = parser.parse_args()

    if args.gensen:
        run_gensen_mode(args.date, args.dry_run, args.from_time, args.until)
    else:
        run_scheduler(args.date, args.dry_run, args.from_time, args.until)
