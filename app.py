import os, sqlite3, urllib.request, urllib.parse, json, time, threading, secrets, hashlib, hmac
from contextlib import closing
from netflix_ai_agent import suggest_match, wikipedia_context, wikidata_context
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
DB='/data/library.db'; KEY=os.getenv('TMDB_API_KEY','')
app=FastAPI(title='Cinefolio')
_tmdb_cache={}
_tmdb_lock=threading.Lock()
def conn():
 c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; return c
@app.on_event('startup')
def init():
 os.makedirs('/data',exist_ok=True)
 with closing(conn()) as c:
  c.executescript('CREATE TABLE IF NOT EXISTS library(kind TEXT NOT NULL, tmdb_id INTEGER NOT NULL, title TEXT NOT NULL, poster TEXT, status TEXT DEFAULT "watchlist", watched INTEGER DEFAULT 0, rating INTEGER, PRIMARY KEY(kind,tmdb_id)); CREATE TABLE IF NOT EXISTS episodes(tmdb_id INTEGER NOT NULL, season INTEGER NOT NULL, episode INTEGER NOT NULL, watched_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(tmdb_id,season,episode));');c.execute('CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,display_name TEXT,salt TEXT,password_hash TEXT)');c.execute('CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,expires INTEGER)');c.execute('CREATE TABLE IF NOT EXISTS netflix_unmatched(id INTEGER PRIMARY KEY, title TEXT NOT NULL, viewed_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT "pending", resolved_kind TEXT, resolved_tmdb_id INTEGER, resolved_season INTEGER, resolved_episode INTEGER, UNIQUE(title,viewed_at))');c.execute('CREATE TABLE IF NOT EXISTS netflix_ai_suggestions(row_id INTEGER PRIMARY KEY,proposal_json TEXT NOT NULL)');c.execute('CREATE TABLE IF NOT EXISTS netflix_series_memory(series_title TEXT PRIMARY KEY COLLATE NOCASE,tmdb_id INTEGER NOT NULL,confirmed_at TEXT DEFAULT CURRENT_TIMESTAMP)');c.execute('CREATE TABLE IF NOT EXISTS netflix_episode_memory(netflix_title TEXT PRIMARY KEY COLLATE NOCASE,tmdb_id INTEGER NOT NULL,season INTEGER NOT NULL,episode INTEGER NOT NULL,confirmed_at TEXT DEFAULT CURRENT_TIMESTAMP)');c.execute('CREATE TABLE IF NOT EXISTS tv_pins(tmdb_id INTEGER PRIMARY KEY,favorite INTEGER NOT NULL DEFAULT 0,up_next INTEGER NOT NULL DEFAULT 0)');c.execute('CREATE TABLE IF NOT EXISTS custom_providers(kind TEXT NOT NULL,tmdb_id INTEGER NOT NULL,name TEXT NOT NULL,provider_id INTEGER,logo_path TEXT,PRIMARY KEY(kind,tmdb_id,name))');c.commit()

class Credentials(BaseModel):
 username:str
 password:str
 display_name:str|None=None
class ProfileUpdate(BaseModel):
 display_name:str
class PasswordUpdate(BaseModel):
 current_password:str
 new_password:str
def hash_password(password,salt):
 return hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=16384,r=8,p=1).hex()
def get_user(request):
 token=request.cookies.get('cinefolio_session','')
 with closing(conn()) as c:
  row=c.execute('SELECT u.id,u.username,u.display_name FROM sessions s CROSS JOIN users u WHERE s.token_hash=? AND s.expires>? LIMIT 1',(hashlib.sha256(token.encode()).hexdigest(),int(time.time()))).fetchone()
 return dict(row) if row else None
def make_session(response):
 token=secrets.token_urlsafe(48)
 with closing(conn()) as c:
  c.execute('DELETE FROM sessions WHERE expires<?',(int(time.time()),))
  c.execute('INSERT INTO sessions VALUES(?,?)',(hashlib.sha256(token.encode()).hexdigest(),int(time.time())+604800))
  c.commit()
 response.set_cookie('cinefolio_session',token,httponly=True,secure=True,samesite='strict',max_age=604800,path='/')
@app.middleware('http')
async def auth_guard(request:Request,call_next):
 path=request.url.path
 if path.startswith('/api/') and not path.startswith('/api/auth/') and not get_user(request):
  return JSONResponse({'detail':'Connexion requise'},status_code=401)
 if request.method not in ('GET','HEAD','OPTIONS'):
  origin=request.headers.get('origin')
  if origin:
   from urllib.parse import urlsplit
   if urlsplit(origin).netloc!=request.headers.get('host'):
    return JSONResponse({'detail':'Origine non autorisée'},status_code=403)
 response=await call_next(request)
 response.headers['X-Content-Type-Options']='nosniff'
 response.headers['X-Frame-Options']='DENY'
 response.headers['Referrer-Policy']='same-origin'
 if path=='/' or path.startswith('/api/auth/'):response.headers['Cache-Control']='no-store'
 return response
@app.get('/api/auth/status')
def auth_status(request:Request):
 user=get_user(request)
 with closing(conn()) as c:setup=c.execute('SELECT COUNT(*) FROM users').fetchone()[0]==0
 return {'authenticated':bool(user),'user':user,'setup_required':setup}
@app.post('/api/auth/register')
def register(i:Credentials,response:Response):
 name=i.username.strip().lower()
 if len(name)<3 or len(name)>40 or not all(ch.isalnum() or ch in '._-' for ch in name):raise HTTPException(400,'Identifiant invalide')
 if not 12<=len(i.password)<=256:raise HTTPException(400,'Mot de passe de 12 caractères minimum requis')
 with closing(conn()) as c:
  c.execute('BEGIN IMMEDIATE')
  if c.execute('SELECT COUNT(*) FROM users').fetchone()[0]:raise HTTPException(403,'Inscriptions fermées')
  salt=secrets.token_hex(16)
  c.execute('INSERT INTO users(username,display_name,salt,password_hash) VALUES(?,?,?,?)',(name,(i.display_name or name).strip()[:80],salt,hash_password(i.password,salt)))
  c.commit()
 make_session(response)
 return {'ok':True}
@app.post('/api/auth/login')
def login(i:Credentials,response:Response):
 with closing(conn()) as c:row=c.execute('SELECT * FROM users WHERE username=?',(i.username.strip().lower(),)).fetchone()
 digest=hash_password(i.password,row['salt'] if row else '00'*16)
 if not row or not hmac.compare_digest(digest,row['password_hash']):raise HTTPException(401,'Identifiants incorrects')
 make_session(response)
 return {'ok':True}
@app.post('/api/auth/logout')
def logout(request:Request,response:Response):
 token=request.cookies.get('cinefolio_session','')
 with closing(conn()) as c:
  c.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(token.encode()).hexdigest(),));c.commit()
 response.delete_cookie('cinefolio_session',path='/')
 return {'ok':True}
@app.get('/api/auth/profile')
def profile(request:Request):
 user=get_user(request)
 if not user:raise HTTPException(401,'Connexion requise')
 return user
@app.patch('/api/auth/profile')
def update_profile(i:ProfileUpdate,request:Request):
 if not get_user(request):raise HTTPException(401,'Connexion requise')
 name=i.display_name.strip()
 if not 1<=len(name)<=80:raise HTTPException(400,'Nom invalide')
 with closing(conn()) as c:c.execute('UPDATE users SET display_name=?',(name,));c.commit()
 return {'ok':True}
