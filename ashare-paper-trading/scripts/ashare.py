#!/usr/bin/env python3
"""Portable Xiaoman paper-trading client. Python 3.9+, no dependencies.

Default: account query. prepare saves an order locally; submit sends it once.
No automatic retry. HTTP results use {http_status, result}; credentials are never
written to request files. Run --help for usage.
"""
import argparse
from decimal import Decimal, InvalidOperation
import json
import hashlib
import http.client
import os
from pathlib import Path
import re
import shlex
import secrets
import stat
import sys
import tempfile
import time
import socket
import urllib.error
import urllib.parse
import urllib.request
import uuid

ENV_KEYS = ('ASHARE_BASE_URL', 'ASHARE_API_KEY')
VERSION = json.loads((Path(__file__).resolve().parents[1] / 'release.json').read_text(encoding='utf-8'))['version']


class ClientError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A login redirect is a failed authentication, not a new credential target.
        return None


def load_config(env_file=None, base_url=None):
    config = {key: os.environ.get(key, '') for key in ENV_KEYS}
    if env_file:
        try:
            lines = Path(env_file).expanduser().read_text(encoding='utf-8').splitlines()
        except OSError as exc:
            raise ClientError('Cannot read credential file: ' + str(exc))
        for number, line in enumerate(lines, 1):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('export '):
                line = line[7:].lstrip()
            if '=' not in line:
                raise ClientError(f'Invalid env entry on line {number}; use NAME=value.')
            name, value = line.split('=', 1)
            name = name.strip()
            if name not in ENV_KEYS:
                continue
            try:
                tokens = shlex.split(value, comments=True, posix=True)
            except ValueError:
                raise ClientError(f'Invalid quoting in env line {number}.')
            if len(tokens) > 1:
                raise ClientError(f'Env line {number} has unquoted whitespace.')
            config[name] = tokens[0] if tokens else ''
    config['ASHARE_BASE_URL'] = normalize_base(base_url or config['ASHARE_BASE_URL'])
    return config


def normalize_base(value):
    if not isinstance(value, str):
        raise ClientError('Base URL must be a string.')
    value = value.rstrip('/')
    url = urllib.parse.urlsplit(value)
    if (not url.hostname or url.username or url.password or url.query or url.fragment
            or url.path or url.scheme not in ('http', 'https')):
        raise ClientError('Use the site root URL, without credentials, path, query or /api/v1.')
    if url.scheme != 'https' and url.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise ClientError('Use HTTPS; HTTP is allowed only for loopback development.')
    return value


