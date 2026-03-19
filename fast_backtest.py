# -*- coding: utf-8 -*-
"""
高速オフラインバックテスト: 生データから予測→中穴EV評価を瞬時に実行

使い方:
  python fast_backtest.py              # 全日分
  python fast_backtest.py 20260317     # 特定日のみ
"""
import sys, io, json, os, glob, time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.path.insert(0, r'c:\money plus\boatrace_ai')

from engine import predict_race
from strategy import (allocate_portfolio, should_look,
                      chuuana_filter, _allocate_by_ev, BetCandidate,
                      CHUUANA_ODDS_MIN, CHUUANA_ODDS_MAX, CHUUANA_BUDGET)
from models import Racer, Race

RAW_DIR = r"c:\money plus\boatrace_ai\data\raw"


def raw_to_race(raw: dict) -> Race:
    """生データJSON → Raceオブジェクトに変換"""
    racers = []
    for rd in raw["racers"]:
        # asdictで保存されたデータなので、そのままRacerに渡せる
        r = Racer(**rd)
        racers.append(r)

    # trifecta_oddsのキーがstr、値がint/floatであることを確認
    odds = raw.get("trifecta_odds", {})

    race = Race(
        place_no=raw["place_no"],
        race_no=raw["race_no"],
        date=raw.get("date", ""),
        racers=racers,
        deadline=raw.get("deadline", ""),
        trifecta_odds=odds,
        result_order=raw.get("result_order"),
        result_kimarite=raw.get("result_kimarite", ""),
        result_payout=raw.get("result_payout", 0),
        seibi_list=raw.get("seibi_list", []),
        flying_info=raw.get("flying_info", []),
    )
    return race


def backtest_day(raw_races: list, date_str: str):
    """1日分の高速バックテスト"""
    results = []

    for raw in raw_races:
        race = raw_to_race(raw)
        venue = raw["venue"]
        race_no = raw["race_no"]

        # 予測
        pred = predict_race(race)
        actual = f"{race.result_order[0]}-{race.result_order[1]}-{race.result_order[2]}"
        actual_prob = pred["probs"].get(actual, 0)

        # 確率順位
        sorted_by_prob = sorted(pred["probs"].items(), key=lambda x: x[1], reverse=True)
        prob_rank = next((i+1 for i, (c, _) in enumerate(sorted_by_prob) if c == actual), 999)

        tag = pred["race_type_tag"]

        # 中穴EVフィルター
        ch_passed, ch_best_ev, ch_combos, ch_reason = chuuana_filter(pred, race)

        # 中穴的中判定
        ch_hit = None
        ch_bet = 0
        ch_pay = 0
        if ch_passed:
            ch_bet = sum(c.bet_amount for c in ch_combos)
            ch_hit = next((c for c in ch_combos if c.combo == actual), None)
            if ch_hit:
                ch_pay = int(race.result_payout * ch_hit.bet_amount / 100)

        results.append({
            "venue": venue, "race_no": race_no,
            "actual": actual, "kimarite": race.result_kimarite,
            "payout_odds": race.result_payout,
            "payout_bai": race.result_payout / 100,
            "prob_rank": prob_rank, "actual_prob": actual_prob,
            "race_type": tag,
            "chuuana_passed": ch_passed,
            "chuuana_best_ev": ch_best_ev,
            "chuuana_reason": ch_reason,
            "chuuana_bet": ch_bet,
            "chuuana_pay": ch_pay,
            "chuuana_hit": ch_hit is not None,
            "chuuana_combos": [(c.combo, c.odds/100, round(c.ev,2), c.bet_amount) for c in ch_combos] if ch_passed else [],
        })

    return results


