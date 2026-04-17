#!/usr/bin/env python3
"""
garmin_to_feishu.py — Sync Garmin sleep + activity data to Feishu Calendar

Usage:
  python3 garmin_to_feishu.py --proxy-url http://127.0.0.1:PORT --proxy-token lmk_xxx [--date YYYY-MM-DD] [--dry-run]

Reads:
  ~/.garminconnect  — Garmin cached OAuth tokens (garth)

Feishu access is via the lark-mcp token proxy — issue a token first with
feishu_auth_issue_token, then pass proxy-url and proxy-token here.

Creates Feishu calendar events for:
  - Sleep window (start → end, from Garmin sleep data)
  - Each activity (running, cycling, etc.)

Events are tagged with [Garmin] prefix so they can be identified/updated.
"""

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

# ── Config ────────────────────────────────────────────────────────────────────

GARMIN_TOKENSTORE = os.path.expanduser("~/.garminconnect")
STATE_FILE        = os.path.expanduser("~/.garmin-feishu-synced.json")
CST               = ZoneInfo("Asia/Shanghai")

ACTIVITY_EMOJI = {
    "running":        "🏃",
    "cycling":        "🚴",
    "swimming":       "🏊",
    "strength_training": "🏋️",
    "walking":        "🚶",
    "yoga":           "🧘",
    "elliptical":     "🏃",
}


# ── Local dedup state ─────────────────────────────────────────────────────────

