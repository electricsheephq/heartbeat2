"""0.3.3: a cron rhythm on the desk is inline code, so chat markdown can't eat its asterisks."""
from test_engine import arm, end, engine, pending, start
from test_v03_u3 import desk


def test_desk_cron_rhythm_is_inline_code_and_keeps_its_asterisks(engine):
    e = engine
    weekly = arm(e, 'cron', role=True, title='Posts', trigger_json={'kind': 'cron', 'cron': '0 10 * * 1,4'})
    tick = arm(e, 'cron', role=True, title='Tick', trigger_json={'kind': 'cron', 'cron': '*/5 * `x` * *'})
    out = desk(e)
    assert f'- role {weekly.id} Posts · cron `0 10 * * 1,4` · ' in out
    row = next(line for line in out.splitlines() if line.startswith(f'- role {tick.id} '))
    assert '· cron `*/5 * x * *` · ' in row and row.count('`') == 2


def test_a_backtick_in_the_title_cannot_open_the_cron_code_span(engine):
    e = engine
    role = arm(e, 'cron', role=True, title='Ship `beta notes', trigger_json={'kind': 'cron', 'cron': '0 9 * * 1'})
    row = next(line for line in desk(e).splitlines() if line.startswith(f'- role {role.id} '))
    assert row.startswith(f"- role {role.id} Ship 'beta notes · cron `0 9 * * 1` · ") and row.count('`') == 2


def test_no_free_text_on_the_desk_can_pair_with_a_cron_code_span(engine):
    e = engine
    notes = arm(e, 'every', role=True, title='Notes', budget_turns_per_day=3,
                trigger_json={'kind': 'every', 'every': '2h'})
    odd = arm(e, 'every', role=True, title='Odd', trigger_json={'kind': 'every', 'every': '2`h', 'at': '09:`00'})
    e.dispatch(pending(e, notes))
    start(e)
    end(e, 'progress', False, summary='updated `RELEASE-NOTES.md')
    paused = arm(e, 'every', role=True, title='Paused', status='paused', status_reason='owner `asked',
                 trigger_json={'kind': 'every', 'every': '1d'})
    weekly = arm(e, 'cron', role=True, title='Posts', trigger_json={'kind': 'cron', 'cron': '0 10 * * 1,4'})
    out = desk(e)
    assert out.count('`') == 2 and 'cron `0 10 * * 1,4`' in out
    assert "last: updated 'RELEASE-NOTES.md" in out and f"- role {odd.id} Odd · every 2'h at 09:'00 · " in out
    assert f"- role {paused.id} Paused · every 1d · last: none · paused · owner 'asked" in out
    assert weekly.id in out
