# -*- coding: utf-8 -*-
"""
ボートレース予想AI - 予測エンジン v5

展開予測ベースのモデル:
  1. 相対特徴量（ST差、モーター差、壁の強度）を算出
  2. 展開予測（逃げ/差し/まくり/まくり差し）の確率を推定
  3. 展開別の条件付き確率テーブルで120通りの確率を算出
  4. オッズとの比較でEVを計算

634Rの統計に基づく:
  - 逃げ54% / まくり17% / 差し13% / まくり差し11% / 抜き5%
  - 中穴(30-100倍): 逃げ35% / まくり26% / 差し19%
  - 大穴(100倍+): まくり41% / 差し21% / まくり差し20%
"""
import numpy as np
from itertools import permutations
from typing import List, Dict, Tuple
from models import Racer, Race
from config import NUM_BOATS, VENUE_MAP
from rolling_stats import RollingStats, DEFAULT_KIMARITE

# ローリング統計のグローバルインスタンス (起動時に1回ロード)
_rolling_stats = RollingStats()
_rolling_loaded = _rolling_stats.load(lookback_days=21)
if _rolling_loaded:
    _BASE_KIMARITE = _rolling_stats.kimarite_dist
else:
    _BASE_KIMARITE = DEFAULT_KIMARITE

# ============================================================
# 場特性テーブル
# ============================================================
VENUE_ICHI_RATE = {
    "大村": 0.65, "芦屋": 0.60, "徳山": 0.60, "下関": 0.45,
    "住之江": 0.55, "尼崎": 0.55, "鳴門": 0.55, "丸亀": 0.55,
    "児島": 0.50, "宮島": 0.50, "若松": 0.55, "福岡": 0.50,
    "唐津": 0.55, "桐生": 0.50, "戸田": 0.45, "江戸川": 0.45,
    "平和島": 0.45, "多摩川": 0.50, "浜名湖": 0.50, "蒲郡": 0.50,
    "常滑": 0.50, "津": 0.50, "三国": 0.55, "びわこ": 0.50,
}
VENUE_ABARE = {"下関", "江戸川", "戸田", "平和島", "浜名湖"}


# ============================================================
# 相対特徴量の算出
# ============================================================

