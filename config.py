# -*- coding: utf-8 -*-
"""
ボートレース予想AI - 設定定数
kyoteibiyori.com (ボートレース日和) 用
"""

# ============================================================
# kyoteibiyori.com URL 設定
# ============================================================
BASE_URL = "https://kyoteibiyori.com"

# 出走表 (slider パラメータでタブ切替)
RACE_SHUSSO_URL = f"{BASE_URL}/race_shusso.php?place_no={{place_no}}&race_no={{race_no}}&hiduke={{hiduke}}&slider={{slider}}"

# レース一覧
RACE_ICHIRAN_URL = f"{BASE_URL}/race_ichiran.php?place_no={{place_no}}&race_no={{race_no}}&hiduke={{hiduke}}"

# 本日の開催一覧
KAISAI_TODAY_URL = f"{BASE_URL}/schedule/kaisai_today.php?hiduke={{hiduke}}"

# 全場出走一覧 (トップページ)
TOP_URL = BASE_URL

# ============================================================
# slider パラメータ
# ============================================================
SLIDER_BASIC    = 0   # 基本情報 (選手名, 勝率, 連対率, 級別)
SLIDER_WAKU     = 1   # 枠別勝率
SLIDER_MOTOR    = 2   # モータ比較
SLIDER_KONSETSU = 3   # 今節成績
SLIDER_CHOKUZEN = 4   # 直前情報 (展示タイム, チルト, ST展示)
SLIDER_ODDS     = 5   # オッズ
SLIDER_RESULT   = 7   # 結果 (着順, 決まり手, 払戻金)
SLIDER_DEME     = 8   # 出目ランク

# ============================================================
# 24場コードマッピング
# ============================================================
VENUE_MAP = {
    1:  "桐生",    2:  "戸田",    3:  "江戸川",
    4:  "平和島",  5:  "多摩川",  6:  "浜名湖",
    7:  "蒲郡",    8:  "常滑",    9:  "津",
    10: "三国",    11: "びわこ",  12: "住之江",
    13: "尼崎",    14: "鳴門",    15: "丸亀",
    16: "児島",    17: "宮島",    18: "徳山",
    19: "下関",    20: "若松",    21: "芦屋",
    22: "福岡",    23: "唐津",    24: "大村",
}

# 逆引き
VENUE_NAME_TO_CODE = {v: k for k, v in VENUE_MAP.items()}

# ============================================================
# ベッティング設定
# ============================================================
BET_TYPE = "3rentan"        # 3連単のみ
NUM_BOATS = 6               # 6艇固定
TRIFECTA_COMBOS = 120       # 6P3 = 6*5*4 = 120通り

# ポートフォリオ配分設定 (Phase 3 で使用)
BET_BASE = 100              # 最小ベット単位 (円)
TOP_N_BETS = 10             # 上位N組に配分
CROSS_MULTIPLIER = 1        # 戦略交差倍率 (デフォルト)

# ============================================================
# スクレイピング設定
# ============================================================
REQUEST_DELAY = 1.0          # リクエスト間隔 (秒)
REQUEST_TIMEOUT = 15         # タイムアウト (秒)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
}

# ============================================================
# Discord 通知 (Phase 4 で使用)
# ============================================================
# DISCORD_WEBHOOK_BOATRACE = os.environ.get("DISCORD_WEBHOOK_BOATRACE", "")

# ============================================================
# スコアリング重み (Phase 2 で使用, チューニング可能)
# ============================================================
WEIGHT_COURSE      = 0.30   # コース有利度 (場別コース1着率)
WEIGHT_ZENKOKU     = 0.25   # 全国勝率
WEIGHT_TOCHI       = 0.10   # 当地勝率
WEIGHT_MOTOR       = 0.15   # モータ性能
WEIGHT_RECENT      = 0.10   # 直近勝率
WEIGHT_ST          = 0.10   # 平均ST
