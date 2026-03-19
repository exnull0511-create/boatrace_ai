# -*- coding: utf-8 -*-
"""
ボートレース予想AI - 戦略・ポートフォリオ配分

Phase 3: ベッティング戦略

機能:
  1. レースフィルタリング (ガチガチ/混戦/通常の判定)
  2. EV > 1.0 の組み合わせ抽出
  3. ポートフォリオ配分 (EV比 or 均等)
  4. 全場全レース一括予測
"""
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass, field

from models import Race
from engine import predict_race, compute_ev_score
from config import BET_BASE, TOP_N_BETS, NUM_BOATS


# ============================================================
# ベット候補
# ============================================================

@dataclass
class BetCandidate:
    """1つの買い目"""
    combo: str           # "1-3-5"
    prob: float          # PL確率
    odds: float          # オッズ
    ev: float            # EV = prob * odds
    bet_amount: int = 0  # 賭け額 (円)


@dataclass
class RacePrediction:
    """1レースの予測結果"""
    race: Race
    race_type_tag: str   # "ガチガチ" / "混戦" / "本命崩れ" / "通常"
    scores: Dict[int, float] = field(default_factory=dict)
    bets: List[BetCandidate] = field(default_factory=list)
    total_bet: int = 0   # 総賭け額
    expected_return: float = 0.0  # 期待リターン

    @property
    def bet_count(self) -> int:
        return len(self.bets)

    @property
    def top_ev(self) -> float:
        return max((b.ev for b in self.bets), default=0.0)


# ============================================================
# レースフィルタ
# ============================================================

def classify_race(scores: Dict[int, float]) -> str:
    """レースタイプを判定"""
    sorted_scores = sorted(scores.values(), reverse=True)
    if len(sorted_scores) < 2:
        return "通常"

    gap_1_2 = sorted_scores[0] - sorted_scores[1]
    gap_2_3 = sorted_scores[1] - sorted_scores[2] if len(sorted_scores) >= 3 else 0

    if gap_1_2 > 2.5:
        return "ガチガチ"  # 1艇が圧倒的に強い
    elif gap_1_2 < 0.5 and gap_2_3 < 0.5:
        return "混戦"      # 上位が拮抗
    elif sorted_scores[0] < 3.5:
        return "本命崩れ"  # トップが弱い
    else:
        return "通常"


def should_look(prediction: dict, race: Race) -> Tuple[bool, str]:
    """
    ルック判定 (賭けずに見送る条件)

    バックテスト120Rの外れ分析から発見したフィルタ:

    共通:
      - ガチガチ → 控除率負けするため不買

    通常:
      - ScoreGap < 0.6 → 的中率0% (5R全滅)
      → 上位2名のスコア差が小さすぎると実質"隠れ混戦"

    混戦:
      - 展示タイム差 < 0.08 → 展示でも差がつかず読めない
      - 1号艇逃げ率 < 0.15 → 逃げが期待できず予測困難

    本命崩れ:
      - 1枠がB1以下 かつ ScoreGap < 0.6 → 的中率低下
      → 混戦に近い構造で予測困難

    Returns:
        (is_look, reason)
    """
    tag = prediction["race_type_tag"]
    scores = prediction["scores"]
    racers = race.racers

    # ガチガチは常にルック
    if tag == "ガチガチ":
        return True, "ガチガチ(控除率負け)"

    sorted_scores = sorted(scores.values(), reverse=True)
    gap = sorted_scores[0] - sorted_scores[1] if len(sorted_scores) >= 2 else 0

    if tag == "通常":
        # ScoreGap < 0.6 → 隠れ混戦パターン
        if gap < 0.6:
            return True, f"通常だがGap={gap:.2f}<0.6(隠れ混戦)"

    elif tag == "混戦":
        # 展示タイム差で差別化
        display_times = [r.tenji_time for r in racers if r.tenji_time > 0]
        if display_times:
            display_diff = max(display_times) - min(display_times)
            if display_diff < 0.08:
                return True, f"混戦で展示差={display_diff:.3f}<0.08"

        # 1号艇逃げ率が極端に低い
        if racers and racers[0].course_nigeritsu < 0.15:
            return True, f"混戦で1枠逃げ率={racers[0].course_nigeritsu:.2f}<0.15"

    elif tag == "本命崩れ":
        # 1枠がB1以下 かつ Gap < 0.6
        if racers and racers[0].grade in ("B1", "B2") and gap < 0.6:
            return True, f"本命崩れで1枠{racers[0].grade}+Gap={gap:.2f}<0.6"

    return False, ""


