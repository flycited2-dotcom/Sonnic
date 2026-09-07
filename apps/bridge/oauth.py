"""Local synthetic-lab OAuth: predefined public client, S256 PKCE, opaque tokens.

No OpenAI API key. Guest sessions are the lab identity, not production accounts.
Tokens/codes/consent handles are stored as hashes and never logged.
"""
import base64
from dataclasses import dataclass
import hashlib
import json
import re
import secrets
from urllib.parse import urlsplit, urlencode

from .queue import BridgeError, Principal, Queue, digest

SCOPE = 'sonnic:process'


@dataclass(frozen=True)
class BridgeSettings:
    origin: str = 'http://127.0.0.1:3000'
    client_id: str = 'sonnic-chatgpt'
    redirect_uri: str | None = None

    def __post_init__(self):
        origin = urlsplit(self.origin)
        if (origin.path or origin.query or origin.fragment or origin.username or origin.password
                or not origin.hostname or origin.scheme not in {'http', 'https'}
                or (origin.scheme == 'http' and origin.hostname not in {'localhost', '127.0.0.1'})):
            raise ValueError('invalid_bridge_origin')
        if self.redirect_uri:
            callback = urlsplit(self.redirect_uri)
            if (callback.fragment or callback.query or callback.username or callback.password
                    or not callback.hostname or callback.scheme not in {'http', 'https'}
                    or (callback.scheme == 'http' and callback.hostname not in {'localhost', '127.0.0.1'})):
                raise ValueError('invalid_oauth_redirect')

    @property
    def resource(self):
        return self.origin + '/mcp'


class LabOAuth:
    def __init__(self, queue: Queue, settings: BridgeSettings):
        self.queue, self.settings = queue, settings
        with queue.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS bridge_consents (
                hash TEXT PRIMARY KEY, owner TEXT NOT NULL, payload TEXT NOT NULL, expires REAL NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS bridge_codes (
                hash TEXT PRIMARY KEY, owner TEXT NOT NULL, payload TEXT NOT NULL, expires REAL NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS bridge_tokens (
                hash TEXT PRIMARY KEY, owner TEXT NOT NULL, client TEXT NOT NULL,
                resource TEXT NOT NULL, scope TEXT NOT NULL, expires REAL NOT NULL)''')

    def validate_request(self, params):
        # ChatGPT adds the optional UI language hint. It must not alter the
        # identity, redirect, scope or resource bound to the authorization.
        params = dict(params)
        locale_hint = params.pop('ui_locales', None)
        if locale_hint is not None and (not isinstance(locale_hint, str)
                or not re.fullmatch(r'[A-Za-z0-9-]{1,35}( [A-Za-z0-9-]{1,35}){0,4}', locale_hint)):
            raise BridgeError('invalid_oauth_request')
        if not self.settings.redirect_uri:
            raise BridgeError('oauth_client_not_configured')
        expected = {'response_type': 'code', 'client_id': self.settings.client_id,
                    'redirect_uri': self.settings.redirect_uri, 'resource': self.settings.resource,
                    'scope': SCOPE, 'code_challenge_method': 'S256'}
        if any(params.get(key) != value for key, value in expected.items()):
            raise BridgeError('invalid_oauth_request')
        if (not re.fullmatch(r'[A-Za-z0-9_-]{43}', params.get('code_challenge', ''))
                or not 16 <= len(params.get('state', '')) <= 512):
            raise BridgeError('invalid_oauth_request')
        if set(params) != set(expected) | {'state', 'code_challenge'}:
            raise BridgeError('invalid_oauth_request')
        return dict(params)

    def clean(self, db):
        now = self.queue.clock()
        for table in ('bridge_consents', 'bridge_codes', 'bridge_tokens'):
            db.execute(f'DELETE FROM {table} WHERE expires<=?', (now,))

    def prepare(self, owner, params):
        payload = self.validate_request(params)
        handle = secrets.token_urlsafe(32)
        with self.queue.db() as db:
            self.clean(db)
            if db.execute('SELECT count(*) FROM bridge_consents WHERE owner=?', (owner,)).fetchone()[0] >= 10:
                raise BridgeError('consent_rate_limited')
            db.execute('INSERT INTO bridge_consents VALUES(?,?,?,?)',
                       (digest(handle), owner, json.dumps(payload), self.queue.clock()+600))
        return handle

    def approve(self, owner, handle, allow):
        with self.queue.db() as db:
            self.clean(db)
            row = db.execute('SELECT * FROM bridge_consents WHERE hash=? AND owner=?', (digest(handle), owner)).fetchone()
            if not row:
                raise BridgeError('invalid_consent')
            payload = json.loads(row['payload'])
            # Revalidate config after a restart/change before redirecting.
            self.validate_request(payload)
            db.execute('DELETE FROM bridge_consents WHERE hash=?', (digest(handle),))
            result = {'state': payload['state'], 'iss': self.settings.origin}
            if allow:
                code = secrets.token_urlsafe(32)
                db.execute('INSERT INTO bridge_codes VALUES(?,?,?,?)',
                           (digest(code), owner, row['payload'], self.queue.clock()+120))
                result['code'] = code
            else:
                result['error'] = 'access_denied'
        return self.settings.redirect_uri + '?' + urlencode(result)

    def exchange(self, params, owner_active):
        if set(params) != {'grant_type', 'code', 'client_id', 'redirect_uri', 'resource', 'code_verifier'}:
            raise BridgeError('invalid_grant')
        verifier = params.get('code_verifier', '')
        if (params.get('grant_type') != 'authorization_code'
                or not re.fullmatch(r'[A-Za-z0-9._~-]{43,128}', verifier)):
            raise BridgeError('invalid_grant')
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        with self.queue.db() as db:
            self.clean(db)
            row = db.execute('SELECT * FROM bridge_codes WHERE hash=?', (digest(params.get('code', '')),)).fetchone()
            if not row or not owner_active(row['owner']):
                raise BridgeError('invalid_grant')
            original = json.loads(row['payload'])
            self.validate_request(original)
            if (any(params.get(k) != original[k] for k in ('client_id', 'redirect_uri', 'resource'))
                    or not secrets.compare_digest(challenge, original['code_challenge'])):
                raise BridgeError('invalid_grant')
            db.execute('DELETE FROM bridge_codes WHERE hash=?', (row['hash'],))
            token = secrets.token_urlsafe(32)
            db.execute('INSERT INTO bridge_tokens VALUES(?,?,?,?,?,?)',
                       (digest(token), row['owner'], original['client_id'], original['resource'], SCOPE, self.queue.clock()+900))
        return {'access_token': token, 'token_type': 'Bearer', 'expires_in': 900, 'scope': SCOPE}

    def authenticate(self, token, owner_active):
        with self.queue.db() as db:
            row = db.execute('SELECT * FROM bridge_tokens WHERE hash=? AND expires>?',
                             (digest(token), self.queue.clock())).fetchone()
        if (not row or row['resource'] != self.settings.resource or row['client'] != self.settings.client_id
                or row['scope'] != SCOPE or not owner_active(row['owner'])):
            raise BridgeError('invalid_token')
        return Principal('oauth:' + row['client'], row['owner'], frozenset({'worker'}))

    def revoke(self, owner):
        with self.queue.db() as db:
            for table in ('bridge_tokens', 'bridge_codes', 'bridge_consents'):
                db.execute(f'DELETE FROM {table} WHERE owner=?', (owner,))