@app.post('/api/auth/password')
def update_password(i:PasswordUpdate,request:Request):
 if not get_user(request):raise HTTPException(401,'Connexion requise')
 if not 12<=len(i.new_password)<=256:raise HTTPException(400,'Mot de passe de 12 caractères minimum requis')
 with closing(conn()) as c:
  row=c.execute('SELECT * FROM users LIMIT 1').fetchone()
  if not hmac.compare_digest(hash_password(i.current_password,row['salt']),row['password_hash']):raise HTTPException(403,'Mot de passe actuel incorrect')
  salt=secrets.token_hex(16)
  c.execute('UPDATE users SET salt=?,password_hash=?',(salt,hash_password(i.new_password,salt)))
  c.execute('DELETE FROM sessions WHERE token_hash!=?',(hashlib.sha256(request.cookies.get('cinefolio_session','').encode()).hexdigest(),))
  c.commit()
 return {'ok':True}

def tmdb(path,params=None):
 if not KEY: raise HTTPException(503,'Configurez TMDB_API_KEY dans .env')
 q=urllib.parse.urlencode(dict(params or {},api_key=KEY,language='fr-CA'))
 cache_key=(path,q)
 with _tmdb_lock:
  hit=_tmdb_cache.get(cache_key)
  if hit and time.monotonic()-hit[0]<900:return hit[1]
 try:
  with urllib.request.urlopen('https://api.themoviedb.org/3/'+path+'?'+q,timeout=12) as r:data=json.load(r)
  with _tmdb_lock:
   if len(_tmdb_cache)>1200:_tmdb_cache.clear()
   _tmdb_cache[cache_key]=(time.monotonic(),data)
  return data
 except Exception as e:raise HTTPException(502,'Erreur TMDB : '+str(e))
class Item(BaseModel):
 kind:str;tmdb_id:int;title:str;poster:str|None=None
class Episode(BaseModel):
 tmdb_id:int;season:int;episode:int;watched:bool=True
@app.get('/api/search')
def search(q:str):
 q=q.strip()
 imdb_match=__import__('re').search(r'(?:imdb\.com/title/)?(tt\d{7,10})',q,__import__('re').I)
 if imdb_match:
  found=tmdb('find/'+imdb_match.group(1).lower(),{'external_source':'imdb_id'})
  results=[]
  for kind,key in (('movie','movie_results'),('tv','tv_results')):
   for item in found.get(key,[]):
    results.append({**item,'media_type':kind,'imdb_id':imdb_match.group(1).lower()})
  return results
 return tmdb('search/multi',{'query':q,'include_adult':'false'}).get('results',[])
@app.get('/api/library')
def library():
 with closing(conn()) as c:
  return [dict(x) for x in c.execute("""SELECT l.*,COALESCE(p.favorite,0) AS favorite,COALESCE(p.up_next,0) AS up_next,CASE WHEN l.kind='tv' THEN (SELECT COUNT(*) FROM episodes e WHERE e.tmdb_id=l.tmdb_id) ELSE 0 END AS watched_episodes_count FROM library l LEFT JOIN tv_pins p ON l.kind='tv' AND l.tmdb_id=p.tmdb_id ORDER BY l.title COLLATE NOCASE""")]

class TVPinUpdate(BaseModel):
 favorite:bool|None=None
 up_next:bool|None=None

@app.patch('/api/tv/{id}/pin')
def update_tv_pin(id:int,payload:TVPinUpdate):
 if payload.favorite is None and payload.up_next is None:raise HTTPException(400,'Aucune modification')
 with closing(conn()) as c:
  if not c.execute("SELECT 1 FROM library WHERE kind='tv' AND tmdb_id=?",(id,)).fetchone():raise HTTPException(404,'Série absente de la bibliothèque')
  c.execute('INSERT OR IGNORE INTO tv_pins(tmdb_id) VALUES(?)',(id,))
  if payload.favorite is not None:c.execute('UPDATE tv_pins SET favorite=? WHERE tmdb_id=?',(int(payload.favorite),id))
  if payload.up_next is not None:c.execute('UPDATE tv_pins SET up_next=? WHERE tmdb_id=?',(int(payload.up_next),id))
  c.commit()
 return {'ok':True}


@app.post('/api/library')
def add(i:Item):
 if i.kind not in ('tv','movie'):raise HTTPException(400,'Type invalide')
 poster=i.poster
 if not poster:
  try:poster=tmdb(f'{i.kind}/{i.tmdb_id}').get('poster_path')
  except HTTPException:pass
 with closing(conn()) as c:
  c.execute('INSERT OR IGNORE INTO library(kind,tmdb_id,title,poster) VALUES(?,?,?,?)',(i.kind,i.tmdb_id,i.title,poster));c.commit()
 return {'ok':True}
@app.delete('/api/library/{kind}/{id}')
def remove(kind:str,id:int):
 with closing(conn()) as c:c.execute('DELETE FROM library WHERE kind=? AND tmdb_id=?',(kind,id));c.commit()
 return {'ok':True}
@app.post('/api/movie/{id}/watched')
def movie(id:int):
 with closing(conn()) as c:c.execute('UPDATE library SET watched=1-watched WHERE kind="movie" AND tmdb_id=?',(id,));c.commit()
 return {'ok':True}
@app.get('/api/tv/{id}')
def show(id:int):
 data=tmdb(f'tv/{id}')
 with closing(conn()) as c:data['watched_episodes']=[dict(x) for x in c.execute('SELECT season,episode FROM episodes WHERE tmdb_id=?',(id,))]
 return data
@app.get('/api/tv/{id}/season/{season}')
def season(id:int,season:int):return tmdb(f'tv/{id}/season/{season}')
@app.post('/api/episode')
def episode(e:Episode):
 with closing(conn()) as c:
  if e.watched:c.execute('INSERT OR IGNORE INTO episodes(tmdb_id,season,episode) VALUES(?,?,?)',(e.tmdb_id,e.season,e.episode))
  else:c.execute('DELETE FROM episodes WHERE tmdb_id=? AND season=? AND episode=?',(e.tmdb_id,e.season,e.episode))
  c.commit()
 return {'ok':True}
@app.get('/',response_class=HTMLResponse)
def home():return open('/app/index.html').read()

from datetime import date, timedelta
@app.get('/api/series-progress')
def tv_progress():
 """Match the detail screen: count watched episodes that actually exist in TMDB seasons."""
 with closing(conn()) as c:
  shows=[r['tmdb_id'] for r in c.execute("SELECT tmdb_id FROM library WHERE kind='tv'")]
  seen={}
  for row in c.execute('SELECT tmdb_id,season,episode FROM episodes'):
   seen.setdefault(row['tmdb_id'],set()).add((row['season'],row['episode']))
 result=[]
 for mid in shows:
  watched_set=seen.get(mid,set())
  try:
   detail=tmdb(f'tv/{mid}')
   seasons=[int(season['season_number']) for season in detail.get('seasons',[]) if int(season.get('season_number',0))>0]
   valid=set()
   for number in seasons:
    season_data=tmdb(f'tv/{mid}/season/{number}')
    valid.update((number,int(ep['episode_number'])) for ep in season_data.get('episodes',[]) if ep.get('episode_number') is not None)
   watched=len(watched_set & valid)
   total=len(valid)
   result.append({'tmdb_id':mid,'watched':watched,'total':total,'percent':round(100*watched/total) if total else 0,'verified':True})
  except Exception:
   result.append({'tmdb_id':mid,'watched':len(watched_set),'total':None,'percent':None,'verified':False})
 return result

