import os, sqlite3, urllib.request, urllib.parse, json, time, threading, secrets, hashlib, hmac
from contextlib import closing
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
  c.executescript('CREATE TABLE IF NOT EXISTS library(kind TEXT NOT NULL, tmdb_id INTEGER NOT NULL, title TEXT NOT NULL, poster TEXT, status TEXT DEFAULT "watchlist", watched INTEGER DEFAULT 0, rating INTEGER, PRIMARY KEY(kind,tmdb_id)); CREATE TABLE IF NOT EXISTS episodes(tmdb_id INTEGER NOT NULL, season INTEGER NOT NULL, episode INTEGER NOT NULL, watched_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(tmdb_id,season,episode));');c.execute('CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,display_name TEXT,salt TEXT,password_hash TEXT)');c.execute('CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,expires INTEGER)');c.execute('CREATE TABLE IF NOT EXISTS custom_providers(kind TEXT NOT NULL,tmdb_id INTEGER NOT NULL,name TEXT NOT NULL,provider_id INTEGER,logo_path TEXT,PRIMARY KEY(kind,tmdb_id,name))');c.commit()

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
 return tmdb('search/multi',{'query':q,'include_adult':'false'}).get('results',[])
@app.get('/api/library')
def library():
 with closing(conn()) as c:return [dict(x) for x in c.execute('SELECT * FROM library ORDER BY title')]
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
import csv, io, re
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

def netflix_episode_title(title):
 # Netflix generally exports "Series: Season 1: Episode title".
 parts=[x.strip() for x in title.split(':')]
 if len(parts)<3:return None
 match=re.search(r'(?:season|saison|series|série|partie|part)\s*(\d+)',parts[1],re.I)
 if not match:return None
 return parts[0],int(match.group(1)),':'.join(parts[2:]).strip()

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
 norm=lambda t:re.sub(r'[^\w]+',' ',t.casefold()).strip()
 by_title={norm(x['title']):x['tmdb_id'] for x in shows}
 movie_titles={norm(x['title']):x['tmdb_id'] for x in movies}
 cache={}
 proposed={}; unmatched=[]; movies_found=set(); invalid=0
 for row in rows:
  title=(row.get(title_col) or '').strip();when=netflix_date(row.get(date_col) or '')
  if not title or not when:invalid+=1;continue
  parsed=netflix_episode_title(title)
  if parsed:
   series,season,episode_name=parsed
   tmdb_id=by_title.get(norm(series))
   if tmdb_id is None:
    if len(unmatched)<30:unmatched.append(title)
    continue
   key=(tmdb_id,season)
   if key not in cache:
    try:cache[key]=tmdb(f'tv/{tmdb_id}/season/{season}').get('episodes',[])
    except Exception:cache[key]=[]
   exact=[e for e in cache[key] if norm(e.get('name') or '')==norm(episode_name)]
   # Never guess episode identity when titles do not match.
   if len(exact)!=1:
    if len(unmatched)<30:unmatched.append(title)
    continue
   ep=int(exact[0]['episode_number']);episode_key=(tmdb_id,season,ep)
   if episode_key not in existing:proposed.setdefault(episode_key,when)
  elif norm(title) in movie_titles:
   movies_found.add(movie_titles[norm(title)])
  elif len(unmatched)<30:unmatched.append(title)
 result={'total_rows':len(rows),'episodes_to_import':len(proposed),'movies_to_mark_watched':len(movies_found),'already_watched_not_overwritten':len(rows)-len(proposed)-len(movies_found)-invalid-len(unmatched),'unmatched_examples':unmatched,'invalid_rows':invalid,'note':'Correspondance exacte avec les séries déjà dans Cinéfolio. Les éléments non reconnus sont ignorés.'}
 if not payload.confirm:return result
 with closing(conn()) as c:
  for (tmdb_id,season,episode),when in proposed.items():
   c.execute('INSERT OR IGNORE INTO episodes(tmdb_id,season,episode,watched_at) VALUES(?,?,?,?)',(tmdb_id,season,episode,when))
  for tmdb_id in movies_found:c.execute("UPDATE library SET watched=1 WHERE kind='movie' AND tmdb_id=?",(tmdb_id,))
  c.commit()
 return {**result,'imported':True}