# ============================================================
# 中穴EV狙いフィルター (v5)
# ============================================================

# 中穴オッズ帯 (倍率)
CHUUANA_ODDS_MIN = 20.0     # 20倍
CHUUANA_ODDS_MAX = 1000.0   # 1000倍 (中穴〜大穴)
CHUUANA_EV_MIN = 1.0        # 最低EV閾値
CHUUANA_BEST_EV_MIN = 1.2   # レース選定: 最大EV閾値
CHUUANA_MAX_EV_COMBOS = 12  # EV>1の組がこれ以上 → 散漫で除外
CHUUANA_MAX_BETS = 5        # 買い目上限
CHUUANA_BUDGET = 1000       # 1R合計予算


def chuuana_filter(prediction: dict, race: Race) -> tuple:
    """
    中穴EV狙いフィルター: 市場が過小評価している中穴組が存在するレースを選出

    条件 (全てAND):
      1. ガチガチ除外 (赤オッズにしかならない)
      2. 中穴帯(20〜100倍)にEV>1.0の組が存在する
      3. 中穴帯の最大EV ≥ 1.2 (市場との乖離が十分)
      4. EV>1.0の中穴組が8組未満 (多すぎ=読めない混戦)

    Returns:
        (passed, best_ev, chuuana_combos, reason)
        chuuana_combos: BetCandidate のリスト (EV降順, 最大5組, EV配分済み)
    """
    tag = prediction["race_type_tag"]
    evs = prediction.get("evs", {})
    probs = prediction.get("probs", {})
    odds = race.trifecta_odds if race.trifecta_odds else {}

    # 条件1: ガチガチ除外
    if tag == "ガチガチ":
        return False, 0.0, [], "ガチガチ(赤オッズのみ)"

    # 中穴帯のEV>1.0組を抽出
    chuuana_hits = []
    for combo, ev in evs.items():
        odds_val = odds.get(combo, 0)
        odds_bai = odds_val / 100.0 if odds_val > 0 else 0
        prob = probs.get(combo, 0)

        if CHUUANA_ODDS_MIN <= odds_bai <= CHUUANA_ODDS_MAX and ev >= CHUUANA_EV_MIN:
            chuuana_hits.append(BetCandidate(
                combo=combo, prob=prob, odds=odds_val,
                ev=ev, bet_amount=100  # 仮
            ))

    # 条件2: 中穴EV組が存在するか
    if not chuuana_hits:
        return False, 0.0, [], "中穴帯にEV>1.0なし"

    # 条件3: 最大EV >= 1.2
    best_ev = max(c.ev for c in chuuana_hits)
    if best_ev < CHUUANA_BEST_EV_MIN:
        return False, 0.0, [], f"最大EV={best_ev:.2f}<{CHUUANA_BEST_EV_MIN}"

    # 条件4: EV>1.0組が多すぎないか (散漫=読めない)
    if len(chuuana_hits) > CHUUANA_MAX_EV_COMBOS:
        return False, 0.0, [], f"EV>1中穴{len(chuuana_hits)}組(散漫)"

    # EV降順でTop5を選出
    chuuana_hits.sort(key=lambda x: x.ev, reverse=True)
    selected = chuuana_hits[:CHUUANA_MAX_BETS]

    # EV比率で配分 (合計1000円, 最低100円, 100円単位)
    selected = _allocate_by_ev(selected, CHUUANA_BUDGET)

    return True, best_ev, selected, ""


def _allocate_by_ev(combos: List[BetCandidate], budget: int) -> List[BetCandidate]:
    """
    EV比率で予算配分: EVが高い組に厚張り

    - 合計 budget 円以内
    - 最低100円/組
    - 100円単位で丸め
    - EV比率で残り予算を配分
    - 余りはEV上位から100円ずつ追加
    """
    n = len(combos)
    if n == 0:
        return combos

    # 全組に最低100円を確保
    base = 100
    remaining = budget - base * n

    if remaining <= 0:
        for c in combos:
            c.bet_amount = base
        return combos

    # EV比率で残りを配分 (切り捨て)
    total_ev = sum(c.ev for c in combos)
    for c in combos:
        extra = int((c.ev / total_ev) * remaining / 100) * 100
        c.bet_amount = base + extra

    # 余りをEV上位から100円ずつ追加
    total = sum(c.bet_amount for c in combos)
    leftover = budget - total
    i = 0
    while leftover >= 100 and i < n:
        combos[i].bet_amount += 100
        leftover -= 100
        i += 1

    return combos


