# -*- coding: utf-8 -*-
"""
ボートレース予想AI - kyoteibiyori.com APIベーススクレイパー

データ取得フロー:
  1. race_shusso.php にGETリクエスト → HTMLからCSRF_TOKEN + ページ変数を抽出
  2. request_race_shusso_detail_v4.php にPOSTリクエスト → JSONでレースデータ取得

APIレスポンス race_list 主要フィールド (1516キー中の主要):
  player_name, player_no, kyubetsu, age, shibu,
  zenkoku_shoritsu, touchi_shoritsu, zenkoku_niren, touchi_niren,
  motor, motor_niren, boat, boat_niren,
  ave_start, course{1-6}_1_ave, course{1-6}_2_ave, course{1-6}_3_ave
"""
import re
import json
import time
import requests
from typing import List, Optional, Dict
from datetime import datetime

from config import (
    BASE_URL, HEADERS, REQUEST_DELAY, REQUEST_TIMEOUT,
    VENUE_MAP, NUM_BOATS,
)
from models import Racer, Race, VenueDay, TodaySchedule


# ============================================================
# ユーティリティ
# ============================================================

def _safe_float(val, default: float = 0.0) -> float:
    """値をfloatに安全変換"""
    if val is None:
        return default
    try:
        if isinstance(val, (int, float)):
            return float(val)
        cleaned = re.sub(r"[^\d.\-]", "", str(val).strip())
        return float(cleaned) if cleaned else default
    except (ValueError, AttributeError):
        return default


def _safe_int(val, default: int = 0) -> int:
    """値をintに安全変換"""
    if val is None:
        return default
    try:
        if isinstance(val, int):
            return val
        cleaned = re.sub(r"[^\d\-]", "", str(val).strip())
        return int(cleaned) if cleaned else default
    except (ValueError, AttributeError):
        return default


