#!/usr/bin/env python3
"""
classTableLib.py — Sync NKU class schedule to Feishu Calendar

Usage:
  python3 classTableLib.py \
    --proxy-url http://127.0.0.1:PORT --proxy-token lmk_xxx \
    --username <NKU student ID> --password <NKU password>

Reads NKU EAMIS timetable and creates recurring Feishu calendar events.
Tracks created event IDs in ~/.nku-class-synced.json for cleanup on re-sync.

Feishu access is via the lark-mcp token proxy — issue a token first with
feishu_auth_issue_token, then pass --proxy-url and --proxy-token.
"""

import re
import time
import json
import argparse
import random
import hashlib
import requests
import datetime
import os
from functools import lru_cache
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from typing import Dict, Tuple, List

STATE_FILE = os.path.expanduser("~/.nku-class-synced.json")

# ---------------------------------------------------------------------------
# NKU credentials / session
# ---------------------------------------------------------------------------

user_agents = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.6.1 Safari/605.1.15',
    'Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/535.1 (KHTML, like Gecko) Chrome/14.0.835.163 Safari/535.1',
    'Mozilla/5.0 (Windows NT 6.1; WOW64; rv:6.0) Gecko/20100101 Firefox/6.0',
)

def new_session():
    session = requests.Session()
    session.headers['User-Agent'] = random.choice(user_agents)
    return session

eamurl = 'https://eamis.nankai.edu.cn'
ssourl = 'https://sso.nankai.edu.cn'
iamurl = 'https://iam.nankai.edu.cn'
refer_finder = re.compile(r'''self.location='([a-zA-Z./]+)';''')

class account:
    def __init__(self, aid, apassword):
        self.ID = aid
        self.PASSWORD = apassword

key = bytes("8bfa9ad090fbbf87e518f1ce24a93eee", encoding='utf8')
iv  = bytes("fbfae671950f423b58d49b91ff6a22b97428219c", encoding='utf8')[:16]

def getIAMenc(message):
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    padder = padding.PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(message.encode('ascii')) + padder.finalize()
    return (encryptor.update(padded) + encryptor.finalize()).hex()

def login(login_session, url, acct):
    if url.startswith(iamurl):
        headers = {}
        if 'csrf-token' in login_session.cookies:
            headers['csrf-token'] = login_session.cookies['csrf-token']
        login_session.get(url, verify=False, allow_redirects=True, headers=headers)
        data = {
            'login_scene': 'feilian',
            'account_type': 'userid',
            'password': getIAMenc(acct.PASSWORD),
            'account': acct.ID,
        }
        headers = {
            'Host': 'iam.nankai.edu.cn',
            'X-Version-Check': '0',
            'Sec-Fetch-Site': 'same-origin',
            'Origin': iamurl,
            'Content-Type': 'application/json',
            'Sec-Fetch-Dest': 'empty',
            'X-Fe-Version': '3.0.3.4344',
            'csrf-token': login_session.cookies['csrf-token'],
        }
        response = login_session.post(iamurl + "/api/v1/login?os=web", json=data,
                                      headers=headers, allow_redirects=False)
        return iamurl + json.loads(response.text)['data']['next']['link']

def eam_login(acct, session=None):
    if not session:
        session = new_session()
    response = session.get(eamurl, verify=False)
    assert '/eams/home.action' == refer_finder.search(response.text).group(1)
    response = session.get(eamurl + '/eams/home.action', verify=False, allow_redirects=False)
    next_url = eamurl + response.headers['Location']
    response = session.get(next_url, verify=False, allow_redirects=False)
    login_url = login(session, response.headers['Location'], acct)
    response = session.get(login_url)
    return session, response

# ---------------------------------------------------------------------------
# Building location → coordinates mapping
# ---------------------------------------------------------------------------

rule_list = []
def contain_constructor(*contents):
    return lambda x: any(content in x for content in contents)
def apply_rules(rules, item):
    for conditioner, values in rules:
        if conditioner(item):
            return values
    return None

