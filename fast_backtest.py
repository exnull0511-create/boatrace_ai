# -*- coding: utf-8 -*-
"""
高速オフラインバックテスト: 生データから予測→中穴EV評価を瞬時に実行

使い方:
  python fast_backtest.py              # 全日分 (v4+chuuana)
  python fast_backtest.py 20260317     # 特定日のみ
  python fast_backtest.py --v6         # v6ハイブリッドモード
  python fast_backtest.py --v6 --compare  # v4 vs v6 比較
"""
import sys, io, json, os, glob, time, argparse

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.path.insert(0, r'c:\money plus\boatrace_ai')

from engine import predict_race
from strategy import (allocate_portfolio, should_look,
                      chuuana_filter, _allocate_by_ev, BetCandidate,
                      CHUUANA_ODDS_MIN, CHUUANA_ODDS_MAX, CHUUANA_BUDGET)
from engine_v6 import (predict_race_v6, should_look_v6, select_ev_top_bets,
                        V6_EV_MIN, V6_PROB_MIN, V6_ODDS_MAX_BAI,
                        LOOK_TENKAI_MIN, LOOK_AGREE_MIN)
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


def backtest_day_v6(raw_races: list, date_str: str):
    """1日分の高速バックテスト (v6ハイブリッドモード)"""
    results = []

    for raw in raw_races:
        race = raw_to_race(raw)
        venue = raw["venue"]
        race_no = raw["race_no"]

        # v6予測
        pred = predict_race_v6(race)
        actual = f"{race.result_order[0]}-{race.result_order[1]}-{race.result_order[2]}"

        # 確率順位
        sorted_by_prob = sorted(pred["probs"].items(), key=lambda x: x[1], reverse=True)
        prob_rank = next((i+1 for i, (c, _) in enumerate(sorted_by_prob) if c == actual), 999)

        tag = pred["race_type_tag"]
        agreement = pred.get("agreement", 0)
        tenkai_max = pred.get("tenkai_max", 0)

        # v6ルックフィルタ
        is_look, look_reason = should_look_v6(pred, race)

        # v6 EV Top5 (ガードレール付き)
        v6_bets = []
        v6_bet = 0
        v6_pay = 0
        v6_hit = None
        if not is_look:
            v6_bets = select_ev_top_bets(pred, race)
            if v6_bets:
                v6_bet = sum(c.bet_amount for c in v6_bets)
                v6_hit = next((c for c in v6_bets if c.combo == actual), None)
                if v6_hit:
                    v6_pay = int(race.result_payout * v6_hit.bet_amount / 100)

        results.append({
            "venue": venue, "race_no": race_no,
            "actual": actual, "kimarite": race.result_kimarite,
            "payout_odds": race.result_payout,
            "payout_bai": race.result_payout / 100,
            "prob_rank": prob_rank,
            "race_type": tag,
            "agreement": round(agreement, 3),
            "tenkai_max": round(tenkai_max, 3),
            "is_look": is_look, "look_reason": look_reason,
            "v6_bet": v6_bet, "v6_pay": v6_pay,
            "v6_hit": v6_hit is not None,
            "v6_combos": [(c.combo, c.odds/100, round(c.ev, 2), c.bet_amount)
                          for c in v6_bets],
        })

    return results