def print_day_summary(date_str, results):
    """1日分のサマリー表示"""
    total = len(results)
    # 中穴候補
    cands = [r for r in results if r["chuuana_passed"]]
    hits = [r for r in cands if r["chuuana_hit"]]
    c_bet = sum(r["chuuana_bet"] for r in cands)
    c_pay = sum(r["chuuana_pay"] for r in cands)
    c_roi = c_pay / c_bet * 100 if c_bet > 0 else 0

    # Top5
    cands_sorted = sorted(cands, key=lambda x: x["chuuana_best_ev"], reverse=True)
    top5 = cands_sorted[:5]
    t5_bet = sum(r["chuuana_bet"] for r in top5)
    t5_pay = sum(r["chuuana_pay"] for r in top5)
    t5_hits = sum(1 for r in top5 if r["chuuana_hit"])
    t5_roi = t5_pay / t5_bet * 100 if t5_bet > 0 else 0

    print(f"  {date_str}: {total:>3}R | "
          f"候補{len(cands):>2}R 的中{len(hits)} ROI:{c_roi:>5.0f}% ({c_pay-c_bet:+,}円) | "
          f"Top5: {t5_hits}/5 ROI:{t5_roi:>5.0f}% ({t5_pay-t5_bet:+,}円)")

    return {
        "date": date_str, "total_races": total,
        "chuuana_count": len(cands), "chuuana_hits": len(hits),
        "chuuana_bet": c_bet, "chuuana_pay": c_pay, "chuuana_roi": c_roi,
        "top5_hits": t5_hits, "top5_bet": t5_bet, "top5_pay": t5_pay, "top5_roi": t5_roi,
        "results": results,  # 詳細
    }


def main():
    # 日付フィルタ
    target_date = sys.argv[1] if len(sys.argv) > 1 else None

    # 生データ読み込み
    files = sorted(glob.glob(os.path.join(RAW_DIR, "*.json")))
    if target_date:
        files = [f for f in files if target_date in f]

    if not files:
        print(f"生データなし: {RAW_DIR}")
        print("先に python collect_raw.py を実行してください")
        return

    print("=" * 75)
    print("  🎯 高速バックテスト (中穴EV狙い)")
    print(f"  レンジ: {CHUUANA_ODDS_MIN:.0f}〜{CHUUANA_ODDS_MAX:.0f}倍 / 5点EV配分 / {CHUUANA_BUDGET}円")
    print("=" * 75)

    all_summaries = []
    start = time.time()

    for fpath in files:
        date_str = os.path.basename(fpath).replace(".json", "")
        raw_races = json.load(open(fpath, encoding="utf-8"))

        results = backtest_day(raw_races, date_str)
        summary = print_day_summary(date_str, results)
        all_summaries.append(summary)

    elapsed = time.time() - start

    # 全日合計
    print(f"\n{'='*75}")
    print(f"  📊 {len(all_summaries)}日間合計 ({elapsed:.1f}秒)")
    print(f"{'='*75}")

    g_races = sum(s["total_races"] for s in all_summaries)
    g_cands = sum(s["chuuana_count"] for s in all_summaries)
    g_hits = sum(s["chuuana_hits"] for s in all_summaries)
    g_bet = sum(s["chuuana_bet"] for s in all_summaries)
    g_pay = sum(s["chuuana_pay"] for s in all_summaries)
    g_roi = g_pay / g_bet * 100 if g_bet > 0 else 0

    t5_hits = sum(s["top5_hits"] for s in all_summaries)
    t5_bet = sum(s["top5_bet"] for s in all_summaries)
    t5_pay = sum(s["top5_pay"] for s in all_summaries)
    t5_roi = t5_pay / t5_bet * 100 if t5_bet > 0 else 0

    print(f"  全レース: {g_races}R")
    print(f"  中穴EV候補: {g_cands}R → 的中{g_hits}R ({g_hits/g_cands*100:.0f}%)" if g_cands else "")
    print(f"  全候補 ROI: {g_roi:.0f}% (投資{g_bet:,}円 → 回収{g_pay:,}円 / {g_pay-g_bet:+,}円)")
    print(f"  Top5/日 ROI: {t5_roi:.0f}% (投資{t5_bet:,}円 → 回収{t5_pay:,}円 / {t5_pay-t5_bet:+,}円)")
    print(f"  Top5 的中: {t5_hits}/{len(all_summaries)*5}")

    # 的中詳細
    print(f"\n  🎯 的中レース一覧:")
    for s in all_summaries:
        for r in s["results"]:
            if r["chuuana_hit"]:
                pnl = r["chuuana_pay"] - r["chuuana_bet"]
                combos_str = " / ".join(f"{c[0]}({c[1]:.0f}倍 {c[3]}円)" for c in r["chuuana_combos"][:3])
                print(f"    ✅ {s['date']} {r['venue']} {r['race_no']}R "
                      f"{r['actual']}({r['kimarite']}) {r['payout_bai']:.0f}倍 "
                      f"{pnl:+,}円")
                print(f"       {combos_str}")


if __name__ == "__main__":
    main()
