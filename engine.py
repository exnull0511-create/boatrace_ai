# -*- coding: utf-8 -*-
"""
ボートレース予想AI - 予測エンジン v4

タイプ別重みプロファイル方式

バックテスト120Rの知見:
  通常: v2重み(逃げ重視/展示なし)がROI 109% → 逃げ評価強化プロファイル
  混戦: v3.1重み(展示活用/差しまくり強化)がROI 104% → 展開重視プロファイル
  本命崩れ: v3重みがROI 199% → バランス型プロファイル
  ガチガチ: 控除率負け → 不買

2パス方式:
  Pass 1: デフォルト重みでスコアリング → レースタイプ判定
  Pass 2: タイプ別の専用重みで再スコアリング → 予測出力
"""
import numpy as np
from itertools import permutations
from typing import List, Dict, Optional
from dataclasses import dataclass

from models import Racer, Race
from config import NUM_BOATS

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
# 重みプロファイル
# ============================================================

@dataclass
class WeightProfile:
    """タイプ別の重み設定"""
    name: str

    # 各要素の重み (合計≒1.0)
    w_course_1chaku: float   # コース別1着率
    w_rate: float            # 勝率基盤
    w_kimarite: float        # 決まり手展開
    w_motor: float           # 機力
    w_st: float              # ST
    w_venue: float           # 場×枠補正
    w_display: float         # 展示タイム

    # 決まり手の内部倍率
    nige_mult: float = 1.0       # 逃げ評価の倍率
    sashi_makuri_mult: float = 1.0  # 差し/まくり評価の倍率
    makurare_pen: float = 1.0    # 捲られペナルティの倍率


# --- 通常: v2ベース (ROI 109.5%) ---
# v2は展示なし/場補正なし/逃げ重視/シンプル
PROFILE_NORMAL = WeightProfile(
    name="通常",
    w_course_1chaku=0.25,    # v2: blended_1chaku * 10.0 * 0.25
    w_rate=0.25,             # v2: zenkoku+recent+tochi = ~0.25
    w_kimarite=0.15,         # v2: kimarite_bonus * 0.15
    w_motor=0.15,            # v2: motor * 0.15
    w_st=0.10,               # v2: st * 0.10
    w_venue=0.00,            # v2にはなし
    w_display=0.00,          # v2にはなし
    nige_mult=1.0,           # v2のオリジナル倍率
    sashi_makuri_mult=1.0,
    makurare_pen=1.0,
)

# --- 混戦: v3.1ベース (ROI 104.8%) ---
# v3.1は展示活用/差しまくり強化/場特性あり/2パス混戦重み
PROFILE_KONSEN = WeightProfile(
    name="混戦",
    w_course_1chaku=0.25,
    w_rate=0.15,
    w_kimarite=0.20,
    w_motor=0.13,
    w_st=0.08,
    w_venue=0.09,
    w_display=0.10,          # v3.1: 展示タイムを活用
    nige_mult=0.8,           # 混戦は逃げにくい
    sashi_makuri_mult=1.5,   # 差し/まくりを大幅強化
    makurare_pen=0.8,
)

# --- 本命崩れ: v3ベース (ROI 199.7%) ---
# v3は展示なし/場特性あり/決まり手差別化/モーター指数控えめ
PROFILE_HONMEI_KUZURE = WeightProfile(
    name="本命崩れ",
    w_course_1chaku=0.30,    # v3: 30%
    w_rate=0.15,             # v3: 15%
    w_kimarite=0.20,         # v3: 20%
    w_motor=0.15,            # v3: 15% (機力差が出やすい)
    w_st=0.10,               # v3: 10%
    w_venue=0.10,            # v3: 10% (場特性あり)
    w_display=0.00,          # v3にはなし
    nige_mult=1.0,
    sashi_makuri_mult=1.0,
    makurare_pen=1.0,
)

# --- デフォルト (タイプ判定用の1パス目) ---
PROFILE_DEFAULT = WeightProfile(
    name="判定用",
    w_course_1chaku=0.28,
    w_rate=0.15,
    w_kimarite=0.20,
    w_motor=0.13,
    w_st=0.09,
    w_venue=0.08,
    w_display=0.07,
    nige_mult=1.0,
    sashi_makuri_mult=1.0,
    makurare_pen=1.0,
)

