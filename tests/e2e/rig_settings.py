"""Config-only seam, importable without the pinned Hermes test helpers."""


def replace_plugin_settings(config, settings):
    if settings is not None:
        config['plugins']['entries']['heartbeat2']['settings'] = dict(settings)
    return config