def validate_key(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{8,128}', value):
        raise ClientError('Idempotency key must contain 8–128 ASCII letters, digits or _ . : - .')
    return value


def validate_order(order):
    if not isinstance(order, dict) or set(order) - {'symbol', 'side', 'type', 'quantity', 'limit_price'}:
        raise ClientError('Order must be an object containing only documented order fields.')
    symbol = order.get('symbol')
    if not isinstance(symbol, str) or not re.fullmatch(r'(?:[0-9]{6}\.(?:SH|SZ|BJ)|[0-9]{5}\.HK)', symbol):
        raise ClientError('Use a canonical symbol such as 600036.SH; check instruments for support.')
    if order.get('side') not in ('buy', 'sell') or order.get('type') not in ('market', 'limit'):
        raise ClientError('side must be buy/sell; type must be market/limit.')
    quantity = order.get('quantity')
    hk = symbol.endswith('.HK')
    star = symbol.endswith('.SH') and symbol.startswith(('688', '689'))
    beijing = symbol.endswith('.BJ')
    chinext = symbol.endswith('.SZ') and symbol.startswith('30')
    if type(quantity) is not int or quantity < 1 or quantity > (9_007_199_254_740_991 if hk else 1_000_000):
        raise ClientError('Quantity must be a positive safe integer; CN maximum is 1,000,000. HK server checks official board lots and 3000-lot limit.')
    if not hk and order['side'] == 'buy' and (quantity < (200 if star else 100) or (not star and not beijing and quantity % 100)):
        raise ClientError('CN buys: STAR minimum 200 increment 1; Beijing minimum 100 increment 1; other boards multiples of 100.')
    maximum = (100_000 if order['type'] == 'limit' else 50_000) if star else (300_000 if order['type'] == 'limit' else 150_000) if chinext else None
    if maximum and quantity > maximum:
        raise ClientError('Quantity exceeds this board and order type limit.')
    scale = 1000 if hk else 100
    output = {key: order[key] for key in ('symbol', 'side', 'type', 'quantity')}
    if order['type'] == 'market':
        if 'limit_price' in order:
            raise ClientError('market orders must omit limit_price.')
    else:
        try:
            price = Decimal(str(order.get('limit_price')))
            if not price.is_finite() or price <= 0 or price > 1_000_000 or price * scale != (price * scale).to_integral_value():
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            raise ClientError('limit_price uses native major currency units: CN up to two decimals, HK up to three, at most 1,000,000. Server validates HK ticks.')
        output['limit_price'] = float(price)
    return output


def save_new_request(path, data):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ClientError('Request file already exists. Reuse it for retry; do not overwrite an existing intent.')
    with os.fdopen(descriptor, 'w', encoding='utf-8') as file:
        json.dump(data, file, ensure_ascii=False, indent=2, allow_nan=False)
        file.write('\n')
        file.flush()
        os.fsync(file.fileno())
    return str(path.resolve())


def credential_fingerprint(config):
    token = config['ASHARE_API_KEY']
    if not re.fullmatch(r'ash_[a-f0-9]{64}', token):
        raise ClientError('Set ASHARE_API_KEY to the API key saved by the connection installer or created in the website.')
    return hashlib.sha256(token.encode('ascii')).hexdigest()


def read_request(path, config):
    try:
        raw = Path(path).expanduser().read_text(encoding='utf-8')
        if len(raw) > 8192:
            raise ClientError('Request file is too large.')
        saved = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise ClientError('Cannot read request JSON: ' + str(exc))
    if not isinstance(saved, dict) or set(saved) != {'version', 'base_url', 'api_key_sha256', 'idempotency_key', 'order'} or type(saved['version']) is not int or saved['version'] != 2:
        raise ClientError('Use a version 2 request created by this client. Preserve older submitted intents and recover them using their original client and credentials.')
    if normalize_base(saved['base_url']) != config['ASHARE_BASE_URL'] or saved['api_key_sha256'] != credential_fingerprint(config):
        raise ClientError('Saved request is bound to a different site or API key. Do not send an existing order intent to another account.')
    return validate_order(saved['order']), validate_key(saved['idempotency_key'])


def call(config, path, payload=None, key=None, timeout=20, method=None):
    started = time.monotonic()
    attempt = str(uuid.uuid4())
    phase = 'waiting_response'
    server_id = None
    def diagnostics(reason=None):
        data = {'client_attempt_id': attempt, 'elapsed_ms': round((time.monotonic() - started) * 1000),
                'configured_timeout_seconds': timeout, 'timeout_scope': 'socket_operations',
                'phase': phase, 'automatic_retries': 0}
        if server_id:
            data['request_id'] = server_id
        if reason:
            data['reason'] = reason
        return data
    if config.get('_diagnostics'):
        print(json.dumps({'event': 'request.start', 'route': path.split('?')[0], 'diagnostics': diagnostics()}), file=sys.stderr, flush=True)
    token = config['ASHARE_API_KEY']
    if path != 'health' or token:
        credential_fingerprint(config)
    headers = {'Accept': 'application/json', 'User-Agent': 'ashare-agent-client/' + VERSION,
               'X-Ashare-Skill-Version': VERSION, 'X-Client-Attempt-ID': attempt}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    content = None
    if payload is not None:
        headers['Content-Type'] = 'application/json'
        if key is not None:
            headers['Idempotency-Key'] = key
        content = json.dumps(payload, separators=(',', ':'), allow_nan=False).encode('utf-8')
    request = urllib.request.Request(config['ASHARE_BASE_URL'] + '/api/v1/' + path, data=content, headers=headers, method=method)
    opener = urllib.request.build_opener(NoRedirect())
    try:
        try:
            response = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            status = response.code
            phase = 'reading_response'
            candidate = getattr(response, 'headers', {}).get('X-Request-ID')
            if isinstance(candidate, str) and re.fullmatch(r'[a-zA-Z0-9_-]{8,80}', candidate):
                server_id = candidate
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ClientError('Response exceeds size limit; request fewer records.')
        try:
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError('Expected JSON object')
        except (ValueError, UnicodeDecodeError):
            result = {'error': {'code': 'NON_JSON_RESPONSE', 'message': 'No JSON API response. Check the API key and site URL; redirects are not followed.'}}
        exit_code = 0 if 200 <= status < 300 else 3 if status >= 500 else 2
        api_error = result.get('error')
        error_code = api_error.get('code') if isinstance(api_error, dict) else None
        if error_code == 'ACCOUNT_BUSY' or (200 <= status < 300 and error_code == 'NON_JSON_RESPONSE'):
            exit_code = 3
        phase = 'complete'
        return {'http_status': status, 'result': result, 'diagnostics': diagnostics()}, exit_code
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        cause = getattr(exc, 'reason', exc)
        reason = 'timeout' if isinstance(cause, (TimeoutError, socket.timeout)) else 'dns' if isinstance(cause, socket.gaierror) else 'connection_error'
        return {'http_status': None, 'result': {'error': {'code': 'NETWORK_RESULT_UNKNOWN', 'message': 'Network failure. For an order, retry the SAME request file and API key; do not create a replacement order.'}}, 'diagnostics': diagnostics(reason)}, 3



def private_path(path):
    path = Path(path).expanduser().absolute()
    # Resolve the directory, but never follow a symlink at the secret file itself.
    return path.parent.resolve() / path.name


def read_private(path):
    path = private_path(path)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'r', encoding='utf-8') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or (hasattr(os, 'getuid') and info.st_uid != os.getuid()):
                raise ClientError('Invitation files must be owned regular files with mode 0600.')
            raw = stream.read(8193)
    except OSError:
        raise ClientError('Cannot safely read private invitation file; symlinks are not allowed.')
    if len(raw) > 8192:
        raise ClientError('Invitation file exceeds size limit.')
    return raw


