"""Bounded deterministic observers. No model calls; errors never become changes.

Fingerprint gating follows the idea in Hermes cron/monitor.py. The argv guard
is deliberately conservative, but is not a sandbox for configured executables.
"""
import hashlib
import json
import os
import stat as stat_module
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import urllib.parse
import urllib.request

MAX_BODY = 256 * 1024
MAX_STATE = 16 * 1024
# Reject output, code/config/helper execution, browser launches and common writes.
UNSAFE_OPTIONS = frozenset({
    '--output', '--output-file', '--output-directory', '--exec', '--exec-path',
    '--upload-pack', '--receive-pack', '--config', '--config-env', '--web',
    '--ext-diff', '--textconv', '--open-files-in-pager', '--paginate', '--pager',
    '--editor', '--command', '--shell', '--eval', '--execute', '--write',
    '--write-out', '--append', '--delete', '--remove', '--force', '--apply',
    '--request', '--data', '--data-raw', '--data-binary', '--form', '--upload-file',
})
UNSAFE_VERBS = frozenset({'sh','bash','zsh','fish','python','python3','node','ruby',
    'perl','eval','exec','rm','mv','cp','touch','tee','sudo','chmod','chown',
    'checkout','switch','commit','push','reset','clean','restore','merge','rebase',
    'cherry-pick','revert','fetch','pull','clone','init','add','apply','config',
    'edit','create','close','delete','rerun','cancel','workflow'})


def command_allowed(argv, allowlist):
    if not isinstance(argv, list) or not argv or not isinstance(allowlist, str):
        return False
    for token in argv:
        if not isinstance(token, str) or not token or '\0' in token:
            return False
        if any(c in token for c in '><|;&`$\n\r'):
            return False
        option=token.split('=',1)[0]
        if token in UNSAFE_VERBS or (option.startswith('--') and any(flag.startswith(option) for flag in UNSAFE_OPTIONS)):
            return False
        if token in {'-w','-p'}: return False
        if argv[:2]==['git','ls-remote'] and token.startswith('-u'):
            return False
        if token.startswith(('-o', '-c', '-P')) and not token.startswith('--'):
            return False
    try:
        return any(argv[:len(prefix)] == prefix for entry in allowlist.split(',')
                   if (prefix := shlex.split(entry.strip())))
    except ValueError:
        return False


def validate_url(url):
    if not isinstance(url, str) or any(c.isspace() for c in url):
        raise ValueError('invalid URL')
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
        raise ValueError('URL requires http or https and a host')
    if parsed.username is not None or parsed.password is not None:
        raise ValueError('URL credentials are refused')
    parsed.port  # validate malformed/out-of-range ports before I/O
    return url


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _get_url(url):
    # No ambient proxy credentials, cookie jar, netrc or Authorization handler.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _SafeRedirect())
    request = urllib.request.Request(validate_url(url), method='GET')
    with opener.open(request, timeout=10) as response:
        body = response.read(MAX_BODY + 1)
    if len(body) > MAX_BODY:
        raise ValueError('URL body exceeds 256 KB')
    return body


def _hash(value):
    if not isinstance(value, bytes):
        value = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    return hashlib.sha256(value).hexdigest()


def _state(wake):
    return wake.probe_state_json if isinstance(wake.probe_state_json, dict) else {}


def _result(wake, state, fire, message):
    state = dict(state)
    state['generation'] = int(_state(wake).get('generation', 0)) + int(fire)
    return {'fire':fire, 'message':message[:2000] if fire else '', 'state':state,
            'identity':_hash([wake.id, state])}


def _file(wake, home):
    trigger, old = wake.trigger_json, _state(wake)
    path = Path(trigger['path']).expanduser()
    if not path.is_absolute():
        if home is None: raise ValueError('relative file path requires profile home')
        path = Path(home) / path
    if not stat_module.S_ISREG(path.stat().st_mode):
        raise ValueError('file probe requires a regular file')
    with os.fdopen(os.open(path,os.O_RDONLY | os.O_NONBLOCK),'rb') as source:
        stat = os.fstat(source.fileno())
        if not stat_module.S_ISREG(stat.st_mode):
            raise ValueError('file probe requires a regular file')
        state = {'fingerprint':_hash([stat.st_mtime_ns, stat.st_size, stat.st_ino, stat.st_dev]),
                 'inode':stat.st_ino, 'device':stat.st_dev, 'size':stat.st_size, 'offset':stat.st_size}
        pattern = trigger.get('pattern')
        if not pattern:
            return _result(wake, state, bool(old and old.get('fingerprint') != state['fingerprint']), 'file changed')
        expression = re.compile(pattern)
        # Existing contents are the baseline, never a new append. Rotation or
        # observed truncation begins a new stream; complete new lines may fire.
        if not old:
            return _result(wake, state, False, '')
        offset = int(old.get('offset', old.get('size', 0)))
        if old.get('inode') != stat.st_ino or old.get('device') != stat.st_dev or stat.st_size < old.get('size', 0):
            offset = 0
        source.seek(offset)
        data = source.read(MAX_BODY)
        end = data.rfind(b'\n') + 1
        state['offset'] = offset + end
        lines = data[:end].decode('utf-8', errors='replace').splitlines()
        matches = [line for line in lines if expression.search(line)]
        return _result(wake, state, bool(matches), '\n'.join(matches)[:2000])


