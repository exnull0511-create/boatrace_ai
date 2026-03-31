# -*- coding: utf-8 -*-
"""
ウォークフォワード検証: ローリング窓でモデルの汎化性能を評価

使い方:
  python walk_forward.py                  # デフォルト (全rawデータ)
  python walk_forward.py --window 21      # 訓練窓21日
  python walk_forward.py --step 7         # 7日ずつ前進

原理:
  1. rawデータを日付順に読み込み
  2. 窓W[t-N : t] のデータでモデルパラメータを固定
  3. 窓W[t : t+S] のデータでOOS (Out-of-Sample) 評価
  4. S日前進して繰り返し
  5. 各窓のROI/的中率/ベット数を記録

注: 現在のモデルは学習パラメータが固定値なので、
    「窓ごとにパラメータを変更」するのではなく、
    「期間によるモデル精度の変動」を計測する。
    将来的にローリング統計を導入したら、窓ごとに統計を更新する。
"""
import sys
import io
import json
import os
import glob
import argparse
from datetime import datetime, timedelta

sys.path.insert(0, r'c:\money plus\boatrace_ai')

from fast_backtest import raw_to_race
from engine_v6 import predict_race_v6, should_look_v6
from strategy import allocate_portfolio, should_look

RAW_DIR = r"c:\money plus\boatrace_ai\data\raw"


def load_all_raw_data():
    """全rawデータを日付別にロード"""
    files = sorted(glob.glob(os.path.join(RAW_DIR, "*.json")))
    data_by_date = {}
    for fpath in files:
        date_str = os.path.basename(fpath).replace(".json", "")
        try:
            races = json.load(open(fpath, encoding="utf-8"))
            if races:  # 空でない日のみ
                data_by_date[date_str] = races
        except Exception:
            pass
    return data_by_date


def evaluate_day(raw_races: list, budget: int = 1000, max_bets: int = 5,
                 max_races_per_day: int = 7):
    """
    1日分を確率Top5で評価。合意度が低い順にtop N レースを選択。

    Returns:
        dict: bet, pay, hits, races, details
    """
    # Pass 1: 全レース予測してフィルタ通過分を収集
    candidates = []
    for raw in raw_races:
        race = raw_to_race(raw)
        if not race.result_order:
            continue

        pred = predict_race_v6(race)

        if pred.get("race_type_tag") == "ガチガチ":
            continue
        is_look, _ = should_look_v6(pred, race)
        if is_look:
            continue

        candidates.append((pred, race, raw))

    # Pass 2: 合意度が低い順にソートしてtop N を選択
    candidates.sort(key=lambda x: x[0].get("agreement", 1.0))
    selected = candidates[:max_races_per_day]

    total_bet = 0
    total_pay = 0
    hits = 0
    races = 0
    details = []

    for pred, race, raw in selected:
        bets = allocate_portfolio(pred, budget=budget, max_bets=max_bets)
        if not bets:
            continue

        races += 1
        bet_amt = sum(b.bet_amount for b in bets)
        total_bet += bet_amt

        actual = f"{race.result_order[0]}-{race.result_order[1]}-{race.result_order[2]}"
        hit = next((b for b in bets if b.combo == actual), None)

        pay = 0
        if hit:
            hits += 1
            pay = int(race.result_payout * hit.bet_amount / 100)
            total_pay += pay

        details.append({
            "venue": raw.get("venue", ""),
            "race_no": raw["race_no"],
            "tag": pred.get("race_type_tag", ""),
            "hit": hit is not None,
            "bet": bet_amt,
            "pay": pay,
            "actual": actual,
            "actual_odds": race.result_payout / 100 if race.result_payout else 0,
        })

    return {
        "bet": total_bet, "pay": total_pay,
        "hits": hits, "races": races,
        "details": details,
    }


