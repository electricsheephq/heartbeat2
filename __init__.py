"""Hermes loader entry point. Importing this file does no registration work."""


def register(ctx):
    from .heartbeat2.registration import register as register_plugin
    return register_plugin(ctx)