@app.get('/api/dashboard')
def dashboard(days:int=30):
 days=max(1,min(days,120)); today=date.today(); until=today+timedelta(days=days)
 with closing(conn()) as c:
  shows=[dict(x) for x in c.execute("SELECT * FROM library WHERE kind='tv'")]
  seen={(x['tmdb_id'],x['season'],x['episode']) for x in c.execute('SELECT tmdb_id,season,episode FROM episodes')}
 upcoming=[]; available=[]; progress=[]
 for item in shows:
  try:
   detail=tmdb(f"tv/{item['tmdb_id']}")
   seasons=[s for s in detail.get('seasons',[]) if s.get('season_number',0)>0]
   episodes=[]
   for season in seasons:
    data=tmdb(f"tv/{item['tmdb_id']}/season/{season['season_number']}")
    for ep in data.get('episodes',[]):
     if not ep.get('air_date'):continue
     try:air=date.fromisoformat(ep['air_date'])
     except ValueError:continue
     row={'tmdb_id':item['tmdb_id'],'series':item['title'],'poster':item['poster'],'season':season['season_number'],'episode':ep['episode_number'],'title':ep.get('name'),'air_date':ep['air_date'],'watched':(item['tmdb_id'],season['season_number'],ep['episode_number']) in seen}
     episodes.append(row)
     if today<air<=until:upcoming.append(row)
     elif air<=today and not row['watched']:available.append(row)
   aired=[e for e in episodes if e['air_date']<=today.isoformat()]
   progress.append({'tmdb_id':item['tmdb_id'],'series':item['title'],'watched':sum(e['watched'] for e in aired),'aired':len(aired),'total':len(episodes),'upcoming':sum(e['air_date']>today.isoformat() for e in episodes),'watched_total':sum(e['watched'] for e in episodes),'next_unwatched':next((e for e in sorted(aired,key=lambda x:(x['season'],x['episode'])) if not e['watched']),None)})
  except Exception as exc:
   progress.append({'tmdb_id':item['tmdb_id'],'series':item['title'],'error':str(exc)})
 return {'upcoming':sorted(upcoming,key=lambda x:x['air_date']),'available':sorted(available,key=lambda x:x['air_date'],reverse=True),'progress':progress,'date':today.isoformat(),'note':'Dates de diffusion originale TMDB, pas disponibilité garantie au Canada.'}

@app.get('/api/featured')
def featured():
 with closing(conn()) as c:
  item=c.execute("SELECT tmdb_id FROM library WHERE kind='tv' ORDER BY title LIMIT 1").fetchone()
 if not item:return {}
 try:
  d=tmdb(f"tv/{item['tmdb_id']}")
  return {'id':item['tmdb_id'],'backdrop_path':d.get('backdrop_path'),'name':d.get('name'),'overview':d.get('overview')}
 except Exception:return {}

@app.post('/api/library/refresh-posters')
def refresh_posters():
 with closing(conn()) as c:
  missing=[dict(x) for x in c.execute("SELECT kind,tmdb_id FROM library WHERE poster IS NULL OR TRIM(poster)=''")]
 updated=0; failed=0
 for item in missing:
  try:
   detail=tmdb(f"{item['kind']}/{item['tmdb_id']}")
   path=detail.get('poster_path')
   if path:
    with closing(conn()) as c:
     c.execute("UPDATE library SET poster=? WHERE kind=? AND tmdb_id=? AND (poster IS NULL OR TRIM(poster)='')",(path,item['kind'],item['tmdb_id']))
     c.commit()
    updated+=1
  except Exception:failed+=1
 return {'checked':len(missing),'updated':updated,'failed':failed}


class ProviderSelection(BaseModel):
 name:str
 provider_id:int|None=None
 logo_path:str|None=None
@app.get('/api/provider-catalog')
def provider_catalog(kind:str='tv'):
 if kind not in ('tv','movie'):raise HTTPException(400,'Type invalide')
 data=tmdb(f'watch/providers/{kind}',{'watch_region':''})
 providers=data.get('results',[])
 return sorted([{'provider_id':p['provider_id'],'provider_name':p['provider_name'],'logo_path':p.get('logo_path')} for p in providers],key=lambda p:p['provider_name'].casefold())
@app.get('/api/watch-providers/{kind}/{id}')
def watch_providers(kind:str,id:int,region:str='ALL'):
 if kind not in ('tv','movie'):raise HTTPException(400,'Type invalide')
 region=region.upper()
 if region!='ALL' and (len(region)!=2 or not region.isalpha()):raise HTTPException(400,'Région invalide')
 data=tmdb(f'{kind}/{id}/watch/providers').get('results',{})
 entries=data.items() if region=='ALL' else [(region,data.get(region,{}))]
 groups={k:{} for k in ('flatrate','free','ads','rent','buy')}
 for code,country in entries:
  for key in groups:
   for p in country.get(key,[]):
    pid=p.get('provider_id')
    if pid is None:continue
    record=groups[key].setdefault(pid,{'provider_id':pid,'provider_name':p.get('provider_name',''),'logo_path':p.get('logo_path'),'regions':[]})
    if code not in record['regions']:record['regions'].append(code)
 with closing(conn()) as c:
  custom=[dict(row) for row in c.execute('SELECT name,provider_id,logo_path FROM custom_providers WHERE kind=? AND tmdb_id=? ORDER BY name',(kind,id))]
 return {'region':region,'regions':sorted(data.keys()),'link':data.get(region,{}).get('link') if region!='ALL' else None,**{k:sorted(v.values(),key=lambda p:p['provider_name'].casefold()) for k,v in groups.items()},'custom':custom,'source':'TMDB / JustWatch'}
@app.post('/api/watch-providers/{kind}/{id}/custom')
def add_custom_provider(kind:str,id:int,item:ProviderSelection):
 if kind not in ('tv','movie') or id<=0:raise HTTPException(400,'Contenu invalide')
 name=item.name.strip()
 if not 1<=len(name)<=100:raise HTTPException(400,'Nom de plateforme invalide')
 if item.provider_id is not None and item.provider_id<=0:raise HTTPException(400,'Identifiant invalide')
 logo=item.logo_path if item.logo_path and item.logo_path.startswith('/') and len(item.logo_path)<256 else None
 with closing(conn()) as c:
  c.execute('INSERT OR REPLACE INTO custom_providers(kind,tmdb_id,name,provider_id,logo_path) VALUES(?,?,?,?,?)',(kind,id,name,item.provider_id,logo));c.commit()
 return {'ok':True}
@app.delete('/api/watch-providers/{kind}/{id}/custom/{name}')
def delete_custom_provider(kind:str,id:int,name:str):
 with closing(conn()) as c:
  c.execute('DELETE FROM custom_providers WHERE kind=? AND tmdb_id=? AND name=?',(kind,id,name));c.commit()
 return {'ok':True}

@app.get('/api/viewing-history')
def viewing_history():
 """Viewing dates are the dates users marked episodes as watched (UTC)."""
 with closing(conn()) as c:
  rows=[dict(x) for x in c.execute("""SELECT e.tmdb_id,e.season,e.episode,e.watched_at,l.title
    FROM episodes e LEFT JOIN library l ON l.kind='tv' AND l.tmdb_id=e.tmdb_id
    ORDER BY e.watched_at DESC""")]
 runtimes={}
 for row in rows:
  key=(row['tmdb_id'],row['season'])
  if key not in runtimes:
   try:
    season_data=tmdb(f"tv/{key[0]}/season/{key[1]}")
    runtimes[key]={int(ep['episode_number']):ep.get('runtime') for ep in season_data.get('episodes',[])}
   except Exception:runtimes[key]={}
  row['minutes']=runtimes[key].get(row['episode'])
  row['estimated']=False
  if not isinstance(row['minutes'],int) or row['minutes']<=0:
   row['minutes']=None
 return {'episodes':rows,'timezone':'UTC','note':'Dates enregistrées lors du marquage comme vu (UTC), pas nécessairement les dates réelles de visionnage. Durées TMDB disponibles seulement.'}

