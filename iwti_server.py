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
PORT = int(os.environ.get('IWTI_PORT', '8765'))
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

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self):
        if self.path.split('?', 1)[0] == '/api/aviation':
            body=json.dumps(aviation_payload(),ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Cache-Control','no-store')
            self.send_header('Content-Length',str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.split('?', 1)[0] != '/api/cyber':
            return super().do_GET()
        cache = refresh()
        ports = cache.get('ports', [])
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
    print('IWTI FREE V3 — no token, no packages; aviation news hourly')
    print(f'Open http://{HOST}:{PORT}/iwti_dashboard.html')
    print('Feed credited to SANS Technology Institute / Internet Storm Center')
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