def validate_invitation(data):
    if not isinstance(data, dict) or set(data) != {'name', 'description', 'token'}:
        raise ClientError('Invalid invitation fields.')
    if not isinstance(data['name'], str) or not 1 <= len(data['name'].strip()) <= 40:
        raise ClientError('Agent name must have 1–40 characters.')
    if not isinstance(data['description'], str) or len(data['description'].strip()) > 160:
        raise ClientError('Description must have at most 160 characters.')
    if not isinstance(data['token'], str) or not re.fullmatch(r'[a-f0-9]{64}', data['token']):
        raise ClientError('Invalid saved invitation token; preserve the original request.')
    return {'name': data['name'].strip(), 'description': data['description'].strip(), 'token': data['token']}


def read_invitation(path, config):
    try:
        saved = json.loads(read_private(path))
    except ValueError:
        raise ClientError('Cannot read private invitation JSON.')
    if not isinstance(saved, dict) or set(saved) != {'version','kind','base_url','api_key_sha256','invitation'} or saved.get('kind') != 'agent_invitation' or type(saved.get('version')) is not int or saved['version'] != 1:
        raise ClientError('Use an invitation request created by invite-prepare.')
    if saved['base_url'] != config['ASHARE_BASE_URL'] or saved['api_key_sha256'] != credential_fingerprint(config):
        raise ClientError('Invitation belongs to a different site or API key. Use the original credentials and request.')
    return validate_invitation(saved['invitation'])


def publish_private(path, content):
    """Publish a complete, durable secret file without replacing any existing path."""
    path = private_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.invitation-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        if os.name != 'nt':
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        os.unlink(temporary)
    return str(path)


def save_invitation_link(path, link):
    path = private_path(path)
    try:
        return publish_private(path, link+'\n')
    except FileExistsError:
        if read_private(path).rstrip('\n') != link:
            raise ClientError('Link output already contains different content; it was not overwritten.')
        return str(path)


