"""Live host/package compatibility, independent of activation and exact run pins."""

import copy

import runtime_paths
from qavector_sdk import ContractError
from qavector_sdk.versions import satisfies, version

API_VERSIONS = {'plugin_api': '1.0.0', 'agent_api': '1.0.0', 'host_api': '1.0.0'}


def host_versions():
    value = runtime_paths.application_version()
    try:
        version(value)
    except ContractError:
        value = None
    return dict(API_VERSIONS, qavector=value)


def report(manifest):
    host = host_versions()
    requirements = manifest.get('compatibility', {})
    issues = []
    required = {'qavector', 'plugin_api', 'host_api'}
    if any(item.get('kind') == 'agent_backend' for item in manifest.get('contributions', [])):
        required.add('agent_api')
    for component in sorted(required | set(requirements)):
        expected, actual = requirements.get(component), host.get(component)
        code = 'QAVECTOR_VERSION_INCOMPATIBLE' if component == 'qavector' else 'PLUGIN_API_INCOMPATIBLE'
        if expected is None:
            reason = 'Required version range is not declared'
        elif actual is None:
            reason = 'Host version is unavailable or unsupported'
        else:
            try:
                if satisfies(actual, expected):
                    continue
                reason = 'Host version is outside the declared range'
            except ContractError:
                reason = 'Invalid version range'
        issues.append(dict(component=component, required=expected, actual=actual, code=code, reason=reason))
    return {'scope': 'qavector_and_public_apis', 'compatible': not issues, 'host': host, 'requirements': copy.deepcopy(requirements), 'issues': issues}


def require(manifest):
    result = report(manifest)
    if result['issues']:
        issue = result['issues'][0]
        raise ContractError(issue['code'], '%s requires %s; running %s. %s.' % (
            issue['component'], issue['required'] or 'an explicit version range',
            issue['actual'] or 'unknown', issue['reason']))
    return result