# レースタイプ → プロファイル
PROFILE_MAP = {
    "通常": PROFILE_NORMAL,
    "混戦": PROFILE_KONSEN,
    "本命崩れ": PROFILE_HONMEI_KUZURE,
    "ガチガチ": PROFILE_NORMAL,  # 判定には使うが買わない
}


# ============================================================
# スコアリング v4
# ============================================================

def compute_ev_score(racer: Racer, profile: WeightProfile,
                     venue_name: str = "", has_seibi: bool = False) -> float:
    """タイプ別重みプロファイルを適用したスコアリング"""
    score = 0.0
    waku = racer.waku
    venue_ichi = VENUE_ICHI_RATE.get(venue_name, 0.50)

    # =============================================
    # A. コース別1着率
    # =============================================
    c1_choku3 = racer.course_1chaku_choku3
    c1_ave = racer.course_1chaku_rate
    c1_tochi = racer.course_1chaku_tochi

    if c1_choku3 > 0:
        blended = c1_choku3 * 0.55 + c1_ave * 0.30 + c1_tochi * 0.15
    elif c1_ave > 0:
        blended = c1_ave * 0.65 + c1_tochi * 0.35
    else:
        blended = 0.0
    score += blended * 10.0 * profile.w_course_1chaku

    # =============================================
    # B. 勝率基盤
    # =============================================
    recent = racer.recent_3m_rate if racer.recent_3m_rate > 0 else racer.zenkoku_rate
    blended_rate = racer.zenkoku_rate * 0.35 + recent * 0.45 + racer.tochi_rate * 0.20
    score += blended_rate * profile.w_rate
    if racer.course_shoritsu > 0:
        score += racer.course_shoritsu * 0.03

    # =============================================
    # C. 決まり手展開 (タイプ別倍率適用)
    # =============================================
    k_score = 0.0
    nige_m = profile.nige_mult
    sm_m = profile.sashi_makuri_mult
    pen_m = profile.makurare_pen

    if waku == 1:
        nige_boost = 1.0 + (venue_ichi - 0.50) * 2.0
        k_score += racer.course_nigeritsu * 4.0 * nige_boost * nige_m
        k_score -= racer.course_sasare * 2.0 * pen_m
        k_score -= racer.course_makurare * 3.0 * pen_m
    elif waku == 2:
        k_score += racer.course_sashi * 3.0 * sm_m
        k_score += racer.course_makuri * 1.5 * sm_m
        k_score -= racer.course_nigashi * 1.0
    elif waku == 3:
        k_score += racer.course_makuri * 2.5 * sm_m
        k_score += racer.course_makurisashi * 2.0 * sm_m
        k_score += racer.course_sashi * 1.0 * sm_m
    elif waku in (4, 5, 6):
        k_score += racer.course_makuri * 3.0 * sm_m
        k_score += racer.course_makurisashi * 2.5 * sm_m
        k_score += racer.course_sashi * 0.8 * sm_m

    score += k_score * profile.w_kimarite

    # =============================================
    # D. 機力
    # =============================================
    m_score = 0.0
    if racer.motor_shisuu != 0:
        m_score += racer.motor_shisuu * 1.5
    m_score += racer.motor_rate / 12.0
    if racer.boat_niren > 0:
        m_score += (racer.boat_niren - 33) / 25.0
    if has_seibi:
        m_score += 0.3
    score += m_score * profile.w_motor

    # =============================================
    # E. ST
    # =============================================
    st_score = max(0, 6.0 - racer.avg_st * 18.0) if racer.avg_st > 0 else 2.5
    if racer.st_junban > 0:
        st_score += max(0, 4.0 - racer.st_junban) * 0.4
    if racer.course_avg_st > 0:
        st_score += max(0, 6.0 - racer.course_avg_st * 18.0) * 0.25
    score += st_score * profile.w_st

    # =============================================
    # F. 場×枠補正
    # =============================================
    v_bonus = 0.0
    if waku == 1:
        v_bonus = (venue_ichi - 0.45) * 8.0
    elif waku in (4, 5, 6) and venue_name in VENUE_ABARE:
        v_bonus = 0.3
    score += v_bonus * profile.w_venue

    # =============================================
    # G. 展示タイム (プロファイルで重みを制御)
    # =============================================
    d_score = 0.0
    if profile.w_display > 0:
        if racer.tenji_time > 0:
            d_score += max(0, 7.0 - racer.tenji_time) * 5.0
        if racer.konsetsu_display > 0 and racer.tenji_time > 0:
            d_score += (racer.konsetsu_display - racer.tenji_time) * 3.0
        if racer.display_junban > 0:
            d_score += max(0, 4.0 - racer.display_junban) * 0.8
    score += d_score * profile.w_display

    return max(score, 0.01)


