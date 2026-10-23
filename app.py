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
  c.executescript('CREATE TABLE IF NOT EXISTS library(kind TEXT NOT NULL, tmdb_id INTEGER NOT NULL, title TEXT NOT NULL, poster TEXT, status TEXT DEFAULT "watchlist", watched INTEGER DEFAULT 0, rating INTEGER, PRIMARY KEY(kind,tmdb_id)); CREATE TABLE IF NOT EXISTS episodes(tmdb_id INTEGER NOT NULL, season INTEGER NOT NULL, episode INTEGER NOT NULL, watched_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(tmdb_id,season,episode));');c.execute('CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,display_name TEXT,salt TEXT,password_hash TEXT)');c.execute('CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,expires INTEGER)');c.commit()

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
   progress.append({'tmdb_id':item['tmdb_id'],'series':item['title'],'watched':sum(e['watched'] for e in aired),'aired':len(aired),'next_unwatched':next((e for e in sorted(aired,key=lambda x:(x['season'],x['episode'])) if not e['watched']),None)})
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