def walk_forward(data_by_date: dict, window_days: int = 21, step_days: int = 7):
    """
    ウォークフォワード検証のメインループ。

    Args:
        data_by_date: {date_str: [raw_races]}
        window_days: 評価窓の日数
        step_days: ステップ日数
    """
    dates = sorted(data_by_date.keys())
    if not dates:
        print("データなし")
        return []

    print(f"データ期間: {dates[0]} 〜 {dates[-1]} ({len(dates)}日)")
    print(f"評価窓: {window_days}日 / ステップ: {step_days}日")
    print(f"{'='*75}")

    results = []
    i = 0

    while i + window_days <= len(dates):
        window_dates = dates[i:i + window_days]
        window_start = window_dates[0]
        window_end = window_dates[-1]

        # この窓のデータで評価
        window_bet = 0
        window_pay = 0
        window_hits = 0
        window_races = 0

        for d in window_dates:
            if d in data_by_date:
                day_result = evaluate_day(data_by_date[d])
                window_bet += day_result["bet"]
                window_pay += day_result["pay"]
                window_hits += day_result["hits"]
                window_races += day_result["races"]

        roi = window_pay / window_bet * 100 if window_bet > 0 else 0
        hit_rate = window_hits / window_races * 100 if window_races > 0 else 0

        result = {
            "start": window_start, "end": window_end,
            "days": len(window_dates),
            "races": window_races, "hits": window_hits,
            "hit_rate": hit_rate,
            "bet": window_bet, "pay": window_pay,
            "roi": roi, "pnl": window_pay - window_bet,
        }
        results.append(result)

        pnl = window_pay - window_bet
        bar = "+" * int(min(roi / 10, 20)) if roi > 0 else ""
        status = "✅" if roi >= 100 else "⚠️" if roi >= 80 else "❌"

        print(f"  {status} {window_start}-{window_end}: "
              f"{window_races:>4}R {window_hits:>3}hit ({hit_rate:>4.1f}%) "
              f"ROI {roi:>5.0f}% ({pnl:>+9,}円) {bar}")

        i += step_days

    return results


def print_summary(results: list):
    """全窓の統計サマリー"""
    if not results:
        return

    total_races = sum(r["races"] for r in results)
    total_hits = sum(r["hits"] for r in results)
    total_bet = sum(r["bet"] for r in results)
    total_pay = sum(r["pay"] for r in results)
    total_roi = total_pay / total_bet * 100 if total_bet > 0 else 0

    rois = [r["roi"] for r in results]
    profitable = sum(1 for r in rois if r >= 100)

    print(f"\n{'='*75}")
    print(f"  📊 ウォークフォワード結果 ({len(results)}窓)")
    print(f"{'='*75}")
    print(f"  全体: {total_races}R {total_hits}hit "
          f"({total_hits/total_races*100:.1f}%) ROI {total_roi:.0f}%")
    print(f"  収支: {total_pay-total_bet:+,}円 "
          f"(投資{total_bet:,}円 → 回収{total_pay:,}円)")
    print(f"  黒字窓: {profitable}/{len(results)} "
          f"({profitable/len(results)*100:.0f}%)")

    if len(rois) >= 2:
        import numpy as np
        arr = np.array(rois)
        print(f"  ROI: mean={arr.mean():.0f}% median={np.median(arr):.0f}% "
              f"std={arr.std():.0f}% min={arr.min():.0f}% max={arr.max():.0f}%")

    # 最大ドローダウン
    cumulative = 0
    peak = 0
    max_dd = 0
    for r in results:
        cumulative += r["pnl"]
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd
    print(f"  最大DD: {max_dd:,}円")


def main():
    parser = argparse.ArgumentParser(description="ウォークフォワード検証")
    parser.add_argument("--window", type=int, default=7,
                        help="評価窓の日数 (default: 7)")
    parser.add_argument("--step", type=int, default=7,
                        help="ステップ日数 (default: 7)")
    parser.add_argument("--max-races", type=int, default=7,
                        help="1日の最大レース数 (default: 7)")
    args = parser.parse_args()

    # evaluate_dayにmax_races_per_dayを渡すためのパッチ
    import functools
    global evaluate_day
    evaluate_day = functools.partial(evaluate_day, max_races_per_day=args.max_races)

    data = load_all_raw_data()
    results = walk_forward(data, window_days=args.window, step_days=args.step)
    print_summary(results)


if __name__ == "__main__":
    main()