# Netflix viewing activity CSV: explicit preview before applying any changes.
import csv, io, re, unicodedata
from difflib import SequenceMatcher
from datetime import datetime
class NetflixCSV(BaseModel):
 csv_text:str
 confirm:bool=False

def netflix_date(raw):
 raw=raw.strip()
 for fmt in ('%m/%d/%y','%m/%d/%Y','%Y-%m-%d','%d/%m/%Y'):
  try:return datetime.strptime(raw,fmt).strftime('%Y-%m-%d 12:00:00')
  except ValueError:pass
 return None

def netflix_norm(value):
 value=unicodedata.normalize('NFKD',value.casefold())
 value=''.join(c for c in value if not unicodedata.combining(c))
 return re.sub(r'[^\w]+',' ',value).strip()

def netflix_episode_title(title):
 parts=[x.strip() for x in title.split(':')]
 if len(parts)<2:return None
 if len(parts)>=3:
  season=re.fullmatch(r'(?:season|saison|series|série|partie|part)\s*(\d+)',parts[1],re.I)
  if season:return parts[0],int(season.group(1)),':'.join(parts[2:]).strip()
  if re.fullmatch(r'(?:miniseries|mini-série|minisérie|limited series)',parts[1],re.I):return parts[0],1,':'.join(parts[2:]).strip()
 if re.fullmatch(r'(?:episode|épisode|chapitre|chapter)\s*\d+',parts[1],re.I):return parts[0],1,':'.join(parts[1:]).strip()
 # A series may have a named episode without a season label.
 return parts[0],None,':'.join(parts[1:]).strip()

@app.post('/api/import/netflix')
def import_netflix(payload:NetflixCSV):
 if len(payload.csv_text.encode('utf-8'))>3_000_000:raise HTTPException(413,'Fichier trop volumineux (3 Mo maximum)')
 content=payload.csv_text.lstrip('\ufeff')
 try:
  dialect=csv.Sniffer().sniff(content[:4096],delimiters=',;\t')
  reader=csv.DictReader(io.StringIO(content),dialect=dialect)
  if not reader.fieldnames:raise ValueError('Entêtes absents')
  cols={k.strip().lower():k for k in reader.fieldnames}
  title_col=next((cols[k] for k in ('title','titre') if k in cols),None)
  date_col=next((cols[k] for k in ('date','date de visionnement','date de visionnage') if k in cols),None)
  if not title_col or not date_col:raise ValueError('Colonnes Title et Date requises')
  rows=list(reader)
 except (csv.Error,ValueError,UnicodeError) as exc:raise HTTPException(400,'CSV Netflix invalide : '+str(exc))
 if len(rows)>15000:raise HTTPException(413,'Trop de lignes (15 000 maximum)')
 with closing(conn()) as c:
  shows=[dict(x) for x in c.execute("SELECT tmdb_id,title FROM library WHERE kind='tv'")]
  movies=[dict(x) for x in c.execute("SELECT tmdb_id,title FROM library WHERE kind='movie'")]
  existing={(x['tmdb_id'],x['season'],x['episode']) for x in c.execute('SELECT tmdb_id,season,episode FROM episodes')}
 norm=netflix_norm
 by_title={norm(x['title']):x['tmdb_id'] for x in shows}
 movie_titles={norm(x['title']):x['tmdb_id'] for x in movies}
 cache={}; discovered={}; new_shows={}; new_movies={}
 proposed={}; unmatched=[]; unmatched_rows=[]; movies_found=set(); invalid=0
 def lookup(kind,title):
  key=(kind,norm(title))
  if key not in discovered:
   try:
    hits=tmdb('search/'+kind,{'query':title}).get('results',[])
    field='name' if kind=='tv' else 'title'
    original='original_name' if kind=='tv' else 'original_title'
    matches=[x for x in hits if norm(x.get(field,'') or '')==norm(title) or norm(x.get(original,'') or '')==norm(title)]
    if not matches:
     # TMDB may index Netflix's French name as an alternative title.
     for candidate in hits[:4]:
      try:
       alt=tmdb(f'{kind}/{candidate["id"]}/alternative_titles')
       names=[a.get('title','') for a in alt.get('titles',alt.get('results',[]))]
       if any(norm(name)==norm(title) for name in names):matches.append(candidate)
      except Exception:pass
    discovered[key]=matches[0] if len(matches)==1 else None
   except Exception:discovered[key]=None
  return discovered[key]
 for row in rows:
  title=(row.get(title_col) or '').strip();when=netflix_date(row.get(date_col) or '')
  if not title or not when:invalid+=1;continue
  parsed=netflix_episode_title(title)
  if parsed:
   series,season,episode_name=parsed
   tmdb_id=by_title.get(norm(series))
   if tmdb_id is None:
    info=lookup('tv',series)
    if info:
     tmdb_id=info['id'];new_shows[tmdb_id]=(info.get('name') or series,info.get('poster_path'))
   if tmdb_id is None:
    # A colon can belong to a movie title, not an episode separator.
    film=movie_titles.get(norm(title))
    info=None if film is not None else lookup('movie',title)
    if film is None and info:
     film=info['id'];new_movies[film]=(info.get('title') or title,info.get('poster_path'))
    if film is not None:
     movies_found.add(film)
    else:
     if len(unmatched)<30:unmatched.append(title)
     unmatched_rows.append((title,when))
    continue
   if season is None:
    key=(tmdb_id,'seasons')
    if key not in cache:
     try:cache[key]=[x['season_number'] for x in tmdb(f'tv/{tmdb_id}').get('seasons',[]) if x['season_number']>0]
     except Exception:cache[key]=[]
    seasons=cache[key]
   else:seasons=[season]
   matches=[]
   for sn in seasons:
    key=(tmdb_id,sn)
    if key not in cache:
     try:cache[key]=tmdb(f'tv/{tmdb_id}/season/{sn}').get('episodes',[])
     except Exception:cache[key]=[]
    matches.extend((sn,e) for e in cache[key] if norm(e.get('name') or '')==norm(episode_name))
   if len(matches)!=1:
    # Explicit Netflix episode numbers are reliable when season is known.
    number=re.search(r'^(?:episode|épisode|chapitre|chapter)\s*(\d+)\b',episode_name,re.I)
    if number and season is not None:
     matches=[(season,e) for e in cache.get((tmdb_id,season),[]) if e.get('episode_number')==int(number.group(1))]
   if len(matches)!=1:
    # Compare translated episode titles conservatively, within the known season.
    candidates=[(sn,e) for sn in seasons for e in cache.get((tmdb_id,sn),[]) if e.get('name')]
    scores=sorted(((SequenceMatcher(None,norm(episode_name),norm(e['name'])).ratio(),sn,e) for sn,e in candidates),reverse=True,key=lambda x:x[0])
    if scores and scores[0][0]>=0.94 and (len(scores)==1 or scores[0][0]-scores[1][0]>=0.10):matches=[(scores[0][1],scores[0][2])]
   if len(matches)!=1:
    if len(unmatched)<30:unmatched.append(title)
    unmatched_rows.append((title,when))
    continue
   sn,ep=matches[0];episode_key=(tmdb_id,sn,int(ep['episode_number']))
   if episode_key not in existing:proposed.setdefault(episode_key,when)
  else:
   mid=movie_titles.get(norm(title))
   if mid is None:
    info=lookup('movie',title)
    if info:
     mid=info['id'];new_movies[mid]=(info.get('title') or title,info.get('poster_path'))
   if mid is not None:movies_found.add(mid)
   else:
    if len(unmatched)<30:unmatched.append(title)
    unmatched_rows.append((title,when))
 result={'total_rows':len(rows),'episodes_to_import':len(proposed),'movies_to_mark_watched':len(movies_found),'new_series':len(new_shows),'new_movies':len(new_movies),'already_watched_not_overwritten':len(existing.intersection(proposed)),'unmatched_examples':unmatched,'invalid_rows':invalid,'note':'Recherche des titres TMDB, numéros explicites et correspondances prudentes. Les ambiguïtés sont ignorées.'}
 if not payload.confirm:return result
 with closing(conn()) as c:
  for title,when in unmatched_rows:c.execute('INSERT OR IGNORE INTO netflix_unmatched(title,viewed_at) VALUES(?,?)',(title,when))
  for tmdb_id,(title,poster) in new_shows.items():c.execute('INSERT OR IGNORE INTO library(kind,tmdb_id,title,poster) VALUES(?,?,?,?)',('tv',tmdb_id,title,poster))
  for tmdb_id,(title,poster) in new_movies.items():c.execute('INSERT OR IGNORE INTO library(kind,tmdb_id,title,poster) VALUES(?,?,?,?)',('movie',tmdb_id,title,poster))
  for (tmdb_id,season,episode),when in proposed.items():
   c.execute('INSERT OR IGNORE INTO episodes(tmdb_id,season,episode,watched_at) VALUES(?,?,?,?)',(tmdb_id,season,episode,when))
  for tmdb_id in movies_found:c.execute("UPDATE library SET watched=1 WHERE kind='movie' AND tmdb_id=?",(tmdb_id,))
  c.commit()
 return {**result,'imported':True}

