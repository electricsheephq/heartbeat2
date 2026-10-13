"""One subprocess and one throwaway PulseRig per scenario repetition."""
import argparse
import copy
import json
import os
import re
import signal
import shlex
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
    if scenario.get('type') == 'capture':
        if last['role'] == 'tool':
            if scenario['id'] == 'T2':
                return Text('It awaits approval; please approve it with /pulse ok.')
            if scenario['id'] == 'T1' and '"error"' in str(last.get('content')):
                return Text('I cannot set that reminder.')
            return Text('The tool returned the reported bounds.')
        if scenario['id'] == 'N7':
            return ToolCall('pulse', {'action': 'cancel', 'id': 'all'})
        if scenario['label'] == 'MUST_NOT_ARM':
            return Text('Blue was the logo choice.' if scenario['id'] == 'N4' else 'Understood.')
        return ToolCall('pulse', scenario['_create'])
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


def prearm(rig, spec):
    """Create through the session-bound plugin engine without a gateway turn."""
    from types import SimpleNamespace
    from heartbeat2.clock import SystemClock
    from heartbeat2.policy import Policy
    from heartbeat2.poller import Poller
    from heartbeat2.store import Store

    cfg = yaml.safe_load((rig.gateway.hermes_home / 'config.yaml').read_text())
    entry = cfg['plugins']['entries']['heartbeat2']
    ctx = SimpleNamespace(_manager=SimpleNamespace(home_path=rig.gateway.hermes_home),
                          _gateway_injection_allowed=lambda: entry.get('allow_gateway_injection') is True)
    session = rig.session()
    clock = SystemClock()
    with Store(rig.pulse_db, clock) as store:
        engine = Poller(ctx, store, Policy.from_settings(entry.get('settings', {}), clock=clock),
                        clock, session_key_fn=lambda: session['session_key'])
        try:
            created = engine.tool.handle({'action': 'create', 'note': spec['note'],
                'trigger': {k: v for k, v in spec.items() if k != 'note'}},
                session_id=session['current_session_id'])
            assert created.get('ok') and created['wake']['creator'] == 'agent', created
            return created['wake']['id']
        finally:
            engine.close()


