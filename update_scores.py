#!/usr/bin/env python3
"""Update sports scores using team schedules, with NHL and CHN fallbacks."""
import datetime as dt
import html
from html.parser import HTMLParser
import json
import pathlib
import re
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parent
TEAMS = json.loads((ROOT / 'teams.json').read_text(encoding='utf-8'))
NOW = dt.datetime.now(dt.timezone.utc)
CENTRAL = ZoneInfo('America/Chicago')
HEADERS = {'User-Agent': 'Mozilla/5.0 (SportsDashboard/1.2)', 'Accept': 'application/json,text/html,*/*'}
CHN = {'north dakota': ('North-Dakota', 40), 'und': ('North-Dakota', 40), 'augustana': ('Augustana', 64)}


def fetch(url, json_mode=True):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=18) as r:
            data = r.read()
        return json.loads(data) if json_mode else data.decode('utf-8', 'replace')
    except Exception as exc:
        print('Feed unavailable:', url, str(exc)[:120])
        return None


def when_utc(value):
    try:
        return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(dt.timezone.utc)
    except (AttributeError, ValueError, TypeError):
        return None


def stamp(value):
    date = when_utc(value)
    if not date:
        return 'Time TBD'
    local = date.astimezone(CENTRAL)
    return f'{local:%b} {local.day}, {local:%I:%M %p} CT'.replace(', 0', ', ')


def identity(t):
    sport, league = t['sport'], t['league']
    ident = t.get('id')
    if ident and not t.get('search'):
        return {'id': str(ident), 'logo': f'https://a.espncdn.com/i/teamlogos/{league}/500/{ident}.png'}
    url = f'https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/teams?limit=500'
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
    return {'id': str(found['id']), 'logo': logos[0].get('href', ''), 'abbreviation': found.get('abbreviation', '')}


def team_data(t, ident):
    return (fetch(f"https://site.api.espn.com/apis/site/v2/sports/{t['sport']}/{t['league']}/teams/{ident['id']}") or {}).get('team') or {}


def record(data):
    items = (data.get('record') or {}).get('items') or []
    item = next((x for x in items if 'overall' in str(x.get('type', x.get('name', ''))).lower()), items[0] if items else {})
    if item.get('summary'):
        return item['summary']
    stats = {x.get('name'): x.get('value') for x in item.get('stats', [])}
    if stats.get('wins') is not None and stats.get('losses') is not None:
        return f"{int(stats['wins'])}-{int(stats['losses'])}"
    return None


def events_in(data):
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    return data.get('events') or (data.get('team') or {}).get('events') or []


def competitors(e):
    competitions = e.get('competitions') or []
    return (competitions[0].get('competitors') or []) if competitions else []


def is_mine(c, t, ident):
    team = c.get('team') or {}
    return (str(team.get('id')) == str(ident['id']) or
            team.get('displayName', '').casefold() == t['name'].casefold())


# Shared cache prevents a separate network request for every team in a league.
SCOREBOARD_CACHE = {}


def daily_scoreboard(sport, league, day):
    day_key = day.strftime('%Y%m%d')
    key = (sport, league, day_key)
    if key not in SCOREBOARD_CACHE:
        base = f'https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}'
        # ESPN's scoreboard uses one calendar date, not a start-end date range.
        # The old ranged requests returned HTTP 400 for several leagues.
        url = f'{base}/scoreboard?dates={day_key}'
        SCOREBOARD_CACHE[key] = events_in(fetch(url))
    return SCOREBOARD_CACHE[key]


def schedule_espn(t, ident):
    sport, league, team_id = t['sport'], t['league'], ident['id']
    base = f'https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}'
    events = []

    # Preserve the existing team-schedule approach for leagues where it works.
    # The site.web.api.espn.com/apis/v2/.../schedule URLs returned HTTP 404,
    # so don't waste requests on those URLs.
    for url in (f'{base}/teams/{team_id}/schedule',
                f'{base}/schedule?team={team_id}'):
        matches = [e for e in events_in(fetch(url))
                   if any(is_mine(c, t, ident) for c in competitors(e))]
        if matches:
            events.extend(matches)
            break

    # ESPN league scoreboard requests must be for *single days* in these feeds.
    # Check more frequently for soccer; NFL fixtures are usually weekly.
    if not events or not any(((e.get('status') or {}).get('type') or {}).get('state') in ('in', 'pre') for e in events):
        today = NOW.date()
        if sport == 'football' and league == 'nfl':
            # Cover every weekday, since NFL also plays Thu/Mon and occasional Sat.
            offsets = range(-3, 43)
        elif sport == 'soccer':
            offsets = range(-3, 46)
        else:
            offsets = range(-2, 22)
        for offset in offsets:
            day = today + dt.timedelta(days=offset)
            matches = [e for e in daily_scoreboard(sport, league, day)
                       if any(is_mine(c, t, ident) for c in competitors(e))]
            events.extend(matches)
            # Once a future fixture is found, later dates are unnecessary.
            if offset >= 0 and any(((e.get('status') or {}).get('type') or {}).get('state') in ('in', 'pre') for e in matches):
                break

    return list({str(e.get('id', idx)): e for idx, e in enumerate(events)}.values())