@app.get('/api/tv/{tmdb_id}/episodes/search')
def search_tv_episodes(tmdb_id:int,q:str):
 import unicodedata
 def norm(value):
  value=unicodedata.normalize('NFKD',str(value or '').casefold())
  return re.sub(r'[^a-z0-9]+',' ',''.join(c for c in value if not unicodedata.combining(c))).strip()
 q=q.strip()
 if not q:raise HTTPException(400,'Recherche vide')
 match=re.search(r'(?:s(?:aison)?\\s*(\\d+)\\s*[- .]*e(?:pisode)?\\s*(\\d+)|(?:saison\\s*)?(\\d+)\\s*x\\s*(\\d+))',q,re.I)
 season_number=int(match.group(1) or match.group(3)) if match else None
 episode_number=int(match.group(2) or match.group(4)) if match else None
 details=tmdb(f'tv/{tmdb_id}')
 seasons=[x.get('season_number') for x in details.get('seasons',[]) if x.get('season_number') is not None and x.get('season_number')>=0]
 if season_number is not None:seasons=[season_number] if season_number in seasons else []
 needle=norm(q);results=[]
 for sn in seasons[:60]:
  data=tmdb(f'tv/{tmdb_id}/season/{sn}')
  for ep in data.get('episodes',[]):
   number=ep.get('episode_number')
   if match:
    if number!=episode_number:continue
    score=100
   else:
    title=norm(ep.get('name'))
    if not title:continue
    if title==needle:score=100
    elif needle in title:score=75
    elif title in needle and len(title)>=5:score=55
    else:continue
   results.append({'season':sn,'episode':number,'name':ep.get('name'),'score':score})
 return sorted(results,key=lambda x:(-x['score'],x['season'],x['episode']))[:60]

class NetflixAutoResolve(BaseModel):
 confirm:bool=False
 limit:int=20

@app.post('/api/import/netflix/unmatched/auto')
def auto_resolve_netflix(payload:NetflixAutoResolve):
 limit=max(1,min(payload.limit,40))
 with closing(conn()) as c:
  rows=[dict(x) for x in c.execute("SELECT id,title,viewed_at FROM netflix_unmatched WHERE status='pending' ORDER BY id LIMIT ?",(limit,))]
  library_titles={(x['kind'],netflix_norm(x['title'])):x['tmdb_id'] for x in c.execute('SELECT kind,title,tmdb_id FROM library')}
 suggestions=[];skipped=0;cache={}
 for row in rows:
  title=row['title'];parsed=netflix_episode_title(title);choice=None
  if parsed:
   series,season,episode_name=parsed
   sid=library_titles.get(('tv',netflix_norm(series)))
   if sid is None:
    try:
     hits=tmdb('search/tv',{'query':series}).get('results',[])
     hits=[h for h in hits if netflix_norm(h.get('name',''))==netflix_norm(series) or netflix_norm(h.get('original_name',''))==netflix_norm(series)]
     if len(hits)==1:sid=hits[0]['id']
    except Exception:pass
   if sid is not None:
    try:
     if sid not in cache:cache[sid]=[x['season_number'] for x in tmdb(f'tv/{sid}').get('seasons',[]) if x.get('season_number',0)>0]
     seasons=[season] if season is not None else cache[sid][:20]
     number=re.search(r'^(?:episode|épisode|chapitre|chapter)\s*(\d+)\b',episode_name,re.I)
     matches=[]
     for sn in seasons:
      key=(sid,sn)
      if key not in cache:cache[key]=tmdb(f'tv/{sid}/season/{sn}').get('episodes',[])
      for ep in cache[key]:
       name=netflix_norm(ep.get('name',''))
       if (number and season is not None and ep.get('episode_number')==int(number.group(1))) or (not number and name and name==netflix_norm(episode_name)):
        matches.append((sn,ep))
     if len(matches)==1:
      sn,ep=matches[0]
      choice={'kind':'tv','tmdb_id':sid,'season':sn,'episode':ep['episode_number'],'match':ep.get('name')}
    except Exception:pass
  else:
   mid=library_titles.get(('movie',netflix_norm(title)))
   if mid is None:
    try:
     hits=tmdb('search/movie',{'query':title}).get('results',[])
     hits=[h for h in hits if netflix_norm(h.get('title',''))==netflix_norm(title) or netflix_norm(h.get('original_title',''))==netflix_norm(title)]
     if len(hits)==1:mid=hits[0]['id']
    except Exception:pass
   if mid is not None:choice={'kind':'movie','tmdb_id':mid,'match':title}
  if choice:suggestions.append({'id':row['id'],'title':title,**choice})
  else:skipped+=1
 if payload.confirm and suggestions:
  with closing(conn()) as c:
   c.execute('BEGIN IMMEDIATE')
   for item in suggestions:
    row=c.execute("SELECT viewed_at FROM netflix_unmatched WHERE id=? AND status='pending'",(item['id'],)).fetchone()
    if not row:continue
    kind=item['kind'];mid=item['tmdb_id']
    try:details=tmdb(f'{kind}/{mid}')
    except Exception:continue
    c.execute('INSERT OR IGNORE INTO library(kind,tmdb_id,title,poster) VALUES(?,?,?,?)',(kind,mid,details.get('name') or details.get('title') or item['title'],details.get('poster_path')))
    if kind=='tv':c.execute('INSERT OR IGNORE INTO episodes(tmdb_id,season,episode,watched_at) VALUES(?,?,?,?)',(mid,item['season'],item['episode'],row['viewed_at']))
    else:c.execute("UPDATE library SET watched=1 WHERE kind='movie' AND tmdb_id=?",(mid,))
    c.execute("UPDATE netflix_unmatched SET status='resolved',resolved_kind=?,resolved_tmdb_id=?,resolved_season=?,resolved_episode=? WHERE id=?",(kind,mid,item.get('season'),item.get('episode'),item['id']))
   c.commit()
 return {'processed':len(rows),'matched':len(suggestions),'unmatched':skipped,'suggestions':suggestions,'applied':payload.confirm}