def cell(args, scenario, rep):
    args.out.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(args.out)
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
                        {'primary': 'fake-model'} if args.path.split('-ctx')[0] == 'P3' else None)
    upstream.scenario, upstream.rep, upstream.path = scenario['id'], rep, args.path
    director = LiveDirector(upstream, scenario['forward'])
    rig = PulseRig(f'{args.path}-{scenario["id"]}-{rep}', args.pin, director=director,
                   config={'model': {'streaming': False}, 'plugins': {'entries': {'heartbeat2': {
                       'settings': scenario.get('settings', {})}}}})
    def terminate(signum, frame):
        upstream.quota = True
        raise QuotaError('QUOTA: another cell stopped the run')
    signal.signal(signal.SIGTERM, terminate)
    if args.path.split('-ctx')[0] == 'P3':
        import hermes_yaml
        cfg_path = rig.gateway.hermes_home / 'config.yaml'
        cfg = hermes_yaml.safe_load(cfg_path.read_text())
        cfg['fallback_model'] = {'provider': 'custom', 'model': 'bench-fallback',
                                 'base_url': rig.llm.base_url, 'api_key': 'fake-loopback-only'}
        cfg_path.write_text(hermes_yaml.safe_dump(cfg, sort_keys=False))
    from .round3 import filler_history, resolve, target, turn_pulse
    fixtures = {}
    if scenario.get('fixture'):
        probe = rig.gateway.home / 'probe.txt'
        probe.write_text('RUNNING')
        fixtures['path'] = str(probe)
        if scenario['fixture'] == 'command':
            script = rig.gateway.home / 'build_status.py'
            script.write_text('from pathlib import Path\nimport sys\nsys.exit(0 if Path(__file__).with_name("probe.txt").read_text() == "PASS" else 1)\n')
            probe.write_text('PASS')
            fixtures.update(command=[sys.executable, str(script)], **{'status command': shlex.join([sys.executable, str(script)])})
            import hermes_yaml
            cfg_path = rig.gateway.hermes_home / 'config.yaml'
            cfg = hermes_yaml.safe_load(cfg_path.read_text())
            cfg['plugins']['entries']['heartbeat2']['settings']['probe_commands'] = fixtures['status command']
            cfg_path.write_text(hermes_yaml.safe_dump(cfg))
    scenario['history'] = [{k: resolve(v, time.time(), 'UTC', fixtures) for k, v in pair.items()} for pair in scenario['history']]
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
              'history': scenario['history'], 'wake_note': scenario.get('arm', {}).get('note', ''),
              'status': 'PENDING', 'delivered': [], 'upstream': args.upstream if args.upstream == 'fake' else 'live'}
    result.update(plugin_version=str(yaml.safe_load((ROOT / 'plugin.yaml').read_text())['version']),
                  type=scenario.get('type', 'wake'), timezone='UTC', fixtures=fixtures)
    code = 0
    try:
        # The helper strips conventional secret names; also protect arbitrary key-env names.
        key = os.environ.pop(args.key_env)
        try:
            rig.start(bootstrap=scenario.get('type') != 'capture' and not args.context_tokens)
        finally:
            os.environ[args.key_env] = key
        def seed(i, pair):
            token = f'history-{i}'
            director.script(token, Text(pair['assistant']))
            rig.owner(f'{pair["owner"]} [in:{token}]', pair['assistant'][:100] if args.context_tokens else pair['assistant'])
        if args.context_tokens:
            filler, prefix, digest = filler_history(args.context_tokens)
            result.update(context_tokens=args.context_tokens, context_hash=digest, context_size=len(prefix) // 4,
                          context_pairs=len(filler))
            for i, pair in enumerate(filler):
                seed(f'ctx-{i}', pair)
        for i, pair in enumerate(scenario['history']):
            if scenario['id'] != 'L4' or i == 0:
                seed(i, pair)
        if scenario.get('type') == 'capture':
            prearm_ids = [prearm(rig, scenario['prearm'])] if scenario.get('prearm') else []
            result.update(prearm_ids=prearm_ids, sent_at=time.time())
            result['owner_message'] = resolve(scenario['owner'], result['sent_at'], result['timezone'], fixtures)
            match = scenario.get('match')
            if match:
                trigger = {'kind': match['families'][0]}
                if 'due' in match:
                    from datetime import datetime, timezone
                    trigger['at'] = datetime.fromtimestamp(target(scenario, result), timezone.utc).isoformat()
                else:
                    trigger.update({'path': fixtures['path'], 'pattern': '^DONE$'} if scenario['id'] == 'A3' else
                                   {'command': fixtures['command'], 'fire_on': 'failure'} if scenario['id'] == 'A4' else {'cron': '0 9 * * 1-5'})
                scenario['_create'] = {'action': 'create', 'trigger': trigger, 'note': match['term'],
                                       'owner_asked': trigger['kind'] == 'at', 'until': {'owner_replies': match.get('cancel_on_reply', False), 'expires': 'in 14d'}}
            else:
                scenario['_create'] = {'action': 'create', 'trigger': {'kind': 'at', 'at': 'in 30d'}, 'note': 'renew the domain', 'owner_asked': True}
            before = rig.visible_ids()
            rig.owner(result['owner_message'] + ' [in:capture]')
            result.update(status='COMPLETE', delivered=rig.new_visible(before), wake_set=rig.rows('wakes'), session=rig.session())
            return 0
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
        if scenario.get('type') == 'capture':
            result['pulse_calls'], result['pulse_results'] = turn_pulse(upstream.rows)
        else:
            result['pulse_results'] = turn_pulse([r for r in upstream.rows if r.get('wake')])[1]
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
    parser.add_argument('--round', type=int, choices=[2, 3], default=2)
    parser.add_argument('--context-tokens', type=int, default=0)
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
    data = {s['id']: s for s in scenarios()['scenarios']}
    selected = args.scenarios.split(',') if args.scenarios else [sid for sid, s in data.items()
        if args.round == 3 or s.get('type') != 'capture']
    if set(selected) - data.keys():
        parser.error('unknown scenario: ' + ', '.join(sorted(set(selected) - data.keys())))
    if args.context_tokens < 0:
        parser.error('--context-tokens must be nonnegative')
    if args.context_tokens:
        from .round3 import CTX_IDS
        selected = [sid for sid in selected if sid in CTX_IDS]
        if not selected:
            parser.error('--context-tokens requires at least one context-eligible scenario')
        args.path += f'-ctx{args.context_tokens}'
    if args.path.split('-ctx')[0] == 'P3' and any(data[sid].get('type') == 'capture' for sid in selected):
        parser.error('P3 does not inject its primary fault into capture turns; run capture scenarios on P1')
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