def _process(wake):
    import psutil  # lazy: only process probes need Hermes-provided psutil
    pid = wake.trigger_json['pid']
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValueError('pid must be positive')
    old = _state(wake)
    expected = old.get('create_time', wake.trigger_json.get('create_time'))
    try:
        created = psutil.Process(pid).create_time()
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        created = None
    exited = created is None or expected is not None and created != expected
    state = {'create_time':expected if expected is not None else created,
             'exited':exited, 'fingerprint':_hash([pid, expected if expected is not None else created, exited])}
    return _result(wake, state, exited and not old.get('exited'), 'process exited (or PID was reused)')


def _url(wake):
    validate_url(wake.trigger_json['url'])
    body = _get_url(wake.trigger_json['url'])
    if len(body) > MAX_BODY: raise ValueError('URL body exceeds 256 KB')
    old, fingerprint = _state(wake), _hash(body)
    pattern = wake.trigger_json.get('pattern')
    changed = old.get('fingerprint') != fingerprint
    fire = changed and (bool(re.search(pattern, body.decode('utf-8', errors='replace'))) if pattern else bool(old))
    return _result(wake, {'fingerprint':fingerprint}, fire, 'URL matched pattern' if pattern else 'URL changed')


def _command(wake, policy, home):
    trigger, old = wake.trigger_json, _state(wake)
    argv = trigger.get('command')
    if not policy.source_command or not command_allowed(argv, policy.probe_commands):
        raise ValueError('command is not allowlisted read-only argv')
    mode = trigger.get('fire_on', 'change')
    if mode not in {'change','success','failure','json'}:
        raise ValueError('invalid fire_on')
    if home is None: raise ValueError('command probe requires profile home')
    cwd = Path(trigger.get('cwd') or home).expanduser()
    if not cwd.is_absolute(): cwd = Path(home) / cwd
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(argv, shell=False, cwd=cwd, timeout=30,
                                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                env={**os.environ,'GIT_OPTIONAL_LOCKS':'0','GIT_PAGER':'cat','GH_PAGER':'cat','PAGER':'cat'})
        output.seek(0)
        body = output.read(MAX_BODY + 1)
    if len(body) > MAX_BODY: raise ValueError('command output exceeds 256 KB')
    success = result.returncode == 0
    if mode in {'change','json'} and not success:
        raise ValueError('command failed')
    state = {'fingerprint':_hash([result.returncode, body.decode('utf-8', errors='replace')])}
    message = body.decode('utf-8', errors='replace')[:2000]
    if mode == 'json':
        contract = json.loads(body)
        if not isinstance(contract, dict) or set(contract) != {'fire','message','state'} or not isinstance(contract['fire'], bool) or not isinstance(contract['message'], str):
            raise ValueError('expected {fire: bool, message: str, state: JSON}')
        state['value'] = contract['state']
        fire, message = contract['fire'], contract['message']
    else:
        fire = (state['fingerprint'] != old.get('fingerprint') if mode == 'change' else
                success if mode == 'success' else not success)
    response = _result(wake, state, fire, message)
    if len(json.dumps(response['state'], allow_nan=False).encode()) > MAX_STATE:
        raise ValueError('command state exceeds 16 KB')
    if not fire: response.pop('state')  # successful fire is the only commit
    return response


def check(wake, *, policy=None, home=None):
    """U2-compatible seam; profile context is supplied by the poller adapter."""
    try:
        if wake.kind == 'file': return _file(wake, home)
        if wake.kind == 'process': return _process(wake)
        if wake.kind == 'url': return _url(wake)
        if wake.kind == 'command':
            if policy is None:
                from .policy import Policy
                policy = Policy()
            return _command(wake, policy, home)
        raise ValueError('unknown probe kind')
    except Exception as exc:
        # Avoid returning paths, URLs, argv/output or exception text on errors.
        return {'fire':False, 'error':type(exc).__name__}