def compute_relative_features(racers: List[Racer], venue_name: str = "") -> dict:
    """
    選手間の相対的な特徴量を算出。
    個人の強さではなく「他者との力関係」を数値化。
    """
    if len(racers) < 6:
        return {}

    r = {i+1: racers[i] for i in range(6)}  # 枠番 → Racer

    venue_ichi = VENUE_ICHI_RATE.get(venue_name, 0.50)

    # --- ST関連 ---
    st = {w: r[w].avg_st if r[w].avg_st > 0 else 0.18 for w in range(1, 7)}
    course_st = {w: r[w].course_avg_st if r[w].course_avg_st > 0 else st[w] for w in range(1, 7)}

    # スロー勢(1-3)とダッシュ勢(4-6)の平均ST
    slow_st = np.mean([course_st[w] for w in (1, 2, 3)])
    dash_st = np.mean([course_st[w] for w in (4, 5, 6)])

    # --- モーター ---
    motor = {w: r[w].motor_rate for w in range(1, 7)}
    motor_shisuu = {w: r[w].motor_shisuu for w in range(1, 7)}

    # --- 展示タイム ---
    tenji = {w: r[w].tenji_time if r[w].tenji_time > 0 else 6.8 for w in range(1, 7)}
    tenji_rank = sorted(range(1, 7), key=lambda w: tenji[w])  # 速い順

    # --- 逃げ/差し/まくり率 ---
    nige_1 = r[1].course_nigeritsu if r[1].course_nigeritsu > 0 else 0.45
    sashi_2 = r[2].course_sashi if r[2].course_sashi > 0 else 0.15
    makuri_3 = r[3].course_makuri if r[3].course_makuri > 0 else 0.10
    makuri_4 = r[4].course_makuri if r[4].course_makuri > 0 else 0.15
    makurisashi_3 = r[3].course_makurisashi if r[3].course_makurisashi > 0 else 0.08
    makurisashi_4 = r[4].course_makurisashi if r[4].course_makurisashi > 0 else 0.10

    # --- 壁の概念 ---
    # 2枠が壁として機能するか (STが速い & モーターが良い → 壁が厚い)
    wall_2 = 0.0
    if course_st[2] < course_st[1] + 0.03:  # 2枠がSTで遅れない
        wall_2 += 0.3
    if motor[2] > 30:  # モーターが平均以上
        wall_2 += 0.2
    if r[2].niren_rate > 30:  # 連対率が高い
        wall_2 += 0.2
    # 壁が厚い = まくられにくい = 逃げ成功率UP

    # --- F持ちリスク ---
    flying_risk = {w: 1.0 for w in range(1, 7)}
    for w in range(1, 7):
        # fmochi_rate > 0 ならF休み経験あり → STが慎重になる
        if r[w].fmochi_rate > 0 and r[w].fmochi_rate < r[w].zenkoku_rate * 0.8:
            flying_risk[w] = 0.7  # F持ちはST遅くなる傾向

    features = {
        # ST差
        "st_diff_12": course_st[2] - course_st[1],     # 正=1枠有利
        "st_diff_13": course_st[3] - course_st[1],
        "st_diff_14": course_st[4] - course_st[1],
        "slow_vs_dash_st": dash_st - slow_st,           # 正=スロー有利

        # モーター差
        "motor_diff_12": motor[1] - motor[2],
        "motor_diff_13": motor[1] - motor[3],
        "motor_diff_14": motor[1] - motor[4],
        "motor_best_dash": max(motor[4], motor[5], motor[6]),

        # 展示タイム (速い=小さい)
        "tenji_1_rank": tenji_rank.index(1) + 1,  # 1枠の展示順位
        "tenji_best_waku": tenji_rank[0],           # 展示最速の枠

        # 選手の決まり手力
        "nige_1": nige_1,
        "sashi_2": sashi_2,
        "makuri_3": makuri_3,
        "makuri_4": makuri_4,
        "makurisashi_3": makurisashi_3,
        "makurisashi_4": makurisashi_4,
        "wall_2": wall_2,

        # 壁の強度
        "wall_strength": wall_2,

        # 場の特性
        "venue_ichi": venue_ichi,
        "is_abare": venue_name in VENUE_ABARE,

        # 選手力
        "grade_scores": {w: _grade_score(r[w].grade) for w in range(1, 7)},
        "zenkoku": {w: r[w].zenkoku_rate for w in range(1, 7)},

        # F持ち
        "flying_risk": flying_risk,
    }
    return features


def _grade_score(grade: str) -> float:
    return {"A1": 4.0, "A2": 3.0, "B1": 2.0, "B2": 1.0}.get(grade, 2.0)


# ============================================================
# 展開予測 (ルールベース v1)
# ============================================================