class NetflixAIScan(BaseModel):
 row_id:int

def netflix_learned_suggestion(title):
 parsed=netflix_episode_title(title)
 if not parsed:return None
 with closing(conn()) as c:
  exact=c.execute('SELECT tmdb_id,season,episode FROM netflix_episode_memory WHERE netflix_title=?',(title,)).fetchone()
  if exact:
   return {'kind':'tv','tmdb_id':exact['tmdb_id'],'season':exact['season'],'episode':exact['episode'],'confidence':1.0,'reason':'Correspondance déjà confirmée dans la mémoire Cinéfolio'}
  series=c.execute('SELECT tmdb_id FROM netflix_series_memory WHERE series_title=?',(parsed[0],)).fetchone()
 if not series:return None
 mid=series['tmdb_id']
 target=netflix_normalize(parsed[2])
 if not target:return None
 try:
  seasons=[parsed[1]] if parsed[1] is not None else [x['season_number'] for x in tmdb('tv/'+str(mid)).get('seasons',[]) if x.get('season_number',0)>0][:12]
  matches=set()
  for sn in seasons:
   for language in ('fr-FR','fr-CA','es-ES','en-US'):
    try:
     for ep in tmdb('tv/'+str(mid)+'/season/'+str(sn),{'language':language}).get('episodes',[]):
      if netflix_normalize(ep.get('name',''))==target:matches.add((sn,ep['episode_number']))
    except Exception:pass
  if len(matches)==1:
   sn,ep=next(iter(matches))
   return {'kind':'tv','tmdb_id':mid,'season':sn,'episode':ep,'confidence':0.99,'reason':'Titre exact retrouvé dans TMDB grâce à la série confirmée (validation requise)'}
 except Exception:pass
 return None

def netflix_normalize(value):
 import unicodedata,re
 value=unicodedata.normalize('NFKD',str(value).casefold())
 return re.sub(r'[^a-z0-9]+',' ',''.join(c for c in value if not unicodedata.combining(c))).strip()

def netflix_reanalyse_series(series_title):
 """Compare all pending episodes of one confirmed series in a single TMDB pass."""
 with closing(conn()) as c:
  series=c.execute('SELECT tmdb_id FROM netflix_series_memory WHERE series_title=?',(series_title,)).fetchone()
  rows=c.execute("SELECT id,title FROM netflix_unmatched WHERE status='pending'").fetchall()
 if not series:return {'series':series_title,'checked':0,'suggested':0}
 rows=[row for row in rows if (parsed:=netflix_episode_title(row['title'])) and netflix_normalize(parsed[0])==netflix_normalize(series_title)]
 mid=series['tmdb_id'];lookup={}
 try:
  seasons=[x['season_number'] for x in tmdb('tv/'+str(mid)).get('seasons',[]) if x.get('season_number',0)>0][:20]
  for sn in seasons:
   for language in ('fr-FR','fr-CA','es-ES','en-US'):
    try:
     for ep in tmdb('tv/'+str(mid)+'/season/'+str(sn),{'language':language}).get('episodes',[]):
      name=netflix_normalize(ep.get('name',''))
      if name and name not in ('episode','episode '+str(ep['episode_number'])):
       lookup.setdefault(name,set()).add((sn,ep['episode_number']))
    except Exception:pass
 except Exception:pass
 proposed=0
 with closing(conn()) as c:
  for row in rows:
   parsed=netflix_episode_title(row['title'])
   name=netflix_normalize(parsed[2])
   matches=lookup.get(name,set())
   if parsed[1] is not None:matches={x for x in matches if x[0]==parsed[1]}
   if len(matches)!=1:continue
   sn,ep=next(iter(matches))
   proposal={'kind':'tv','tmdb_id':mid,'season':sn,'episode':ep,'confidence':0.99,'reason':'Titre exact trouvé dans les épisodes TMDB multilingues de la série confirmée. Vérification manuelle requise.','source':'tmdb-grouped'}
   c.execute('INSERT OR REPLACE INTO netflix_ai_suggestions(row_id,proposal_json) VALUES(?,?)',(row['id'],json.dumps(proposal)))
   proposed+=1
  c.commit()
 return {'series':series_title,'checked':len(rows),'suggested':proposed}

@app.post('/api/import/netflix/series/reanalyse')
def netflix_reanalyse_confirmed_series():
 with closing(conn()) as c:
  series=[row['series_title'] for row in c.execute('SELECT series_title FROM netflix_series_memory').fetchall()]
 if not series:return {'series':0,'checked':0,'suggested':0}
 with _ai_jobs_lock:
  if any(j['status'] in ('queued','running') for j in _ai_jobs.values()):raise HTTPException(409,'Une analyse est déjà en cours')
  job_id=secrets.token_hex(8)
  _ai_jobs[job_id]={'id':job_id,'status':'queued','total':len(series),'processed':0,'matched':0,'errors':[]}
 def worker():
  with _ai_jobs_lock:_ai_jobs[job_id]['status']='running'
  for name in series:
   with _ai_jobs_lock:
    if _ai_jobs[job_id]['status']=='cancelled':break
   try:
    result=netflix_reanalyse_series(name)
    with _ai_jobs_lock:_ai_jobs[job_id]['matched']+=result['suggested']
   except Exception as exc:
    with _ai_jobs_lock:_ai_jobs[job_id]['errors'].append({'series':name,'message':str(exc)[:200]})
   with _ai_jobs_lock:_ai_jobs[job_id]['processed']+=1
  with _ai_jobs_lock:
   if _ai_jobs[job_id]['status']=='running':_ai_jobs[job_id]['status']='completed'
 threading.Thread(target=worker,daemon=True).start()
 return {'id':job_id,'series':len(series)}

def netflix_movie_details(mid,hit=None):
 """Enrich a proposed film with public TMDB metadata; keep failure non-fatal."""
 hit=hit or {}
 try:details=tmdb('movie/'+str(mid),{'language':'fr-FR','append_to_response':'credits'})
 except Exception:details=hit
 directors=[x.get('name') for x in details.get('credits',{}).get('crew',[]) if x.get('job')=='Director']
 return {'kind':'movie','tmdb_id':mid,'title':details.get('title') or hit.get('title') or hit.get('original_title'),
  'original_title':details.get('original_title') or hit.get('original_title'),
  'year':(details.get('release_date') or hit.get('release_date') or '')[:4],
  'release_date':details.get('release_date') or hit.get('release_date') or '',
  'poster':details.get('poster_path') or hit.get('poster_path'),
  'overview':details.get('overview') or hit.get('overview') or '',
  'runtime':details.get('runtime'),'genres':[g.get('name') for g in details.get('genres',[])],
  'directors':directors,'rating':details.get('vote_average'),
  'tmdb_url':'https://www.themoviedb.org/movie/'+str(mid)}

