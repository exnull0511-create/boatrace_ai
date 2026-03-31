# -*- coding: utf-8 -*-
"""
ボートレース予想AI - 予測エンジン v6

v4(PL確率) × v5(展開予測) のハイブリッドアンサンブル。

核心アイデア:
  - v4は EV計算が強い (ROI 108%) が、的中数が少ない (152/634)
  - v5は 的中数が多い (170/634) が、確率分布が平坦でEV計算が弱い (ROI 84%)
  - 幾何平均アンサンブルで合成:
      P_v6(combo) = P_v4(combo)^α × P_v5(combo)^(1-α)   → 正規化
  - 両モデルが「合意」した組が高確率になるため、偽陽性が減少
  - v5の展開タグ(混戦/ガチガチ等)をレースフィルタに活用

パラメータ:
  α (ALPHA): v4への重み (0.0=v5のみ, 1.0=v4のみ)

ルックフィルタ (633Rバックテスト最適化):
  中間: tenkai_max ≥ 0.62 + agreement ≥ 0.75 + top15/日 → ROI 612%, MaxDD 26,000円
  厳選: tenkai_max ≥ 0.65 + agreement ≥ 0.75 → ROI 1240% (ボラ高)
"""
import numpy as np
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass

from models import Race, Racer
from config import VENUE_MAP

# v4, v5 の予測関数をインポート
from engine import predict_race as predict_v4
from engine_v5 import predict_race_v5 as predict_v5

# ============================================================
# ハイブリッドパラメータ
# ============================================================

ALPHA = 0.3  # v4の重み (634Rバックテストで最適: ROI 143%, 的中率32.3%)

# ルックフィルタ閾値
# 23日間3212Rの分析結果:
#   ag<0.40 top5/日: ROI 129%, DD 30,650円, 5R/日 (推奨)
#   ag<0.15:         ROI 121%, DD 34,690円, 10R/日
#   ag<0.30:         ROI 128%, DD 58,490円, 18R/日
# 理論: モデル不一致 → オッズ非効率 → 優位性あり
LOOK_TENKAI_MIN = 0.0     # 展開確信度フィルタは無効化
LOOK_AGREE_MAX = 0.40     # 合意度の上限 (低合意のみベット)
LOOK_AGREE_MIN = 0.0      # 合意度の下限 (互換性のため残す)

# 買い目設定
V6_MAX_BETS = 5           # EV Top5
V6_BUDGET = 1000          # 1R合計予算

# ガードレール (Phase 1 バグ修正: 3月全滅の根本原因)
V6_EV_MIN = 0.8           # EV最低閾値 (1.0=損益分岐, 0.8=20%損でもシグナルあり)
V6_PROB_MIN = 0.01        # 確率フロア (1%未満=ランダム0.83%と区別不能)
V6_ODDS_MAX_BAI = 300.0   # オッズ上限 (300倍超=確率推定の精度が極端に低い)


# ============================================================
# 幾何平均アンサンブル
# ============================================================

def ensemble_geometric(probs_v4: Dict[str, float],
                       probs_v5: Dict[str, float],
                       alpha: float = ALPHA) -> Dict[str, float]:
    """
    幾何平均で2つの確率分布を合成。

    P_v6(c) = P_v4(c)^α × P_v5(c)^(1-α)  → 正規化
    """
    all_combos = set(probs_v4.keys()) | set(probs_v5.keys())
    eps = 1e-8

    merged = {}
    for combo in all_combos:
        p4 = max(probs_v4.get(combo, 0), eps)
        p5 = max(probs_v5.get(combo, 0), eps)
        log_p = alpha * np.log(p4) + (1 - alpha) * np.log(p5)
        merged[combo] = np.exp(log_p)

    total = sum(merged.values())
    if total > 0:
        merged = {k: v / total for k, v in merged.items()}

    return merged


# ============================================================
# v4v5合意度 (確率分布の相関)
# ============================================================

def compute_agreement(probs_v4: Dict[str, float],
                      probs_v5: Dict[str, float]) -> float:
    """
    v4とv5の確率分布の相関係数 (ピアソン)。

    高い = 両モデルが同じレース構造を読んでいる → 的中しやすい
    低い = モデルが矛盾 → 予測困難
    """
    common = sorted(set(probs_v4.keys()) & set(probs_v5.keys()))
    if len(common) < 2:
        return 0.0

    p4 = [probs_v4.get(c, 0) for c in common]
    p5 = [probs_v5.get(c, 0) for c in common]
    corr = np.corrcoef(p4, p5)[0, 1]
    return float(corr) if not np.isnan(corr) else 0.0


# ============================================================
# EV計算
# ============================================================

def compute_expected_values(probs: Dict[str, float],
                            odds: Dict[str, float]) -> Dict[str, float]:
    """EV = モデル確率 × オッズ (倍率)。EV >= 1.0 が損益分岐点。"""
    evs = {}
    for combo, prob in probs.items():
        if combo in odds and odds[combo] > 0:
            evs[combo] = prob * odds[combo]
    return evs


# ============================================================
# メイン予測関数
# ============================================================