def select_chuuana_bets(prediction: dict, race: Race) -> List[BetCandidate]:
    """
    中穴EV狙い買い目選定 (chuuana_filter通過後に使用)

    Returns:
        List[BetCandidate] - EV上位の中穴組 (最大5点, EV配分, 合計1000円)
    """
    passed, best_ev, combos, reason = chuuana_filter(prediction, race)
    if not passed:
        return []
    return combos



# ============================================================
# ポートフォリオ配分
# ============================================================

def allocate_portfolio(prediction: dict, budget: int = 1000,
                       max_bets: int = 5,
                       min_prob: float = 0.005) -> List[BetCandidate]:
    """
    確率Top5配分方式

    バックテストの知見:
      - 確率モデル精度: Top1=33%, Top3=50% → 高精度
      - 的中は全て確率上位から出ていた
      - EV穴枠からの的中はゼロ → 穴枠を廃止
      - 確率上位5組に確率比で配分が最適

    Args:
        prediction: predict_race() の返り値
        budget: 1レースの予算 (円)
        max_bets: 最大買い目数 (5固定)
        min_prob: 最低確率フィルター
    """
    evs = prediction.get("evs", {})
    probs = prediction.get("probs", {})
    odds = prediction.get("race", Race(0, 0, "")).trifecta_odds

    if not probs:
        return []

    # 確率上位を抽出
    prob_top = sorted(probs.items(), key=lambda x: x[1], reverse=True)
    candidates = []
    for combo, prob in prob_top:
        if prob < min_prob:
            break
        odds_val = odds.get(combo, 0) if odds else 0
        ev = prob * odds_val if odds_val > 0 else 0
        candidates.append(BetCandidate(
            combo=combo, prob=prob, odds=odds_val, ev=ev
        ))
        if len(candidates) >= max_bets:
            break

    if not candidates:
        return []

    # 確率比で配分
    total_prob = sum(c.prob for c in candidates)
    for c in candidates:
        raw = (c.prob / total_prob) * budget
        c.bet_amount = max(BET_BASE, int(raw / BET_BASE) * BET_BASE)

    return candidates


def allocate_portfolio_prob(prediction: dict, budget: int = 1000,
                            top_n: int = 5) -> List[BetCandidate]:
    """
    オッズがない場合に確率ベースで配分 (確率上位N組に均等配分)
    """
    probs = prediction.get("probs", {})
    sorted_combos = sorted(probs.items(), key=lambda x: x[1], reverse=True)[:top_n]

    unit = max(BET_BASE, int(budget / len(sorted_combos) / BET_BASE) * BET_BASE)

    return [
        BetCandidate(combo=combo, prob=prob, odds=0, ev=0, bet_amount=unit)
        for combo, prob in sorted_combos
    ]


# ============================================================
# 全場全レース一括予測
# ============================================================

def predict_all_races(scraper, hiduke: str, budget_per_race: int = 1000,
                      min_ev: float = 1.0) -> List[RacePrediction]:
    """
    当日の全場全レースを一括予測

    Args:
        scraper: BoatraceScraper インスタンス
        hiduke: 日付 (yyyymmdd)
        budget_per_race: 1レースあたりの予算
        min_ev: EV閾値

    Returns:
        RacePrediction のリスト
    """
    schedule = scraper.scrape_today_schedule(hiduke)
    results = []

    for venue in schedule.active_venues:
        print(f"\n{'='*50}")
        print(f"  {venue.venue_name} ({venue.day_label})")
        print(f"{'='*50}")

        for race_no in range(1, 13):
            race = scraper.scrape_full_race(venue.place_no, race_no, hiduke)
            if not race or not race.racers:
                continue

            # オッズ取得
            odds = scraper.scrape_odds(venue.place_no, race_no, hiduke)
            race.trifecta_odds = odds

            # 予測
            pred = predict_race(race)

            # ポートフォリオ配分
            if odds:
                bets = allocate_portfolio(pred, budget=budget_per_race, min_ev=min_ev)
            else:
                bets = allocate_portfolio_prob(pred, budget=budget_per_race)

            rp = RacePrediction(
                race=race,
                race_type_tag=pred["race_type_tag"],
                scores=pred["scores"],
                bets=bets,
                total_bet=sum(b.bet_amount for b in bets),
                expected_return=sum(b.prob * b.odds * b.bet_amount for b in bets),
            )
            results.append(rp)

            # フラグ表示
            ev_flag = f"EV{rp.top_ev:.2f}" if rp.top_ev > 0 else "---"
            bet_flag = f"{rp.bet_count}点" if rp.bets else "SKIP"
            print(f"  {race_no:>2}R [{rp.race_type_tag}] {ev_flag} {bet_flag}")

    return results