def netflix_movie_alternatives(title):
 """Find exact-title homonyms across localized TMDB results."""
 query=title.strip()
 if ':' in query:return []
 choices={}
 for language in ('fr-CA','fr-FR','en-US'):
  try:
   for hit in tmdb('search/movie',{'query':query,'language':language}).get('results',[])[:20]:
    names=[hit.get('title',''),hit.get('original_title','')]
    if not any(netflix_normalize(n)==netflix_normalize(query) for n in names):continue
    choices[hit['id']]=hit
  except Exception:pass
 hits=sorted(choices.values(),key=lambda h:(h.get('release_date') or '',h['id']),reverse=True)[:8]
 return [netflix_movie_details(h['id'],h) for h in hits]

@app.post('/api/import/netflix/ai/suggest')
def netflix_ai_suggest(payload:NetflixAIScan):
 with closing(conn()) as c:
  row=c.execute("SELECT title FROM netflix_unmatched WHERE id=? AND status='pending'",(payload.row_id,)).fetchone()
 if not row:raise HTTPException(404,'Visionnement introuvable')
 title=row['title'];parsed=netflix_episode_title(title);query=parsed[0] if parsed else title
 alternatives=netflix_movie_alternatives(title) if not parsed else []
 if len(alternatives)>1:
  proposal={'kind':'movie','tmdb_id':None,'confidence':0,'reason':'Plusieurs films portent ce titre. Choisissez la version réellement visionnée; aucun choix automatique.','ambiguous':True,'alternatives':alternatives}
  with closing(conn()) as c:
   c.execute('INSERT OR REPLACE INTO netflix_ai_suggestions(row_id,proposal_json) VALUES(?,?)',(payload.row_id,json.dumps(proposal)));c.commit()
  return {'id':payload.row_id,'title':title,'suggestion':proposal}
 learned=netflix_learned_suggestion(title)
 if learned:
  with closing(conn()) as c:
   c.execute('INSERT OR REPLACE INTO netflix_ai_suggestions(row_id,proposal_json) VALUES(?,?)',(payload.row_id,json.dumps(learned)));c.commit()
  return {'id':payload.row_id,'title':title,'suggestion':learned}
 if not os.getenv('GEMINI_API_KEY'):raise HTTPException(503,'GEMINI_API_KEY manquante')
 candidates=[];episodes=[]
 for kind in ('tv','movie'):
  try:
   for hit in tmdb('search/'+kind,{'query':query}).get('results',[])[:5]:
    candidates.append({'kind':kind,'id':hit['id'],'title':hit.get('name') or hit.get('title'),'original':hit.get('original_name') or hit.get('original_title')})
  except Exception:pass
 if parsed:
  # Fetch localized episode names; Netflix's French episode titles may differ from TMDB fr-CA.
  for item in [x for x in candidates if x['kind']=='tv'][:2]:
   try:
    seasons=[parsed[1]] if parsed[1] is not None else [x['season_number'] for x in tmdb('tv/'+str(item['id'])).get('seasons',[]) if x.get('season_number',0)>0][:5]
    for sn in seasons:
     by_episode={}
     for language in ('fr-FR','fr-CA','es-ES','en-US'):
      try:
       for ep in tmdb('tv/'+str(item['id'])+'/season/'+str(sn),{'language':language}).get('episodes',[])[:35]:
        number=ep['episode_number']
        key=(item['id'],sn,number)
        if key not in by_episode:by_episode[key]={'id':item['id'],'season':sn,'episode':number,'names':[]}
        name=(ep.get('name') or '').strip()
        if name and name not in by_episode[key]['names']:by_episode[key]['names'].append(name)
      except Exception:pass
     episodes.extend(by_episode.values())
   except Exception:pass
 try:
  wiki=wikipedia_context(query,parsed[2]) if parsed and candidates else []
  wd=wikidata_context(query,parsed[2]) if parsed and candidates else []
  proposal=suggest_match(title,candidates,episodes[:500],wiki,wd) if candidates else None
 except Exception as exc:raise HTTPException(502,'Recherche IA indisponible : '+str(exc)[:300])
 with closing(conn()) as c:
  c.execute('INSERT OR REPLACE INTO netflix_ai_suggestions(row_id,proposal_json) VALUES(?,?)',(payload.row_id,json.dumps(proposal)));c.commit()
 return {'id':payload.row_id,'title':title,'suggestion':proposal}

@app.delete('/api/import/netflix/ai/suggestions/{row_id}')
def netflix_ai_delete_suggestion(row_id:int):
 with closing(conn()) as c:
  c.execute('DELETE FROM netflix_ai_suggestions WHERE row_id=?',(row_id,))
  c.commit()
 return {'ok':True}

@app.get('/api/import/netflix/movie/{tmdb_id}/details')
def netflix_movie_proposal_details(tmdb_id:int):
 if tmdb_id<=0:raise HTTPException(400,'Identifiant TMDB invalide')
 # Details are public TMDB metadata; this endpoint never confirms or imports.
 return netflix_movie_details(tmdb_id)

@app.get('/api/import/netflix/ai/suggestions')
def netflix_ai_list():
 with closing(conn()) as c:
  rows=c.execute("SELECT s.row_id,u.title,s.proposal_json FROM netflix_ai_suggestions s JOIN netflix_unmatched u ON u.id=s.row_id WHERE u.status='pending' ORDER BY s.row_id DESC").fetchall()
 return [{'id':x['row_id'],'title':x['title'],'suggestion':json.loads(x['proposal_json'])} for x in rows]


_ai_jobs={}
_ai_jobs_lock=threading.Lock()

def _ai_batch_worker(job_id,limit):
 import time
 with _ai_jobs_lock:_ai_jobs[job_id]['status']='running'
 with closing(conn()) as c:
  rows=c.execute("SELECT u.id FROM netflix_unmatched u LEFT JOIN netflix_ai_suggestions s ON s.row_id=u.id WHERE u.status='pending' AND s.row_id IS NULL ORDER BY u.id LIMIT ?",(limit,)).fetchall()
 with _ai_jobs_lock:_ai_jobs[job_id]['total']=len(rows)
 consecutive_errors=0
 for row in rows:
  with _ai_jobs_lock:
   if _ai_jobs[job_id]['status']=='cancelled':break
  try:
   result=netflix_ai_suggest(NetflixAIScan(row_id=row['id']))
   consecutive_errors=0
   with _ai_jobs_lock:
    if result['suggestion']:_ai_jobs[job_id]['matched']+=1
  except Exception as exc:
   consecutive_errors+=1
   message=str(getattr(exc,'detail',exc))[:350]
   with _ai_jobs_lock:_ai_jobs[job_id]['errors'].append({'id':row['id'],'message':message})
  with _ai_jobs_lock:_ai_jobs[job_id]['processed']+=1
  if consecutive_errors>=3:
   with _ai_jobs_lock:
    _ai_jobs[job_id]['status']='stopped'
    _ai_jobs[job_id]['reason']='Arrêt après 3 erreurs consécutives. Vérifiez le modèle, les quotas et la connectivité Gemini.'
   break
  with _ai_jobs_lock:
   if _ai_jobs[job_id]['status']=='cancelled':break
  time.sleep(1.5)
 with _ai_jobs_lock:
  if _ai_jobs[job_id]['status']=='running':_ai_jobs[job_id]['status']='completed'

class NetflixAIBatch(BaseModel):
 limit:int=20

@app.post('/api/import/netflix/ai/batch')
def netflix_ai_batch(payload:NetflixAIBatch):
 if not os.getenv('GEMINI_API_KEY'):raise HTTPException(503,'GEMINI_API_KEY manquante')
 limit=max(1,min(payload.limit,100))
 with _ai_jobs_lock:
  if any(j['status'] in ('queued','running') for j in _ai_jobs.values()):raise HTTPException(409,'Une analyse IA est déjà en cours')
  job_id=secrets.token_hex(8)
  _ai_jobs[job_id]={'id':job_id,'status':'queued','total':0,'processed':0,'matched':0,'errors':[]}
 threading.Thread(target=_ai_batch_worker,args=(job_id,limit),daemon=True).start()
 return {'id':job_id}

