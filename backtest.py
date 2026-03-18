# -*- coding: utf-8 -*-
"""
全場全レース 5点予想バックテスト

本日開催の全場を自動検出し、全完了レースで検証する。
場ごとにCSRFを取得し直し、取得できない場はスキップする。
"""
import sys
import io
import time
import json

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from scraper import BoatraceScraper
from engine import predict_race
from strategy import allocate_portfolio, should_look
from config import VENUE_MAP


def run_full_backtest(hiduke: str, budget: int = 1000):
    scraper = BoatraceScraper()

    # 全24場をスキャンして開催場を検出
    print(f"本日 ({hiduke}) の全場バックテスト")
    print(f"5点予想 / 確率Top5配分 / {budget}円")
    print("=" * 60)

    all_results = []
    total_bet = 0
    total_payout = 0
    total_races = 0
    total_hits = 0
    prob_top1_hits = 0
    prob_top3_hits = 0
    prob_top5_hits = 0
    type_stats = {}

    for place_no in range(1, 25):
        venue = VENUE_MAP.get(place_no, f"?{place_no}")

        # 1Rでテスト取得
        test_race = scraper.scrape_full_race(place_no, 1, hiduke, include_result=True)
        if not test_race or not test_race.racers:
            continue  # この場は開催なし

        if not test_race.result_order:
            print(f"\n  {venue}: まだ結果なし → SKIP")
            continue

        print(f"\n{'='*55}")
        print(f"  {venue}")
        print(f"{'='*55}")

        for race_no in range(1, 13):
            try:
                if race_no == 1:
                    race = test_race  # 既に取得済み
                else:
                    race = scraper.scrape_full_race(place_no, race_no, hiduke, include_result=True)

                if not race or not race.racers or not race.result_order:
                    continue

                # オッズ取得
                odds = scraper.scrape_odds(place_no, race_no, hiduke)
                race.trifecta_odds = odds

                # 予測
                pred = predict_race(race)
                actual = f"{race.result_order[0]}-{race.result_order[1]}-{race.result_order[2]}"
                actual_prob = pred["probs"].get(actual, 0)

                # 確率順位
                sorted_by_prob = sorted(pred["probs"].items(), key=lambda x: x[1], reverse=True)
                prob_rank = next((i+1 for i, (c, _) in enumerate(sorted_by_prob) if c == actual), 999)
                if prob_rank == 1: prob_top1_hits += 1
                if prob_rank <= 3: prob_top3_hits += 1
                if prob_rank <= 5: prob_top5_hits += 1

                # ルック判定
                tag = pred["race_type_tag"]
                is_look, look_reason = should_look(pred, race)

                # ベッティング
                if is_look:
                    bets = []
                else:
                    bets = allocate_portfolio(pred, budget=budget)
                race_bet = sum(b.bet_amount for b in bets)
                hit_bet = next((b for b in bets if b.combo == actual), None)
                payout = int(race.result_payout * hit_bet.bet_amount / 100) if hit_bet else 0

                total_bet += race_bet
                total_payout += payout
                total_races += 1
                if hit_bet: total_hits += 1

                # タイプ別
                tag = pred["race_type_tag"]
                if tag not in type_stats:
                    type_stats[tag] = {"count": 0, "hits": 0, "bet": 0, "payout": 0}
                type_stats[tag]["count"] += 1
                type_stats[tag]["hits"] += 1 if hit_bet else 0
                type_stats[tag]["bet"] += race_bet
                type_stats[tag]["payout"] += payout

                result_entry = {
                    "venue": venue, "race_no": race_no,
                    "actual": actual, "kimarite": race.result_kimarite,
                    "payout_odds": race.result_payout,
                    "prob_rank": prob_rank, "actual_prob": actual_prob,
                    "bet_amount": race_bet, "win_amount": payout,
                    "hit": hit_bet is not None, "race_type": tag,
                    "num_bets": len(bets),
                    # --- ルック判定用特徴量 ---
                    "score_gap_12": sorted(pred["scores"].values(), reverse=True)[0] - sorted(pred["scores"].values(), reverse=True)[1] if len(pred["scores"]) >= 2 else 0,
                    "score_top": max(pred["scores"].values()) if pred["scores"] else 0,
                    "score_1waku": pred["scores"].get(1, 0),
                    "nige_1waku": race.racers[0].course_nigeritsu if race.racers else 0,
                    "grade_1waku": race.racers[0].grade if race.racers else "",
                    "actual_head": int(actual.split("-")[0]),
                    "is_1head": actual.startswith("1-"),
                    "display_diff_max": max(r.tenji_time for r in race.racers if r.tenji_time > 0) - min(r.tenji_time for r in race.racers if r.tenji_time > 0) if any(r.tenji_time > 0 for r in race.racers) else 0,
                }
                all_results.append(result_entry)

                hit_mark = "✅" if hit_bet else "❌"
                if is_look:
                    hit_mark = "👀"
                pnl = payout - race_bet
                look_str = f" [LOOK:{look_reason}]" if is_look else ""
                print(f"  {race_no:>2}R [{tag}] {actual}({race.result_kimarite:<4}) "
                      f"P#{prob_rank:>3} {hit_mark} {pnl:+,}円{look_str}")

            except Exception as e:
                print(f"  {race_no:>2}R: ERROR {e}")

    # === サマリー ===
    print(f"\n{'='*60}")
    print(f"  全場バックテスト結果")
    print(f"{'='*60}")
    print(f"日付: {hiduke} / {total_races}レース")
    roi = (total_payout / total_bet * 100) if total_bet > 0 else 0
    print(f"\n■ ベッティング成績 (5点予想)")
    print(f"  投資額:    {total_bet:,}円")
    print(f"  回収額:    {total_payout:,}円")
    print(f"  ROI:       {roi:.1f}%")
    print(f"  収支:      {total_payout-total_bet:+,}円")
    print(f"  的中率:    {total_hits}/{total_races} ({total_hits/total_races*100:.1f}%)" if total_races else "")

    print(f"\n■ 確率モデル精度")
    if total_races:
        print(f"  Top1的中: {prob_top1_hits}/{total_races} ({prob_top1_hits/total_races*100:.1f}%)")
        print(f"  Top3的中: {prob_top3_hits}/{total_races} ({prob_top3_hits/total_races*100:.1f}%)")
        print(f"  Top5的中: {prob_top5_hits}/{total_races} ({prob_top5_hits/total_races*100:.1f}%)")

    print(f"\n■ レースタイプ別")
    for tag in sorted(type_stats.keys()):
        s = type_stats[tag]
        r = (s["payout"]/s["bet"]*100) if s["bet"] > 0 else 0
        hit_pct = s["hits"]/s["count"]*100 if s["count"] > 0 else 0
        print(f"  {tag:6}: {s['count']:>3}R 的中{s['hits']:>3}({hit_pct:>4.0f}%) "
              f"ROI={r:>6.1f}% ({s['payout']-s['bet']:+,}円)")

    # JSON保存
    report = {
        "date": hiduke, "total_races": total_races,
        "total_bet": total_bet, "total_payout": total_payout,
        "roi_pct": roi,
        "hit_rate": total_hits/total_races if total_races else 0,
        "prob_top1_rate": prob_top1_hits/total_races if total_races else 0,
        "prob_top3_rate": prob_top3_hits/total_races if total_races else 0,
        "prob_top5_rate": prob_top5_hits/total_races if total_races else 0,
        "type_stats": type_stats,
        "results": all_results,
    }
    fname = f"backtest_{hiduke}_full.json"
    with open(fname, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n保存: {fname}")
    return report


if __name__ == "__main__":
    run_full_backtest("20260317", budget=1000)
