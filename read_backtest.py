# -*- coding: utf-8 -*-
"""外れレースの共通パターン分析 → ルック判定の条件を発見"""
import json, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

d = json.load(open("backtest_20260317_full.json", encoding="utf-8"))
results = d["results"]

for tag in ["通常", "混戦", "本命崩れ"]:
    races = [r for r in results if r["race_type"] == tag]
    hits = [r for r in races if r["hit"]]
    miss = [r for r in races if not r["hit"] and r.get("bet_amount", 0) > 0]

    if not races:
        continue

    print(f"\n{'='*65}")
    print(f"  【{tag}】 {len(races)}R (的中{len(hits)} 外れ{len(miss)})")
    print(f"{'='*65}")

    if not miss:
        print("  外れなし！")
        continue

    # --- 特徴量の統計比較: 的中 vs 外れ ---
    def avg(lst, key, default=0):
        vals = [r.get(key, default) for r in lst if r.get(key) is not None]
        return sum(vals)/len(vals) if vals else default

    print(f"\n■ 特徴量比較 (的中 vs 外れ)")
    print(f"{'指標':12} | {'的中':>8} | {'外れ':>8} | 差分")
    print("-" * 50)

    for feat_name, key, fmt in [
        ("ScoreGap",    "score_gap_12", ".2f"),
        ("ScoreTop",    "score_top",    ".2f"),
        ("Score1枠",    "score_1waku",  ".2f"),
        ("逃げ率1枠",   "nige_1waku",   ".3f"),
        ("展示差Max",   "display_diff_max", ".3f"),
        ("確率順位",    "prob_rank",    ".0f"),
        ("払戻金額",    "payout_odds",  ".0f"),
    ]:
        h = avg(hits, key) if hits else 0
        m = avg(miss, key)
        diff = h - m
        print(f"  {feat_name:10} | {h:>8{fmt}} | {m:>8{fmt}} | {diff:+.3f}")

    # --- 1号艇頭率 ---
    h1 = sum(1 for r in hits if r.get("is_1head")) / len(hits) * 100 if hits else 0
    m1 = sum(1 for r in miss if r.get("is_1head")) / len(miss) * 100 if miss else 0
    print(f"  {'1号艇頭率':10} | {h1:>7.0f}% | {m1:>7.0f}% | {h1-m1:+.0f}pt")

    # --- 決まり手分布 ---
    print(f"\n■ 外れの決まり手")
    kime = {}
    for r in miss:
        k = r.get("kimarite", "?")
        kime[k] = kime.get(k, 0) + 1
    for k, c in sorted(kime.items(), key=lambda x: -x[1]):
        print(f"  {k:8}: {c:>2}R ({c/len(miss)*100:.0f}%)")

    # --- 1号艇の級別 ---
    print(f"\n■ 1枠の級別")
    for grp in ["A1", "A2", "B1", "B2"]:
        h_g = sum(1 for r in hits if r.get("grade_1waku") == grp) if hits else 0
        m_g = sum(1 for r in miss if r.get("grade_1waku") == grp)
        total = h_g + m_g
        if total > 0:
            print(f"  {grp}: 的中{h_g} 外れ{m_g} ({h_g/total*100:.0f}%)")

    # --- ScoreGap分布 (ルック判定の閾値を探す) ---
    print(f"\n■ ScoreGap分布")
    for lo, hi, label in [(0, 0.3, "<0.3"), (0.3, 0.6, "0.3-0.6"),
                           (0.6, 1.0, "0.6-1.0"), (1.0, 1.5, "1.0-1.5"),
                           (1.5, 2.5, "1.5-2.5"), (2.5, 99, "2.5+")]:
        h_c = sum(1 for r in hits if lo <= r.get("score_gap_12",0) < hi) if hits else 0
        m_c = sum(1 for r in miss if lo <= r.get("score_gap_12",0) < hi)
        total = h_c + m_c
        rate = h_c/total*100 if total > 0 else 0
        print(f"  Gap {label:>7}: 的中{h_c:>2} 外れ{m_c:>2} ({rate:>4.0f}%)")

    # --- 1枠逃げ率のしきい値 ---
    print(f"\n■ 1枠逃げ率しきい値")
    for thr in [0.0, 0.2, 0.3, 0.4, 0.5]:
        h_c = sum(1 for r in hits if r.get("nige_1waku",0) >= thr) if hits else 0
        m_c = sum(1 for r in miss if r.get("nige_1waku",0) >= thr)
        total = h_c + m_c
        rate = h_c/total*100 if total > 0 else 0
        print(f"  逃げ率>={thr:.1f}: 的中{h_c:>2} 外れ{m_c:>2} ({rate:>4.0f}%)")

    # --- 外れレース詳細 (パターン発見用) ---
    print(f"\n■ 外れレース詳細 ({len(miss)}R)")
    for r in miss[:10]:
        print(f"  {r['venue']} {r['race_no']:>2}R: {r['actual']}({r['kimarite']}) "
              f"P#{r['prob_rank']:>3} Gap={r.get('score_gap_12',0):.2f} "
              f"逃げ{r.get('nige_1waku',0):.2f} {r.get('grade_1waku','')}")