def predict_race_v6(race: Race, alpha: float = ALPHA) -> dict:
    """
    v6ハイブリッド予測エンジン

    Returns に agreement, tenkai_max を追加 (ルックフィルタ用)
    """
    # === v4 予測 ===
    pred_v4 = predict_v4(race)
    probs_v4 = pred_v4.get("probs", {})
    scores_v4 = pred_v4.get("scores", {})

    # === v5 予測 ===
    pred_v5 = predict_v5(race)
    probs_v5 = pred_v5.get("probs", {})
    tenkai_probs = pred_v5.get("tenkai_probs", {})
    features = pred_v5.get("features", {})

    # === アンサンブル ===
    probs_v6 = ensemble_geometric(probs_v4, probs_v5, alpha)

    # === 合意度 ===
    agreement = compute_agreement(probs_v4, probs_v5)

    # === 展開確信度 ===
    tenkai_max = max(tenkai_probs.values()) if tenkai_probs else 0.0

    # === EV計算 ===
    evs = compute_expected_values(probs_v6, race.trifecta_odds) if race.trifecta_odds else {}

    # === Top20 ===
    if evs:
        top = sorted(evs.items(), key=lambda x: x[1], reverse=True)[:20]
        top_combos = [(c, probs_v6.get(c, 0), ev) for c, ev in top]
    else:
        top = sorted(probs_v6.items(), key=lambda x: x[1], reverse=True)[:20]
        top_combos = [(c, p, 0.0) for c, p in top]

    # 展開タグはv5から継承
    race_type_tag = pred_v5.get("race_type_tag", pred_v4.get("race_type_tag", "通常"))

    return {
        "race": race,
        "scores": scores_v4,
        "probs": probs_v6,
        "probs_v4": probs_v4,
        "probs_v5": probs_v5,
        "evs": evs,
        "top_combos": top_combos,
        "race_type_tag": race_type_tag,
        "profile_used": f"v6ハイブリッド(α={alpha:.1f})",
        "tenkai_probs": tenkai_probs,
        "features": features,
        "alpha": alpha,
        "agreement": agreement,
        "tenkai_max": tenkai_max,
    }


# ============================================================
# ルックフィルタ (v6用)
# ============================================================

def should_look_v6(prediction: dict, race: Race,
                   tenkai_min: float = LOOK_TENKAI_MIN,
                   agree_max: float = LOOK_AGREE_MAX,
                   agree_min: float = LOOK_AGREE_MIN) -> Tuple[bool, str]:
    """
    v6ルックフィルタ: 低合意レースのみベット。

    12日1762Rの分析結果:
      - ag < 0.5: ROI 140%, 黒字日67%
      - ag >= 0.5: ROI 80-82% (赤字)
    理論: モデル不一致→オッズ非効率→優位性あり

    Returns:
        (is_look, reason): is_look=True → 見送り
    """
    tenkai_max = prediction.get("tenkai_max", 0)
    agreement = prediction.get("agreement", 0)
    tag = prediction.get("race_type_tag", "")

    # 展開確信度が極端に低い → 読めないレース
    if tenkai_min > 0 and tenkai_max < tenkai_min:
        return True, f"展開不明(tk={tenkai_max:.2f}<{tenkai_min})"

    # 合意度が高すぎる → オッズが効率的で優位性なし
    if agreement >= agree_max:
        return True, f"高合意(ag={agreement:.2f}>={agree_max})"

    # 合意度の下限 (互換性)
    if agree_min > 0 and agreement < agree_min:
        return True, f"モデル不一致(ag={agreement:.2f}<{agree_min})"

    return False, ""


# ============================================================
# EV Top5 買い目選定 (v6用)
# ============================================================

@dataclass
class BetCandidateV6:
    """1つの買い目"""
    combo: str
    prob: float
    odds: float
    ev: float
    bet_amount: int = 0


def select_ev_top_bets(prediction: dict, race: Race,
                       max_bets: int = V6_MAX_BETS,
                       budget: int = V6_BUDGET,
                       ev_min: float = V6_EV_MIN,
                       prob_min: float = V6_PROB_MIN,
                       odds_max_bai: float = V6_ODDS_MAX_BAI,
                       ) -> List[BetCandidateV6]:
    """
    v6のEV Top N を買い目として選定。
    EV比率で予算配分。

    ガードレール (Phase 1):
      - ev >= ev_min: 期待値が低すぎるcomboを排除
      - prob >= prob_min: モデルにシグナルがないcomboを排除
      - odds <= odds_max_bai: 超大穴(確率推定が不正確)を排除
    """
    evs = prediction.get("evs", {})
    probs = prediction.get("probs", {})
    odds = race.trifecta_odds or {}

    if not evs:
        return []

    ev_sorted = sorted(evs.items(), key=lambda x: -x[1])
    candidates = []
    for combo, ev in ev_sorted:
        if ev < ev_min:
            break  # EV降順なのでこれ以降は全て閾値未満
        odds_val = odds.get(combo, 0)
        prob = probs.get(combo, 0)

        if prob < prob_min:
            continue
        if odds_val > odds_max_bai:
            continue

        candidates.append(BetCandidateV6(
            combo=combo, prob=prob, odds=odds_val, ev=ev
        ))
        if len(candidates) >= max_bets:
            break

    if not candidates:
        return []

    # EV比率で予算配分 (最低100円, 100円単位)
    n = len(candidates)
    base = 100
    remaining = budget - base * n
    if remaining <= 0:
        for c in candidates:
            c.bet_amount = base
        return candidates

    total_ev = sum(c.ev for c in candidates)
    for c in candidates:
        extra = int((c.ev / total_ev) * remaining / 100) * 100
        c.bet_amount = base + extra

    # 余りをEV上位から追加
    total_bet = sum(c.bet_amount for c in candidates)
    leftover = budget - total_bet
    i = 0
    while leftover >= 100 and i < n:
        candidates[i].bet_amount += 100
        leftover -= 100
        i += 1

    return candidates
