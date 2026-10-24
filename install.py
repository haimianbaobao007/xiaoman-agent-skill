#!/usr/bin/env python3
"""Install the A-share paper-trading skill and claim a shared enrollment link.

Python 3.9+, standard library only. Does not submit trades or schedule tasks.
Read the private link with --link-stdin so it never enters process arguments.
Never prints the invitation or API key. Re-run the SAME link and credential path
after interruption; the saved key is used to recover an uncertain claim.
"""
import argparse
import hashlib
import http.client
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shlex
import shutil
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile

VERSION = '3.6.1'
PACKAGE = 'ashare-paper-trading'
MARKER = '.ashare-package.json'
MAX_ARCHIVE = 2 * 1024 * 1024
MAX_EXPANDED = 8 * 1024 * 1024
MAX_FILE = 2 * 1024 * 1024
MAX_ENTRIES = 128
REQUIRED = {'SKILL.md', 'scripts/ashare.py', 'references/api.md', 'references/setup.md'}
ENV_FIELDS = {'ASHARE_BASE_URL', 'ASHARE_API_KEY', 'ASHARE_ENROLLMENT_TOKEN_SHA256', 'ASHARE_ENROLLMENT_VERSION'}


class InstallError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def origin_of(value):
    try:
        parsed = urllib.parse.urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
        if not hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError()
        hostname = hostname.encode('idna').decode('ascii').lower()
        if not re.fullmatch(r'[a-z0-9.-]+|::1', hostname):
            raise ValueError()
        if parsed.scheme not in ('https', 'http'):
            raise ValueError()
        if parsed.scheme == 'http' and hostname not in ('localhost', '127.0.0.1', '::1'):
            raise ValueError()
        if port is not None and not 1 <= port <= 65535:
            raise ValueError()
        netloc = '[' + hostname + ']' if ':' in hostname else hostname
        if port is not None and port != (443 if parsed.scheme == 'https' else 80):
            netloc += ':' + str(port)
        return parsed.scheme + '://' + netloc
    except (ValueError, UnicodeError):
        raise InstallError('Use an HTTPS site URL without embedded credentials; HTTP is allowed only on loopback.')


def parse_link(link):
    if not isinstance(link, str) or any(ord(char) < 32 or ord(char) == 127 for char in link):
        raise InstallError('Invalid connection link.')
    base = origin_of(link)
    parsed = urllib.parse.urlsplit(link)
    try:
        fragment = urllib.parse.parse_qs(parsed.fragment, strict_parsing=True, keep_blank_values=True)
    except ValueError:
        raise InstallError('The link must end with #invite=<invitation token>.')
    if parsed.path != '/agent/connect' or parsed.query or set(fragment) != {'invite'} or len(fragment['invite']) != 1:
        raise InstallError('Use the complete /agent/connect#invite= link shared by the account owner.')
    token = fragment['invite'][0]
    if not re.fullmatch(r'[0-9a-f]{64}', token):
        raise InstallError('The invitation must be a 64-character lowercase hexadecimal token.')
    return base, token


def parse_base_url(value):
    if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise InstallError('Use the original site root URL for --base-url.')
    base = origin_of(value)
    parsed = urllib.parse.urlsplit(value)
    if parsed.path not in ('', '/') or parsed.query or parsed.fragment:
        raise InstallError('--base-url must be a site root without a path, query or fragment.')
    return base