def choose(events):
    def state(e):
        return ((e.get('status') or {}).get('type') or {}).get('state')
    live = [e for e in events if state(e) == 'in']
    upcoming = sorted((e for e in events if state(e) == 'pre' and
                       (when_utc(e.get('date')) or NOW) >= NOW - dt.timedelta(hours=2)),
                      key=lambda e: e.get('date', ''))
    recent = sorted((e for e in events if state(e) == 'post'), key=lambda e: e.get('date', ''), reverse=True)
    return (live or upcoming or recent or [None])[0]


def game_espn(t, ident):
    e = choose(schedule_espn(t, ident))
    if not e:
        return {'match': 'Schedule unavailable', 'detail': 'No schedule returned', 'score': ''}
    cs = competitors(e)
    mine = next((c for c in cs if is_mine(c, t, ident)), None)
    other = next((c for c in cs if c is not mine), None)
    state = ((e.get('status') or {}).get('type') or {}).get('state')
    opponent = ((other or {}).get('team') or {}).get('displayName') or 'TBD'
    away = (mine or {}).get('homeAway') == 'away'
    out = {'match': ('at ' if away else 'vs ') + opponent,
           'detail': ('LIVE' if state == 'in' else 'FINAL' if state == 'post' else 'NEXT') + ' · ' + stamp(e.get('date')),
           'score': '', 'live': state == 'in'}
    if state in ('in', 'post') and mine and other:
        def score(c):
            value = c.get('score', '–')
            return str(value.get('displayValue', value.get('value', '–'))) if isinstance(value, dict) else str(value)
        out['score'] = score(mine) + ' – ' + score(other)
    return out


def game_nhl(t, ident, data):
    abbr = (data.get('abbreviation') or ident.get('abbreviation') or '').upper()
    if not abbr:
        return None
    season = f'{NOW.year if NOW.month >= 7 else NOW.year-1}{NOW.year+1 if NOW.month >= 7 else NOW.year}'
    games = (fetch(f'https://api-web.nhle.com/v1/club-schedule-season/{abbr}/{season}') or {}).get('games') or []
    def nhl_date(g):
        return when_utc(g.get('startTimeUTC'))
    live = [g for g in games if g.get('gameState') in ('LIVE', 'CRIT')]
    future = sorted((g for g in games if g.get('gameState') in ('FUT', 'PRE') and nhl_date(g) and nhl_date(g) >= NOW - dt.timedelta(hours=2)), key=lambda g: g['startTimeUTC'])
    past = sorted((g for g in games if g.get('gameState') in ('OFF', 'FINAL')), key=lambda g: g.get('startTimeUTC', ''), reverse=True)
    g = (live or future or past or [None])[0]
    if not g:
        return None
    home = (g.get('homeTeam') or {}).get('abbrev') == abbr
    mine = g.get('homeTeam') if home else g.get('awayTeam')
    opp = g.get('awayTeam') if home else g.get('homeTeam')
    opp_name = (opp or {}).get('placeName') or {}
    if isinstance(opp_name, dict):
        opp_name = opp_name.get('default', '')
    opponent = ((opp or {}).get('abbrev') or opp_name or 'TBD')
    state = g.get('gameState')
    status = 'LIVE' if state in ('LIVE', 'CRIT') else 'FINAL' if state in ('OFF', 'FINAL') else 'NEXT'
    result = {'match': ('vs ' if home else 'at ') + opponent,
              'detail': status + ' · ' + stamp(g.get('startTimeUTC')),
              'score': '', 'live': status == 'LIVE'}
    if status != 'NEXT':
        result['score'] = f"{(mine or {}).get('score', '–')} – {(opp or {}).get('score', '–')}"
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
    text = '\n'.join(re.sub(r'\s+', ' ', x) for x in lines if x.strip())
    result = {'record': None, 'match': 'Schedule unavailable', 'detail': 'College Hockey News', 'score': ''}
    m = re.search(r'The Latest\s+(\d+-\d+-\d+)', text, re.I) or re.search(r'\bRecord:\s*(\d+-\d+-\d+)', text, re.I)
    if m:
        result['record'] = m.group(1)
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
                result['detail'] = 'NEXT · ' + f'{local:%b} {local.day}, {local:%I:%M %p} CT'
            except ValueError:
                result['detail'] = 'NEXT · ' + when.group(1) + ' CT'
    return result


output = {'updated_at': NOW.isoformat(), 'teams': {}}
for t in TEAMS:
    key = '/'.join([t['sport'], t['league'], t['name']])
    result = {'record': None, 'match': 'Games unavailable', 'detail': 'Feed unavailable', 'score': '', 'logo': ''}
    try:
        chn = college(t) if t['sport'] == 'hockey' and t['league'] != 'nhl' else None
        if chn:
            result.update(chn)
        else:
            ident = identity(t)
            if ident:
                result['logo'] = ident.get('logo', '')
                data = team_data(t, ident)
                result['record'] = record(data)
                nhl = game_nhl(t, ident, data) if t['sport'] == 'hockey' and t['league'] == 'nhl' else None
                result.update(nhl or game_espn(t, ident))
    except Exception as exc:
        print('Team error:', t['name'], str(exc))
    output['teams'][key] = result
    print(t['name'], result['record'], result['match'], result['detail'])
(ROOT / 'scores.json').write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