def print_day_summary_v6(date_str, results):
    """1日分のv6サマリー表示"""
    total = len(results)
    # ルックフィルタ通過
    passed = [r for r in results if not r["is_look"]]
    # ベットあり (ガードレール通過)
    bet_races = [r for r in passed if r["v6_bet"] > 0]
    hits = [r for r in bet_races if r["v6_hit"]]
    total_bet = sum(r["v6_bet"] for r in bet_races)
    total_pay = sum(r["v6_pay"] for r in bet_races)
    roi = total_pay / total_bet * 100 if total_bet > 0 else 0

    # ガードレールで弾かれたレース
    guardrail_blocked = len(passed) - len(bet_races)

    print(f"  {date_str}: {total:>3}R | "
          f"フィルタ通過{len(passed):>2}R ベット{len(bet_races):>2}R "
          f"(GR除外{guardrail_blocked}) "
          f"的中{len(hits)} ROI:{roi:>5.0f}% ({total_pay-total_bet:+,}円)")

    return {
        "date": date_str, "total_races": total,
        "passed": len(passed), "bet_races": len(bet_races),
        "guardrail_blocked": guardrail_blocked,
        "hits": len(hits),
        "total_bet": total_bet, "total_pay": total_pay, "roi": roi,
        "results": results,
    }


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
    parser = argparse.ArgumentParser(description="高速オフラインバックテスト")
    parser.add_argument("date", nargs="?", default=None, help="日付 (yyyymmdd)")
    parser.add_argument("--v6", action="store_true", help="v6ハイブリッドモードで実行")
    parser.add_argument("--compare", action="store_true", help="v4 vs v6 比較")
    args = parser.parse_args()

    target_date = args.date

    # 生データ読み込み
    files = sorted(glob.glob(os.path.join(RAW_DIR, "*.json")))
    if target_date:
        files = [f for f in files if target_date in f]

    if not files:
        print(f"生データなし: {RAW_DIR}")
        print("先に python collect_raw.py を実行してください")
        return

    if args.compare:
        _run_compare(files)
    elif args.v6:
        _run_v6(files)
    else:
        _run_v4(files)


def _run_v4(files):
    """従来のv4+chuuanaバックテスト"""
    print("=" * 75)
    print("  🎯 高速バックテスト (v4 + 中穴EV狙い)")
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

    print(f"\n{'='*75}")
    print(f"  📊 v4 {len(all_summaries)}日間合計 ({elapsed:.1f}秒)")
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
    if g_cands:
        print(f"  中穴EV候補: {g_cands}R → 的中{g_hits}R ({g_hits/g_cands*100:.0f}%)")
    print(f"  全候補 ROI: {g_roi:.0f}% (投資{g_bet:,}円 → 回収{g_pay:,}円 / {g_pay-g_bet:+,}円)")
    print(f"  Top5/日 ROI: {t5_roi:.0f}% (投資{t5_bet:,}円 → 回収{t5_pay:,}円 / {t5_pay-t5_bet:+,}円)")
    print(f"  Top5 的中: {t5_hits}/{len(all_summaries)*5}")

    _print_v4_hits(all_summaries)


def _run_v6(files):
    """v6ハイブリッドバックテスト (ガードレール付き)"""
    print("=" * 75)
    print("  🎯 高速バックテスト (v6ハイブリッド + ガードレール)")
    print(f"  EV>={V6_EV_MIN} / 確率>={V6_PROB_MIN:.0%} / オッズ<={V6_ODDS_MAX_BAI:.0f}倍")
    print(f"  ルック: tk>={LOOK_TENKAI_MIN} ag>={LOOK_AGREE_MIN}")
    print("=" * 75)

    all_summaries = []
    start = time.time()

    for fpath in files:
        date_str = os.path.basename(fpath).replace(".json", "")
        raw_races = json.load(open(fpath, encoding="utf-8"))
        results = backtest_day_v6(raw_races, date_str)
        summary = print_day_summary_v6(date_str, results)
        all_summaries.append(summary)

    elapsed = time.time() - start

    print(f"\n{'='*75}")
    print(f"  📊 v6 {len(all_summaries)}日間合計 ({elapsed:.1f}秒)")
    print(f"{'='*75}")

    g_races = sum(s["total_races"] for s in all_summaries)
    g_passed = sum(s["passed"] for s in all_summaries)
    g_bet_races = sum(s["bet_races"] for s in all_summaries)
    g_gr_blocked = sum(s["guardrail_blocked"] for s in all_summaries)
    g_hits = sum(s["hits"] for s in all_summaries)
    g_bet = sum(s["total_bet"] for s in all_summaries)
    g_pay = sum(s["total_pay"] for s in all_summaries)
    g_roi = g_pay / g_bet * 100 if g_bet > 0 else 0

    print(f"  全レース: {g_races}R")
    print(f"  ルック通過: {g_passed}R → ベット: {g_bet_races}R (GR除外: {g_gr_blocked}R)")
    if g_bet_races:
        print(f"  的中: {g_hits}R ({g_hits/g_bet_races*100:.1f}%)")
    print(f"  ROI: {g_roi:.0f}% (投資{g_bet:,}円 → 回収{g_pay:,}円 / {g_pay-g_bet:+,}円)")

    # 的中詳細
    print(f"\n  🎯 的中レース一覧:")
    for s in all_summaries:
        for r in s["results"]:
            if r["v6_hit"]:
                pnl = r["v6_pay"] - r["v6_bet"]
                combos_str = " / ".join(f"{c[0]}({c[1]:.0f}倍 {c[3]}円)" for c in r["v6_combos"][:3])
                print(f"    ✅ {s['date']} {r['venue']} {r['race_no']}R "
                      f"{r['actual']}({r['kimarite']}) {r['payout_bai']:.0f}倍 "
                      f"{pnl:+,}円")
                print(f"       {combos_str}")


