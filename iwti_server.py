"""IWTI Free Edition — zero API keys, Python 3.9+ standard library only.
Start: python iwti_server_free.py
Open: http://127.0.0.1:8765/iwti_dashboard.html
Data: SANS ISC / DShield topports_reports feed. Feed downloaded at most hourly.
Attribution: SANS Technology Institute, Internet Storm Center https://isc.sans.edu
"""
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import quote
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent
HOST = os.environ.get('IWTI_HOST', '127.0.0.1')
PORT = int(os.environ.get('PORT', os.environ.get('IWTI_PORT', '8765')))
FEED_URL = 'https://feeds.dshield.org/feeds/topports_reports.txt'
MIN_INTERVAL = 3600  # SANS recommends not downloading static feeds more than once per hour.
STATE_PATH = ROOT / 'iwti_free_cache.json'
AVIATION_CACHE_PATH = ROOT / 'iwti_aviation_cache.json'
AVIATION_INTERVAL = 3600
# Civil aviation / airspace-disruption coverage only. No military live tracking.
AVIATION_QUERIES = [
    'Israel airspace closure flight cancellations',
    'Middle East airspace restrictions commercial airlines',
    'Israel aviation NOTAM airport disruption',
]
LOCK = threading.Lock()


def load_cache():
    try:
        obj = json.loads(STATE_PATH.read_text(encoding='utf-8'))
        return obj if isinstance(obj, dict) else {}
    except (OSError, ValueError):
        return {}

CACHE = load_cache()


def parse_feed(text):
    """Parse tab/space-separated numeric port report counts; ignore comments & headers."""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        fields = re.split(r'\s+', line)
        if len(fields) < 2 or not fields[0].isdigit():
            continue
        port = int(fields[0])
        if not 0 < port <= 65535:
            continue
        # DShield feed exposes counts ordered after the port. We label the first
        # observed metric as reported observations, without claiming unique attacks.
        metric = fields[1].replace(',', '')
        if not metric.isdigit():
            continue
        rows.append({'port': port, 'observations': int(metric)})
    unique = {}
    for item in rows:
        unique[item['port']] = item
    return sorted(unique.values(), key=lambda a: a['observations'], reverse=True)[:20]


def refresh():
    now = time.time()
    with LOCK:
        if now - float(CACHE.get('last_attempt', 0)) < MIN_INTERVAL:
            return CACHE
        CACHE['last_attempt'] = now
        try:
            req = Request(FEED_URL, headers={'User-Agent': 'IWTI-Free/1.0 (public feed; hourly fetch)', 'Accept': 'text/plain'})
            with urlopen(req, timeout=18) as resp:
                raw = resp.read(160000).decode('utf-8', errors='replace')
            ports = parse_feed(raw)
            if not ports:
                raise ValueError('No numeric port records found in DShield feed')
            CACHE.update(ports=ports, source_updated_at=datetime.now(timezone.utc).isoformat(), error=None)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            CACHE['error'] = str(exc)[:200]
        try:
            STATE_PATH.write_text(json.dumps(CACHE, ensure_ascii=False), encoding='utf-8')
        except OSError:
            pass
        return CACHE