rule_list.append((contain_constructor('教学基地', '停课', '在线教学'), ''))
rule_list.append((contain_constructor('二主楼'), (117.170235, 39.103638)))
rule_list.append((contain_constructor('主楼'), (117.171265, 39.102205)))
rule_list.append((contain_constructor('八里台二教'), (117.17566433656555, 39.101886135534286)))
rule_list.append((contain_constructor('八里台三教', '三教'), (117.172381, 39.102619)))
rule_list.append((contain_constructor('第五教学楼', '五教'), (117.169415, 39.102012)))
rule_list.append((contain_constructor('八里台七教'), (117.165557, 39.103987)))
rule_list.append((contain_constructor('八里台综合实验楼'), (117.176569, 39.102747)))
rule_list.append((contain_constructor('津南公教楼D'), (117.346040, 38.989386)))
rule_list.append((contain_constructor('津南公教楼C'), (117.346165, 38.988749)))
rule_list.append((contain_constructor('津南公教楼B'), (117.346062, 38.988110)))
rule_list.append((contain_constructor('津南公教楼A'), (117.345796, 38.987488)))
rule_list.append((contain_constructor('津南实验楼A', '津南综合实验楼A'), (117.348486, 38.987357)))
rule_list.append((contain_constructor('津南实验楼B', '津南综合实验楼B'), (117.348633, 38.987964)))
rule_list.append((contain_constructor('津南实验楼C', '津南综合实验楼C'), (117.348600, 38.988469)))
rule_list.append((contain_constructor('津南实验楼D', '津南综合实验楼D'), (117.348539, 38.989187)))
rule_list.append((contain_constructor('泰达'), (117.71409599813819, 39.02611400865321)))

@lru_cache(maxsize=32)
def _getCoord(name) -> Tuple[float, float]:
    return apply_rules(rule_list, name)

# ---------------------------------------------------------------------------
# Feishu API helpers (proxy-based)
# ---------------------------------------------------------------------------

def _feishu_header(proxy_token: str) -> Dict[str, str]:
    return {
        'Content-Type': 'application/json; charset=utf-8',
        'Authorization': f'Bearer {proxy_token}',
    }

def _fetchMainCalID(base: str, proxy_token: str) -> str:
    url = f'{base}/calendar/v4/calendars/primary'
    res = requests.post(url, params={'user_id_type': 'user_id'},
                        headers=_feishu_header(proxy_token)).json()
    if res['code']:
        raise ValueError(f"Error fetching calendar: {res['msg']}")
    d = res.get('data', {})
    if 'calendar_id' in d:
        return d['calendar_id']
    return d['calendars'][0]['calendar']['calendar_id']

def _deleteEvent(base: str, proxy_token: str, calendar_id: str, event_id: str):
    url = f'{base}/calendar/v4/calendars/{calendar_id}/events/{event_id}'
    requests.delete(url, headers=_feishu_header(proxy_token),
                    params={'need_notifications': False})

def _convertTimeStringToTimestamp(time_string: str) -> int:
    return int(time.mktime(time.strptime(time_string, '%Y-%m-%d,%H:%M')))

def _createEvent(base: str, proxy_token: str, calendar_id: str,
                 summary: str, location: str,
                 start_date: str, start_time: str,
                 end_date: str, end_time: str, rrule: str) -> str:
    url = f'{base}/calendar/v4/calendars/{calendar_id}/events'
    event = {
        'summary': summary,
        'start_time': {'timestamp': _convertTimeStringToTimestamp(f'{start_date},{start_time}')},
        'end_time':   {'timestamp': _convertTimeStringToTimestamp(f'{end_date},{end_time}')},
        'vchat': {'vc_type': 'no_meeting'},
        'reminders': [{'type': 'popup', 'minutes': 15}],
    }
    coords = _getCoord(location)
    if not coords:
        event['location'] = {'name': location}
    else:
        event['location'] = {'name': location, 'latitude': coords[1], 'longitude': coords[0]}
    if rrule:
        event['recurrence'] = rrule
    res = requests.post(url, json=event, headers=_feishu_header(proxy_token)).json()
    if res['code']:
        raise ValueError(f"Error creating event: {res['msg']}")
    return res['data']['event']['event_id']

# ---------------------------------------------------------------------------
# Timetable parsing
# ---------------------------------------------------------------------------

