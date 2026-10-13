"""One subprocess and one throwaway PulseRig per scenario repetition."""
import argparse
import copy
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

from .upstream import QuotaError, Upstream

ROOT = Path(__file__).resolve().parents[2]


def scenarios():
    return yaml.safe_load(Path(__file__).with_name('scenarios.yaml').read_text())


def fake_reply(scenario, record):
    from tests.fakes.fake_llm_provider import Text, ToolCall

    messages = record['body']['messages']
    users = [str(m.get('content', '')) for m in messages if m['role'] == 'user']
    stop = users and '[in:stop]' in users[-1]
    tools = [m for m in messages if m['role'] == 'tool']
    last = messages[-1]
    if stop:
        if last['role'] != 'tool':
            ids = re.findall(r'"id"\s*:\s*"(w_[^"]+)"', ' '.join(str(t['content']) for t in tools))
            return ToolCall('pulse', {'action': 'cancel', 'id': ids[-1]})
        return Text('I have stopped checking in.')
    if scenario['id'] == 'L9' and last['role'] != 'tool':
        return ToolCall('lookup', {'topic': 'report'})
    if last['role'] != 'tool' or '"result"' in str(last.get('content')):
        notify = scenario['label'] == 'MUST_NOTIFY'
        return ToolCall('pulse', {'action': 'respond', 'notify': notify,
                                  'outcome': 'progress' if notify else 'no_change',
                                  'summary': 'fake plumbing verdict'})
    return Text(' '.join(token.split('|')[0] for token in scenario['expect']) or 'Nothing new.')


def tool_arguments(call):
    try:
        return json.loads(call['function']['arguments'])
    except (TypeError, ValueError):  # malformed model arguments are a finding, not a crash
        return {'unparsed': str(call['function'].get('arguments'))[:500]}


