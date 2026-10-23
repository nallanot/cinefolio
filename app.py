import os, sqlite3, urllib.request, urllib.parse, json
from contextlib import closing
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
DB='/data/library.db'; KEY=os.getenv('TMDB_API_KEY','')
app=FastAPI(title='Cinefolio')
def conn():
 c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; return c
@app.on_event('startup')
def init():
 os.makedirs('/data',exist_ok=True)
 with closing(conn()) as c:
  c.executescript('CREATE TABLE IF NOT EXISTS library(kind TEXT NOT NULL, tmdb_id INTEGER NOT NULL, title TEXT NOT NULL, poster TEXT, status TEXT DEFAULT "watchlist", watched INTEGER DEFAULT 0, rating INTEGER, PRIMARY KEY(kind,tmdb_id)); CREATE TABLE IF NOT EXISTS episodes(tmdb_id INTEGER NOT NULL, season INTEGER NOT NULL, episode INTEGER NOT NULL, watched_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(tmdb_id,season,episode));');c.commit()
def tmdb(path,params=None):
 if not KEY: raise HTTPException(503,'Configurez TMDB_API_KEY dans .env')
 q=urllib.parse.urlencode(dict(params or {},api_key=KEY,language='fr-CA'))
 try:
  with urllib.request.urlopen('https://api.themoviedb.org/3/'+path+'?'+q,timeout=12) as r:return json.load(r)
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
 with closing(conn()) as c:
  c.execute('INSERT OR IGNORE INTO library(kind,tmdb_id,title,poster) VALUES(?,?,?,?)',(i.kind,i.tmdb_id,i.title,i.poster));c.commit()
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
