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
    needle = (t.get('search') or t['name']).casefold()
    aliases = ['wrexham', 'wrexham afc'] if 'wrexham' in needle else [needle]
    found = next((x for x in candidates if any(a in ' '.join(str(x.get(k, '')).casefold() for k in ('displayName', 'name', 'shortDisplayName', 'abbreviation')) for a in aliases)), None)
    if not found and 'wrexham' in needle:
        # ESPN soccer team IDs can be found in league scoreboards even when
        # the /teams endpoint does not list all clubs.
        for offset in range(-2, 9):
            for event in daily_scoreboard(sport, league, NOW.date() + dt.timedelta(days=offset)):
                for c in competitors(event):
                    team = c.get('team') or {}
                    if 'wrexham' in str(team.get('displayName', '')).casefold():
                        found = team
                        break
                if found:
                    break
            if found:
                break
    if not found:
        return None
    logos = found.get('logos') or []
    logo = (logos[0].get('href') if logos else '') or found.get('logo', '')
    return {'id': str(found['id']), 'logo': logo,
            'abbreviation': found.get('abbreviation', '')}

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
    for url in (f'{base}/teams/{team_id}/schedule', f'{base}/schedule?team={team_id}'):
        matches = [e for e in events_in(fetch(url))
                   if any(is_mine(c, t, ident) for c in competitors(e))]
        if matches:
            events.extend(matches)
            break
    # Schedules can omit today's in-progress game or recently completed game.
    # Fetch a narrow scoreboard window, with league-wide cache.
    local_day = NOW.astimezone(CENTRAL).date()
    for offset in range(-2, 9):
        day = local_day + dt.timedelta(days=offset)
        matches = [e for e in daily_scoreboard(sport, league, day)
                   if any(is_mine(c, t, ident) for c in competitors(e))]
        events.extend(matches)
    return list({str(e.get('id', idx)): e for idx, e in enumerate(events)}.values())


def cutoff_after_game(e):
    # Keep a final score until 8 AM Central on the calendar day AFTER
    # the local game date (not 8 AM UTC).
    started = when_utc(e.get('date'))
    if not started:
        return None
    next_day = started.astimezone(CENTRAL).date() + dt.timedelta(days=1)
    return dt.datetime.combine(next_day, dt.time(8, 0), tzinfo=CENTRAL).astimezone(dt.timezone.utc)


def choose(events):
    def state(e):
        return ((e.get('status') or {}).get('type') or {}).get('state')
    live = [e for e in events if state(e) == 'in']
    if live:
        return live[0]
    recent = sorted((e for e in events if state(e) == 'post' and
                     cutoff_after_game(e) and NOW < cutoff_after_game(e)),
                    key=lambda e: e.get('date', ''), reverse=True)
    if recent:
        return recent[0]
    upcoming = sorted((e for e in events if state(e) == 'pre' and
                       (when_utc(e.get('date')) or NOW) >= NOW - dt.timedelta(hours=2)),
                      key=lambda e: e.get('date', ''))
    return (upcoming or [None])[0]


def game_espn(t, ident):
    e = choose(schedule_espn(t, ident))
    if not e:
        return {'match': 'Schedule unavailable', 'detail': 'No upcoming game found', 'score': ''}
    cs = competitors(e)
    mine = next((c for c in cs if is_mine(c, t, ident)), None)
    other = next((c for c in cs if c is not mine), None)
    state = ((e.get('status') or {}).get('type') or {}).get('state')
    opponent = ((other or {}).get('team') or {}).get('displayName') or 'TBD'
    away = (mine or {}).get('homeAway') == 'away'
    status_obj = e.get('status') or {}
    live_detail = str(status_obj.get('displayClock') or '').strip()
    period = status_obj.get('period')
    if state == 'in':
        label = ('Period' if t['sport'] == 'hockey' else 'Quarter' if t['sport'] == 'football' else 'Half' if t['sport'] == 'soccer' else 'Inning')
        clock_label = f'{label} {period}' if period else ''
        detail = 'LIVE · ' + ' · '.join(x for x in (clock_label, live_detail) if x)
    else:
        detail = ('FINAL' if state == 'post' else 'NEXT') + ' · ' + stamp(e.get('date'))
    out = {'match': ('at ' if away else 'vs ') + opponent,
           'detail': detail, 'score': '', 'live': state == 'in'}
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
    recent = sorted((g for g in games if g.get('gameState') in ('OFF', 'FINAL') and
                     nhl_date(g) and NOW < dt.datetime.combine(
                         nhl_date(g).astimezone(CENTRAL).date() + dt.timedelta(days=1),
                         dt.time(8), tzinfo=CENTRAL).astimezone(dt.timezone.utc)),
                    key=lambda g: g.get('startTimeUTC', ''), reverse=True)
    future = sorted((g for g in games if g.get('gameState') in ('FUT', 'PRE') and
                     nhl_date(g) and nhl_date(g) >= NOW - dt.timedelta(hours=2)),
                    key=lambda g: g['startTimeUTC'])
    g = (live or recent or future or [None])[0]
    if not g:
        return None
    home = (g.get('homeTeam') or {}).get('abbrev') == abbr
    mine = g.get('homeTeam') if home else g.get('awayTeam')
    opp = g.get('awayTeam') if home else g.get('homeTeam')
    opponent = (opp or {}).get('abbrev') or 'TBD'
    state = g.get('gameState')
    status = 'LIVE' if state in ('LIVE', 'CRIT') else 'FINAL' if state in ('OFF', 'FINAL') else 'NEXT'
    detail = status + ' · ' + stamp(g.get('startTimeUTC'))
    if status == 'LIVE':
        landing = fetch(f"https://api-web.nhle.com/v1/gamecenter/{g.get('id')}/landing") or {}
        period = (landing.get('periodDescriptor') or {}).get('number')
        clock = (landing.get('clock') or {}).get('timeRemaining', '')
        in_intermission = (landing.get('clock') or {}).get('inIntermission', False)
        if period:
            detail = f"LIVE · {'Intermission after' if in_intermission else 'Period'} {period}"
            if clock and not in_intermission:
                detail += ' · ' + str(clock)
    result = {'match': ('vs ' if home else 'at ') + opponent,
              'detail': detail, 'score': '', 'live': status == 'LIVE'}
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


