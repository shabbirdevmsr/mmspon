import os, time, threading, logging
from datetime import datetime, timezone
from urllib.parse import quote_plus
import requests
from dotenv import load_dotenv
from flask import Flask, jsonify
from pymongo import MongoClient, ASCENDING

BASE_DIR=os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR,'.env'))
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

def env(k,d=''): return os.getenv(k,d)
MONGO_USER=env('MONGO_USER'); MONGO_PASS=env('MONGO_PASS'); MONGO_HOST=env('MONGO_HOST','localhost'); MONGO_PORT=int(env('MONGO_PORT','27017')); MONGO_DB=env('MONGO_DB','mo8022_bachelor')
TG_API_SERVER=env('TG_API_SERVER','https://api.telegram.org').rstrip('/')
MONGO_URI=f"mongodb://{quote_plus(MONGO_USER)}:{quote_plus(MONGO_PASS)}@{MONGO_HOST}:{MONGO_PORT}/?authSource={quote_plus(MONGO_DB)}"
mongo=MongoClient(MONGO_URI,serverSelectionTimeoutMS=10000,connectTimeoutMS=10000)
db=mongo[MONGO_DB]
bots=db.bots; sources=db.sources; destinations=db.destinations; users=db.users; videos=db.videos; jobs=db.jobs; channel_posts=db.channel_posts
jobs.create_index([('status',ASCENDING),('created_at',ASCENDING)])
videos.create_index([('source_id',ASCENDING),('external_id',ASCENDING)],unique=True)

def now(): return datetime.now(timezone.utc)
def tg(token, method, payload=None):
    r=requests.post(f'{TG_API_SERVER}/bot{token}/{method}',json=payload or {},timeout=90); r.raise_for_status(); return r.json()

def api_items(s,page):
    r=requests.get(s['base_url'],params={'page':page},timeout=90); r.raise_for_status(); d=r.json(); return d.get('data') or [], d.get('pagination') or {}

def process_source(s):
    page=int(s.get('state',{}).get('current_page',1) or 1)
    while True:
        items,p=api_items(s,page)
        if not items:
            sources.update_one({'_id':s['_id']},{'$set':{'state.current_page':page,'state.slug':s.get('state',{}).get('slug'),'state.updated_at':now()}}); break
        for item in items:
            ext=str(item.get('id') or item.get('slug') or item.get('video') or '')
            if not ext: continue
            videos.update_one({'source_id':s['_id'],'external_id':ext},{'$setOnInsert':{'source_id':s['_id'],'external_id':ext,'title':item.get('name') or item.get('title') or 'Untitled','slug':item.get('slug'),'image_url':item.get('image'),'video_url':item.get('video') or item.get('downloadLink'),'status':'discovered','created_at':now()}},upsert=True)
        next_page=p.get('next_page')
        sources.update_one({'_id':s['_id']},{'$set':{'state.current_page':page,'state.slug':p.get('slug') or s.get('state',{}).get('slug'),'state.total_pages':p.get('total_pages'),'state.total_items':p.get('total_items'),'state.has_next':bool(p.get('has_next')),'state.updated_at':now()}})
        if not p.get('has_next'): break
        page=int(next_page or page+1)

def process_job(j):
    jobs.update_one({'_id':j['_id']},{'$set':{'status':'running','started_at':now()}})
    try:
        for sid in j.get('source_ids',[]):
            s=sources.find_one({'_id':sid,'enabled':True})
            if s: process_source(s)
        jobs.update_one({'_id':j['_id']},{'$set':{'status':'completed','finished_at':now()}})
    except Exception as e:
        logging.exception('job failed')
        jobs.update_one({'_id':j['_id']},{'$set':{'status':'failed','error':str(e),'finished_at':now()}})

def worker():
    while True:
        j=jobs.find_one_and_update({'status':'queued'},{'$set':{'status':'claimed','claimed_at':now()}},sort=[('created_at',ASCENDING)])
        if j: process_job(j)
        else: time.sleep(2)

app=Flask(__name__)
@app.get('/health')
def health():
    mongo.admin.command('ping'); return jsonify({'ok':True,'db':MONGO_DB,'time':now().isoformat()})

if __name__=='__main__':
    mongo.admin.command('ping'); logging.info('MongoDB connected: %s',MONGO_DB)
    threading.Thread(target=worker,daemon=True).start()
    app.run(host=env('PANEL_HOST','0.0.0.0'),port=int(env('PANEL_PORT','5000')))