def _run_compare(files):
    """v4 vs v6 比較バックテスト"""
    print("=" * 75)
    print("  🔄 v4 vs v6 比較バックテスト")
    print("=" * 75)

    start = time.time()
    v4_summaries = []
    v6_summaries = []

    for fpath in files:
        date_str = os.path.basename(fpath).replace(".json", "")
        raw_races = json.load(open(fpath, encoding="utf-8"))

        # v4
        v4_results = backtest_day(raw_races, date_str)
        v4_summary = print_day_summary(date_str, v4_results)
        v4_summaries.append(v4_summary)

        # v6
        v6_results = backtest_day_v6(raw_races, date_str)
        v6_summary = print_day_summary_v6(date_str, v6_results)
        v6_summaries.append(v6_summary)

    elapsed = time.time() - start

    # 比較サマリー
    print(f"\n{'='*75}")
    print(f"  📊 比較結果 ({elapsed:.1f}秒)")
    print(f"{'='*75}")

    v4_bet = sum(s["chuuana_bet"] for s in v4_summaries)
    v4_pay = sum(s["chuuana_pay"] for s in v4_summaries)
    v4_hits = sum(s["chuuana_hits"] for s in v4_summaries)
    v4_cands = sum(s["chuuana_count"] for s in v4_summaries)
    v4_roi = v4_pay / v4_bet * 100 if v4_bet > 0 else 0

    v6_bet = sum(s["total_bet"] for s in v6_summaries)
    v6_pay = sum(s["total_pay"] for s in v6_summaries)
    v6_hits = sum(s["hits"] for s in v6_summaries)
    v6_races = sum(s["bet_races"] for s in v6_summaries)
    v6_roi = v6_pay / v6_bet * 100 if v6_bet > 0 else 0

    print(f"  {'':12} {'v4+chuuana':>14} {'v6+GR':>14}")
    print(f"  {'ベット数':12} {v4_cands:>12}R {v6_races:>12}R")
    print(f"  {'的中':12} {v4_hits:>12} {v6_hits:>12}")
    if v4_cands:
        print(f"  {'的中率':12} {v4_hits/v4_cands*100:>11.1f}% {v6_hits/v6_races*100 if v6_races else 0:>11.1f}%")
    print(f"  {'投資':12} {v4_bet:>10,}円 {v6_bet:>10,}円")
    print(f"  {'回収':12} {v4_pay:>10,}円 {v6_pay:>10,}円")
    print(f"  {'ROI':12} {v4_roi:>11.0f}% {v6_roi:>11.0f}%")
    print(f"  {'収支':12} {v4_pay-v4_bet:>+10,}円 {v6_pay-v6_bet:>+10,}円")


def _print_v4_hits(all_summaries):
    """v4的中詳細"""
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
