"""Metadata-only proxies; payload hooks are never called by the host."""

from cycle.registry import CyclePlugin, PluginMetadata, field, output


class InstalledNode(CyclePlugin):
    def __init__(self, catalog):
        self.catalog = catalog
        identifier = catalog['contribution']['id']
        self.metadata = PluginMetadata(id=identifier, name=identifier, version='1.0.0',
            category='action', summary='Installed Node Provider · ' + catalog['effective_state'],
            inputs=(field('config', 'Configuration (JSON)', 'json', default={}),
                    field('inputs', 'Inputs (JSON)', 'json', default={}),
                    field('output_schema', 'Expected output schema (JSON)', 'json'),
                    field('plugin_version', 'Package version constraint', default='>=0.0.0')),
            outputs=(output('output', 'object'), output('summary'), output('node_result', 'object')),
            reusable=False, recoverable=True)

    def problems(self, settings):
        found = super().problems(settings)
        for name in ('config', 'inputs'):
            value = settings.get(name, {})
            if not isinstance(value, dict) and not (isinstance(value, str) and value.startswith('${')):
                found.append(name + ' must be an object or a Cycle expression.')
        from qavector_sdk import ContractError
        from qavector_sdk.versions import satisfies
        try:
            satisfies('0.0.0', settings.get('plugin_version', '>=0.0.0'))
        except ContractError as exc:
            found.append('plugin_version: ' + str(exc))
        # Actual schemas belong to the exact run lock, never the newest active version.
        return found

    def execute(self, context, step):
        from cycle.checkpoints import CheckpointError
        raise CheckpointError('Installed contributions require execution.runtime; host execution is forbidden.')