def check_update_target(target, base):
    target = ensure_no_symlink(target)
    try:
        marker_path = ensure_no_symlink(target / MARKER)
        marker = json.loads(marker_path.read_text(encoding='utf-8'))
        if not isinstance(marker, dict) or marker.get('package') != PACKAGE or not isinstance(marker.get('files'), dict):
            raise ValueError()
        if not REQUIRED.issubset(marker['files']):
            raise ValueError()
    except (OSError, ValueError, TypeError):
        raise InstallError('--update-only requires an existing installer-managed skill directory.')
    if marker.get('source_origin') not in (None, base):
        raise InstallError('Skill updates must use the original installation site.')
    previous = marker.get('version')
    if not isinstance(previous, str) or not re.fullmatch(r'(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})', previous):
        raise InstallError('Existing package version is invalid; no files were changed.')
    if tuple(map(int, previous.split('.'))) > tuple(map(int, VERSION.split('.'))):
        raise InstallError('This installer is older than the installed skill. Download the latest installer from the original site.')
    return target


def same_origin_url(base, reference):
    if not isinstance(reference, str) or not reference or any(ord(c) < 32 for c in reference):
        raise InstallError('Invalid package URL in manifest.')
    target = urllib.parse.urljoin(base + '/', reference)
    parsed = urllib.parse.urlsplit(target)
    if origin_of(target) != base or parsed.fragment or parsed.query or not parsed.path.startswith('/agent/'):
        raise InstallError('Package downloads must stay under /agent/ on the same HTTPS site.')
    return target


def request(base, path, payload=None, api_key=None, limit=65536):
    url = same_origin_url(base, path)
    # API paths are constructed by this program, never supplied by a manifest.
    return http_request(url, payload, api_key, limit)


def http_request(url, payload=None, api_key=None, limit=65536):
    headers = {'Accept': 'application/json', 'User-Agent': 'ashare-skill-installer/' + VERSION}
    data = None
    if payload is not None:
        data = json.dumps(payload, separators=(',', ':')).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    if api_key:
        headers['Authorization'] = 'Bearer ' + api_key
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        try:
            response = urllib.request.build_opener(NoRedirect()).open(req, timeout=20)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            status = response.code
            body = response.read(limit + 1)
        if 300 <= status < 400:
            raise InstallError('The server returned a redirect. Redirects are not followed; credentials were not forwarded.')
        if len(body) > limit:
            raise InstallError('Server response exceeds the permitted size.')
        return status, body
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
        raise InstallError('Network result is uncertain. Re-run with the same inputs and preserve saved credentials; for enrollment, use the SAME link and env-file.')


def json_object(raw):
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise InstallError('Expected a JSON response from the server.')
    if not isinstance(value, dict):
        raise InstallError('Expected a JSON object from the server.')
    return value


def download_package(base):
    status, raw = request(base, '/agent/manifest.json')
    if status != 200:
        raise InstallError('Cannot download package manifest (HTTP %d).' % status)
    manifest = json_object(raw)
    if manifest.get('version') != VERSION or not isinstance(manifest.get('sha256'), str) or not re.fullmatch(r'[a-f0-9]{64}', manifest['sha256']):
        raise InstallError('Manifest version or checksum is unsupported.')
    archive_url = same_origin_url(base, manifest.get('archive_url'))
    status, archive = http_request(archive_url, limit=MAX_ARCHIVE)
    if status != 200:
        raise InstallError('Cannot download skill archive (HTTP %d).' % status)
    if not secrets.compare_digest(hashlib.sha256(archive).hexdigest(), manifest['sha256']):
        raise InstallError('Skill archive checksum does not match its manifest.')
    files = unpack(archive)
    if 'release.json' not in files or json_object(files['release.json']).get('version') != VERSION:
        raise InstallError('Skill release metadata does not match the manifest.')
    return files, manifest['sha256']


