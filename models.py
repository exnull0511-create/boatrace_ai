# -*- coding: utf-8 -*-
"""
ボートレース予想AI - データモデル定義
"""
from dataclasses import dataclass, field
from typing import List, Optional, Dict


@dataclass
class Racer:
    """1レースにおける1名の選手情報"""
    waku: int                          # 枠番 (1-6)
    touroku_no: str = ""               # 登録番号 (4桁)
    name: str = ""                     # 選手名
    grade: str = ""                    # 級別 (A1/A2/B1/B2)

    # --- 勝率 ---
    zenkoku_rate: float = 0.0          # 全国勝率
    tochi_rate: float = 0.0            # 当地勝率
    recent_3m_rate: float = 0.0        # 直近3ヶ月勝率
    recent_1m_rate: float = 0.0        # 直近1ヶ月勝率
    night_rate: float = 0.0            # ナイター勝率
    fmochi_rate: float = 0.0           # F持ち時勝率

    # --- 連対率 ---
    niren_rate: float = 0.0            # 全国2連対率 (%)
    tochi_niren: float = 0.0           # 当地2連対率 (%)
    sanren_rate: float = 0.0           # 全国3連対率 (%)

    # --- ST (スタートタイミング) ---
    avg_st: float = 0.0                # 平均ST (秒) 小さいほど良い
    st_junban: float = 0.0             # 平均ST順位 (小さいほど速い)

    # --- モータ ---
    motor_no: int = 0                  # モータ番号
    motor_rate: float = 0.0            # モータ2連対率 (%)
    motor_sanren: float = 0.0          # モータ3連対率 (%)
    motor_rank: int = 0                # モータ場内ランク (1が最上位)
    motor_shisuu: float = 0.0          # モーター指数 (サイト独自評価)

    # --- ボート ---
    boat_no: int = 0                   # ボート番号
    boat_niren: float = 0.0            # ボート2連対率 (%)

    # --- 直前情報 (展示) ---
    tenji_time: float = 0.0            # 展示タイム (秒)
    tilt: float = 0.0                  # チルト角度
    tenji_course: int = 0              # 展示コース (進入)
    tenji_st: float = 0.0              # 展示ST
    konsetsu_display: float = 0.0      # 今節展示タイム平均
    display_junban: float = 0.0        # 直近3M展示順位平均

    # --- 選手決まり手回数 ---
    kimete_nige: int = 0               # 逃げ回数
    kimete_sashi: int = 0              # 差し回数
    kimete_makuri: int = 0             # 捲り回数
    kimete_makurisashi: int = 0        # 捲り差し回数
    kimete_nuki: int = 0               # 抜き回数
    kimete_megumare: int = 0           # 恵まれ回数

    # --- コース別入着率 (当該レースの枠に対応するデータ) ---
    course_1chaku_rate: float = 0.0    # 当該コースでの1着率 (通算)
    course_2chaku_rate: float = 0.0    # 当該コースでの2着率
    course_3chaku_rate: float = 0.0    # 当該コースでの3着率
    course_1chaku_choku3: float = 0.0  # 直近3ヶ月コース別1着率
    course_1chaku_choku1: float = 0.0  # 直近1ヶ月コース別1着率
    course_1chaku_tochi: float = 0.0   # 当地コース別1着率
    course_1chaku_nomal: float = 0.0   # 一般戦コース別1着率
    course_shoritsu: float = 0.0       # コース別勝率

    # --- コース別決まり手率 ---
    course_nigeritsu: float = 0.0      # 逃げ率 (主に1コース)
    course_sasare: float = 0.0         # 差され率 (被弾/1コース)
    course_makurare: float = 0.0       # 捲られ率 (被弾/1コース)
    course_sashi: float = 0.0          # 差し率
    course_makuri: float = 0.0         # 捲り率
    course_makurisashi: float = 0.0    # 捲り差し率
    course_nigashi: float = 0.0        # 逃がし率 (2コース等)

    # --- コース別平均ST ---
    course_avg_st: float = 0.0         # 当該コースでの平均ST
    st_junban_course: float = 0.0      # 当該コースでのST順位

    # --- 計算済みスコア (Phase 2 で設定) ---
    ev_score: float = 0.0              # EV スコア

    def __repr__(self):
        return f"Racer({self.waku}枠 {self.name} {self.grade} 勝率{self.zenkoku_rate})"


@dataclass
class Race:
    """1レースの情報"""
    place_no: int                      # 場コード (1-24)
    race_no: int                       # レース番号 (1-12)
    date: str                          # 日付 (yyyymmdd)
    venue_name: str = ""               # 場名
    grade_name: str = ""               # 大会名
    race_type: str = ""                # レース種別 (予選/準優/優勝戦等)
    deadline: str = ""                 # 締切時刻 (HH:MM)

    racers: List[Racer] = field(default_factory=list)  # 6名の選手

    # --- オッズ ---
    trifecta_odds: Dict[str, float] = field(default_factory=dict)
    # キー: "1-2-3", "1-3-2", ... 120通り

    # --- 結果 ---
    result_order: List[int] = field(default_factory=list)   # 着順 [1着艇番, 2着, 3着]
    result_kimarite: str = ""                                 # 決まり手
    result_payout: int = 0                                    # 3連単払戻金 (円)

    # --- 中間整備・F情報 ---
    seibi_list: List[dict] = field(default_factory=list)     # 中間整備 [{waku, 部品, 交換日}]
    flying_info: List[dict] = field(default_factory=list)    # F休み情報 [{waku, 期間}]

    @property
    def race_id(self) -> str:
        """場_レース番号_日付のユニークID"""
        return f"{self.place_no:02d}_{self.race_no:02d}_{self.date}"

    def __repr__(self):
        return f"Race({self.venue_name} {self.race_no}R {self.date})"


@dataclass
class VenueDay:
    """1日の1場の開催情報"""
    place_no: int
    venue_name: str
    date: str
    day_label: str = ""                # "初日", "2日目", "最終日" 等
    grade_name: str = ""               # 大会名
    num_races: int = 12                # レース数 (通常12R)
    status: str = ""                   # "開催中", "本日終了", "次開催" 等
    current_race: int = 0              # 現在のレース番号

    races: List[Race] = field(default_factory=list)

    def __repr__(self):
        return f"VenueDay({self.venue_name} {self.day_label} {self.date})"


@dataclass
class TodaySchedule:
    """本日の全場開催スケジュール"""
    date: str
    venues: List[VenueDay] = field(default_factory=list)

    @property
    def active_venues(self) -> List[VenueDay]:
        """開催中の場のみ"""
        return [v for v in self.venues if v.status not in ("次開催",)]

    def __repr__(self):
        return f"TodaySchedule({self.date} {len(self.venues)}場)"