class BoatraceScraper:
    """kyoteibiyori.com APIベーススクレイパー"""

    API_URL = f"{BASE_URL}/request_race_shusso_detail_v4.php"
    PAGE_URL = f"{BASE_URL}/race_shusso.php"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self._csrf_cache: Dict[str, str] = {}  # url -> csrf_token

    def _get_csrf_and_params(self, place_no: int, race_no: int, hiduke: str) -> Optional[dict]:
        """
        出走表ページにアクセスしてCSRFトークンとJS変数を取得

        Returns:
            {csrf_token, kaisai_key, race_name, season, term, grade, type, taikai_count, group_no}
        """
        url = f"{self.PAGE_URL}?place_no={place_no}&race_no={race_no}&hiduke={hiduke}&slider=0"
        try:
            resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            html = resp.text
            time.sleep(REQUEST_DELAY)
        except Exception as e:
            print(f"[FETCH ERROR] {url}: {e}")
            return None

        result = {}

        # CSRF_TOKEN 抽出 (var CSRF_TOKEN = "xxx";)
        csrf_match = re.search(r'var\s+CSRF_TOKEN\s*=\s*["\']([^"\']+)', html)
        if csrf_match:
            result["csrf_token"] = csrf_match.group(1)
        else:
            print(f"[WARN] CSRF_TOKEN not found")
            return None

        # ページ変数抽出 (var m_Xxx = "yyy";)
        var_patterns = {
            "kaisai_key": r'var\s+m_Kaisai_key\s*=\s*["\']([^"\']*)',
            "race_name":  r'var\s+m_RaceName\s*=\s*["\']([^"\']*)',
            "season":     r'var\s+m_Season\s*=\s*["\']([^"\']*)',
            "term":       r'var\s+m_Term\s*=\s*["\']([^"\']*)',
            "grade":      r'var\s+m_Grade\s*=\s*["\']([^"\']*)',
            "type":       r'var\s+m_Type\s*=\s*["\']([^"\']*)',
            "taikai_count": r'var\s+m_Taikai_count\s*=\s*["\']([^"\']*)',
            "group_no":   r'var\s+m_Group_no\s*=\s*["\']([^"\']*)',
        }

        for key, pattern in var_patterns.items():
            m = re.search(pattern, html)
            result[key] = m.group(1) if m else ""

        return result

    def _call_api(self, place_no: int, race_no: int, hiduke: str,
                  params: dict) -> Optional[dict]:
        """
        request_race_shusso_detail_v4.php を呼び出してJSONを取得
        """
        data_payload = json.dumps({
            "place_no": str(place_no),
            "race_no": str(race_no),
            "hiduke": hiduke,
            "race_name": params.get("race_name", ""),
            "season": params.get("season", ""),
            "term": params.get("term", ""),
            "kaisai_key": params.get("kaisai_key", ""),
            "taikai_count": params.get("taikai_count", ""),
            "group_no": params.get("group_no", ""),
            "type": params.get("type", ""),
            "grade": params.get("grade", ""),
        })

        form_data = {
            "data": data_payload,
            "token": params["csrf_token"],
        }

        try:
            resp = self.session.post(
                self.API_URL,
                data=form_data,
                headers={
                    **HEADERS,
                    "X-Requested-With": "XMLHttpRequest",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            time.sleep(REQUEST_DELAY)
            return resp.json()
        except Exception as e:
            print(f"[API ERROR] {e}")
            return None

    def _parse_racer(self, data: dict, waku: int) -> Racer:
        """APIレスポンスの1艇分のデータをRacerオブジェクトに変換"""
        r = Racer(waku=waku)
        r.name = str(data.get("player_name", "")).strip()
        r.touroku_no = str(data.get("player_no", "")).strip()
        r.grade = str(data.get("kyubetsu", "")).strip()

        # === 勝率 ===
        r.zenkoku_rate = _safe_float(data.get("zenkoku_shoritsu"))
        r.tochi_rate = _safe_float(data.get("touchi_shoritsu"))
        r.recent_3m_rate = _safe_float(data.get("saikin_shoritsu"))
        r.recent_1m_rate = _safe_float(data.get("one_month_shoritsu"))
        r.night_rate = _safe_float(data.get("shoritsu_night"))
        r.fmochi_rate = _safe_float(data.get("shoritsu_fmochi"))

        # === 連対率 ===
        r.niren_rate = _safe_float(data.get("zenkoku_niren"))
        r.tochi_niren = _safe_float(data.get("touchi_niren"))
        r.sanren_rate = _safe_float(data.get("zenkoku_sanren"))

        # === 平均ST ===
        ave_st = data.get("ave_start", "0")
        if ave_st and str(ave_st).isdigit() and len(str(ave_st)) <= 3:
            r.avg_st = int(ave_st) / 100.0
        else:
            r.avg_st = _safe_float(ave_st)
        r.st_junban = _safe_float(data.get("st_junban"))

        # === モータ ===
        r.motor_no = _safe_int(data.get("motor"))
        r.motor_rate = _safe_float(data.get("motor_niren"))
        r.motor_sanren = _safe_float(data.get("motor_sanren"))
        r.motor_rank = _safe_int(data.get("motor_rank"))
        r.motor_shisuu = _safe_float(data.get("motor_shisuu"))

        # === ボート ===
        r.boat_no = _safe_int(data.get("boat"))
        r.boat_niren = _safe_float(data.get("boat_niren"))

        # === 展示タイム (API キー名: display 系) ===
        r.tenji_time = _safe_float(data.get("display"))             # 展示タイム
        r.tilt = _safe_float(data.get("tilt"))
        r.tenji_course = _safe_int(data.get("tenji_course"))
        r.tenji_st = _safe_float(data.get("tenji_st"))
        r.konsetsu_display = _safe_float(data.get("konsetsu_display_ave"))  # 今節展示平均
        r.display_junban = _safe_float(data.get("display_junban_ave_choku3"))  # 直近展示順位

        # === 決まり手回数 ===
        r.kimete_nige = _safe_int(data.get("kimete_nige"))
        r.kimete_sashi = _safe_int(data.get("kimete_sashi"))
        r.kimete_makuri = _safe_int(data.get("kimete_makuri"))
        r.kimete_makurisashi = _safe_int(data.get("kimete_makurisashi"))
        r.kimete_nuki = _safe_int(data.get("kimete_nuki"))
        r.kimete_megumare = _safe_int(data.get("kimete_megumare"))

        # === コース別データ (枠番に対応) ===
        c = str(waku)

        # コース別入着率 (通算)
        r.course_1chaku_rate = _safe_float(data.get(f"course{c}_1_ave"))
        r.course_2chaku_rate = _safe_float(data.get(f"course{c}_2_ave"))
        r.course_3chaku_rate = _safe_float(data.get(f"course{c}_3_ave"))

        # コース別入着率 (条件別) - 直近の調子を反映
        r.course_1chaku_choku3 = _safe_float(data.get(f"course{c}_1_ave_choku3"))
        r.course_1chaku_choku1 = _safe_float(data.get(f"course{c}_1_ave_choku1"))
        r.course_1chaku_tochi = _safe_float(data.get(f"course{c}_1_ave_tochi"))
        r.course_1chaku_nomal = _safe_float(data.get(f"course{c}_1_ave_nomal"))

        # コース別勝率 (APIは100倍値: 848 → 8.48)
        raw_shoritsu = _safe_float(data.get(f"course{c}_shoritsu"))
        r.course_shoritsu = raw_shoritsu / 100.0 if raw_shoritsu > 10 else raw_shoritsu

        # コース別決まり手率
        r.course_nigeritsu = _safe_float(data.get(f"course{c}_nigeritsu"))
        r.course_sasare = _safe_float(data.get(f"course{c}_sasare"))
        r.course_makurare = _safe_float(data.get(f"course{c}_makurare"))
        r.course_sashi = _safe_float(data.get(f"course{c}_sashi"))
        r.course_makuri = _safe_float(data.get(f"course{c}_makuri"))
        r.course_makurisashi = _safe_float(data.get(f"course{c}_makurisashi"))
        r.course_nigashi = _safe_float(data.get(f"course{c}_nigashi"))

        # コース別ST
        course_st = data.get(f"start{c}_ave", "0")
        if course_st and str(course_st).isdigit() and len(str(course_st)) <= 3:
            r.course_avg_st = int(course_st) / 100.0
        else:
            r.course_avg_st = _safe_float(course_st)
        r.st_junban_course = _safe_float(data.get(f"st_junban_{c}"))

        return r

    # ================================================================
    # 公開API
    # ================================================================

    def scrape_today_schedule(self, hiduke: str = None) -> TodaySchedule:
        """本日の開催場一覧を取得"""
        if hiduke is None:
            hiduke = datetime.now().strftime("%Y%m%d")

        schedule = TodaySchedule(date=hiduke)

        try:
            resp = self.session.get(BASE_URL, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            time.sleep(REQUEST_DELAY)
        except Exception as e:
            print(f"[FETCH ERROR] {e}")
            return schedule

        # race_ichiran リンクからスケジュール抽出
        links = re.findall(
            r'<a[^>]*href="[^"]*race_ichiran\.php\?place_no=(\d+)&amp;race_no=(\d+)&amp;hiduke=(\d+)"[^>]*>([^<]*)</a>',
            resp.text
        )

        seen_places = set()
        for place_no_str, race_no_str, _, text in links:
            place_no = int(place_no_str)
            if place_no in seen_places:
                continue
            seen_places.add(place_no)

            if "次開催" in text:
                status = "次開催"
            elif "本日終了" in text:
                status = "本日終了"
            else:
                status = "開催中"

            day_match = re.search(r"(\d+日目|初日|最終日)", text)
            day_label = day_match.group(1) if day_match else ""
            venue_name = VENUE_MAP.get(place_no, f"場{place_no}")

            vd = VenueDay(
                place_no=place_no,
                venue_name=venue_name,
                date=hiduke,
                day_label=day_label,
                status=status,
                current_race=int(race_no_str),
            )
            schedule.venues.append(vd)

        return schedule

    def scrape_race_list(self, place_no: int, hiduke: str) -> List[dict]:
        """1場のレース一覧(1R~12R)の基本情報を取得"""
        url = f"{BASE_URL}/race_ichiran.php?place_no={place_no}&race_no=1&hiduke={hiduke}"
        try:
            resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            time.sleep(REQUEST_DELAY)
        except Exception as e:
            print(f"[FETCH ERROR] {e}")
            return []

        races = []
        # HTMLからテキストを抽出してレース情報を検索
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, "html.parser")
        page_text = soup.get_text()
        
        for m in re.finditer(r"(\d+)R\s*([^\d]*?)締切.*?(\d{1,2}:\d{2})", page_text):
            race_type = re.sub(r'[\s　]+', '', m.group(2).strip())
            race_type = re.sub(r'<[^>]+>', '', race_type)  # HTMLタグ除去
            races.append({
                "race_no": int(m.group(1)),
                "race_type": race_type,
                "deadline": m.group(3).strip(),
            })

        return races

    def scrape_full_race(self, place_no: int, race_no: int, hiduke: str,
                         include_result: bool = False,
                         race_info_cache: list = None) -> Optional[Race]:
        """
        1レースの全データをAPI経由で取得

        1回のAPI呼び出しで基本情報・モータ・勝率等の全データを取得可能

        Args:
            race_info_cache: scrape_race_list() の結果を外部キャッシュとして渡す
                             (同一場で複数Rを処理する際の重複リクエスト防止)
        """
        venue = VENUE_MAP.get(place_no, "?")
        print(f"[SCRAPE] {venue} {race_no}R ({hiduke})...")

        # 1. CSRF + ページ変数取得
        params = self._get_csrf_and_params(place_no, race_no, hiduke)
        if not params:
            print(f"  x CSRF/params failed")
            return None
        print(f"  ok csrf: {params['csrf_token'][:16]}...")

        # 2. API呼び出し
        api_data = self._call_api(place_no, race_no, hiduke, params)
        if not api_data:
            print(f"  x API call failed")
            return None

        race_list = api_data.get("race_list", [])
        print(f"  ok api: {len(race_list)} racers, keys={len(race_list[0]) if race_list else 0}")

        # 3. Race オブジェクト構築
        race = Race(
            place_no=place_no,
            race_no=race_no,
            date=hiduke,
            venue_name=venue,
            grade_name=params.get("race_name", ""),
        )

        # レース情報 (キャッシュがあればそこから、なければ取得)
        race_info_list = race_info_cache if race_info_cache is not None else self.scrape_race_list(place_no, hiduke)
        for ri in race_info_list:
            if ri["race_no"] == race_no:
                race.deadline = ri["deadline"]
                race.race_type = ri["race_type"]
                break

        # 4. 選手データ変換
        for i, racer_data in enumerate(race_list[:NUM_BOATS]):
            waku = i + 1
            racer = self._parse_racer(racer_data, waku)
            race.racers.append(racer)

        # 5. トップレベルデータ (中間整備・F休み)
        seibi = api_data.get("chukan_seibi_list")
        if seibi and isinstance(seibi, list):
            race.seibi_list = seibi
            print(f"  ok seibi: {len(seibi)} items")

        flying = api_data.get("flying_kikan_list")
        if flying and isinstance(flying, list):
            race.flying_info = flying
            print(f"  ok flying: {len(flying)} items")

        # 6. 結果 (API方式)
        if include_result:
            self._fetch_result_api(race, hiduke, params.get("csrf_token", ""))

        return race

    def _call_simple_api(self, endpoint: str, place_no: int, race_no: int,
                          hiduke: str, csrf_token: str) -> Optional[dict]:
        """シンプルなAPI呼び出し (オッズ/結果/払戻用)"""
        data_payload = json.dumps({
            "place_no": str(place_no),
            "race_no": str(race_no),
            "hiduke": hiduke,
        })
        form_data = f"data={requests.utils.quote(data_payload)}&token={requests.utils.quote(csrf_token)}"

        try:
            resp = self.session.post(
                f"{BASE_URL}/{endpoint}",
                data=form_data,
                headers={
                    **HEADERS,
                    "X-Requested-With": "XMLHttpRequest",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            time.sleep(REQUEST_DELAY)
            return resp.json()
        except Exception as e:
            print(f"  x {endpoint} error: {e}")
            return None

    def _fetch_result_api(self, race: Race, hiduke: str, csrf_token: str):
        """結果をAPI経由で取得

        API:
          - request_race_kekka.php → 着順・決まり手・気象
          - request_rentan_three.php → 3連単払戻金
        """
        # --- 着順・決まり手 ---
        kekka = self._call_simple_api("request_race_kekka.php",
                                       race.place_no, race.race_no, hiduke, csrf_token)
        if kekka:
            # kekka は配列 [{rank, player_name, shinnyuu, start, kimete, ...}]
            result_data = kekka if isinstance(kekka, list) else kekka.get("kekka_list", [])
            if result_data:
                sorted_results = sorted(result_data, key=lambda x: _safe_int(x.get("rank", 99)))
                race.result_order = [_safe_int(r.get("shinnyuu", r.get("waku", 0)))
                                     for r in sorted_results[:3]]

                # 決まり手 (1着の選手のkimeteフィールド)
                first = sorted_results[0] if sorted_results else {}
                race.result_kimarite = str(first.get("kimete", "")).strip()

                print(f"  ok kekka: {race.result_order} {race.result_kimarite}")
            else:
                print(f"  warn kekka: empty result")
        else:
            print(f"  x kekka API failed")

        # --- 3連単払戻金 ---
        payout_data = self._call_simple_api("request_rentan_three.php",
                                             race.place_no, race.race_no, hiduke, csrf_token)
        if payout_data:
            # payout_data は配列かオブジェクト
            pdata = payout_data[0] if isinstance(payout_data, list) and payout_data else payout_data
            if isinstance(pdata, dict):
                race.result_payout = _safe_int(pdata.get("kingaku", 0))

                # 着順が未取得の場合、払戻金データからも取得可能
                if not race.result_order:
                    c1 = _safe_int(pdata.get("course1"))
                    c2 = _safe_int(pdata.get("course2"))
                    c3 = _safe_int(pdata.get("course3"))
                    if c1 and c2 and c3:
                        race.result_order = [c1, c2, c3]

                print(f"  ok payout: {race.result_payout}yen (人気{pdata.get('ninki', '?')})")
            else:
                print(f"  warn payout: unexpected format")
        else:
            print(f"  x payout API failed")

    def scrape_odds(self, place_no: int, race_no: int, hiduke: str,
                    csrf_token: str = None) -> Dict[str, float]:
        """3連単オッズ(120通り)をAPI経由で取得

        API: request_odds_shousai.php
        レスポンス: odds_list[{type, kumiawase, odds}]
          - type: "3t"=3連単, "2t"=2連単, "3f"=3連複 等
          - kumiawase: "135" → 1-3-5
          - odds: "9.9" (文字列)
        """
        # CSRF未指定の場合は新規取得
        if not csrf_token:
            params = self._get_csrf_and_params(place_no, 1, hiduke)
            csrf_token = params.get("csrf_token", "") if params else ""

        odds_json = self._call_simple_api("request_odds_shousai.php",
                                           place_no, race_no, hiduke, csrf_token)
        if not odds_json:
            print(f"  x odds API failed")
            return {}

        odds_list = odds_json.get("odds_list", [])
        trifecta_odds = {}

        for item in odds_list:
            if item.get("type") != "3t":
                continue
            kumi = str(item.get("kumiawase", ""))
            if len(kumi) == 3:
                combo = f"{kumi[0]}-{kumi[1]}-{kumi[2]}"
                odds_val = _safe_float(item.get("odds", "0"))
                if odds_val > 0:
                    trifecta_odds[combo] = odds_val

        print(f"  ok odds: {len(trifecta_odds)} 3連単 combos")
        return trifecta_odds


# ============================================================
# CLI テスト
# ============================================================

if __name__ == "__main__":
    import sys
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

    print("=" * 60)
    print("Boatrace AI - API Scraper Test")
    print("=" * 60)

    scraper = BoatraceScraper()

    # テスト: 江戸川 1R (本日のレース)
    TEST_PLACE = 3   # 江戸川
    TEST_RACE = 1
    TEST_DATE = "20260317"

    print(f"\n--- Schedule ({TEST_DATE}) ---")
    schedule = scraper.scrape_today_schedule(TEST_DATE)
    print(f"Date: {schedule.date}, Venues: {len(schedule.venues)}")
    for v in schedule.venues:
        icon = "OPEN" if v.status == "開催中" else "DONE" if v.status == "本日終了" else "NEXT"
        print(f"  [{icon}] {v.venue_name} {v.day_label} R{v.current_race}")

    print(f"\n--- Full Race: {VENUE_MAP[TEST_PLACE]} {TEST_RACE}R ---")
    race = scraper.scrape_full_race(TEST_PLACE, TEST_RACE, TEST_DATE, include_result=True)

    if race:
        print(f"\n{race.venue_name} {race.race_no}R [{race.grade_name}]")
        print(f"Deadline: {race.deadline}  Type: {race.race_type}")
        print(f"\n{'Wk':>2} | {'Name':<10} | {'Gr':>2} | {'Z_Rate':>6} | {'T_Rate':>6} | "
              f"{'ST':>5} | {'Mtr':>4} | {'M_2R':>5} | {'2Ren':>5} | {'T2Ren':>5}")
        print("-" * 80)
        for r in race.racers:
            print(f"{r.waku:>2} | {r.name:<10} | {r.grade:>2} | {r.zenkoku_rate:>6.2f} | "
                  f"{r.tochi_rate:>6.2f} | {r.avg_st:>5.2f} | {r.motor_no:>4} | "
                  f"{r.motor_rate:>5.1f} | {r.niren_rate:>5.1f} | {r.tochi_niren:>5.1f}")

        if race.result_order:
            print(f"\nResult: {'-'.join(map(str, race.result_order))} "
                  f"({race.result_kimarite}) {race.result_payout:,}yen")
    else:
        print("Failed to get race data")
