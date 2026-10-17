#!/usr/bin/env python3
"""Refresh sports-dashboard scores.json from team schedules and College Hockey News."""
import datetime as dt
import html
from html.parser import HTMLParser
import json
import pathlib
import re
import urllib.request
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parent
TEAMS = json.loads((ROOT / 'teams.json').read_text(encoding='utf-8'))
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; SportsDashboard/1.1)', 'Accept': '*/*'}
CENTRAL = ZoneInfo('America/Chicago')
NOW = dt.datetime.now(dt.timezone.utc)
CHN = {'north dakota': ('North-Dakota', 40), 'und': ('North-Dakota', 40),
       'augustana': ('Augustana', 64)}


def fetch(url, json_mode=True):
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read()
        return json.loads(raw) if json_mode else raw.decode('utf-8', 'replace')
    except Exception as exc:
        print('Feed unavailable:', url, str(exc)[:160])
        return None


def info(t):
    ident = t.get('id')
    if ident and not t.get('search'):
        return {'id': str(ident), 'logo': f"https://a.espncdn.com/i/teamlogos/{t['league']}/500/{ident}.png"}
    url = f"https://site.api.espn.com/apis/site/v2/sports/{t['sport']}/{t['league']}/teams?limit=500"
    data = fetch(url) or {}
    sports = data.get('sports') or [{}]
    leagues = sports[0].get('leagues') or [{}]
    candidates = [x.get('team', x) for x in leagues[0].get('teams', [])]
    needle = (t.get('search') or t['name']).lower()
    found = next((x for x in candidates if x.get('displayName', '').lower() == t['name'].lower()), None)
    if not found:
        found = next((x for x in candidates if needle in x.get('displayName', '').lower() or needle in x.get('name', '').lower()), None)
    if not found:
        return None
    logos = found.get('logos') or [{}]
    return {'id': str(found['id']), 'logo': logos[0].get('href', '')}


def record(t, i):
    url = f"https://site.api.espn.com/apis/site/v2/sports/{t['sport']}/{t['league']}/teams/{i['id']}"
    team = (fetch(url) or {}).get('team') or {}
    items = (team.get('record') or {}).get('items') or []
    rec = next((x for x in items if 'overall' in str(x.get('type', x.get('name', ''))).lower()), items[0] if items else {})
    if rec.get('summary'):
        return rec['summary']
    stats = {x.get('name'): x.get('value') for x in rec.get('stats', [])}
    if stats.get('wins') is not None and stats.get('losses') is not None:
        return f"{int(stats['wins'])}-{int(stats['losses'])}"
    return None


def parse_date(value):
    try:
        return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(dt.timezone.utc)
    except (ValueError, AttributeError):
        return None


def find_events(data):
    # ESPN returns events directly on scoreboard and under team.events on schedule.
    if not isinstance(data, dict):
        return []
    return data.get('events') or (data.get('team') or {}).get('events') or []


def event_for_team(events, t, i):
    matched = []
    for event in events:
        comps = (event.get('competitions') or [{}])[0].get('competitors') or []
        if any(str((c.get('team') or {}).get('id')) == str(i['id']) or
               (c.get('team') or {}).get('displayName', '').lower() == t['name'].lower() for c in comps):
            matched.append(event)
    return matched


def game(t, i):
    base = f"https://site.api.espn.com/apis/site/v2/sports/{t['sport']}/{t['league']}"
    # Team schedule endpoints are much more reliable than one league-wide 500-game response.
    events = []
    for suffix in (f"/teams/{i['id']}/schedule?limit=300", f"/teams/{i['id']}/schedule?season={NOW.year}&limit=300"):
        data = fetch(base + suffix)
        events.extend(event_for_team(find_events(data), t, i))
        if events:
            break
    if not events:
        for start in (-7, 0, 7, 14, 21):
            a = (NOW + dt.timedelta(days=start)).strftime('%Y%m%d')
            b = (NOW + dt.timedelta(days=start + 6)).strftime('%Y%m%d')
            data = fetch(f'{base}/scoreboard?dates={a}-{b}&limit=500')
            events.extend(event_for_team(find_events(data), t, i))
    unique = {str(e.get('id', idx)): e for idx, e in enumerate(events)}
    events = list(unique.values())
    def state(e):
        return ((e.get('status') or {}).get('type') or {}).get('state')
    live = [e for e in events if state(e) == 'in']
    future = sorted((e for e in events if state(e) == 'pre' and parse_date(e.get('date')) and parse_date(e['date']) >= NOW - dt.timedelta(hours=2)), key=lambda e: e.get('date', ''))
    past = sorted((e for e in events if state(e) == 'post'), key=lambda e: e.get('date', ''), reverse=True)
    ev = (live or future or past or [None])[0]
    if not ev:
        return {'match': 'Schedule unavailable', 'detail': 'No schedule returned', 'score': ''}
    comps = (ev.get('competitions') or [{}])[0].get('competitors') or []
    mine = next((c for c in comps if str((c.get('team') or {}).get('id')) == str(i['id']) or (c.get('team') or {}).get('displayName', '').lower() == t['name'].lower()), None)
    opp = next((c for c in comps if c is not mine), None)
    opponent = ((opp or {}).get('team') or {}).get('displayName', 'TBD')
    when = parse_date(ev.get('date'))
    stamp = when.astimezone(CENTRAL).strftime('%b %-d, %-I:%M %p CT') if when else 'Time TBD'
    status = state(ev)
    away = (mine or {}).get('homeAway') == 'away'
    result = {'match': ('at ' if away else 'vs ') + opponent,
              'detail': ('LIVE' if status == 'in' else 'FINAL' if status == 'post' else 'NEXT') + ' · ' + stamp,
              'score': '', 'live': status == 'in'}
    if status in ('in', 'post') and mine and opp:
        result['score'] = f"{mine.get('score', '–')} – {opp.get('score', '–')}"
    return result


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0
    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if tag in ('p', 'div', 'tr', 'td', 'th', 'h1', 'h2', 'h3', 'br', 'li'):
            self.parts.append('\n')
    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)
        if tag in ('p', 'div', 'tr', 'h1', 'h2', 'h3', 'li'):
            self.parts.append('\n')
    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def college(t):
    name = t['name'].lower()
    selection = next((v for k, v in CHN.items() if k in name), None)
    if not selection:
        return None
    slug, team_id = selection
    page = fetch(f'https://www.collegehockeynews.com/reports/team/{slug}/{team_id}', False)
    if not page:
        return None
    parser = VisibleText()
    parser.feed(page)
    lines = [html.unescape(x).strip() for x in ''.join(parser.parts).splitlines()]
    lines = [re.sub(r'\s+', ' ', x) for x in lines if x.strip()]
    text = '\n'.join(lines)
    result = {'record': None, 'match': 'Schedule unavailable', 'detail': 'College Hockey News', 'score': ''}
    # The team's main page shows current season record as "The Latest 2-0-1, .833".
    m = re.search(r'The Latest\s+(\d+-\d+-\d+)', text, re.I)
    if not m:
        m = re.search(r'\bRecord:\s*(\d+-\d+-\d+)', text, re.I)
    if m:
        result['record'] = m.group(1)
    # Team page's Next Game block includes opponent and local Central game time.
    m = re.search(r'Next Game\s+(.+?)\s+(?:October|November|December|January|February|March|April|May|June|July|August|September)\s+\d{1,2},\s+\d{4},\s+\d{1,2}:\d{2}', text, re.I | re.S)
    if m:
        block = m.group(0)
        matchup = re.search(r'(?:NORTH DAKOTA|AUGUSTANA)\s+(vs\.|at)\s+([^\n]+)', block, re.I)
        when = re.search(r'((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4},\s+\d{1,2}:\d{2})', block, re.I)
        if matchup:
            result['match'] = matchup.group(1).replace('vs.', 'vs') + ' ' + matchup.group(2).strip()
        if when:
            try:
                local = dt.datetime.strptime(when.group(1), '%B %d, %Y, %H:%M').replace(tzinfo=CENTRAL)
                result['detail'] = 'NEXT · ' + local.strftime('%b %-d, %-I:%M %p CT')
            except ValueError:
                result['detail'] = 'NEXT · ' + when.group(1) + ' CT'
    return result


output = {'updated_at': NOW.isoformat(), 'teams': {}}
for team in TEAMS:
    key = '/'.join([team['sport'], team['league'], team['name']])
    result = {'record': None, 'match': 'Games unavailable', 'detail': 'Feed unavailable', 'score': '', 'logo': ''}
    try:
        chn = college(team) if team['sport'] == 'hockey' and team['league'] != 'nhl' else None
        if chn:
            result.update(chn)
        else:
            identity = info(team)
            if identity:
                result['logo'] = identity.get('logo', '')
                result['record'] = record(team, identity)
                result.update(game(team, identity))
    except Exception as exc:
        print('Team error:', team['name'], str(exc))
    output['teams'][key] = result
    print(team['name'], result['record'], result['match'], result['detail'])
(ROOT / 'scores.json').write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