@app.get('/api/import/netflix/ai/batch/{job_id}')
def netflix_ai_batch_status(job_id:str):
 with _ai_jobs_lock:
  if job_id not in _ai_jobs:raise HTTPException(404,'Analyse introuvable (redémarrage du serveur possible)')
  return dict(_ai_jobs[job_id])

@app.post('/api/import/netflix/ai/batch/{job_id}/cancel')
def netflix_ai_batch_cancel(job_id:str):
 with _ai_jobs_lock:
  if job_id not in _ai_jobs:raise HTTPException(404,'Analyse introuvable')
  if _ai_jobs[job_id]['status'] in ('queued','running'):_ai_jobs[job_id]['status']='cancelled'
  return dict(_ai_jobs[job_id])

class NetflixResolve(BaseModel):
 kind:str
 tmdb_id:int
 season:int|None=None
 episode:int|None=None

class NetflixViewingDecision(BaseModel):
 decision:str
 kind:str|None=None
 tmdb_id:int|None=None
 season:int|None=None
 episode:int|None=None

@app.post('/api/import/netflix/unmatched/{row_id}/decision')
def netflix_viewing_decision(row_id:int,payload:NetflixViewingDecision):
 if payload.decision not in ('not_watched','started'):raise HTTPException(400,'Décision invalide')
 if payload.decision=='started':
  if payload.kind not in ('movie','tv') or not payload.tmdb_id or payload.tmdb_id<=0:raise HTTPException(400,'Film ou série TMDB requis')
  if payload.kind=='tv' and (payload.season is None or payload.episode is None or payload.season<0 or payload.episode<=0):raise HTTPException(400,'Épisode requis')
  details=tmdb(f'{payload.kind}/{payload.tmdb_id}')
  if payload.kind=='tv':
   eps=tmdb(f'tv/{payload.tmdb_id}/season/{payload.season}').get('episodes',[])
   if not any(e.get('episode_number')==payload.episode for e in eps):raise HTTPException(400,'Épisode absent de TMDB')
  title=details.get('title') or details.get('name')
  if not title:raise HTTPException(400,'Titre TMDB introuvable')
 with closing(conn()) as c:
  c.execute('BEGIN IMMEDIATE')
  row=c.execute("SELECT * FROM netflix_unmatched WHERE id=? AND status='pending'",(row_id,)).fetchone()
  if not row:raise HTTPException(404,'Élément déjà traité ou introuvable')
  if payload.decision=='started':
   c.execute("INSERT OR IGNORE INTO library(kind,tmdb_id,title,poster,status,watched) VALUES(?,?,?,?,?,0)",(payload.kind,payload.tmdb_id,title,details.get('poster_path'),'watching'))
   if payload.kind=='movie':c.execute("UPDATE library SET status='watching' WHERE kind='movie' AND tmdb_id=? AND watched=0",(payload.tmdb_id,))
   c.execute('CREATE TABLE IF NOT EXISTS netflix_started_episodes(row_id INTEGER PRIMARY KEY,tmdb_id INTEGER NOT NULL,season INTEGER NOT NULL,episode INTEGER NOT NULL,started_at TEXT NOT NULL)')
   if payload.kind=='tv':c.execute('INSERT OR REPLACE INTO netflix_started_episodes(row_id,tmdb_id,season,episode,started_at) VALUES(?,?,?,?,?)',(row_id,payload.tmdb_id,payload.season,payload.episode,row['viewed_at']))
  c.execute('UPDATE netflix_unmatched SET status=?,resolved_kind=?,resolved_tmdb_id=?,resolved_season=?,resolved_episode=? WHERE id=?',('started' if payload.decision=='started' else 'not_watched',payload.kind if payload.decision=='started' else None,payload.tmdb_id if payload.decision=='started' else None,payload.season if payload.decision=='started' else None,payload.episode if payload.decision=='started' else None,row_id))
  c.execute('DELETE FROM netflix_ai_suggestions WHERE row_id=?',(row_id,))
  c.commit()
 return {'ok':True,'status':payload.decision}

@app.get('/api/import/netflix/unmatched')
def netflix_unmatched(status:str='pending'):
 if status not in ('pending','resolved'):raise HTTPException(400,'Statut invalide')
 with closing(conn()) as c:return [dict(x) for x in c.execute('SELECT * FROM netflix_unmatched WHERE status=? ORDER BY viewed_at DESC,id DESC LIMIT 3000',(status,))]

@app.post('/api/import/netflix/unmatched/{row_id}/resolve')
def resolve_netflix(row_id:int,payload:NetflixResolve):
 if payload.kind not in ('tv','movie'):raise HTTPException(400,'Type invalide')
 if payload.tmdb_id<=0:raise HTTPException(400,'Identifiant TMDB invalide')
 if payload.kind=='tv' and (payload.season is None or payload.episode is None or payload.season<0 or payload.episode<=0):
  raise HTTPException(400,'Saison et épisode requis')
 details=tmdb(f'{payload.kind}/{payload.tmdb_id}')
 if payload.kind=='tv':
  episodes=tmdb(f'tv/{payload.tmdb_id}/season/{payload.season}').get('episodes',[])
  if not any(x.get('episode_number')==payload.episode for x in episodes):raise HTTPException(400,'Épisode absent de TMDB')
 title=details.get('name') or details.get('title')
 if not title:raise HTTPException(400,'Titre TMDB invalide')
 with closing(conn()) as c:
  c.execute('BEGIN IMMEDIATE')
  row=c.execute('SELECT * FROM netflix_unmatched WHERE id=? AND status=?',(row_id,'pending')).fetchone()
  if not row:raise HTTPException(404,'Élément introuvable ou déjà résolu')
  c.execute('INSERT OR IGNORE INTO library(kind,tmdb_id,title,poster) VALUES(?,?,?,?)',(payload.kind,payload.tmdb_id,title,details.get('poster_path')))
  if payload.kind=='tv':
   c.execute('INSERT OR IGNORE INTO episodes(tmdb_id,season,episode,watched_at) VALUES(?,?,?,?)',(payload.tmdb_id,payload.season,payload.episode,row['viewed_at']))
  else:c.execute('UPDATE library SET watched=1 WHERE kind=? AND tmdb_id=?',('movie',payload.tmdb_id))
  if payload.kind=='tv':
   parsed=netflix_episode_title(row['title'])
   if parsed:
    c.execute('INSERT OR REPLACE INTO netflix_series_memory(series_title,tmdb_id) VALUES(?,?)',(parsed[0],payload.tmdb_id))
    c.execute('INSERT OR REPLACE INTO netflix_episode_memory(netflix_title,tmdb_id,season,episode) VALUES(?,?,?,?)',(row['title'],payload.tmdb_id,payload.season,payload.episode))
  c.execute('UPDATE netflix_unmatched SET status=?,resolved_kind=?,resolved_tmdb_id=?,resolved_season=?,resolved_episode=? WHERE id=?',('resolved',payload.kind,payload.tmdb_id,payload.season,payload.episode,row_id))
  c.commit()
 if payload.kind=='tv':
  parsed=netflix_episode_title(row['title'])
  if parsed:threading.Thread(target=netflix_reanalyse_series,args=(parsed[0],),daemon=True).start()
 return {'ok':True}
