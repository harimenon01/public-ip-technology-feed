#!/usr/bin/env python3
"""Collect only headline metadata from an explicit set of public RSS/Atom feeds."""
import datetime as dt
import email.utils
import hashlib
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MAX_BYTES = 2_000_000
TIMEOUT = 20
RETENTION = dt.timedelta(days=30)
MAX_ITEMS = 400

def iso(value):
    if not value:
        return None
    try:
        d = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        try:
            d = email.utils.parsedate_to_datetime(value)
        except (ValueError, TypeError, OverflowError):
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc).isoformat().replace('+00:00', 'Z')

def canonical(url):
    u = urllib.parse.urlsplit(url.strip())
    if u.scheme.lower() not in ('http', 'https') or not u.hostname or u.username or u.password:
        return None
    q = urllib.parse.parse_qsl(u.query, keep_blank_values=True)
    q = [(k, v) for k, v in q if not k.lower().startswith('utm_') and k.lower() not in ('fbclid', 'gclid')]
    return urllib.parse.urlunsplit((u.scheme.lower(), u.netloc.lower(), u.path or '/', urllib.parse.urlencode(sorted(q)), ''))

def parse(data, source):
    # Reject DTD and entities rather than allowing internal-entity expansion.
    if re.search(br'<!\s*(DOCTYPE|ENTITY)', data, re.I):
        raise ValueError('DTD or entity declaration rejected')
    root = ET.fromstring(data)
    entries = root.findall('.//item') + root.findall('{http://www.w3.org/2005/Atom}entry')
    if root.tag not in ('rss', '{http://www.w3.org/2005/Atom}feed', '{http://purl.org/rss/1.0/}RDF'):
        raise ValueError('unsupported feed format')
    if root.tag == '{http://purl.org/rss/1.0/}RDF':
        entries += root.findall('{http://purl.org/rss/1.0/}item')
    def field(e, names):
        for name in names:
            el = e.find(name)
            if el is not None and ''.join(el.itertext()).strip():
                return ''.join(el.itertext()).strip()
        return ''
    out = []
    for e in entries:
        ns = '{http://www.w3.org/2005/Atom}'
        title = field(e, ['title', ns+'title', '{http://purl.org/rss/1.0/}title'])
        title = html.unescape(re.sub(r'<[^>]*>', '', title)).strip()
        title = re.sub(r'\s+', ' ', title)[:500]
        link = field(e, ['link', '{http://purl.org/rss/1.0/}link'])
        for candidate in e.findall(ns+'link'):
            if candidate.get('rel', 'alternate') == 'alternate' and candidate.get('href'):
                link = candidate.get('href'); break
        link = canonical(urllib.parse.urljoin(source['url'], link)) if link else None
        published = iso(field(e, ['pubDate', ns+'published', ns+'updated', '{http://purl.org/dc/elements/1.1/}date']))
        if title and link and len(link) <= 2048 and published:
            out.append({'id': hashlib.sha256(link.encode()).hexdigest()[:24], 'title': title, 'url': link,
                        'source': source['name'], 'source_id': source['id'], 'published_at': published,
                        'topics': source.get('topics', [])[:50], 'domains': source.get('domains', [])[:50], 'summary': ''})
    return out

class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, hosts): self.hosts = hosts
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        u = urllib.parse.urlsplit(newurl)
        if u.scheme != 'https' or u.hostname not in self.hosts:
            raise ValueError('redirect outside allowed HTTPS hosts')
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def fetch(source, attempts=2):
    u = urllib.parse.urlsplit(source['url'])
    hosts = set(source['allowed_hosts'])
    if u.scheme != 'https' or u.hostname not in hosts or u.username or u.password:
        raise ValueError('source is not on its HTTPS allowlist')
    opener = urllib.request.build_opener(SafeRedirect(hosts))
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(source['url'], headers={'User-Agent': 'PublicFeedCollector/1.0', 'Accept': 'application/rss+xml, application/atom+xml, application/xml, text/xml'})
            with opener.open(req, timeout=TIMEOUT) as response:
                data = response.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES: raise ValueError('feed exceeds size limit')
            return parse(data, source)
        except (OSError, ValueError, ET.ParseError) as exc:
            if attempt + 1 == attempts: raise exc
            time.sleep(1)

def collect(sources, old, now, fetcher=fetch):
    checked = now.isoformat().replace('+00:00', 'Z')
    previous = old.get('items', [])
    records, items, ok = [], [], 0
    for source in sources:
        rec = {k: source[k] for k in ('id', 'name', 'url')}
        rec['checked_at'] = checked
        try:
            found = fetcher(source)
            rec['status'] = 'complete'; ok += 1
            items.extend(found)
        except (OSError, ValueError, ET.ParseError) as exc:
            rec['status'] = 'failed'; rec['error'] = type(exc).__name__
            items.extend(i for i in previous if i.get('source_id') == source['id'])
        records.append(rec)
    # Retain prior dated items from successful sources too, since feeds are often short.
    valid_ids = {s['id'] for s in sources}
    items.extend(i for i in previous if i.get('source_id') in valid_ids)
    cutoff = now - RETENTION
    dedup = {}
    for item in items:
        date = iso(item.get('published_at', ''))
        url = canonical(item.get('url', ''))
        if not date or not url: continue
        parsed = dt.datetime.fromisoformat(date.replace('Z', '+00:00'))
        if not cutoff <= parsed <= now: continue
        if url not in dedup: dedup[url] = item
    ranked = sorted(dedup.values(), key=lambda i: i['published_at'], reverse=True)[:MAX_ITEMS]
    status = 'complete' if ok == len(sources) and sources else 'partial' if ok else 'failed'
    return {'schema_version': 1, 'generated_at': checked, 'last_successful_at': checked if ok else old.get('last_successful_at'), 'status': status,
            'sources': records, 'items': ranked}

def main():
    sources = json.loads((ROOT / 'sources.json').read_text(encoding='utf-8'))
    path = ROOT / 'feed.json'
    try: old = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError: old = {}
    result = collect(sources, old, dt.datetime.now(dt.timezone.utc))
    temp = path.with_suffix('.json.tmp')
    temp.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)
    print('status:', result['status'], 'sources:', len(result['sources']), 'items:', len(result['items']))
    return 1 if result['status'] == 'failed' else 0

if __name__ == '__main__': sys.exit(main())

