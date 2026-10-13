"""Run offline scenarios, report both systems, gate only HeartBeat 2.0."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import time

from .baseline.heartbeat1 import LABEL
from .sim.metrics import percentile, latency_samples
from .sim.runner import simulate

ROOT=Path(__file__).resolve().parents[1]
CLAIM='deterministic simulation on this suite (advisory); not live gateway delivery'


def load_scenarios(selected=None):
    # PyYAML is a development/test dependency, never a runtime plugin dependency.
    import yaml
    rows=[yaml.safe_load(path.read_text()) for path in sorted((ROOT/'bench/scenarios').glob('*.yaml'))]
    ids=[x['id'] for x in rows]
    if len(ids)!=len(set(ids)):raise ValueError('duplicate scenario ids')
    for row in rows:
        for field in ('id','title','cites','settings','arms','timeline','model_policy','duration','expectations','heartbeat_checklist'):
            if field not in row:raise ValueError(f'{row.get("id")}: missing {field}')
        if row['duration']<=0 or not row['cites']:raise ValueError('scenario must have duration and provenance')
        for event in row['timeline']:
            if not 0<=event['t']<=row['duration']:raise ValueError('event outside duration')
    if selected:
        rows=[x for x in rows if x['id']==selected]
        if not rows:raise ValueError('unknown scenario: '+selected)
    return rows


def totals(results,system):
    rows=[x[system] for x in results if system in x]
    if not rows:return None
    metrics=[x['metrics'] for x in rows]
    summed=('duplicate_deliveries','missed_required_fires','model_wakes','noop_model_wakes','est_tokens','sentinel_leaks','wrong_destination',
            'delivered_messages','message_attempts','resent_messages')
    answer={k:sum(x[k] for x in metrics) for k in summed}
    for k in ('msgs_per_silence_episode','max_burst_per_min'):answer[k]=max(x[k] for x in metrics)
    answer['noop_ratio']=answer['noop_model_wakes']/answer['model_wakes'] if answer['model_wakes'] else 0
    lateness,deferred=latency_samples([t for x in rows for t in x['turns']])
    answer.update(lateness_p50_s=percentile(lateness,.5),lateness_p95_s=percentile(lateness,.95),
                  deferred_p50_s=percentile(deferred,.5),deferred_p95_s=percentile(deferred,.95),
                  deferred_total_s=sum(deferred),latency_sample_count=len(lateness))
    answer['fires_until_pause']={r['id']:r[system]['metrics']['fires_until_pause'] for r in results if system in r and r[system]['metrics']['fires_until_pause']}
    return answer


def display(value):
    if isinstance(value,float):return f'{value:.3f}'
    if value is None:return 'n/a'
    if isinstance(value,dict):return json.dumps(value,sort_keys=True)
    return str(value)


def markdown(report):
    lines=['# HeartBeat 2.0 simulation benchmark', '', 'Claim class: '+CLAIM+'.', '',
           'Baseline: **'+LABEL+'**. Its numbers are never measurements of OpenClaw.', '',
           f'Source HEAD: `{report["source"]["head"]}`; dirty working copy: `{report["source"]["dirty"]}`. Engine content digest: `{report["source"]["engine_digest"]}`.', '',
           'Latency samples cover fires that happened. Lateness excludes observed designed gate deferrals; deferred_s reports those separately. Open ticks have 15 s resolution. Missing fires have no latency sample.', '',
           f'B1 runtime: **{report["engine_runtime_s"]:.3f} s**; all requested arms including baseline: **{report["runtime_s"]:.3f} s**.', '',
           'Message attempts include failed sends; resent_messages counts retries of existing output, without a new model turn. Accepted but lost sends are not retried.', '',
           '| Scenario | HeartBeat 2.0 bars | Naive emulator bars (reported only) | Model wakes HB2 / naive | Wake messages HB2 / naive | Message attempts HB2 / naive | Resent messages HB2 / naive | Est. tokens HB2 / naive |',
           '|---|---|---|---:|---:|---:|---:|---:|']
    for row in report['scenarios']:
        h=row['heartbeat2'];b=row.get('baseline');bm=b['metrics'] if b else {}
        naive='PASS' if b and not b['failures'] else str(len(b['failures']))+' failed bars' if b else 'not run'
        if b and not b['turns']: naive='never fired; see mechanism trace'
        pairs=lambda k:f'{display(h["metrics"][k])} / {display(bm.get(k))}'
        deliveries=lambda arm:sum(d['wake_message'] for d in arm['deliveries']) if arm else None
        lines.append(f'| {row["id"]} | {"PASS" if not h["failures"] else "FAIL"} | {naive} | {pairs("model_wakes")} | {deliveries(h)} / {display(deliveries(b))} | {pairs("message_attempts")} | {pairs("resent_messages")} | {pairs("est_tokens")} |')
    for name,metric in [('Total',report['totals'])]+[(row['id'],{'heartbeat2':row['heartbeat2']['metrics'],'baseline':row.get('baseline',{}).get('metrics')}) for row in report['scenarios']]:
        lines+=['',f'## {name}', '', '| Metric | HeartBeat 2.0 | Naive emulator |', '|---|---:|---:|']
        for key,value in metric['heartbeat2'].items():
            other=(metric.get('baseline') or {}).get(key)
            lines.append(f'| {key} | {display(value)} | {display(other)} |')
        if name!='Total':
            row=next(x for x in report['scenarios'] if x['id']==name)
            lines+=['', row['title'], '', 'Provenance: '+', '.join(row['cites'])+'.', '',
                    f'Settings overrides: `{json.dumps(row["settings"],sort_keys=True)}`; naive fixed interval: {row["baseline_interval_s"]} s; context: {row["context_tokens"]} tokens per model turn.', '',
                    'HeartBeat 2.0 failures: '+('; '.join(row['heartbeat2']['failures']) or 'none')+'.',
                    'Naive failures: '+('; '.join(row.get('baseline',{}).get('failures',[])) or 'none / not run')+'.']
    return '\n'.join(lines)+'\n'


def main(argv=None):
    parser=argparse.ArgumentParser(description=CLAIM)
    parser.add_argument('--scenario');parser.add_argument('--baseline',action='store_true')
    parser.add_argument('--gate',action='store_true');parser.add_argument('--out',type=Path,default=ROOT/'bench/results')
    args=parser.parse_args(argv)
    started=time.perf_counter();engine_time=0;results=[]
    try:scenarios=load_scenarios(args.scenario)
    except ValueError as exc:parser.error(str(exc))
    with tempfile.TemporaryDirectory(prefix='heartbeat2-bench-') as home:
        for scenario in scenarios:
            start=time.perf_counter()
            h=simulate(scenario,Path(home)/scenario['id']/'heartbeat2')
            engine_time+=time.perf_counter()-start
            row={'id':scenario['id'],'title':scenario['title'],'cites':scenario['cites'],'settings':scenario['settings'],
                 'baseline_interval_s':scenario.get('baseline_interval_s',1800),'context_tokens':scenario.get('context_tokens',150000),'heartbeat2':h}
            if args.baseline:row['baseline']=simulate(scenario,Path(home)/scenario['id']/'baseline',baseline=True)
            results.append(row)
    head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    dirty=bool(subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True).strip())
    digest=hashlib.sha256()
    for path in sorted((ROOT/'heartbeat2').glob('*.py')):digest.update(path.name.encode());digest.update(path.read_bytes())
    bench_digest=hashlib.sha256()
    for path in sorted((ROOT/'bench').rglob('*')):
        if path.is_file() and path.suffix in {'.py','.yaml'}:
            bench_digest.update(str(path.relative_to(ROOT)).encode());bench_digest.update(path.read_bytes())
    failures=[f'{row["id"]}: {failure}' for row in results for failure in row['heartbeat2']['failures']]
    if engine_time>=30:failures.append(f'B1 runtime_s={engine_time:.3f} (required <30)')
    report={'claim_class':CLAIM,'baseline_label':LABEL,'source':{'head':head,'dirty':dirty,'engine_digest':digest.hexdigest(),'bench_digest':bench_digest.hexdigest()},
            'runtime_s':time.perf_counter()-started,'engine_runtime_s':engine_time,'scenarios':results,
            'totals':{'heartbeat2':totals(results,'heartbeat2'),'baseline':totals(results,'baseline')},'failures':failures}
    args.out.mkdir(parents=True,exist_ok=True)
    path=args.out/(datetime.now(timezone.utc).strftime('%Y%m%d')+'-'+head[:7])
    path.with_suffix('.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    path.with_suffix('.md').write_text(markdown(report))
    for failure in failures:print('FAIL '+failure)
    print(f'{len(results)} scenarios; B1 {engine_time:.3f}s; '+('PASS' if not failures else 'FAIL'))
    print(path.with_suffix('.md'))
    return int(bool(failures)) if args.gate else 0


if __name__=='__main__':raise SystemExit(main())