tab_id_finder    = re.compile(r"""bg.form.addInput\(form,\"ids\",\"(\d+)\"\);""")
teacher_finder   = re.compile(r"""\{id:\d+,name\:\"([^"]+)\",lab""")
paragraph_finder = re.compile(
    r"""var actTeachers = (?P<Teacher_List>\[(?:\{id:\d+,name:\"[^\"]+\",[a-z\:]+\},?)+\]);"""
    r"""[^\r]*?TaskActivity\(actTeacherId\.join\(\',\'\),actTeacherName\.join\(\',\'\),"""
    r"""\"(?P<class_id>[^\"]+)\",\"(?P<class_name>[^\"]+)\"\,\"(?P<loc_id>[^\"]+)\","""
    r"""\"(?P<location>[^\"]+)\",\"(?P<occurence>[01]+)\",(?:[^,]+),(?:[^,]+),"""
    r"""assistantName,\"(?P<item_name>[^\"]*)\",\"(?P<group_no>[^\"]*)\"\)\;\n\t+"""
    r"""(?P<times>(?:index =\d\*unitCount\+\d{1,2};\n[^\n]+\n\t+)+)"""
)
time_finder = re.compile(r"""index =(\d)\*unitCount\+(\d+)""")

CLASS_TIME_S = ['08:00','08:55','10:00','10:55','12:00','12:55',
                '14:00','14:55','16:00','16:55','18:30','19:25','20:20','21:15']
CLASS_TIME_E = ['08:45','09:40','10:45','11:40','12:45','13:40',
                '14:45','15:40','16:45','17:40','19:15','20:10','21:05','22:00']

def _parse_time(time_tuple):
    ptime = sorted(list(map(lambda x: tuple(map(int, x)), time_tuple)))
    wkday = ptime[0][0]
    for i, _ in ptime:
        assert i == wkday, "Not in the same day of the week"
    for idx, t in enumerate(ptime):
        if idx:
            assert ptime[idx-1][1]+1 == t[1], "Not consecutive!"
    return wkday, CLASS_TIME_S[ptime[0][1]], CLASS_TIME_E[ptime[-1][1]]

def _fetch_occurrence(occurrence):
    occurrence = list(map(int, occurrence))
    occurrences = []
    for i, j in enumerate(occurrence):
        if not i:
            assert j == 0
            continue
        if occurrence[i-1] < occurrence[i]:
            start = i
        elif occurrence[i-1] > occurrence[i]:
            end = i - 1
            occurrences.append((start, end))
    return occurrences

def _scrape_timetable(session, semester_id: str) -> str:
    """Scrape raw timetable HTML from NKU EAMIS."""
    tab_url = 'https://eamis.nankai.edu.cn/eams/courseTableForStd'

    def get_table(tab_id):
        return session.post(
            f'{tab_url}!courseTable.action',
            data={'ignoreHead': 1, 'startWeek': '', 'ids': tab_id,
                  'setting.kind': 'std', 'semester.id': semester_id},
        ).text

    contents = ''
    try:
        content = session.get(f'{tab_url}.action?_={int(time.mktime(time.gmtime()))}').text
        tab_id = tab_id_finder.search(content).group(1)
        contents += get_table(tab_id)
    except AttributeError:
        for proj in (1, 2):
            session.get(f'{tab_url}!index.action?projectId={proj}')
            content = session.get(f'{tab_url}!innerIndex.action?projectId={proj}').text
            tab_id = tab_id_finder.search(content).group(1)
            contents += get_table(tab_id)
            if proj == 1:
                time.sleep(0.2)
    assert '没有权限' not in contents
    return contents

def _parse_class_list(contents: str) -> list:
    class_list = []
    for classes in paragraph_finder.findall(contents):
        cur_class = {
            'name':       classes[2],
            'location':   classes[4],
            'occurrence': classes[5],
            'group':      classes[7],
            'teacher':    teacher_finder.findall(classes[0]),
            'time':       time_finder.findall(classes[-1]),
        }
        class_list.append(cur_class)

    suspend = [c for c in class_list if c['location'] == '停课']
    class_list = [c for c in class_list if c['location'] != '停课']
    for cls in class_list:
        for sus in suspend:
            if cls['name'] == sus['name'] and cls['time'] == sus['time']:
                cls['occurrence'] = ''.join(
                    '1' if a == '1' and b == '0' else '0'
                    for a, b in zip(cls['occurrence'], sus['occurrence'])
                )
    return class_list

