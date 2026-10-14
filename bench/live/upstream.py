"""Body-only recording forwarder; credentials never enter gateway configuration."""
import copy
import json
import os
import threading
import time
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class QuotaError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Upstream:
    def __init__(self, url, model, key_env, record_path, fail_plan=None):
        self.url, self.model, self.key_env = url.rstrip('/'), model, key_env
        self.record_path, self.fail_plan = Path(record_path), fail_plan
        self.scenario, self.rep, self.path, self.wake = '', 0, 'P1', False
        self.quota = False
        self.rows = []
        self.lock = threading.Lock()
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def forward(self, body):
        if self.quota:
            return {'error': {'message': 'QUOTA: run stopped'}}, 429
        request = copy.deepcopy(body)
        request['model'] = (self.model.get(body.get('model'), self.model['primary'])
                            if isinstance(self.model, dict) else self.model)
        request['stream'] = False  # Raw JSON, including real upstream usage.
        request.pop('stream_options', None)
        injected = bool(self.fail_plan and self.wake and
                        body.get('model') == self.fail_plan['primary'])
        rate_limited = 0
        for attempt in range(2):
            started = time.monotonic()
            started_at = time.time()
            retry_after = None
            if injected:
                response, status = {'error': {'message': 'scripted primary outage'}}, 503
            else:
                req = Request(self.url + '/chat/completions', json.dumps(request).encode(),
                              {'Content-Type': 'application/json',
                               'Authorization': 'Bearer ' + os.environ[self.key_env]})
                try:
                    with self.opener.open(req, timeout=180) as stream:
                        status, response = stream.status, json.load(stream)
                except HTTPError as exc:
                    status, retry_after = exc.code, exc.headers.get('Retry-After')
                    raw = exc.read()
                    try:
                        response = json.loads(raw)
                    except ValueError:  # an HTML or plain-text error page
                        response = {'error': {'message': raw[:500].decode('utf-8', 'replace')}}
                except (URLError, TimeoutError) as exc:
                    status, response = 502, {'error': {'message': f'upstream unreachable: {type(exc).__name__}'}}
            rate_limited += status == 429
            with self.lock:
                row = {'scenario': self.scenario, 'rep': self.rep, 'path': self.path,
                       'request': request, 'response': response, 'status': status,
                       'usage': response.get('usage', {}), 'elapsed': time.monotonic() - started, 'started_at': started_at,
                       'wake': self.wake, 'injected': injected,
                       'run_status': 'QUOTA' if rate_limited == 2 else 'CALL'}
                self.record_path.parent.mkdir(parents=True, exist_ok=True)
                with self.record_path.open('a') as stream:
                    stream.write(json.dumps(row) + '\n')
                self.rows.append(row)
            if rate_limited == 2:  # two actual 429s, not a 429 after some other failure
                self.quota = True
            if injected or attempt or not (status == 429 or status >= 500):
                return response, status
            delay = 10.0
            if retry_after:
                try:
                    delay = float(retry_after)
                except ValueError:
                    delay = parsedate_to_datetime(retry_after).timestamp() - time.time()
            time.sleep(max(0, delay))