def load_state() -> dict:
    """Load synced-events state from local JSON file."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def is_duplicate(state: dict, date_key: str, start_ts: int, end_ts: int) -> bool:
    """Return True if an event with identical time range was already synced."""
    for entry in state.get(date_key, []):
        if entry.get("start_ts") == start_ts and entry.get("end_ts") == end_ts:
            return True
    return False


def record_event(state: dict, date_key: str, summary: str,
                 start_ts: int, end_ts: int, event_id: str):
    state.setdefault(date_key, []).append({
        "summary":  summary,
        "start_ts": start_ts,
        "end_ts":   end_ts,
        "event_id": event_id,
    })


# ── Garmin client ─────────────────────────────────────────────────────────────

def load_garmin_client():
    import garminconnect
    client = garminconnect.Garmin(is_cn=True)
    client.garth.load(GARMIN_TOKENSTORE)
    return client


# ── Feishu API helpers ────────────────────────────────────────────────────────

def feishu_get(base: str, path: str, token: str, params: dict = None):
    r = requests.get(
        f"{base}{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params or {},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def feishu_post(base: str, path: str, token: str, body: dict):
    r = requests.post(
        f"{base}{path}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=body,
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def get_primary_calendar_id(base: str, token: str) -> str:
    resp = feishu_get(base, "/calendar/v4/calendars/primary", token)
    d = resp.get("data", {})
    # Try flat structure first, then nested
    if "calendar_id" in d:
        return d["calendar_id"]
    calendars = d.get("calendars", [])
    if calendars:
        return calendars[0]["calendar"]["calendar_id"]
    raise RuntimeError(f"Cannot find calendar_id in: {d}")


def create_event(base: str, calendar_id: str, token: str, summary: str,
                 start_dt: datetime, end_dt: datetime,
                 description: str = "", dry_run: bool = False,
                 state: dict = None, date_key: str = "") -> str:
    """Create a calendar event. Returns event_id, 'dry-run', 'skipped', or ''."""
    # start_dt / end_dt are timezone-aware CST datetimes.
    # .timestamp() returns the correct UTC Unix timestamp regardless of server tz.
    start_ts = int(start_dt.timestamp())
    end_ts   = int(end_dt.timestamp())

    # Dedup check
    if state is not None and date_key:
        if is_duplicate(state, date_key, start_ts, end_ts):
            print(f"  [SKIP] Already synced: {summary}  {start_dt:%H:%M}–{end_dt:%H:%M}")
            return "skipped"

    body = {
        "summary": summary,
        "description": description,
        "start_time": {"timestamp": str(start_ts), "timezone": "Asia/Shanghai"},
        "end_time":   {"timestamp": str(end_ts),   "timezone": "Asia/Shanghai"},
        "visibility": "private",
    }
    if dry_run:
        print(f"  [DRY-RUN] Would create: {summary}  {start_dt:%H:%M}–{end_dt:%H:%M}")
        return "dry-run"

    resp = feishu_post(base, f"/calendar/v4/calendars/{calendar_id}/events", token, body)
    if resp.get("code") == 0:
        event_id = resp["data"]["event"]["event_id"]
        print(f"  Created: {summary}  {start_dt:%H:%M}–{end_dt:%H:%M}  (id={event_id})")
        if state is not None and date_key:
            record_event(state, date_key, summary, start_ts, end_ts, event_id)
        return event_id
    else:
        print(f"  ERROR creating event: {resp}")
        return ""


# ── Sleep sync ────────────────────────────────────────────────────────────────

def sync_sleep(base: str, garmin_client, calendar_id: str, token: str,
               target_date: date, dry_run: bool, state: dict):
    """Pull sleep data and create a calendar event."""
    print(f"\n[Sleep] Fetching for {target_date}...")
    data = garmin_client.get_sleep_data(target_date.isoformat())
    dto = data.get("dailySleepDTO", {}) if data else {}

    start_ms = dto.get("sleepStartTimestampLocal")
    end_ms   = dto.get("sleepEndTimestampLocal")
    if not start_ms or not end_ms:
        print("  No sleep data available.")
        return

    # sleepStartTimestampLocal encodes CST wall-clock time as-if-UTC ms.
    # Parse as UTC then swap tzinfo to CST (no conversion) to get correct local time.
    start_dt = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).replace(tzinfo=CST)
    end_dt   = datetime.fromtimestamp(end_ms   / 1000, tz=timezone.utc).replace(tzinfo=CST)
    total_h  = (end_ms - start_ms) / 3_600_000
    deep_m   = (dto.get("deepSleepSeconds", 0) or 0) // 60
    rem_m    = (dto.get("remSleepSeconds", 0) or 0) // 60
    score    = (dto.get("sleepScores", {}) or {}).get("overall", {}).get("value", "?")

    summary = f"[Garmin] 睡眠 {total_h:.1f}h  评分{score}"
    desc    = (f"深睡 {deep_m}min  REM {rem_m}min\n"
               f"数据来源：Garmin Connect  日期：{target_date}")

    create_event(base, calendar_id, token, summary, start_dt, end_dt, desc, dry_run,
                 state=state, date_key=target_date.isoformat())


# ── Activity sync ──────────────────────────────────────────────────────────────

def sync_activities(base: str, garmin_client, calendar_id: str, token: str,
                    target_date: date, dry_run: bool, state: dict):
    """Pull activities and create calendar events."""
    print(f"\n[Activity] Fetching for {target_date}...")
    acts = garmin_client.get_activities_by_date(
        target_date.isoformat(), target_date.isoformat()
    )
    if not acts:
        print("  No activities.")
        return

    for a in acts:
        act_type = a.get("activityType", {}).get("typeKey", "other")
        emoji    = ACTIVITY_EMOJI.get(act_type, "🏅")
        name     = a.get("activityName") or act_type
        dur_s    = int(a.get("duration", 0) or 0)
        dist_m   = float(a.get("distance", 0) or 0)
        start_str = a.get("startTimeLocal", "")

        try:
            start_dt = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
        except Exception:
            print(f"  Cannot parse start time: {start_str}")
            continue

        end_dt = start_dt + timedelta(seconds=dur_s)
        dist_km = dist_m / 1000

        summary = f"[Garmin] {emoji} {name}"
        if dist_km > 0.1:
            summary += f"  {dist_km:.1f}km"
        summary += f"  {dur_s//60}min"

        avg_hr  = a.get("averageHR") or a.get("avgHr") or ""
        calories = a.get("calories", "")
        desc = (f"距离: {dist_km:.2f}km  时长: {dur_s//60}min\n"
                f"平均心率: {avg_hr}  消耗: {calories}kcal\n"
                f"类型: {act_type}  来源: Garmin Connect")

        create_event(base, calendar_id, token, summary, start_dt, end_dt, desc, dry_run,
                     state=state, date_key=target_date.isoformat())


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Sync Garmin data to Feishu Calendar")
    ap.add_argument("--proxy-url",   required=True,
                    help="lark-mcp proxy base URL, e.g. http://127.0.0.1:PORT")
    ap.add_argument("--proxy-token", required=True,
                    help="lmk_xxx token issued by feishu_auth_issue_token")
    ap.add_argument("--date",    default=None,
                    help="Target date YYYY-MM-DD (default: yesterday)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would be created without calling API")
    ap.add_argument("--force",   action="store_true",
                    help="Ignore local dedup state and re-create events")
    args = ap.parse_args()

    base  = args.proxy_url.rstrip("/") + "/open-apis"
    token = args.proxy_token

    target_date = (date.fromisoformat(args.date) if args.date
                   else date.today() - timedelta(days=1))
    print(f"Target date: {target_date}  dry_run={args.dry_run}  force={args.force}")
    print(f"Proxy: {args.proxy_url}")

    print("\nLoading Garmin client...")
    garmin = load_garmin_client()

    print("Getting primary calendar...")
    calendar_id = get_primary_calendar_id(base, token)
    print(f"  Calendar ID: {calendar_id}")

    state = {} if args.force else load_state()

    sync_sleep(base, garmin, calendar_id, token, target_date, args.dry_run, state)
    sync_activities(base, garmin, calendar_id, token, target_date, args.dry_run, state)

    if not args.dry_run:
        save_state(state)

    print("\nDone.")


if __name__ == "__main__":
    main()