def refresh_aviation():
    """Collect published civil aviation disruption headlines (not aircraft telemetry)."""
    try:
        cached = json.loads(AVIATION_CACHE_PATH.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        cached = {}
    now = time.time()
    if now - float(cached.get('last_attempt', 0)) < AVIATION_INTERVAL:
        return cached
    cached['last_attempt'] = now
    articles = []
    errors = []
    for q in AVIATION_QUERIES:
        url = 'https://news.google.com/rss/search?q=' + quote(q + ' when:7d') + '&hl=en&gl=US&ceid=US:en'
        try:
            req = Request(url, headers={'User-Agent':'Mozilla/5.0 (IWTI public-news aggregation)'})
            with urlopen(req, timeout=12) as r:
                root = ET.fromstring(r.read(450000))
            for item in root.findall('./channel/item')[:35]:
                title = (item.findtext('title') or '').strip()
                link = (item.findtext('link') or '').strip()
                published = (item.findtext('pubDate') or '').strip()
                if title and link.startswith('https://'):
                    articles.append({'title':title[:220], 'url':link, 'published':published, 'topic':q})
        except (OSError, ValueError, ET.ParseError) as exc:
            errors.append(type(exc).__name__)
    seen = set()
    unique = []
    for a in articles:
        key = a['title'].casefold()
        if key not in seen:
            unique.append(a)
            seen.add(key)
    if unique:
        cached.update(items=unique[:70], source_updated_at=datetime.now(timezone.utc).isoformat(), error=None)
    else:
        cached['error']='; '.join(errors) or 'News feed returned no articles'
    try:
        AVIATION_CACHE_PATH.write_text(json.dumps(cached, ensure_ascii=False), encoding='utf-8')
    except OSError:
        pass
    return cached


def aviation_payload():
    cache = refresh_aviation()
    if cache.get('items'): record_snapshot(aviation_count=len(cache['items']))
    items = cache.get('items', [])
    return {
        'source': 'Google News RSS • published civil aviation disruption reports',
        'source_updated_at': cache.get('source_updated_at'),
        'refresh_interval_seconds': AVIATION_INTERVAL,
        'articles_7d':len(items),
        'items':items[:18],
        'stale':bool(cache.get('error')),
        'error':cache.get('error'),
        'metric':'unique_public_headline_count_7d',
        'note':'Headline count is NOT fighter-jet activity or probability of war. Duplicate removal is approximate. Search results are not comprehensive; media attention bias applies.',
        'github_references':[
            {'name':'OpenSky Network API', 'url':'https://github.com/openskynetwork/opensky-api', 'use':'Aviation research API; not connected to live military tracking'},
            {'name':'FlightRadarAPI (unofficial)', 'url':'https://github.com/JeanExtreme002/FlightRadarAPI', 'use':'Unofficial FR24 SDK; not connected or scraped'},
            {'name':'airplanes.live docs', 'url':'https://airplanes.live/', 'use':'Reference only; not used for live aircraft targeting'}
        ]
    }


# Public news monitoring. We do not infer operational actions or assign probabilities to war.
import sqlite3
from email.utils import parsedate_to_datetime
NEWS_INTERVAL = 1800
NEWS_QUERIES = {
 'מתיחות אזורית': 'Israel regional tensions diplomacy',
 'הצהרות רשמיות': 'Israel Iran official statements diplomacy',
 'משבר אזורי': 'Middle East conflict ceasefire negotiations',
}
NEWS_CACHE_PATH = ROOT / 'iwti_news_cache.json'
DATA_DIR = Path(os.environ.get('IWTI_DATA_DIR', str(ROOT)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / 'iwti_history.sqlite3'
NEWS_LOCK = threading.Lock()

def db_init():
    with sqlite3.connect(DB_PATH, timeout=8) as db:
        db.execute('CREATE TABLE IF NOT EXISTS samples (sample_at TEXT PRIMARY KEY, news_count INTEGER, publishers INTEGER, cyber_count INTEGER, aviation_count INTEGER)')

def record_snapshot(news_count=None, publishers=None, cyber_count=None, aviation_count=None):
    # Bucket hourly for fair comparisons; don't count page views as samples.
    key = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:00:00Z')
    try:
        with sqlite3.connect(DB_PATH, timeout=8) as db:
            db.execute('INSERT OR IGNORE INTO samples(sample_at,news_count,publishers,cyber_count,aviation_count) VALUES (?,?,?,?,?)', (key,None,None,None,None))
            db.execute('UPDATE samples SET news_count=COALESCE(?,news_count), publishers=COALESCE(?,publishers), cyber_count=COALESCE(?,cyber_count), aviation_count=COALESCE(?,aviation_count) WHERE sample_at=?', (news_count,publishers,cyber_count,aviation_count,key))
            db.execute("DELETE FROM samples WHERE sample_at < strftime('%Y-%m-%dT%H:00:00Z','now','-365 days')")
    except (sqlite3.Error, OSError):
        pass

def history_payload():
    try:
        with sqlite3.connect(DB_PATH, timeout=8) as db:
            rows=db.execute('SELECT sample_at, news_count, publishers, cyber_count, aviation_count FROM samples ORDER BY sample_at DESC LIMIT 1000').fetchall()
        return {'samples':[dict(zip(('sample_at','news_count','publishers','cyber_count','aviation_count'),row)) for row in rows], 'durable': bool(os.environ.get('IWTI_DATA_DIR')), 'note':'Sampling history is not training labels or calibrated AI forecasts. Render Free local filesystem is ephemeral.'}
    except sqlite3.Error as e:
        return {'samples':[], 'error':str(e)[:150]}

def news_payload():
    with NEWS_LOCK:
        try: cached=json.loads(NEWS_CACHE_PATH.read_text(encoding='utf8'))
        except (OSError,ValueError): cached={}
        now=time.time()
        if now - float(cached.get('last_attempt',0)) >= NEWS_INTERVAL:
            cached['last_attempt']=now
            articles=[];errs=[]
            for topic,q in NEWS_QUERIES.items():
                u='https://news.google.com/rss/search?q='+quote(q+' when:7d')+'&hl=en&gl=US&ceid=US:en'
                try:
                    req=Request(u,headers={'User-Agent':'Mozilla/5.0 (IWTI public RSS monitor)'})
                    with urlopen(req,timeout=12) as r: xml=r.read(450000)
                    root=ET.fromstring(xml)
                    for it in root.findall('./channel/item')[:40]:
                        title=(it.findtext('title') or '').strip(); link=(it.findtext('link') or '').strip()
                        if not title or not link.startswith('https://'):continue
                        pub=it.find('source')
                        publisher=(pub.text or '').strip() if pub is not None and pub.text else 'לא מזוהה'
                        articles.append({'title':title[:240], 'url':link, 'publisher':publisher[:90], 'published':(it.findtext('pubDate') or '')[:80], 'topic':topic})
                except (OSError,ValueError,ET.ParseError) as e:errs.append(type(e).__name__)
            seen=set();uni=[]
            for it in articles:
                norm=re.sub(r'\s+',' ',it['title'].casefold()).strip()
                if norm not in seen:seen.add(norm);uni.append(it)
            if uni:
                cached.update(items=uni[:120],source_updated_at=datetime.now(timezone.utc).isoformat(),error=None)
            else: cached['error']='; '.join(errs) or 'No RSS items returned'
            try:NEWS_CACHE_PATH.write_text(json.dumps(cached,ensure_ascii=False),encoding='utf8')
            except OSError:pass
        items=cached.get('items',[])
        pubs=len({i.get('publisher') for i in items if i.get('publisher')!='לא מזוהה'})
        if items:record_snapshot(news_count=len(items),publishers=pubs)
        return {'source':'Google News RSS public aggregator', 'source_updated_at':cached.get('source_updated_at'), 'refresh_interval_seconds':NEWS_INTERVAL,'count_7d':len(items),'publisher_count':pubs,'items':items[:50],'stale':bool(cached.get('error')),'error':cached.get('error'),'metric':'sampled_unique_news_headlines_in_search_last_7_days','note':'Number of articles is not the probability of war or independent verification; coverage incomplete and potentially biased.'}

db_init()

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self):
        from urllib.parse import urlsplit, unquote
        path = unquote(urlsplit(self.path).path)
        if path in ('/', '/index.html'):
            self.send_response(302)
            self.send_header('Location', '/iwti_dashboard.html')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            return
        # Public hosting: expose the dashboard and explicitly supported APIs only.
        # Never expose Python code, caches, deployment config, or directory indexes.
        if path not in ('/iwti_dashboard.html', '/api/cyber', '/api/aviation', '/api/news', '/api/history', '/api/status', '/favicon.ico'):
            self.send_error(404, 'Not Found')
            return
        if path == '/favicon.ico':
            self.send_error(404, 'Not Found')
            return
        if path == '/api/status':
            body=json.dumps({'service':'IWTI V6','status':'up','sources':{'cyber':'SANS DShield topports_reports','news':'Google News RSS','aviation':'Google News RSS'},'note':'Sources are retrieved on request and cached; this status does not guarantee upstream availability.'},ensure_ascii=False).encode('utf8')
            self.send_response(200);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body);return
        if path in ('/api/news', '/api/history', '/api/export'):
            result = news_payload() if path == '/api/news' else history_payload()
            body=json.dumps(result,ensure_ascii=False).encode('utf8')
            self.send_response(200)
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            
            self.send_header('Content-Length',str(len(body)))
            self.end_headers();self.wfile.write(body)
            return
        if path == '/api/aviation':
            body=json.dumps(aviation_payload(),ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Cache-Control','no-store')
            self.send_header('Content-Length',str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path != '/api/cyber':
            return super().do_GET()
        cache = refresh()
        ports = cache.get('ports', [])
        if ports: record_snapshot(cyber_count=sum(x['observations'] for x in ports))
        if ports:
            result = {
                'source': 'SANS ISC / DShield — Top Ports by Reports',
                'source_url': FEED_URL,
                'source_updated_at': cache.get('source_updated_at'),
                'unit': 'count',
                'metric': 'reported_firewall_observations',
                'refresh_interval_seconds': MIN_INTERVAL,
                'stale': bool(cache.get('error')),
                'pairs': [{'origin': 'SENSORS', 'target': f"PORT {x['port']}", 'count': x['observations'], 'value': x['observations']} for x in ports],
                'note': 'Sample of firewall reports from participating sensors; not total global attacks, not per-minute attack volume, no country-to-country attribution. Dashboard checks every minute; upstream downloads at most hourly.'
            }
            status = 200
        else:
            result = {'error': 'DShield feed unavailable or cannot be parsed: ' + str(cache.get('error', 'unknown')),
                      'source': 'SANS ISC / DShield', 'demo': False}
            status = 503
        body = json.dumps(result, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == '__main__':
    print('IWTI V4 LIVE — news every 30 min, aviation and cyber hourly')
    print(f'Open http://{HOST}:{PORT}/iwti_dashboard.html')
    print('Feed credited to SANS Technology Institute / Internet Storm Center')
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