# ============================================================
# レポート生成
# ============================================================

def generate_race_report(rp: RacePrediction) -> str:
    """1レースの予測レポート (Discord通知用テキスト)"""
    race = rp.race
    lines = []
    lines.append(f"🏁 **{race.venue_name} {race.race_no}R** [{rp.race_type_tag}]")

    # スコアテーブル
    for r in sorted(race.racers, key=lambda x: x.ev_score, reverse=True):
        star = "⭐" if r.waku == 1 and r.course_nigeritsu > 0.4 else ""
        lines.append(
            f"  {r.waku}枠 {r.name}({r.grade}) "
            f"Score={r.ev_score:.1f} C1着={r.course_1chaku_rate:.0%} "
            f"M={r.motor_shisuu:+.2f} {star}"
        )

    if rp.bets:
        lines.append(f"\n📊 **推奨買い目** ({rp.bet_count}点 計{rp.total_bet}円)")
        for b in rp.bets:
            lines.append(f"  {b.combo} {b.odds:.1f}倍 EV={b.ev:.2f} → {b.bet_amount}円")
        lines.append(f"  期待リターン: {rp.expected_return:.0f}円")
    else:
        lines.append("\n❌ EV > 1.0 なし → SKIP")

    return "\n".join(lines)


# ============================================================
# CLI テスト
# ============================================================

if __name__ == "__main__":
    import sys
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

    from scraper import BoatraceScraper

    scraper = BoatraceScraper()

    TEST_PLACE = 3
    TEST_RACE = 1
    TEST_DATE = "20260317"

    print("=" * 60)
    print("Boatrace AI - Strategy Test")
    print("=" * 60)

    # 1レース取得
    race = scraper.scrape_full_race(TEST_PLACE, TEST_RACE, TEST_DATE, include_result=True)
    if not race:
        print("Failed")
        sys.exit(1)

    # オッズ取得
    odds = scraper.scrape_odds(TEST_PLACE, TEST_RACE, TEST_DATE)
    race.trifecta_odds = odds

    # 予測
    pred = predict_race(race)
    bets = allocate_portfolio(pred, budget=1000, min_ev=0.8)

    rp = RacePrediction(
        race=race,
        race_type_tag=pred["race_type_tag"],
        scores=pred["scores"],
        bets=bets,
        total_bet=sum(b.bet_amount for b in bets),
        expected_return=sum(b.prob * b.odds * b.bet_amount for b in bets),
    )

    # レポート出力
    report = generate_race_report(rp)
    print(report)

    # ファイル保存
    with open("strategy_out.txt", "w", encoding="utf-8") as f:
        f.write(report)
    print("\nSaved to strategy_out.txt")

    # 的中チェック
    if race.result_order:
        actual = f"{race.result_order[0]}-{race.result_order[1]}-{race.result_order[2]}"
        hit = any(b.combo == actual for b in rp.bets)
        print(f"\n結果: {actual} {race.result_kimarite} {race.result_payout}円")
        print(f"的中: {'✅ HIT!' if hit else '❌ MISS'}")
        if hit:
            hit_bet = next(b for b in rp.bets if b.combo == actual)
            profit = race.result_payout * hit_bet.bet_amount / 100 - rp.total_bet
            print(f"収支: +{profit:.0f}円 (払戻{race.result_payout * hit_bet.bet_amount / 100:.0f}円 - 投資{rp.total_bet}円)")