def validate_managed(method, path, payload, recovery=False):
    valid = (method == 'POST' and path in ('managed-orders', 'exit-rules')) or (method == 'PATCH' and isinstance(path, str) and re.fullmatch(r'(managed-orders|exit-rules)/[a-f0-9-]{36}', path))
    if not valid:
        raise ClientError('Use POST managed-orders/exit-rules or PATCH collection/UUID.')
    if not isinstance(payload, dict):
        raise ClientError('Managed payload must be a JSON object.')
    try:
        encoded = json.dumps(payload, separators=(',', ':'), allow_nan=False)
    except (ValueError, TypeError):
        raise ClientError('Managed JSON must contain finite values.')
    if len(encoded) > 4096:
        raise ClientError('Managed JSON exceeds 4096 characters.')
    allowed = {'action','expected_version','exits','quantity','reset_trigger'} if method == 'PATCH' else {'symbol','side','quantity','limit_price','expires_at','exits'} if path == 'managed-orders' else {'symbol','quantity','exits'}
    if set(payload) - allowed:
        raise ClientError('Managed payload contains unknown fields.')
    if recovery:
        # Preserve already submitted 3.6.0 intents exactly for idempotent replay.
        return payload
    def quantity(name):
        value = payload.get(name)
        if type(value) is not int or value < 1 or (name == 'quantity' and value > 1_000_000):
            raise ClientError(name + ' must be a positive integer within supported bounds.')
    def price(value):
        if type(value) not in (int, float):
            raise ClientError('Managed prices must be JSON numbers, not strings or booleans.')
        number = Decimal(str(value))
        if not number.is_finite() or number <= 0 or number > 1_000_000 or number * 100 != (number * 100).to_integral_value():
            raise ClientError('Managed prices must be positive CNY values with at most two decimals.')
    def exits(value):
        if not isinstance(value, dict) or set(value) - {'stop_loss', 'take_profit', 'max_slippage_percent', 'retry_policy'}:
            raise ClientError('exits must contain only documented exit settings.')
        policy = value.get('retry_policy')
        if not isinstance(policy, str) or policy not in ('continue_next_session', 'stop_at_close'):
            raise ClientError('retry_policy must be the string continue_next_session or stop_at_close.')
        selected = [value.get(k) for k in ('stop_loss', 'take_profit') if value.get(k) is not None]
        if not selected:
            raise ClientError('Set stop_loss or take_profit.')
        for item in selected:
            price(item)
        if len(selected) == 2 and selected[0] >= selected[1]:
            raise ClientError('stop_loss must be lower than take_profit.')
        slip = value.get('max_slippage_percent')
        if slip is not None:
            if type(slip) not in (int, float) or not 0 <= slip < 100 or Decimal(str(slip)) * 10000 != (Decimal(str(slip)) * 10000).to_integral_value():
                raise ClientError('max_slippage_percent must be a number in [0, 100) with at most four decimals.')
    if method == 'PATCH':
        quantity('expected_version')
        action = payload.get('action')
        if action == 'cancel':
            if set(payload) != {'action', 'expected_version'}:
                raise ClientError('Cancellation accepts only action and expected_version.')
            return payload
        if action != 'update':
            raise ClientError('action must be update or cancel.')
        if 'reset_trigger' in payload and type(payload['reset_trigger']) is not bool:
            raise ClientError('reset_trigger must be a boolean.')
        if 'quantity' in payload:
            quantity('quantity')
        if 'exits' not in payload:
            raise ClientError('Updating exit settings requires exits.')
        if payload['exits'] is not None:
            exits(payload['exits'])
    else:
        symbol = payload.get('symbol')
        if not isinstance(symbol, str) or not re.fullmatch(r'[0-9]{6}\.(SH|SZ|BJ)', symbol):
            raise ClientError('Managed trading supports canonical A-share symbols only.')
        quantity('quantity')
        if path == 'managed-orders':
            if payload.get('side') not in ('buy', 'sell'):
                raise ClientError('side must be buy or sell.')
            price(payload.get('limit_price'))
            if not isinstance(payload.get('expires_at'), str) or not re.search(r'(Z|[+-][0-9]{2}:[0-9]{2})$', payload['expires_at']):
                raise ClientError('expires_at must include a timezone.')
            if payload.get('exits') is not None:
                if payload['side'] != 'buy':
                    raise ClientError('Only buy orders may include exits.')
                exits(payload['exits'])
        else:
            exits(payload.get('exits'))
    return payload