def unpack(archive):
    files = {}
    seen = set()
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as package:
            entries = package.infolist()
            if not entries or len(entries) > MAX_ENTRIES:
                raise InstallError('Archive has an invalid number of entries.')
            for entry in entries:
                name = entry.orig_filename
                parts = name.rstrip('/').split('/')
                if ('\\' in name or '\x00' in name or len(name) > 240 or ':' in name or
                        any(part in ('', '.', '..') for part in parts) or parts[0] != PACKAGE or name.startswith('/')):
                    raise InstallError('Archive contains an unsafe path.')
                if any(not re.fullmatch(r'[A-Za-z0-9_.-]+', part) for part in parts):
                    raise InstallError('Archive paths must use portable ASCII filenames.')
                path = '/'.join(parts[1:])
                if not path:
                    if not entry.is_dir():
                        raise InstallError('Archive root is not a directory.')
                    continue
                if path.casefold() == MARKER or path.casefold() in seen:
                    raise InstallError('Archive contains a reserved or duplicate path.')
                seen.add(path.casefold())
                mode = entry.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if stat.S_ISLNK(mode) or kind not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise InstallError('Archive contains a symlink or special file.')
                if entry.flag_bits & 1 or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise InstallError('Encrypted or unsupported archive entries are rejected.')
                if entry.file_size < 0 or entry.file_size > MAX_FILE:
                    raise InstallError('Archive entry exceeds the permitted size.')
                total += entry.file_size
                if total > MAX_EXPANDED:
                    raise InstallError('Expanded archive exceeds the permitted size.')
                if entry.is_dir():
                    if entry.file_size:
                        raise InstallError('Archive directory contains unexpected data.')
                    continue
                if kind == stat.S_IFDIR:
                    raise InstallError('Archive file has an inconsistent type.')
                with package.open(entry) as stream:
                    content = stream.read(MAX_FILE + 1)
                if len(content) != entry.file_size or len(content) > MAX_FILE:
                    raise InstallError('Archive entry size is inconsistent.')
                files[path] = content
            if not REQUIRED.issubset(files):
                raise InstallError('Archive is missing required skill files.')
            folded_files = {name.casefold() for name in files}
            for name in files:
                if any(str(parent).casefold() in folded_files for parent in PurePosixPath(name).parents if str(parent) != '.'):
                    raise InstallError('Archive contains conflicting file and directory paths.')
    except (zipfile.BadZipFile, RuntimeError, OSError, EOFError):
        raise InstallError('Cannot read skill archive.')
    return files


def ensure_no_symlink(path):
    path = Path(os.path.abspath(os.path.expanduser(str(path))))
    for component in [path] + list(path.parents):
        if component.is_symlink():
            raise InstallError('Installation and credential paths must not contain symlinks.')
    return path


