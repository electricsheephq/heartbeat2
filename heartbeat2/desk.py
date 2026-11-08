"""A bounded, read-only view of this chat's responsibilities and budgets."""
from types import SimpleNamespace

from .commands import _clip, _oneline, _time, quiet_until
from .policy import is_watcher
from .schedule import quiet_end, zone
from .wake_text import _age


def cost_line(store, key, timezone, policy):
    counts = store.agent_cost_today(key, timezone)
    text = ''
    if policy.daily_cost_limit_usd or counts['cost_microusd']:
        text = f'cost today ${counts["cost_microusd"]/1e6:.2f}'
        if policy.daily_cost_limit_usd: text += f' of ${policy.daily_cost_limit_usd:.2f}'
    if counts['unpriced_calls']:
        text += (' · ' if text else '') + f'{counts["unpriced_calls"]} unpriced'
    return text


def _plain(text: str) -> str:
    """Free text for a desk row, with backticks made apostrophes so it can't open or close a code span."""
    return text.replace('`', "'")


def _rhythm(wake) -> str:
    """The role's rhythm as the owner set it: every 2m, every 1d at 09:00, cron …, or self-paced."""
    trigger = wake.trigger_json if isinstance(wake.trigger_json, dict) else {}
    if wake.kind == 'every' and trigger.get('every') is not None:
        anchor = f" at {_plain(_clip(_oneline(str(trigger['at'])), 8))}" if trigger.get('at') else ''
        return f"every {_plain(_clip(_oneline(str(trigger['every'])), 16))}{anchor}"
    if wake.kind == 'cron' and trigger.get('cron'):
        # Inline code: chat markdown (Telegram) would otherwise eat a cron expression's `* *` as bold markers.
        return 'cron `' + _clip(_oneline(str(trigger['cron'])).replace('`', ''), 40) + '`'
    if wake.kind == 'self':
        return 'self-paced'
    return wake.kind


def render_desk(engine_or_store, session, *, clock, timezone, policy, measure=len) -> str:
    store = getattr(engine_or_store, 'store', engine_or_store)
    now, key = clock.now(), session.session_key
    # The existing command formatters/override resolver need only these read seams.
    view = SimpleNamespace(store=store, clock=clock, policy=SimpleNamespace(
        resolved_timezone=lambda: zone(timezone), quiet_hours_urgent=policy.quiet_hours_urgent,
        quiet_until=lambda **kw: quiet_end(kw.get('now', now), policy.quiet_hours, timezone)))
    wakes = [w for w in store.list_wakes(key) if w.status in {'active', 'paused'}]
    roles = sorted((w for w in wakes if w.role), key=lambda w: (w.updated_at, w.created_at), reverse=True)
    rows = []
    for w in roles:
        history = [o for o in store.list_occurrences(w.id) if o.started_at is not None and o.ended_at is not None]
        last = max(history, key=lambda o: o.ended_at) if history else None
        # Free text loses its backticks: Hermes pairs inline-code backticks across lines, so a stray one anywhere
        # before a cron rhythm (this row's title, or an earlier row's summary or reason) would swallow its code span.
        summary = _plain(_clip(_oneline(last.summary or last.outcome or last.state), 80)) if last else 'none'
        today, budget = store.dispatches_today(w.id, zone(timezone)), policy.role_budget(w)
        due = ('paused · ' + _plain(_oneline(w.status_reason or 'paused')) if w.status == 'paused'
               else 'resting until tomorrow' if today >= budget
               else 'next ' + _time(view, w.next_due_at, compact=True))
        title = _plain(_clip(_oneline(w.title or w.note), 80))
        rows.append(f'- role {w.id} {title} · {_rhythm(w)} · last: {summary} · '
                    f'{due} · today {today}/{budget}')
    other = [w for w in wakes if not w.role and w.status == 'active']
    due = [(t, w) for w in other for t in (w.next_due_at, w.deadline_at) if t is not None]
    next_due = min(due, key=lambda pair: pair[0]) if due else None
    tail = [f'- Other: {sum(not is_watcher(w) for w in other)} wakes, '
            f'{sum(is_watcher(w) for w in other)} watchers · next due ' +
            (f'{_time(view,next_due[0],compact=True)} ({_clip(_oneline(next_due[1].title or next_due[1].note),40)})'
             if next_due else 'none')]
    counts = store.daily_counts(key, zone(timezone))
    age = _age(now-session.last_human_at) + ' ago' if session.last_human_at is not None else 'unknown'
    cost = cost_line(store,key,zone(timezone),policy)
    tail.append(f'- Owner last wrote {age} · unanswered {session.unanswered_count} · '
                f'messages today {counts["delivered"]}/{policy.max_messages_per_day} · '
                f'turns today {counts["model_turns"]}/{policy.max_model_wakes_per_day}' + (' · '+cost if cost else ''))
    held = ('quiet hours' if quiet_until(view,key) is not None else
            "the owner hasn't replied yet" if session.unanswered_count >= policy.max_unanswered else
            "today's message limit" if counts['delivered'] >= policy.max_messages_per_day else '')
    if held: tail.append('- Messages held now: '+held)
    omitted = 0
    while True:
        text = '\n'.join(['Your desk:', *rows, *tail, *([f'… {omitted} more'] if omitted else [])])
        if measure(text) <= 1800: return text
        if rows:
            rows.pop()  # Keep the most recently touched roles, and preserve the budget lines.
            omitted += 1
        else:
            while measure(text) > 1800: text = text[:-1]
            return text
