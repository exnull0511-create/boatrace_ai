# -*- coding: utf-8 -*-
"""
ボートレース予想AI - Discord通知
"""
import os
import json
import requests
from datetime import datetime, timezone


WEBHOOK_URL = os.environ.get(
    "DISCORD_WEBHOOK_BOATRACE",
    "https://discordapp.com/api/webhooks/1483677536251674695/jPmU0dMlynu_gkfftgPX6xm-QivD8t8IwUXUBHKXyIQnUi2IPt6thitfsRk0RECgOkyp"
)

# 結果・精算・日次サマリー用 (別チャンネル)
WEBHOOK_RESULTS = os.environ.get(
    "DISCORD_WEBHOOK_BOATRACE_RESULTS",
    "https://discordapp.com/api/webhooks/1483679569180360825/YIzOw3FZJ2vSMNlROmb1u9zrgW6Aa2ITrYR310uYArFxIpckgT8A1Hd1_DJ_9e0XRpEx"
)


def send_prediction(venue: str, race_no: int, race_type: str,
                    bets: list, deadline: str, is_look: bool = False,
                    look_reason: str = ""):
    """予想通知をDiscordに送信"""
    if is_look:
        color = 0x808080  # グレー
        title = f"👀 {venue} {race_no}R [LOOK]"
        desc = f"**見送り**: {look_reason}"
        embed = {
            "title": title, "description": desc, "color": color,
            "footer": {"text": f"締切 {deadline} | {race_type}"}
        }
    else:
        color_map = {"通常": 0x3498db, "混戦": 0xe74c3c, "本命崩れ": 0xf39c12}
        color = color_map.get(race_type, 0x2ecc71)
        title = f"🏁 {venue} {race_no}R [{race_type}]"

        lines = []
        total_bet = 0
        for b in bets:
            lines.append(f"`{b['combo']}` {b['odds']:.1f}倍 → {b['amount']}円")
            total_bet += b['amount']

        desc = "\n".join(lines)
        embed = {
            "title": title, "description": desc, "color": color,
            "fields": [
                {"name": "投資額", "value": f"{total_bet:,}円", "inline": True},
                {"name": "締切", "value": deadline, "inline": True},
            ],
            "footer": {"text": f"5点予想 | {race_type}プロファイル"},
            "timestamp": datetime.now(timezone.utc).isoformat()
        }

    payload = {"embeds": [embed]}
    try:
        resp = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[DISCORD] Error: {e}")
        return False


def send_result(venue: str, race_no: int, actual: str, kimarite: str,
                payout: int, hit: bool, bet_amount: int, win_amount: int):
    """結果通知"""
    pnl = win_amount - bet_amount
    if hit:
        color = 0x2ecc71
        title = f"✅ 的中! {venue} {race_no}R"
        desc = f"**{actual}** ({kimarite}) {payout:,}円\n収支: **{pnl:+,}円**"
    else:
        color = 0xe74c3c
        title = f"❌ 不的中 {venue} {race_no}R"
        desc = f"**{actual}** ({kimarite}) {payout:,}円"

    embed = {"title": title, "description": desc, "color": color,
             "timestamp": datetime.now(timezone.utc).isoformat()}
    payload = {"embeds": [embed]}
    try:
        requests.post(WEBHOOK_RESULTS, json=payload, timeout=10)
    except Exception as e:
        print(f"[DISCORD] Error: {e}")


def send_daily_summary(date: str, total_races: int, hits: int,
                       total_bet: int, total_payout: int, look_count: int):
    """日次サマリー"""
    roi = total_payout / total_bet * 100 if total_bet > 0 else 0
    pnl = total_payout - total_bet
    pnl_emoji = "📈" if pnl >= 0 else "📉"

    embed = {
        "title": f"{pnl_emoji} 日次レポート {date}",
        "color": 0x9b59b6,
        "fields": [
            {"name": "対象R", "value": f"{total_races}R", "inline": True},
            {"name": "的中", "value": f"{hits}R", "inline": True},
            {"name": "LOOK", "value": f"{look_count}R", "inline": True},
            {"name": "投資額", "value": f"{total_bet:,}円", "inline": True},
            {"name": "回収額", "value": f"{total_payout:,}円", "inline": True},
            {"name": "ROI", "value": f"{roi:.1f}%", "inline": True},
            {"name": "収支", "value": f"**{pnl:+,}円**", "inline": False},
        ],
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    payload = {"embeds": [embed]}
    try:
        requests.post(WEBHOOK_RESULTS, json=payload, timeout=10)
    except Exception as e:
        print(f"[DISCORD] Error: {e}")


if __name__ == "__main__":
    # テスト送信
    send_prediction("テスト場", 1, "通常",
                    [{"combo": "1-2-3", "odds": 5.0, "amount": 300},
                     {"combo": "1-3-2", "odds": 8.0, "amount": 200}],
                    "15:00")
