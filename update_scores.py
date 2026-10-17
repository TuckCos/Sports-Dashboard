#!/usr/bin/env python3
"""Fetch ESPN data server-side in GitHub Actions; unavailable feeds remain unavailable."""
import json,urllib.request,urllib.parse,datetime,pathlib,time
ROOT=pathlib.Path(__file__).resolve().parent
TEAMS=json.loads((ROOT/'teams.json').read_text())
HEADERS={'User-Agent':'Mozilla/5.0 SportsDashboard/1.0','Accept':'application/json'}
def get(url):
 try:
  req=urllib.request.Request(url,headers=HEADERS)
  with urllib.request.urlopen(req,timeout=16) as r:return json.load(r)
 except Exception as e:
  print('Feed unavailable:',url, str(e)[:120]);return None

def info(t):
 league=t['league'];sport=t['sport'];ident=t.get('id')
 if ident and not t.get('search'):
  return {'id':ident,'logo':f'https://a.espncdn.com/i/teamlogos/{league}/500/{ident}.png'}
 url=f'https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/teams?limit=500'
 j=get(url) or {}; teams=((j.get('sports') or [{}])[0].get('leagues') or [{}])[0].get('teams') or []
 candidates=[x.get('team',x) for x in teams]; needle=(t.get('search') or t['name']).lower()
 found=next((x for x in candidates if x.get('displayName','').lower()==t['name'].lower()),None)
 if not found:found=next((x for x in candidates if needle in x.get('displayName','').lower() or needle in x.get('name','').lower()),None)
 if not found:return None
 return {'id':str(found['id']),'logo':((found.get('logos') or [{}])[0].get('href') or '')}

def record(t,i):
 url=f"https://site.api.espn.com/apis/site/v2/sports/{t['sport']}/{t['league']}/teams/{i['id']}"
 j=get(url) or {};team=j.get('team') or {};items=(team.get('record') or {}).get('items') or []
 rec=next((x for x in items if 'overall' in str(x.get('type',x.get('name',''))).lower()),items[0] if items else {})
 if rec.get('summary'):return rec['summary']
 stats={x.get('name'):x.get('value') for x in rec.get('stats',[])}
 if stats.get('wins') is not None and stats.get('losses') is not None:return str(int(stats['wins']))+'-'+str(int(stats['losses']))
 return None

def game(t,i):
 now=datetime.datetime.now(datetime.timezone.utc)
 a=(now-datetime.timedelta(days=10)).strftime('%Y%m%d');b=(now+datetime.timedelta(days=21)).strftime('%Y%m%d')
 url=f"https://site.api.espn.com/apis/site/v2/sports/{t['sport']}/{t['league']}/scoreboard?dates={a}-{b}&limit=500"
 j=get(url) or {}; events=[]
 for e in j.get('events',[]):
  cs=((e.get('competitions') or [{}])[0].get('competitors') or [])
  if any(str(c.get('team',{}).get('id'))==str(i['id']) or c.get('team',{}).get('displayName')==t['name'] for c in cs):events.append(e)
 def state(e):return ((e.get('status') or {}).get('type') or {}).get('state')
 live=next((e for e in events if state(e)=='in'),None)
 future=sorted((e for e in events if state(e)=='pre' and e.get('date','')>=now.isoformat()[:19]),key=lambda e:e.get('date',''))
 past=sorted((e for e in events if state(e)=='post'),key=lambda e:e.get('date',''),reverse=True)
 ev=live or (future[0] if future else None) or (past[0] if past else None)
 if not ev:return {'match':'No games found','detail':'Check back soon','score':''}
 cs=((ev.get('competitions') or [{}])[0].get('competitors') or [])
 mine=next((c for c in cs if str(c.get('team',{}).get('id'))==str(i['id']) or c.get('team',{}).get('displayName')==t['name']),None)
 opp=next((c for c in cs if c is not mine),None)
 date=ev.get('date','');status=state(ev)
 try:date=datetime.datetime.fromisoformat(date.replace('Z','+00:00')).strftime('%b %d %H:%M UTC')
 except:pass
 out={'match':'vs '+((opp or {}).get('team') or {}).get('displayName','TBD'),'detail':('LIVE' if status=='in' else 'FINAL' if status=='post' else 'NEXT')+' · '+date,'score':'','live':status=='in'}
 if status in ('in','post') and mine and opp:out['score']=str(mine.get('score','–'))+' – '+str(opp.get('score','–'))
 return out

output={'updated_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'teams':{}}
for t in TEAMS:
 key='/'.join([t['sport'],t['league'],t['name']]);result={'record':None,'match':'Games unavailable','detail':'Feed unavailable','score':'','logo':''}
 try:
  i=info(t)
  if i:
   result['logo']=i.get('logo','');result['record']=record(t,i);result.update(game(t,i))
 except Exception as e:print('Team error',t['name'],str(e))
 output['teams'][key]=result
 print(t['name'],result['match'])
(ROOT/'scores.json').write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n')