def read_managed(path, config):
    saved = json.loads(read_private(path))
    expected = {'version','kind','base_url','api_key_sha256','idempotency_key','method','path','payload'}
    if not isinstance(saved, dict) or set(saved) != expected or saved.get('version') != 1 or saved.get('kind') != 'managed_request':
        raise ClientError('Use managed-prepare to create a managed intent.')
    if saved['base_url'] != config['ASHARE_BASE_URL'] or saved['api_key_sha256'] != credential_fingerprint(config):
        raise ClientError('Managed intent belongs to a different site or API key.')
    validate_key(saved['idempotency_key'])
    validate_managed(saved['method'], saved['path'], saved['payload'], recovery=True)
    # Do not check current expiry here: this may recover an already accepted intent.
    return saved


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument('--env-file', help='Credential file; overrides same-named process variables without executing shell code.')
    root.add_argument('--base-url', help='Explicit site root URL, no /api/v1.')
    root.add_argument('--timeout', type=float, default=20, help='HTTP timeout seconds, 1–120; default 20.')
    root.add_argument('--diagnostics', action='store_true', help='Print the attempt ID to stderr before sending; output JSON always includes timing diagnostics.')
    actions = root.add_subparsers(dest='action')
    for action in ('account', 'positions', 'health', 'invitations', 'managed-orders', 'exit-rules'):
        actions.add_parser(action)
    managed = actions.add_parser('managed-prepare', help='Save a persistent limit/exit command locally; no network.')
    managed.add_argument('--method', choices=['POST', 'PATCH'], required=True)
    managed.add_argument('--path', required=True)
    managed.add_argument('--body', required=True, help='JSON object file containing documented API fields.')
    managed.add_argument('--out', required=True)
    managed.add_argument('--key')
    managed_send = actions.add_parser('managed-submit', help='Send original saved managed intent once; no automatic retry.')
    managed_send.add_argument('--request', required=True)
    active = actions.add_parser('activation', help='Snapshot plus repeatable events; save next_after only after processing.')
    active.add_argument('--after', type=int, default=0)
    active.add_argument('--through', type=int)
    status = actions.add_parser('status')
    status.add_argument('--market', choices=['CN', 'HK'], default='CN')
    instruments = actions.add_parser('instruments')
    instruments.add_argument('--market', choices=['CN', 'HK', 'all'], default='all')
    instruments.add_argument('--query', default='')
    instruments.add_argument('--page', type=int, default=1)
    instruments.add_argument('--limit', type=int, default=30)
    quotes = actions.add_parser('quotes')
    quotes.add_argument('--symbols', help='Comma-separated canonical symbols; at most 100; omit for the default watchlist only.')
    for action in ('orders', 'trades'):
        command = actions.add_parser(action)
        command.add_argument('--limit', type=int, default=100)
        command.add_argument('--cursor', help='Opaque next_cursor from the previous page; keep the same account and command.')
    prepare = actions.add_parser('prepare', help='Save a new immutable request intent locally; no network request.')
    prepare.add_argument('side', choices=['buy', 'sell'])
    prepare.add_argument('symbol')
    prepare.add_argument('quantity', type=int)
    prepare.add_argument('--limit', help='Limit price in native currency; omit for CN opposing best quote / HK last-trade simulation.')
    prepare.add_argument('--key', help='Optional caller-supplied unique idempotency key; otherwise generated once.')
    prepare.add_argument('--out', required=True, help='New persistent order JSON path; existing files are never overwritten.')
    submit = actions.add_parser('submit', help='Submit a prepared order exactly once. No automatic retry.')
    submit.add_argument('--request', required=True)
    invite = actions.add_parser('invite-prepare', help='Save a private invitation intent locally; no network request.')
    invite.add_argument('--name', required=True)
    invite.add_argument('--description', default='')
    invite.add_argument('--out', required=True)
    create = actions.add_parser('invite-create', help='Send a saved invitation once; writes link to a private file, never stdout.')
    create.add_argument('--request', required=True)
    create.add_argument('--link-out', required=True)
    revoke = actions.add_parser('invite-revoke', help='Revoke an unclaimed invitation for this Agent.')
    revoke.add_argument('--id', required=True)
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if not 1 <= args.timeout <= 120:
            raise ClientError('Timeout must be between 1 and 120 seconds.')
        config = load_config(args.env_file, args.base_url)
        config['_diagnostics'] = args.diagnostics
        action = args.action or 'account'
        if action == 'managed-prepare':
            payload = validate_managed(args.method, args.path, json.loads(Path(args.body).expanduser().read_text(encoding='utf-8')))
            saved = {'version':1, 'kind':'managed_request', 'base_url':config['ASHARE_BASE_URL'], 'api_key_sha256':credential_fingerprint(config), 'idempotency_key':validate_key(args.key or str(uuid.uuid4())), 'method':args.method, 'path':args.path, 'payload':payload}
            print(json.dumps({'prepared':True, 'sent':False, 'request_file':save_new_request(args.out, saved)}, ensure_ascii=False))
            return 0
        if action == 'managed-submit':
            saved = read_managed(args.request, config)
            output, exit_code = call(config, saved['path'], saved['payload'], saved['idempotency_key'], args.timeout, method=saved['method'])
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return exit_code
        if action == 'activation':
            if args.after < 0 or (args.through is not None and args.through < args.after):
                raise ClientError('Use non-negative ordered event cursors.')
            query = {'after':args.after}
            if args.through is not None:
                query['through'] = args.through
            output, exit_code = call(config, 'activation?' + urllib.parse.urlencode(query), timeout=args.timeout)
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return exit_code
        if action == 'invite-prepare':
            invitation = validate_invitation({'name':args.name,'description':args.description,'token':secrets.token_hex(32)})
            saved = {'version':1,'kind':'agent_invitation','base_url':config['ASHARE_BASE_URL'],'api_key_sha256':credential_fingerprint(config),'invitation':invitation}
            filename = publish_private(args.out, json.dumps(saved, ensure_ascii=False, indent=2)+'\n')
            print(json.dumps({'prepared':True,'sent':False,'request_file':filename}, ensure_ascii=False))
            return 0
        if action == 'invite-create':
            payload = read_invitation(args.request, config)
            output, exit_code = call(config, 'invitations', payload, timeout=args.timeout)
            if exit_code == 0:
                result = output['result']
                expected = config['ASHARE_BASE_URL']+'/agent/connect#invite='+payload['token']
                if result.get('url') != expected or not isinstance(result.get('id'), str):
                    raise ClientError('Invitation response did not match the saved request; preserve it for recovery.')
                filename = save_invitation_link(args.link_out, expected)
                output = {'http_status':output['http_status'],'diagnostics':output.get('diagnostics', {}),'result':{k:result.get(k) for k in ('id','name','expires_at','claimed_at','revoked_at','child_account_id','replayed')},'link_file':filename}
                if 'skill_update' in result:
                    output['result']['skill_update'] = result['skill_update']
            # Never echo the secret-bearing raw invitation response, even on errors.
            encoded = json.dumps(output, ensure_ascii=False, indent=2).replace(payload['token'], '[redacted]')
            print(encoded)
            return exit_code
        if action == 'prepare':
            order = {'symbol': args.symbol, 'side': args.side, 'type': 'limit' if args.limit is not None else 'market', 'quantity': args.quantity}
            if args.limit is not None:
                order['limit_price'] = args.limit
            saved = {'version': 2, 'base_url': config['ASHARE_BASE_URL'], 'api_key_sha256': credential_fingerprint(config), 'idempotency_key': validate_key(args.key or str(uuid.uuid4())), 'order': validate_order(order)}
            result = {'prepared': True, 'sent': False, 'request_file': save_new_request(args.out, saved), 'request': saved}
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        payload = key = None
        if action == 'invite-revoke':
            path = 'invitations/revoke'
            payload = {'id':args.id}
        elif action == 'submit':
            payload, key = read_request(args.request, config)
            path = 'orders'
        elif action == 'status':
            path = 'market/status?' + urllib.parse.urlencode({'market': args.market})
        elif action == 'instruments':
            if args.page < 1 or not 1 <= args.limit <= 500:
                raise ClientError('page must be positive and limit 1–500.')
            path = 'instruments?' + urllib.parse.urlencode({'market': args.market, 'q': args.query, 'page': args.page, 'limit': args.limit})
        elif action == 'quotes' and args.symbols:
            symbols = args.symbols.split(',')
            if any(not re.fullmatch(r'(?:[0-9]{6}\.(?:SH|SZ|BJ)|[0-9]{5}\.HK)', symbol) for symbol in symbols):
                raise ClientError('Use comma-separated symbols such as 600036.SH,920002.BJ,00700.HK.')
            path = 'quotes?' + urllib.parse.urlencode({'symbols': args.symbols})
        elif action in ('orders', 'trades'):
            if not 1 <= args.limit <= 500:
                raise ClientError('limit must be between 1 and 500.')
            query = {'limit': args.limit}
            if args.cursor is not None:
                query['cursor'] = args.cursor
            path = action + '?' + urllib.parse.urlencode(query)
        else:
            path = action
        output, exit_code = call(config, path, payload, key, args.timeout)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return exit_code
    except (ClientError, OSError, ValueError) as exc:
        print(json.dumps({'error': {'code': 'LOCAL_CONFIGURATION_ERROR', 'message': str(exc)}}, ensure_ascii=False), file=sys.stderr)
        return 4


if __name__ == '__main__':
    sys.exit(main())