def predict_tenkai(features: dict) -> Dict[str, float]:
    """
    展開パターンの確率を推定。
    634Rの統計に基づくルールベース。

    Returns:
        {"逃げ": 0.55, "差し": 0.13, "まくり": 0.17, "まくり差し": 0.10, "抜き": 0.05}
    """
    if not features:
        return _BASE_KIMARITE.copy()

    # ベース確率 (ローリング統計 or デフォルト)
    p = _BASE_KIMARITE.copy()

    nige_1 = features["nige_1"]
    sashi_2 = features["sashi_2"]
    st_diff_12 = features["st_diff_12"]
    wall_2 = features["wall_strength"]
    venue_ichi = features["venue_ichi"]
    makuri_3 = features["makuri_3"]
    makuri_4 = features["makuri_4"]
    ms_3 = features["makurisashi_3"]
    ms_4 = features["makurisashi_4"]
    slow_dash = features["slow_vs_dash_st"]
    motor_best_dash = features["motor_best_dash"]

    # --- 1. 逃げ確率の調整 ---
    # 1枠の逃げ率が高い → 逃げ確率UP
    nige_adj = (nige_1 - 0.50) * 0.8  # 逃げ率50%を基準
    p["逃げ"] += nige_adj

    # 場の1コース有利度
    p["逃げ"] += (venue_ichi - 0.50) * 0.3

    # 壁が厚い → 逃げやすい
    p["逃げ"] += wall_2 * 0.15

    # 1枠STが有利 → 逃げやすい
    if st_diff_12 > 0.02:
        p["逃げ"] += 0.08
    elif st_diff_12 < -0.02:
        p["逃げ"] -= 0.10

    # --- 2. 差し確率の調整 ---
    # 2枠の差し率が高い
    p["差し"] += (sashi_2 - 0.15) * 0.5

    # 1枠STが不利 → 差されやすい
    if st_diff_12 < -0.02:
        p["差し"] += 0.08

    # --- 3. まくり確率の調整 ---
    # 3,4枠のまくり率
    best_makuri = max(makuri_3, makuri_4)
    p["まくり"] += (best_makuri - 0.12) * 0.6

    # ダッシュ勢のSTが速い → まくりやすい
    if slow_dash > 0.02:
        p["まくり"] += 0.06

    # ダッシュ勢のモーターが良い → まくりやすい
    if motor_best_dash > 40:
        p["まくり"] += 0.05

    # 壁が弱い → まくられやすい
    if wall_2 < 0.3:
        p["まくり"] += 0.04

    # --- 4. まくり差し確率 ---
    best_ms = max(ms_3, ms_4)
    p["まくり差し"] += (best_ms - 0.08) * 0.5

    # 荒れ場 → まくり差し増
    if features["is_abare"]:
        p["まくり差し"] += 0.03
        p["逃げ"] -= 0.03

    # --- 正規化 (合計=1.0) ---
    total = sum(p.values())
    p = {k: max(v, 0.01) / total for k, v in p.items()}
    total = sum(p.values())
    p = {k: v / total for k, v in p.items()}

    return p


# ============================================================
# 展開→条件付き着順確率
# ============================================================

# 634Rの統計から構築した条件付き確率テーブル
# P(着順 | 展開) - 展開ごとにどの枠が何着に来やすいかの確率

def compute_conditional_probs(tenkai_probs: Dict[str, float],
                               features: dict) -> Dict[str, float]:
    """
    展開確率から120通りの3連単確率を算出。
    各展開パターンごとに着順の分布を持ち、
    展開確率で重み付け合算する。

    PLモデルと違い、「まくり展開なら4-5-xが高い」
    「差し展開なら2-1-xが高い」のような相関を表現可能。
    """
    all_boats = list(range(1, 7))
    combo_probs = {}

    # 各展開パターンの着順確率
    for tenkai, t_prob in tenkai_probs.items():
        if t_prob < 0.001:
            continue

        # 展開ごとの1着確率
        p1 = _first_place_probs(tenkai, features)
        # 展開ごとの2着確率 (1着が決まった後)
        p2_given_1 = _second_place_probs(tenkai, features)
        # 3着確率
        p3_given_12 = _third_place_probs(tenkai, features)

        for first in all_boats:
            if p1.get(first, 0) < 0.001:
                continue
            for second in all_boats:
                if second == first:
                    continue
                p2 = p2_given_1.get((first, second), 0)
                if p2 < 0.001:
                    continue
                for third in all_boats:
                    if third in (first, second):
                        continue
                    p3 = p3_given_12.get((first, second, third), 0)
                    if p3 < 0.001:
                        continue

                    combo = f"{first}-{second}-{third}"
                    joint = t_prob * p1[first] * p2 * p3
                    combo_probs[combo] = combo_probs.get(combo, 0) + joint

    # 正規化
    total = sum(combo_probs.values())
    if total > 0:
        combo_probs = {k: v / total for k, v in combo_probs.items()}

    return combo_probs


