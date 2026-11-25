"""Address reputation SIGNALS from the free, keyless GoPlus Security API (data sources named by the API,
e.g. SlowMist/BlockSec). These are third-party flags shown WITH their source -- not verdicts by this tool.
Individual flags can be wrong in either direction; 'no flags' is not proof of legitimacy.
API docs: https://docs.gopluslabs.io/reference/address-security
"""
import json, urllib.parse, urllib.request

API = 'https://api.gopluslabs.io/api/v1/address_security/'

FLAG_MEANING = {  # field -> short meaning; value '1' means the data source flagged it
    'cybercrime': 'linked to cybercrime', 'money_laundering': 'linked to money laundering',
    'financial_crime': 'linked to financial crime', 'darkweb_transactions': 'darkweb transactions',
    'phishing_activities': 'phishing activity', 'stealing_attack': 'stealing attack involvement',
    'blackmail_activities': 'blackmail activity', 'sanctioned': 'flagged as sanctioned by GoPlus sources',
    'mixer': 'mixer involvement', 'fake_token': 'fake token involvement', 'fake_kyc': 'fake KYC',
    'honeypot_related_address': 'honeypot-related', 'malicious_mining_activities': 'malicious mining',
    'gas_abuse': 'gas abuse', 'blacklist_doubt': 'blacklist doubt (suspected)',
    'contract_address': 'is a contract (not an EOA)', 'reinit': 'reinitializable contract pattern',
    'fake_standard_interface': 'fake standard interface',
}


def check(address, chainid='1', timeout=30, opener=None):
    """Returns {'ok': True, 'flags': {meaning...}, 'counts': {...}, 'data_source': str} or {'ok': False, 'error': str}.
    Never raises; a failure is an explicit unavailable result, not 'no flags'."""
    op = opener or urllib.request.urlopen
    try:
        url = API + urllib.parse.quote(address) + '?' + urllib.parse.urlencode({'chain_id': chainid})
        req = urllib.request.Request(url, headers={'User-Agent': 'dappman/0.5 (read-only reputation check)'})
        d = json.loads(op(req, timeout=timeout).read().decode())
    except Exception as e:
        return {'ok': False, 'error': 'GoPlus unavailable: ' + str(e)[:120]}
    if d.get('code') != 1 or not isinstance(d.get('result'), dict):
        return {'ok': False, 'error': 'GoPlus returned code %r (%s) for chain %s' % (d.get('code'), str(d.get('message'))[:80], chainid)}
    r = d['result']
    flags = {FLAG_MEANING[k] for k, v in r.items() if k in FLAG_MEANING and str(v) == '1'}
    try:
        n = int(r.get('number_of_malicious_contracts_created') or 0)
    except ValueError:
        n = 0
    if n:
        flags.add('created %d malicious contract(s) per GoPlus sources' % n)
    return {'ok': True, 'flags': sorted(flags), 'data_source': r.get('data_source') or '(none named)',
            'note': 'third-party signals, not a verdict; no-flags is not proof of legitimacy'}
