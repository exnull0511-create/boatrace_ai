# -*- coding: utf-8 -*-
"""
7日分バッチバックテスト: 全場全レースのデータを収集して保存
3/12 〜 3/18 の7日間

各日のデータを backtest_yyyymmdd_full.json として保存
"""
import sys
import io
import time
import json
import os

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# プロジェクトパス
sys.path.insert(0, r'c:\money plus\boatrace_ai')

from scraper import BoatraceScraper
from engine import predict_race
from strategy import allocate_portfolio, should_look, chuuana_filter
from config import VENUE_MAP


DATES = [
    "20260312",  # 木
    "20260313",  # 金
    "20260314",  # 土
    "20260315",  # 日
    "20260316",  # 月
    "20260317",  # 火
    "20260318",  # 水
]

BUDGET = 1000  # 通常モードの予算


def collect_one_day(scraper, hiduke):
    """1日分の全場全レースを収集"""
    all_results = []
    total_bet = 0
    total_payout = 0
    total_races = 0
    total_hits = 0
    type_stats = {}

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

        print(f"  🏁 {venue}", end="", flush=True)
        venue_results = 0

        for race_no in range(1, 13):
            try:
                if race_no == 1:
                    race = test_race
                else:
                    race = scraper.scrape_full_race(place_no, race_no, hiduke, include_result=True)

                if not race or not race.racers or not race.result_order:
                    continue

                odds = scraper.scrape_odds(place_no, race_no, hiduke)
                race.trifecta_odds = odds

                pred = predict_race(race)
                actual = f"{race.result_order[0]}-{race.result_order[1]}-{race.result_order[2]}"
                actual_prob = pred["probs"].get(actual, 0)

                sorted_by_prob = sorted(pred["probs"].items(), key=lambda x: x[1], reverse=True)
                prob_rank = next((i+1 for i, (c, _) in enumerate(sorted_by_prob) if c == actual), 999)

                tag = pred["race_type_tag"]
                is_look, look_reason = should_look(pred, race)

                # 中穴EVフィルター
                ch_passed, ch_best_ev, ch_combos, ch_reason = chuuana_filter(pred, race)

                # 通常ベッティング
                if is_look:
                    bets = []
                else:
                    bets = allocate_portfolio(pred, budget=BUDGET)

                race_bet = sum(b.bet_amount for b in bets)
                hit_bet = next((b for b in bets if b.combo == actual), None)
                payout = int(race.result_payout * hit_bet.bet_amount / 100) if hit_bet else 0

                total_bet += race_bet
                total_payout += payout
                total_races += 1
                if hit_bet: total_hits += 1

                if tag not in type_stats:
                    type_stats[tag] = {"count": 0, "hits": 0, "bet": 0, "payout": 0}
                type_stats[tag]["count"] += 1
                type_stats[tag]["hits"] += 1 if hit_bet else 0
                type_stats[tag]["bet"] += race_bet
                type_stats[tag]["payout"] += payout

                # 中穴EV買い目情報も保存
                ch_bets_info = []
                if ch_passed:
                    ch_bets_info = [{"combo": c.combo, "odds": c.odds, "ev": c.ev,
                                     "amount": c.bet_amount} for c in ch_combos]

                # EV上位10組を保存 (分析用)
                evs = pred.get("evs", {})
                sorted_evs = sorted(evs.items(), key=lambda x: x[1], reverse=True)[:10]
                top10_evs = [{"combo": c, "ev": round(e, 4),
                              "odds": odds.get(c, 0),
                              "prob": round(pred["probs"].get(c, 0), 6)}
                             for c, e in sorted_evs]

                result_entry = {
                    "venue": venue, "race_no": race_no, "place_no": place_no,
                    "actual": actual, "kimarite": race.result_kimarite,
                    "payout_odds": race.result_payout,
                    "prob_rank": prob_rank, "actual_prob": round(actual_prob, 6),
                    "bet_amount": race_bet, "win_amount": payout,
                    "hit": hit_bet is not None, "race_type": tag,
                    "num_bets": len(bets), "is_look": is_look,
                    "score_gap_12": round(sorted(pred["scores"].values(), reverse=True)[0] - sorted(pred["scores"].values(), reverse=True)[1], 3) if len(pred["scores"]) >= 2 else 0,
                    "score_top": round(max(pred["scores"].values()), 3) if pred["scores"] else 0,
                    "grade_1waku": race.racers[0].grade if race.racers else "",
                    "actual_head": int(actual.split("-")[0]),
                    "is_1head": actual.startswith("1-"),
                    # 中穴EV情報
                    "chuuana_passed": ch_passed,
                    "chuuana_best_ev": round(ch_best_ev, 4) if ch_best_ev else 0,
                    "chuuana_reason": ch_reason,
                    "chuuana_bets": ch_bets_info,
                    "top10_evs": top10_evs,
                }
                all_results.append(result_entry)
                venue_results += 1

            except Exception as e:
                pass

        if venue_results > 0:
            print(f" ({venue_results}R)", flush=True)

    roi = (total_payout / total_bet * 100) if total_bet > 0 else 0
    report = {
        "date": hiduke, "total_races": total_races,
        "total_bet": total_bet, "total_payout": total_payout,
        "roi_pct": round(roi, 1),
        "hit_rate": round(total_hits/total_races, 3) if total_races else 0,
        "type_stats": type_stats,
        "results": all_results,
    }
    return report


def main():
    scraper = BoatraceScraper()
    output_dir = r"c:\money plus\boatrace_ai"

    print("=" * 65)
    print("  🎯 7日分バッチバックテスト開始")
    print(f"  対象: {DATES[0]} 〜 {DATES[-1]}")
    print("=" * 65)

    for hiduke in DATES:
        fname = os.path.join(output_dir, f"backtest_{hiduke}_full.json")

        # 既に存在するならスキップ
        if os.path.exists(fname):
            print(f"\n✅ {hiduke}: 既存データあり → SKIP")
            continue

        print(f"\n{'='*55}")
        print(f"  📅 {hiduke} 収集開始")
        print(f"{'='*55}")

        start = time.time()
        report = collect_one_day(scraper, hiduke)
        elapsed = time.time() - start

        # 保存
        with open(fname, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

        roi = report["roi_pct"]
        races = report["total_races"]
        print(f"\n  ✅ {hiduke} 完了: {races}R / ROI {roi}% / {elapsed:.0f}秒")
        print(f"     → {fname}")

    # 完了サマリー
    print(f"\n{'='*65}")
    print(f"  📊 全日サマリー")
    print(f"{'='*65}")

    for hiduke in DATES:
        fname = os.path.join(output_dir, f"backtest_{hiduke}_full.json")
        if os.path.exists(fname):
            d = json.load(open(fname, encoding='utf-8'))
            print(f"  {hiduke}: {d['total_races']:>3}R / ROI {d['roi_pct']:>5.1f}% / "
                  f"収支 {d['total_payout']-d['total_bet']:+,}円")
        else:
            print(f"  {hiduke}: データなし")

    print(f"\n✅ 全完了!")


if __name__ == "__main__":
    main()