def _first_place_probs(tenkai: str, features: dict) -> Dict[int, float]:
    """展開ごとの1着確率"""
    # 634Rの統計ベース
    if tenkai == "逃げ":
        # 逃げ: 1号艇が94%で1着(323/342)
        return {1: 0.94, 2: 0.01, 3: 0.01, 4: 0.01, 5: 0.01, 6: 0.01}

    elif tenkai == "差し":
        # 差し: 2号艇64%(52/81), 3号12%, 4号14%
        p = {1: 0.02, 2: 0.64, 3: 0.12, 4: 0.14, 5: 0.04, 6: 0.01}
        # 2枠の差し率で調整
        if features and features.get("sashi_2", 0) > 0.20:
            p[2] += 0.08
            p[3] -= 0.04
            p[4] -= 0.04
        return p

    elif tenkai == "まくり":
        # まくり: 3号32%(35/109), 4号33%(36/109), 2号18%
        p = {1: 0.00, 2: 0.18, 3: 0.32, 4: 0.33, 5: 0.09, 6: 0.06}
        # 4枠のまくり率が高い → 4号有利
        if features and features.get("makuri_4", 0) > features.get("makuri_3", 0):
            p[4] += 0.08
            p[3] -= 0.08
        return p

    elif tenkai == "まくり差し":
        # まくり差し: 3号45%(30/67), 4号19%, 5号21%, 6号13%
        return {1: 0.00, 2: 0.00, 3: 0.45, 4: 0.19, 5: 0.21, 6: 0.13}

    elif tenkai == "抜き":
        # 抜き: 1号36%(12/33), 3号27%, 2号15%
        return {1: 0.36, 2: 0.15, 3: 0.27, 4: 0.12, 5: 0.06, 6: 0.03}

    else:
        return {w: 1/6 for w in range(1, 7)}


def _second_place_probs(tenkai: str, features: dict) -> Dict[Tuple, float]:
    """展開ごとの2着確率 P(2着=y | 1着=x, 展開)"""
    probs = {}
    all_boats = list(range(1, 7))

    if tenkai == "逃げ":
        # 逃げ展開: 2着は内枠が多い (2号>3号>4号)
        for first in all_boats:
            remaining = [b for b in all_boats if b != first]
            if first == 1:
                # 1が逃げたとき: 2着=2(32%), 3(26%), 4(16%), 5(15%), 6(11%)
                dist = {2: 0.32, 3: 0.26, 4: 0.16, 5: 0.15, 6: 0.11}
            else:
                dist = {b: 1/len(remaining) for b in remaining}
            for sec in remaining:
                probs[(first, sec)] = dist.get(sec, 0.1)

    elif tenkai == "差し":
        for first in all_boats:
            remaining = [b for b in all_boats if b != first]
            if first == 2:
                # 2が差したとき: 1号が2着に残る(63%)
                dist = {1: 0.63, 3: 0.12, 4: 0.10, 5: 0.08, 6: 0.07}
            else:
                dist = {b: 0.15 for b in remaining if b != 1}
                dist[1] = 0.35
            for sec in remaining:
                probs[(first, sec)] = dist.get(sec, 0.1)

    elif tenkai == "まくり":
        for first in all_boats:
            remaining = [b for b in all_boats if b != first]
            if first in (3, 4):
                # まくった選手の外が連動 (5,6号)、1号は沈む
                dist = {}
                for b in remaining:
                    if b > first:  # 外の連動
                        dist[b] = 0.25
                    elif b == 1:  # 1号は引き波で沈みやすい
                        dist[b] = 0.12
                    elif b == 2:  # 2号差し残り
                        dist[b] = 0.20
                    else:
                        dist[b] = 0.10
            else:
                dist = {b: 1/len(remaining) for b in remaining}
            for sec in remaining:
                probs[(first, sec)] = dist.get(sec, 0.1)

    elif tenkai == "まくり差し":
        for first in all_boats:
            remaining = [b for b in all_boats if b != first]
            # まくり差し: 1号が2着に残りやすい(634R統計)
            dist = {}
            for b in remaining:
                if b == 1:
                    dist[b] = 0.45  # 1号2着残り率が高い
                elif b > first:
                    dist[b] = 0.15
                else:
                    dist[b] = 0.10
            for sec in remaining:
                probs[(first, sec)] = dist.get(sec, 0.1)

    else:  # 抜き
        for first in all_boats:
            remaining = [b for b in all_boats if b != first]
            dist = {b: 1/len(remaining) for b in remaining}
            for sec in remaining:
                probs[(first, sec)] = dist.get(sec, 0.2)

    return probs