def read_private_env(path, base, token_hash):
    path = ensure_no_symlink(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        return None
    except OSError:
        raise InstallError('Cannot safely read the existing credential file.')
    with os.fdopen(descriptor, 'r', encoding='utf-8') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or (hasattr(os, 'getuid') and info.st_uid != os.getuid()):
            raise InstallError('Existing credentials must be an owned regular file with mode 0600; fix permissions before retrying.')
        raw = stream.read(8193)
    if len(raw) > 8192:
        raise InstallError('Existing credential file is too large.')
    config = {}
    try:
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            name, value = line.split('=', 1)
            if name not in ENV_FIELDS or name in config:
                raise ValueError()
            values = shlex.split(value, comments=True)
            if len(values) != 1:
                raise ValueError()
            config[name] = values[0]
    except ValueError:
        raise InstallError('Existing credential file is not installer-managed; choose another --env-file. It was not overwritten.')
    if (set(config) != ENV_FIELDS or config.get('ASHARE_ENROLLMENT_VERSION') != '1' or
            not re.fullmatch(r'ash_[a-f0-9]{64}', config.get('ASHARE_API_KEY', '')) or
            not re.fullmatch(r'[a-f0-9]{64}', config.get('ASHARE_ENROLLMENT_TOKEN_SHA256', ''))):
        raise InstallError('Existing credentials cannot be safely resumed; choose another --env-file. They were not overwritten.')
    if config['ASHARE_BASE_URL'] != base or not secrets.compare_digest(config['ASHARE_ENROLLMENT_TOKEN_SHA256'], token_hash):
        raise InstallError('This credential file belongs to a DIFFERENT enrollment link. Use a different --env-file; existing credentials were not changed.')
    return config['ASHARE_API_KEY']


def fsync_dir(path):
    if os.name != 'posix':
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def persist_key(path, base, token_hash):
    existing = read_private_env(path, base, token_hash)
    if existing:
        return existing
    path = ensure_no_symlink(path)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    key = 'ash_' + secrets.token_hex(32)
    content = '\n'.join([
        '# A-share paper-trading enrollment; keep private and outside the skill directory.',
        'ASHARE_BASE_URL=' + shlex.quote(base),
        'ASHARE_API_KEY=' + key,
        'ASHARE_ENROLLMENT_TOKEN_SHA256=' + token_hash,
        'ASHARE_ENROLLMENT_VERSION=1', '',
    ])
    descriptor, temporary = tempfile.mkstemp(prefix='.ashare-credential-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # Linking the complete, fsynced file is atomic and never replaces a user file.
            os.link(temporary, path)
            fsync_dir(path.parent)
        except FileExistsError:
            return read_private_env(path, base, token_hash)
    finally:
        os.unlink(temporary)
    return key


def inventory(directory):
    found = set()
    for folder, directories, filenames in os.walk(directory, followlinks=False):
        for name in directories + filenames:
            path = Path(folder) / name
            if path.is_symlink() or not (path.is_dir() or path.is_file()):
                raise InstallError('Existing skill contains a symlink or special file; installation was stopped.')
        for name in filenames:
            found.add((Path(folder) / name).relative_to(directory).as_posix())
    return found


def install_package(target, files, digest, update=False, source_origin=None):
    target = ensure_no_symlink(target)
    managed = {}
    if target.exists():
        if not target.is_dir():
            raise InstallError('Skill destination is not a directory.')
        found = inventory(target)
        if found or any(target.iterdir()):
            if MARKER not in found:
                raise InstallError('Skill destination is unfamiliar. Choose another --skill-dir; --update never overwrites an unmanaged directory.')
            try:
                marker = json.loads((target / MARKER).read_text(encoding='utf-8'))
                managed = marker['files']
                if marker['package'] != PACKAGE or not isinstance(managed, dict) or not REQUIRED.issubset(managed):
                    raise ValueError()
                if source_origin is not None and marker.get('source_origin') not in (None, source_origin):
                    raise InstallError('Skill updates must use the original installation site.')
                for name, expected in managed.items():
                    parts = name.split('/')
                    if any(part in ('', '.', '..') for part in parts) or '\\' in name or name == MARKER:
                        raise ValueError()
                    path = target.joinpath(*parts)
                    if name not in found or not isinstance(expected, str) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                        raise InstallError('A managed skill file was modified or removed. Preserve it and choose a new --skill-dir before upgrading.')
            except (KeyError, ValueError, TypeError, OSError):
                raise InstallError('Existing package marker is invalid; no files were overwritten.')
            if (marker.get('archive_sha256') == digest and set(managed) == set(files)
                    and (source_origin is None or marker.get('source_origin') == source_origin)
                    and all(managed[name] == hashlib.sha256(content).hexdigest() for name, content in files.items())):
                return 'unchanged'
            if not update:
                raise InstallError('A different package version is installed. Re-run with --update to replace managed files and preserve other files.')
            for name in files:
                if name in found and name not in managed:
                    raise InstallError('Package would overwrite an unmanaged file; choose another --skill-dir.')
                for parent in PurePosixPath(name).parents:
                    if str(parent) in found and str(parent) not in managed:
                        raise InstallError('Package would replace an unmanaged path; choose another --skill-dir.')
                if (target / name).is_dir():
                    raise InstallError('Package would replace an existing directory; choose another --skill-dir.')
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.' + PACKAGE + '-stage-', dir=target.parent))
    backup = None
    try:
        if target.exists():
            shutil.copytree(target, stage, dirs_exist_ok=True)
            for name in managed:
                (stage / name).unlink()
        for name, content in files.items():
            path = stage / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            path.chmod(0o644)
        marker = {'package': PACKAGE, 'version': VERSION, 'archive_sha256': digest,
                  'files': {name: hashlib.sha256(content).hexdigest() for name, content in sorted(files.items())}}
        if source_origin is not None:
            marker['source_origin'] = source_origin
        (stage / MARKER).write_text(json.dumps(marker, indent=2) + '\n', encoding='utf-8')
        if target.exists():
            backup = target.parent / ('.' + PACKAGE + '-backup-' + secrets.token_hex(8))
            target.rename(backup)
        try:
            stage.rename(target)
        except OSError:
            if backup is not None:
                backup.rename(target)
                backup = None
            raise
        if backup is not None:
            shutil.rmtree(backup)
        return 'updated' if managed else 'installed'
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def claim(base, token, key):
    status, raw = http_request(base + '/api/agent/claim', {'token': token, 'api_key': key})
    if status == 409:
        raise InstallError('Enrollment conflict (HTTP 409): the link may have been claimed with another key, or the candidate key is already in use. Saved credentials were kept; ask the owner to check the link and account before reconnecting.')
    if status == 410:
        raise InstallError('Enrollment unavailable (HTTP 410): the link is expired or revoked, or its enrolled key was revoked. Saved credentials were kept; ask the owner to reconnect the existing account or resend an unclaimed invitation.')
    if status not in (200, 201):
        raise InstallError('Enrollment returned HTTP %d. Saved credentials were kept; retry the SAME link and env-file if the result is uncertain.' % status)
    result = json_object(raw)
    agent = result.get('agent')
    if not isinstance(result.get('account_id'), str) or not isinstance(agent, dict) or not isinstance(agent.get('id'), str) or type(result.get('replayed')) is not bool:
        raise InstallError('Enrollment response is incomplete. Saved credentials were kept; re-run the SAME link and env-file to recover.')
    if any(token in value or key in value for value in (result['account_id'], agent['id'])):
        raise InstallError('Enrollment response contains unexpected credential material; output was suppressed.')
    return {'account_id': result['account_id'], 'agent_id': result['account_id'], 'api_key_id': agent['id'], 'replayed': result['replayed']}


def validate_read_result(path, result, account_id):
    if path == 'account':
        return (isinstance(result.get('id'), str) and bool(result['id'].strip()) and
                result['id'] == account_id and type(result.get('initial_cash')) is int and
                type(result.get('cash')) is int and isinstance(result.get('positions'), list) and
                result.get('simulation') is True and result.get('monetary_unit') == 'CNY_FEN')
    if path == 'market/status':
        return (type(result.get('open')) is bool and result.get('timezone') == 'Asia/Shanghai' and
                result.get('simulation') is True and isinstance(result.get('phase'), str) and bool(result['phase'].strip()))
    if path == 'instruments':
        instruments = result.get('instruments')
        return (isinstance(instruments, list) and isinstance(result.get('scope'), str) and bool(result['scope'].strip()) and
                all(isinstance(item, dict) and isinstance(item.get('symbol'), str) and
                    re.fullmatch(r'(?:[0-9]{6}\.(?:SH|SZ|BJ)|[0-9]{5}\.HK)', item['symbol']) and
                    isinstance(item.get('name'), str) and bool(item['name'].strip()) and
                    isinstance(item.get('sector'), str) for item in instruments))
    return False


def verify_read_only(base, key, account_id):
    results = {}
    for path in ('account', 'market/status', 'instruments'):
        status, raw = http_request(base + '/api/v1/' + path, api_key=key, limit=2_000_000)
        result = json_object(raw)
        if status != 200 or 'error' in result or not validate_read_result(path, result, account_id):
            raise InstallError('Credentials were saved, but read-only verification failed for %s (HTTP %d): response fields or claimed account do not match. No trade was submitted.' % (path, status))
        results[path] = status
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    link_input = parser.add_mutually_exclusive_group(required=True)
    link_input.add_argument('--link-stdin', action='store_true', help='Recommended: read the complete private link from standard input, without exposing it in process arguments.')
    link_input.add_argument('--link', help='Legacy input: complete link in process arguments; visible to process inspection. Prefer --link-stdin.')
    link_input.add_argument('--update-only', action='store_true', help='Update an installed skill from its original site, without accessing credentials or claiming an invitation.')
    parser.add_argument('--base-url', help='Original site root; required only for --update-only.')
    parser.add_argument('--skill-dir', help='Skill directory; defaults to ~/.codex/skills/ashare-paper-trading for enrollment, required for --update-only.')
    parser.add_argument('--env-file', help='Persistent private credential destination; existing enrollments are never replaced.')
    parser.add_argument('--update', action='store_true', help='Update verified installer-managed files while preserving unmanaged files.')
    args = parser.parse_args(argv)
    try:
        if args.update_only:
            if not args.base_url or not args.skill_dir or args.env_file or args.update:
                raise InstallError('--update-only requires --base-url and --skill-dir; do not combine with --env-file or --update.')
            base = parse_base_url(args.base_url)
            skill_dir = check_update_target(args.skill_dir, base)
            files, digest = download_package(base)
            installation = install_package(skill_dir, files, digest, update=True, source_origin=base)
            print(json.dumps({'ok': True, 'version': VERSION, 'installation': installation,
                              'skill_dir': str(skill_dir), 'read_skill_file': str(skill_dir / 'SKILL.md'),
                              'credentials_changed': False, 'enrollment_attempted': False,
                              'trade_submitted': False, 'scheduled': False}, ensure_ascii=False, indent=2))
            return 0
        if args.base_url:
            raise InstallError('--base-url is only used with --update-only; enrollment uses the site in the private link.')
        link = args.link
        if args.link_stdin:
            link = sys.stdin.read(16385)
            if len(link) > 16384:
                raise InstallError('Connection link input exceeds the permitted size.')
            link = link.rstrip('\r\n')
        base, token = parse_link(link)
        token_hash = hashlib.sha256(token.encode('ascii')).hexdigest()
        parsed = urllib.parse.urlsplit(base)
        host_directory = parsed.hostname.replace(':', '_') + ('-' + str(parsed.port) if parsed.port else '')
        env_file = ensure_no_symlink(args.env_file or str(Path.home() / '.config' / 'ashare' / host_directory / '.env.agent'))
        skill_dir = ensure_no_symlink(args.skill_dir or '~/.codex/skills/' + PACKAGE)
        if env_file == skill_dir or skill_dir in env_file.parents:
            raise InstallError('Save credentials outside the skill installation directory.')
        read_private_env(env_file, base, token_hash)
        files, digest = download_package(base)
        installation = install_package(skill_dir, files, digest, args.update, source_origin=base)
        key = persist_key(env_file, base, token_hash)
        enrollment = claim(base, token, key)
        verified = verify_read_only(base, key, enrollment['account_id'])
        print(json.dumps({'ok': True, 'version': VERSION, 'installation': installation,
                          'skill_dir': str(skill_dir), 'env_file': str(env_file),
                          'enrollment': enrollment, 'read_only_checks': verified,
                          'trade_submitted': False, 'scheduled': False}, ensure_ascii=False, indent=2))
        return 0
    except (InstallError, OSError, ValueError, UnicodeError):
        error = sys.exc_info()[1]
        # Raw HTTP bodies, URLs and secrets never enter diagnostic messages.
        message = str(error) if isinstance(error, InstallError) else 'Local filesystem or configuration operation failed; saved credentials were kept.'
        print(json.dumps({'ok': False, 'error': message}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