def college_live(t):
    """Use ESPN's college hockey scoreboard when it includes a current game.

    Keep College Hockey News as the schedule/record fallback. No fake scores.
    """
    if not any(k in t['name'].lower() for k in ('north dakota', 'und', 'augustana')):
        return None
    for offset in (-1, 0, 1):
        for e in daily_scoreboard('hockey', 'mens-college-hockey', NOW.date() + dt.timedelta(days=offset)):
            status = (e.get('status') or {}).get('type') or {}
            if status.get('state') not in ('in', 'post'):
                continue
            cs = competitors(e)
            def mine(c):
                name = ((c.get('team') or {}).get('displayName') or '').lower()
                if 'augustana' in t['name'].lower():
                    return 'augustana' in name
                return 'north dakota' in name or name == 'fighting hawks'
            me = next((c for c in cs if mine(c)), None)
            other = next((c for c in cs if c is not me), None)
            if not me or not other:
                continue
            event_date = when_utc(e.get('date'))
            if not event_date or abs((NOW - event_date).total_seconds()) > 24 * 3600:
                continue
            def score(c):
                v = c.get('score', '–')
                return str(v.get('displayValue', v.get('value', '–'))) if isinstance(v, dict) else str(v)
            state = status.get('state')
            status_obj = e.get('status') or {}
            period = status_obj.get('period')
            clock = status_obj.get('displayClock') or ''
            detail = ('LIVE · Period ' + str(period) + (' · ' + clock if clock else '')
                      if state == 'in' and period else
                      'LIVE' if state == 'in' else 'FINAL · ' + stamp(e.get('date')))
            return {'match': ('at ' if me.get('homeAway') == 'away' else 'vs ') +
                    ((other.get('team') or {}).get('displayName') or 'TBD'),
                    'detail': detail, 'score': score(me) + ' – ' + score(other),
                    'live': state == 'in'}
    return None


# Preserve the last successful game entry if a provider temporarily fails.
try:
    PREVIOUS = json.loads((ROOT / 'scores.json').read_text(encoding='utf-8')).get('teams', {})
except (OSError, ValueError, TypeError):
    PREVIOUS = {}

output = {'updated_at': NOW.isoformat(), 'teams': {}}
for t in TEAMS:
    key = '/'.join([t['sport'], t['league'], t['name']])
    result = {'record': None, 'match': 'Games unavailable', 'detail': 'Feed unavailable', 'score': '', 'logo': ''}
    try:
        ident = identity(t)
        if ident:
            result['logo'] = ident.get('logo', '')
            data = team_data(t, ident)
            result['record'] = record(data)
            nhl = game_nhl(t, ident, data) if t['sport'] == 'hockey' and t['league'] == 'nhl' else None
            result.update(nhl or game_espn(t, ident))
        if t['sport'] == 'hockey' and t['league'] != 'nhl':
            chn = college(t)
            # CHN can fill schedule/record gaps, but ESPN live/final wins.
            if chn:
                if not result['record']:
                    result['record'] = chn.get('record')
                if result['match'] in ('Games unavailable', 'Schedule unavailable'):
                    result.update({k: v for k, v in chn.items() if k in ('match', 'detail', 'score')})
            live = college_live(t)
            if live and (live.get('live') or result['match'] in ('Games unavailable', 'Schedule unavailable')):
                result.update(live)
        if result['match'] in ('Games unavailable', 'Schedule unavailable'):
            old = PREVIOUS.get(key) or {}
            if old.get('match') not in (None, 'Games unavailable', 'Schedule unavailable'):
                result.update(old)
        if not result.get('logo'):
            result['logo'] = (PREVIOUS.get(key) or {}).get('logo', '')
    except Exception as exc:
        print('Team error:', t['name'], str(exc))
        result.update(PREVIOUS.get(key) or {})
    output['teams'][key] = result
    print(t['name'], result['record'], result['match'], result['detail'])
(ROOT / 'scores.json').write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