# ============================================================
# PL確率
# ============================================================

def compute_pl_probabilities(racers: List[Racer], profile: WeightProfile,
                             venue_name: str = "",
                             seibi_wakus: set = None) -> Dict[str, float]:
    if seibi_wakus is None:
        seibi_wakus = set()

    scores = {}
    for r in racers:
        r.ev_score = compute_ev_score(
            r, profile, venue_name, has_seibi=(r.waku in seibi_wakus)
        )
        scores[r.waku] = r.ev_score

    max_ev = max(scores.values()) if scores else 0
    strengths = {w: np.exp(ev - max_ev) for w, ev in scores.items()}
    all_boats = list(range(1, NUM_BOATS + 1))

    probs = {}
    for first, second, third in permutations(all_boats, 3):
        d1 = sum(strengths[b] for b in all_boats)
        d2 = sum(strengths[b] for b in all_boats if b != first)
        d3 = sum(strengths[b] for b in all_boats if b not in (first, second))
        p = (strengths[first]/d1) * (strengths[second]/d2) * (strengths[third]/d3) if d1 and d2 and d3 else 0
        probs[f"{first}-{second}-{third}"] = p

    return probs


# ============================================================
# EV計算
# ============================================================

def compute_expected_values(probs, odds):
    return {c: p * odds[c] for c, p in probs.items() if c in odds and odds[c] > 0}


# ============================================================
# レースタイプ判定
# ============================================================

def classify_race(scores: Dict[int, float]) -> str:
    s = sorted(scores.values(), reverse=True)
    if len(s) < 2:
        return "通常"
    gap = s[0] - s[1]
    if gap > 2.5:
        return "ガチガチ"
    elif gap < 0.5:
        return "混戦"
    elif s[0] < 3.5:
        return "本命崩れ"
    return "通常"


# ============================================================
# レース予測 (2パス方式)
# ============================================================

def predict_race(race: Race) -> dict:
    """
    2パス方式:
      Pass 1: デフォルトプロファイルでスコアリング → タイプ判定
      Pass 2: タイプ別プロファイルで再スコアリング → 予測結果
    """
    venue = race.venue_name

    seibi_wakus = set()
    for s in race.seibi_list:
        w = s.get("waku") or s.get("boat_no")
        if w:
            seibi_wakus.add(int(w))

    # === Pass 1: タイプ判定 ===
    scores_p1 = {}
    for r in race.racers:
        r.ev_score = compute_ev_score(r, PROFILE_DEFAULT, venue, r.waku in seibi_wakus)
        scores_p1[r.waku] = r.ev_score

    race_type_tag = classify_race(scores_p1)

    # === Pass 2: タイプ別プロファイルで再計算 ===
    profile = PROFILE_MAP.get(race_type_tag, PROFILE_DEFAULT)

    scores = {}
    for r in race.racers:
        r.ev_score = compute_ev_score(r, profile, venue, r.waku in seibi_wakus)
        scores[r.waku] = r.ev_score

    probs = compute_pl_probabilities(race.racers, profile, venue, seibi_wakus)

    evs = compute_expected_values(probs, race.trifecta_odds) if race.trifecta_odds else {}

    if evs:
        top = sorted(evs.items(), key=lambda x: x[1], reverse=True)[:20]
        top_combos = [(c, probs.get(c, 0), ev) for c, ev in top]
    else:
        top = sorted(probs.items(), key=lambda x: x[1], reverse=True)[:20]
        top_combos = [(c, p, 0.0) for c, p in top]

    return {
        "race": race,
        "scores": scores,
        "probs": probs,
        "evs": evs,
        "top_combos": top_combos,
        "race_type_tag": race_type_tag,
        "profile_used": profile.name,
    }
