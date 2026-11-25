"""OFAC SDN sanctions lookup for crypto addresses. Official source only, with its publish date:
the tool reports whether an address appears on the OFAC SDN list (as data, with the list's date) --
never a 'clean' verdict: absence from the list proves nothing.

Source: U.S. Treasury OFAC, SDN.XML publication preview (same file https://www.treasury.gov/ofac/downloads/sdn.xml redirects to).
The 30MB XML is parsed once and only the 'Digital Currency Address - *' entries (~70KB JSON) are cached in the local DB.
"""
import json, time, urllib.request
import xml.etree.ElementTree as ET

OFAC_URL = 'https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.XML'
NS = '{https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/XML}'
REFRESH_AFTER = 7 * 86400  # seconds; OFAC can update any day, so the list's own publish date is always shown


def parse_sdn_xml(path):
    """Extract digital-currency addresses. Returns {'publish_date': str, 'entries': {addr_lower: {'entity': str, 'currencies': [...]}}}."""
    pub = None
    entries = {}
    for _ev, el in ET.iterparse(path, events=('end',)):
        tag = el.tag.replace(NS, '')
        if tag == 'Publish_Date' and pub is None:
            pub = el.text
        elif tag == 'sdnEntry':
            parts = []
            for n in ('firstName', 'lastName'):
                e = el.find(f'{NS}{n}')
                if e is not None and e.text:
                    parts.append(e.text)
            name = ' '.join(parts)
            for idel in el.iter(f'{NS}id'):
                t = (idel.findtext(f'{NS}idType') or '')
                if t.startswith('Digital Currency Address'):
                    cur = t.split('-', 1)[1].strip().lower()
                    num = (idel.findtext(f'{NS}idNumber') or '').strip().lower()
                    if not num:
                        continue
                    ent = entries.setdefault(num, {'entity': name, 'currencies': []})
                    if cur not in ent['currencies']:
                        ent['currencies'].append(cur)
                    if not ent['entity']:
                        ent['entity'] = name
            el.clear()
    return {'publish_date': pub, 'entries': entries}


def refresh(store, url=OFAC_URL, timeout=120):
    """Download + parse + cache. Returns summary dict; raises on failure (caller shows the error, no fake data)."""
    import tempfile, os
    fd, tmp = tempfile.mkstemp(suffix='.xml')
    os.close(fd)
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'dappman/0.5 (read-only sanctions check)'})
        with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, 'wb') as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
        data = parse_sdn_xml(tmp)
    finally:
        try: os.unlink(tmp)
        except OSError: pass
    store.set_setting('ofac_sdn', json.dumps(data))
    store.set_setting('ofac_sdn_fetched_at', int(time.time()))
    return {'publish_date': data['publish_date'], 'addresses': len(data['entries'])}


def load(store):
    raw = store.get_setting('ofac_sdn')
    if not raw:
        return None
    d = json.loads(raw)
    d['fetched_at'] = int(store.get_setting('ofac_sdn_fetched_at', 0) or 0)
    return d


def stale(store, now=None):
    fetched = int(store.get_setting('ofac_sdn_fetched_at', 0) or 0)
    return not fetched or (now or time.time()) - fetched > REFRESH_AFTER


def check(store, address):
    """Match an address against the cached list. None if unknown (list absent) or not listed --
    'unknown' and 'not listed' are different states and callers must not merge them."""
    d = load(store)
    if d is None:
        return {'state': 'unknown', 'detail': 'OFAC list not downloaded yet (run: dappman sanctions-update)'}
    ent = d['entries'].get((address or '').lower())
    age_days = int((time.time() - d['fetched_at']) / 86400) if d.get('fetched_at') else None
    is_stale = age_days is None or age_days * 86400 > REFRESH_AFTER
    base = {'publish_date': d['publish_date'], 'fetched_at': d.get('fetched_at'), 'cache_age_days': age_days,
            'stale': is_stale,
            'network_note': 'OFAC lists an address string per currency label; EVM addresses are identical on every EVM chain, so a match is by address, not proof of activity on this chain'}
    if ent:
        return {**base, 'state': 'listed', 'entity': ent['entity'], 'currencies': ent['currencies'],
                'detail': 'address appears on the cached OFAC SDN list (a fact about the list, not a legal conclusion)'
                          + ('; cache is OLD, list may have changed since' if is_stale else '')}
    if is_stale:  # an old cache cannot say 'not listed' with any freshness: fail closed to unknown
        return {**base, 'state': 'unknown', 'detail': f'cached OFAC list is {age_days} days old (limit {REFRESH_AFTER // 86400}); not listed in that OLD snapshot, but freshness is not established. Run sanctions-update'}
    return {**base, 'state': 'not_listed',
            'detail': 'not on this OFAC SDN snapshot; not proof of legitimacy'}