# ---------------------------------------------------------------------------
# Main sync function
# ---------------------------------------------------------------------------

def updateClassTable(username: str, password: str,
                     proxy_url: str, proxy_token: str,
                     semester_id: str, start_date_str: str,
                     dry_run: bool = False) -> None:
    base = proxy_url.rstrip('/') + '/open-apis'
    acct = account(username, password)

    print("Logging into NKU EAMIS...")
    session, _ = eam_login(acct)
    session.cookies['semester.id'] = semester_id

    print(f"Scraping timetable (semester {semester_id})...")
    contents = _scrape_timetable(session, semester_id)
    class_list = _parse_class_list(contents)
    print(f"  Found {len(class_list)} classes.")

    print("Getting primary Feishu calendar...")
    calendar_id = _fetchMainCalID(base, proxy_token)
    print(f"  Calendar ID: {calendar_id}")

    # Load state and clean up old events
    state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            state = json.load(f)
    past_event_ids = state.pop(start_date_str, [])
    if past_event_ids and not dry_run:
        print(f"Deleting {len(past_event_ids)} old events from {start_date_str}...")
        for eid in past_event_ids:
            _deleteEvent(base, proxy_token, calendar_id, eid)

    start_date = datetime.date.fromisoformat(start_date_str)
    assert start_date.isoweekday() == 1, f"{start_date_str} is not a Monday"

    def get_date(wkday, lapse):
        start_point = start_date + datetime.timedelta(wkday)
        return (start_point + datetime.timedelta(7 * (lapse - 1))).strftime("%Y-%m-%d")

    new_event_ids = []

    for cls in class_list:
        summary = f"{cls['name']}\n{'、'.join(cls['teacher'])}"
        if cls['group']:
            summary += f"组{cls['group']}"
        location = cls['location']
        occurrences = _fetch_occurrence(cls['occurrence'])
        wkday, start_time, end_time = _parse_time(cls['time'])

        for start, end in occurrences:
            date = get_date(wkday, start)
            rrule = '' if start == end else f"FREQ=WEEKLY;COUNT={end - start + 1}"
            if dry_run:
                print(f"  [DRY-RUN] {summary!r}  {date} {start_time}–{end_time}"
                      + (f"  RRULE={rrule}" if rrule else ''))
            else:
                try:
                    eid = _createEvent(base, proxy_token, calendar_id, summary,
                                       location, date, start_time, date, end_time, rrule)
                    print(f"  Created: {cls['name']}  {date} {start_time}–{end_time}")
                    new_event_ids.append(eid)
                except Exception as e:
                    print(f"  ERROR: {cls['name']}: {e}")

    if not dry_run:
        state[start_date_str] = new_event_ids
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f, indent=2, ensure_ascii=False)

    print(f"\nDone. {'(dry run)' if dry_run else f'{len(new_event_ids)} events created.'}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    ap = argparse.ArgumentParser(description="Sync NKU class schedule to Feishu Calendar")
    ap.add_argument('--proxy-url',   required=True,
                    help='lark-mcp proxy base URL, e.g. http://127.0.0.1:PORT')
    ap.add_argument('--proxy-token', required=True,
                    help='lmk_xxx token issued by feishu_auth_issue_token')
    ap.add_argument('--username',    required=True, help='NKU student ID')
    ap.add_argument('--password',    required=True, help='NKU unified auth password')
    ap.add_argument('--semester-id', default='4364',
                    help='EAMIS semester.id cookie value (default: 4364 = 2025 fall)')
    ap.add_argument('--start-date',  default='2025-09-08',
                    help='Semester start Monday YYYY-MM-DD (default: 2025-09-08)')
    ap.add_argument('--dry-run', action='store_true',
                    help='Print events without creating them')
    args = ap.parse_args()

    updateClassTable(
        username=args.username,
        password=args.password,
        proxy_url=args.proxy_url,
        proxy_token=args.proxy_token,
        semester_id=args.semester_id,
        start_date_str=args.start_date,
        dry_run=args.dry_run,
    )