def _third_place_probs(tenkai: str, features: dict) -> Dict[Tuple, float]:
    """3着確率 P(3着=z | 1着=x, 2着=y, 展開)"""
    probs = {}
    all_boats = list(range(1, 7))

    for first in all_boats:
        for second in all_boats:
            if second == first:
                continue
            remaining = [b for b in all_boats if b not in (first, second)]
            # 3着は残り4艇からほぼ均等(若干内寄り)
            for i, third in enumerate(sorted(remaining)):
                # 内枠ほど少し有利
                weight = 1.0 + (4 - third) * 0.05
                probs[(first, second, third)] = weight

    # 正規化 (同じ1着2着の組内で合計1)
    for first in all_boats:
        for second in all_boats:
            if second == first:
                continue
            remaining = [b for b in all_boats if b not in (first, second)]
            total = sum(probs.get((first, second, b), 0) for b in remaining)
            if total > 0:
                for b in remaining:
                    probs[(first, second, b)] = probs.get((first, second, b), 0) / total

    return probs


# ============================================================
# EV計算
# ============================================================

def compute_expected_values(probs: Dict[str, float],
                            odds: Dict[str, float]) -> Dict[str, float]:
    """EV = モデル確率 × オッズ"""
    evs = {}
    for combo, prob in probs.items():
        if combo in odds and odds[combo] > 0:
            evs[combo] = prob * odds[combo] / 100  # odds/100 = 倍率
    return evs


# ============================================================
# メインの予測関数
# ============================================================

def predict_race_v5(race: Race) -> dict:
    """
    展開予測ベースの予測エンジン v5

    Returns:
        probs: 120通りの3連単確率
        evs: EV値
        tenkai_probs: 展開確率
        features: 相対特徴量
    """
    venue = race.venue_name
    if not venue:
        venue = VENUE_MAP.get(race.place_no, "")

    # 整備情報
    seibi_wakus = set()
    for s in race.seibi_list:
        w = s.get("waku") or s.get("boat_no")
        if w:
            seibi_wakus.add(int(w))

    # 1. 相対特徴量
    features = compute_relative_features(race.racers, venue)

    # 2. 展開予測
    tenkai_probs = predict_tenkai(features)

    # 3. 展開→条件付き確率→120通り
    probs = compute_conditional_probs(tenkai_probs, features)

    # 4. EV計算
    evs = compute_expected_values(probs, race.trifecta_odds) if race.trifecta_odds else {}

    # Top20
    if evs:
        top = sorted(evs.items(), key=lambda x: x[1], reverse=True)[:20]
        top_combos = [(c, probs.get(c, 0), ev) for c, ev in top]
    else:
        top = sorted(probs.items(), key=lambda x: x[1], reverse=True)[:20]
        top_combos = [(c, p, 0.0) for c, p in top]

    # 展開から推定するレースタイプ
    dominant = max(tenkai_probs, key=tenkai_probs.get)
    if tenkai_probs.get("逃げ", 0) > 0.70:
        race_type_tag = "ガチガチ"
    elif tenkai_probs.get("逃げ", 0) > 0.55:
        race_type_tag = "通常"
    elif max(tenkai_probs.get("まくり", 0), tenkai_probs.get("まくり差し", 0)) > 0.30:
        race_type_tag = "混戦"
    else:
        race_type_tag = "本命崩れ"

    return {
        "race": race,
        "scores": {r.waku: r.ev_score for r in race.racers},
        "probs": probs,
        "evs": evs,
        "top_combos": top_combos,
        "race_type_tag": race_type_tag,
        "profile_used": "v5展開予測",
        "tenkai_probs": tenkai_probs,
        "features": features,
    }
