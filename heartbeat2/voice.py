"""Optional, profile-local live voice transport. Ordinary API sessions stay refused."""


def hub(engine):
    transport = getattr(getattr(engine.ctx, '_manager', None), '_heartbeat2_voice_hub', None)
    methods = ('voice_target', 'owner_message', 'wake_ready', 'inject_wake')
    return transport if all(callable(getattr(transport, name, None)) for name in methods) else None


def current(engine):
    from .tool import session_env
    transport = hub(engine)
    if transport is None or session_env('HERMES_SESSION_PLATFORM') != 'api_server':
        return None
    sid = session_env('HERMES_SESSION_ID') or session_env('HERMES_SESSION_CHAT_ID')
    target = transport.voice_target(sid)
    return (target, sid) if target else None


def supported(engine, session):
    transport = hub(engine)
    return (session.platform == 'api_server' and transport is not None
            and transport.voice_target(session.chat_id) == session.chat_id
            and session.session_key == 'heartbeat2:voice:' + session.chat_id)


def inject(engine, session, text):
    transport = hub(engine)
    return bool(transport and transport.inject_wake(session.chat_id, text))