def cell(args, scenario, rep):
    parent = Path(tempfile.mkdtemp(prefix='hb-live-parent-'))
    os.environ.update(HOME=str(parent), HERMES_HOME=str(parent / '.hermes'),
                      HERMES_DISABLE_LAZY_INSTALLS='1', PYTHONDONTWRITEBYTECODE='1')
    sys.dont_write_bytecode = True
    sys.path[:0] = [str(ROOT / 'tests/e2e'), str(args.pin)]
    if args.upstream == 'fake':
        from loopback import install
        install()
    from harness import PulseRig
    from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall
    from .director import LiveDirector
    from .facts_plugin import install

    dest = args.out / args.path
    dest.mkdir(parents=True, exist_ok=True)
    record = dest / f'{scenario["id"]}-{rep}.jsonl'
    fake = None
    if args.upstream == 'fake':
        fake = FakeLLMServer(lambda r: fake_reply(scenario, r))
        fake.start()
        url = fake.base_url
        args.key_env = 'BENCH_FAKE_KEY'
        os.environ[args.key_env] = 'fake-loopback-only'
    else:
        url = args.upstream_url
    models = {'primary': args.model, 'fake-model': args.model,
              'bench-fallback': args.fallback_model}
    upstream = Upstream(url, models, args.key_env, record,
                        {'primary': 'fake-model'} if args.path == 'P3' else None)
    upstream.scenario, upstream.rep, upstream.path = scenario['id'], rep, args.path
    director = LiveDirector(upstream, scenario['forward'])
    rig = PulseRig(f'{args.path}-{scenario["id"]}-{rep}', args.pin, director=director,
                   config={'model': {'streaming': False}})
    def terminate(signum, frame):
        upstream.quota = True
        raise QuotaError('QUOTA: another cell stopped the run')
    signal.signal(signal.SIGTERM, terminate)
    if args.path == 'P3':
        import hermes_yaml
        cfg_path = rig.gateway.hermes_home / 'config.yaml'
        cfg = hermes_yaml.safe_load(cfg_path.read_text())
        cfg['fallback_model'] = {'provider': 'custom', 'model': 'bench-fallback',
                                 'base_url': rig.llm.base_url, 'api_key': 'fake-loopback-only'}
        cfg_path.write_text(hermes_yaml.safe_dump(cfg, sort_keys=False))
    install(rig, scenario['facts'])
    original_wait = rig.wait

    def wait(predicate, what, timeout=90):
        def guarded():
            if upstream.quota:
                raise QuotaError('QUOTA: second upstream 429; run stopped')
            return predicate()
        return original_wait(guarded, what, timeout)
    rig.wait = wait
    result = {'scenario': scenario['id'], 'rep': rep, 'path': args.path,
              'history': scenario['history'], 'wake_note': scenario['arm']['note'],
              'status': 'PENDING', 'delivered': [], 'upstream': args.upstream if args.upstream == 'fake' else 'live'}
    code = 0
    try:
        # The helper strips conventional secret names; also protect arbitrary key-env names.
        key = os.environ.pop(args.key_env)
        try:
            rig.start()
        finally:
            os.environ[args.key_env] = key
        def seed(i, pair):
            token = f'history-{i}'
            director.script(token, Text(pair['assistant']))
            rig.owner(f'{pair["owner"]} [in:{token}]', pair['assistant'])
        for i, pair in enumerate(scenario['history']):
            if scenario['id'] != 'L4' or i == 0:
                seed(i, pair)
        arm = scenario['arm']
        if 'owner' in arm:
            wid = rig.arm(arm['note'], '5s')
            before = rig.visible_ids()
        else:
            create = copy.deepcopy(arm['create'])
            if 'probe' in arm:
                probe = rig.gateway.home / 'ci.txt'
                probe.write_text(arm['probe']['baseline'])
                create['trigger']['path'] = str(probe)
            if arm.get('prior_wake'):
                director.scripted_wakes = True
                director.final = 'Any update on the draft?'
                director.script('arm', ToolCall('pulse', {'action': 'create', 'note': arm['note'],
                    'trigger': {'kind': 'at', 'at': 'in 5s'}}),
                    ToolCall('pulse', {'action': 'create', 'note': arm['note'], **create}), Text('ARMED'))
                rig.owner('arm the check-ins [in:arm]', 'ARMED')
                wakes = sorted(rig.rows('wakes'), key=lambda w: w['next_due_at'])
                rig.settled(wakes[0]['id'])
                assert rig.session()['unanswered_count'] == 1
                director.scripted_wakes = False
                wid = wakes[1]['id']
            else:
                wid = rig.agent_arm('arm', create['trigger'], note=arm['note'])
            before = rig.visible_ids()
            if scenario['id'] == 'L4':
                seed(1, scenario['history'][1])
                before = rig.visible_ids()
            if 'probe' in arm:
                # A fresh wake stores the JSON text 'null'; wait for a real baseline state.
                rig.wait(lambda: json.loads(next(w for w in rig.rows('wakes') if w['id'] == wid)
                                            ['probe_state_json'] or 'null') is not None,
                         'probe baseline', 20)
                if 'changed' in arm['probe']:
                    probe.write_text(arm['probe']['changed'])
            if 'event' in arm:
                from heartbeat2.clock import SystemClock
                from heartbeat2.store import Store
                store = Store(rig.pulse_db, SystemClock())
                try:
                    store.add_event(arm['event']['kind'], f'bench-{rep}', arm['event']['payload'],
                                    session_key=rig.session()['session_key'])
                finally:
                    store.close()
        if 'stop' in arm:
            rig.owner(arm['stop'] + ' [in:stop]')
            before = rig.visible_ids()  # owner stop acknowledgement is not a wake delivery.
            rig.tick_barrier(arm['observe_seconds'])
            rig.gateway.wait_idle(timeout=30)
            rig.owner('/pulse status', 'HeartBeat 2.0:')
        else:
            rig.settled(wid)
        texts = [t for t in rig.new_visible(before) if not t.startswith('HeartBeat 2.0:')]
        result.update(status='COMPLETE', delivered=texts, occurrence=rig.occurrence(wid),
                      wake_set=rig.rows('wakes'), session=rig.session(), tool_errors=[])
        try:
            rig.assert_tools_ok()
        except AssertionError as exc:  # a model's malformed tool call is a finding, not a void cell
            result['tool_errors'] = [str(exc)[:500]]
    except Exception as exc:
        code = 2 if upstream.quota else 1
        result.update(status='QUOTA' if upstream.quota else 'VERIFIED_FAILURE', error=str(exc))
    finally:
        try:
            rig.close()
        finally:
            if fake:
                fake.stop()
        calls = rig.llm.main_requests()
        pulse = {c['id']: tool_arguments(c) for b in calls
            for m in b.get('messages', []) if m['role'] == 'assistant'
            for c in m.get('tool_calls', []) if c['function']['name'] == 'pulse'}
        result['pulse_calls'] = list(pulse.values())
        history = upstream.rows[0]['request']['messages'] if upstream.rows else []
        result['history_rows'] = [{'role': m['role'], 'content': re.sub(r'\s*\[in:[^]]+\]', '',
            director._text(m))} for m in history if m['role'] in ('user', 'assistant') and
            director._text(m) and '⟦pulse o:' not in director._text(m)][-6:]
        result.update(model_calls=calls, upstream_rows=upstream.rows, evidence=str(rig.root))
        result['tokens'] = {k: sum(r['usage'].get(k, 0) for r in upstream.rows)
                            for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')}
        result['tokens']['cached_tokens'] = sum(r['usage'].get('prompt_tokens_details', {}).get(
                                               'cached_tokens', 0) for r in upstream.rows)
        (dest / f'{scenario["id"]}-{rep}.json').write_text(json.dumps(result, indent=2))
        print(f'{scenario["id"]}-{rep}: {result["status"]}', flush=True)
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pin', type=Path, required=True)
    parser.add_argument('--path', choices=['P1', 'P3'], required=True)
    parser.add_argument('--upstream', choices=['fake'])
    parser.add_argument('--upstream-url')
    parser.add_argument('--model')
    parser.add_argument('--fallback-model')
    parser.add_argument('--key-env')
    parser.add_argument('--reps', type=int, default=3)
    parser.add_argument('--workers', type=int, choices=[1, 2], default=2)
    parser.add_argument('--scenarios')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--cell', help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.pin, args.out = args.pin.resolve(), args.out.resolve()
    if args.out == ROOT or ROOT in args.out.parents:
        parser.error('results must stay outside the repository')
    if '.hermes' in args.pin.parts or not (args.pin / 'tests/fakes/fake_llm_provider.py').is_file():
        parser.error('select an isolated Hermes pin containing E2E helpers')
    if args.upstream != 'fake' and not (args.upstream_url and args.key_env):
        parser.error('provide --upstream fake or --upstream-url and --key-env')
    if args.upstream == 'fake':
        args.model = args.model or 'fake-primary'
        args.fallback_model = args.fallback_model or 'fake-secondary'
    elif not args.model or (args.path == 'P3' and not args.fallback_model):
        parser.error('real routing requires --model and P3 also requires --fallback-model')
    if args.reps < 1:
        parser.error('--reps must be positive')
    selected = args.scenarios.split(',') if args.scenarios else [s['id'] for s in scenarios()['scenarios']]
    data = {s['id']: s for s in scenarios()['scenarios']}
    if set(selected) - data.keys():
        parser.error('unknown scenario')
    if args.cell:
        sid, rep = args.cell.split(':')
        return cell(args, data[sid], int(rep))
    args.out.mkdir(parents=True, exist_ok=True)
    jobs = [(sid, rep) for sid in selected for rep in range(1, args.reps + 1)]
    active, code = [], 0
    while jobs or active:
        while jobs and len(active) < args.workers and code != 2:
            sid, rep = jobs.pop(0)
            log = (args.out / f'{args.path}-{sid}-{rep}.log').open('w')
            proc = subprocess.Popen([sys.executable, '-m', 'bench.live.run', *sys.argv[1:],
                                     '--cell', f'{sid}:{rep}'], stdout=log, stderr=subprocess.STDOUT)
            active.append((proc, log))
        for proc, log in list(active):
            rc = proc.poll()
            if rc is not None:
                code = max(code, rc if rc >= 0 else 1)
                log.close()
                print(Path(log.name).read_text(), end='', flush=True)
                print(f'cell exit={rc}', flush=True)
                active.remove((proc, log))
        if code == 2:
            for proc, log in active:
                proc.terminate()
                proc.wait(timeout=30)
                log.close()
            return 2
        time.sleep(.1)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
