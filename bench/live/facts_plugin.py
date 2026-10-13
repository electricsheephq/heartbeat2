"""Standalone, test-only read-only tool; installed into each rig's temp home."""
import json
import os
import shutil
from pathlib import Path


def register(ctx):
    def lookup(args, **kwargs):
        path = Path(os.environ['HERMES_HOME']) / 'facts.json'
        facts = json.loads(path.read_text()) if path.exists() else {}
        return json.dumps({'result': facts.get(args['topic'], 'no record')})

    ctx.register_tool(name='lookup', toolset='bench-facts', handler=lookup,
                      description='Read the bench facts for a topic; no record means unknown.',
                      schema={'name': 'lookup', 'description': 'Read facts for a topic.',
                              'parameters': {'type': 'object', 'properties': {
                                  'topic': {'type': 'string'}}, 'required': ['topic'],
                                  'additionalProperties': False}})


def install(rig, facts):
    import hermes_yaml as yaml

    home = rig.gateway.hermes_home
    plugin = home / 'plugins/bench-facts'
    plugin.mkdir(parents=True)
    shutil.copyfile(__file__, plugin / '__init__.py')
    (plugin / 'plugin.yaml').write_text('name: bench-facts\nversion: 0.0.1\n'
        'manifest_version: 2\napi_version: 1\nkind: standalone\ndescription: Test-only facts\n')
    (home / 'facts.json').write_text(json.dumps(facts))
    path = home / 'config.yaml'
    cfg = yaml.safe_load(path.read_text())
    cfg['plugins']['enabled'].append('bench-facts')
    cfg['plugins']['entries']['bench-facts'] = {'enabled': True}
    cfg['platform_toolsets']['telegram'].append('bench-facts')
    path.write_text(yaml.safe_dump(cfg, sort_keys=False))
