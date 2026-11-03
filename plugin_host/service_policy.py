"""Deployment-owned services offered to a single installed contribution."""

import copy
import ipaddress
import re
import ssl
from urllib.parse import urlsplit

from execution_runtime._json import Snapshot, object_fields
from execution_runtime.errors import RuntimePolicyError
from execution_runtime.policy import PolicyPatch


def deny(message):
    raise RuntimePolicyError('plugin_service_denied', message)


def name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,127}', value):
        deny('Service references must be bounded identifiers.')


def limit(value, maximum, field):
    if type(value) is not int or not 1 <= value <= maximum:
        deny('%s must be between 1 and %s.' % (field, maximum))


class NetworkRoute(Snapshot):
    def __init__(self, raw):
        object_fields(raw, ('endpoint', 'addresses', 'method', 'credential', 'authentication',
            'timeout_seconds', 'request_bytes', 'response_bytes', 'max_requests',
            'allow_private_addresses', 'ca_pem', 'models'), 'plugin network route', ('endpoint', 'addresses'))
        try:
            endpoint = raw['endpoint']
            if not isinstance(endpoint, str) or len(endpoint) > 2048 or any(ord(c) < 33 or ord(c) > 126 for c in endpoint) or '\\' in endpoint:
                raise ValueError()
            url = urlsplit(endpoint)
            port = 443 if url.port is None else url.port
            if (url.scheme != 'https' or not url.hostname or url.username is not None or url.password is not None
                    or '?' in endpoint or '#' in endpoint or not url.path.startswith('/') or url.path.startswith('//')
                    or url.netloc.endswith(':') or not 1 <= port <= 65535):
                raise ValueError()
            PolicyPatch({'network': {'mode': 'restricted', 'endpoints': [{'host': url.hostname, 'port': port}]}})
        except (ValueError, RuntimePolicyError):
            deny('Approve an exact HTTPS endpoint without user info, query or fragment.')
        addresses = raw['addresses']
        private = raw.get('allow_private_addresses', False)
        if type(private) is not bool or not isinstance(addresses, list) or not 1 <= len(addresses) <= 8:
            deny('Pin 1..8 reviewed IP addresses; private addresses require explicit approval.')
        for value in addresses:
            try:
                ip = ipaddress.ip_address(value)
                if (not isinstance(value, str) or value != str(ip) or ip.is_unspecified or ip.is_multicast
                        or ip.is_link_local or ip.is_reserved or getattr(ip, 'ipv4_mapped', None) is not None
                        or not ip.is_global and not (private and (ip.is_private or ip.is_loopback))):
                    raise ValueError()
            except (ValueError, TypeError):
                deny('Network address is not an approved unicast pin.')
        if len(set(addresses)) != len(addresses):
            deny('Network address pins must be unique.')
        try:
            literal = ipaddress.ip_address(url.hostname)
        except ValueError:
            literal = None
        if literal is not None and set(addresses) != {str(literal)}:
            deny('Numeric endpoint and address pin must agree.')
        if raw.get('method', 'POST') not in ('GET', 'POST'):
            deny('Supported route methods are GET and POST.')
        for key, default, maximum in (('timeout_seconds', 30, 120), ('request_bytes', 65536, 65536),
                                      ('response_bytes', 65536, 65536), ('max_requests', 4, 16)):
            limit(raw.get(key, default), maximum, key)
        if 'credential' in raw:
            name(raw['credential'])
            if raw.get('authentication', 'bearer') not in ('bearer', 'x-api-key', 'api-key'):
                deny('Unsupported credential header delivery.')
        elif 'authentication' in raw:
            deny('Authentication needs a credential alias.')
        if 'models' in raw and (not isinstance(raw['models'], list) or not 1 <= len(raw['models']) <= 32 or
                any(not isinstance(item, str) or not item or len(item) > 256 or
                    any(ord(char) < 33 or ord(char) > 126 for char in item) for item in raw['models']) or
                len(set(raw['models'])) != len(raw['models'])):
            deny('Models must be a bounded allowlist.')
        if 'ca_pem' in raw:
            if not isinstance(raw['ca_pem'], str) or not 1 <= len(raw['ca_pem']) <= 16384:
                deny('CA must be bounded PEM text.')
            try:
                ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cadata=raw['ca_pem'])
            except (ValueError, OSError):
                deny('Invalid reviewed CA certificate.')
        super().__init__(raw)

    @property
    def endpoint(self):
        url = urlsplit(self.to_document()['endpoint'])
        return {'host': url.hostname, 'port': 443 if url.port is None else url.port, 'protocol': 'tcp'}


def validate_services(document):
    if not isinstance(document, dict) or len(document) > 32:
        deny('Expected at most 32 contribution service profiles.')
    for identifier, config in document.items():
        name(identifier)
        object_fields(config, ('network', 'processes', 'credentials'), 'plugin services')
        for kind in ('network', 'processes', 'credentials'):
            entries = config.get(kind, {})
            if not isinstance(entries, dict) or len(entries) > 16:
                deny('Expected at most 16 named %s services.' % kind)
            for alias in entries:
                name(alias)
        credentials = config.get('credentials', {})
        for reference in credentials.values():
            name(reference)
        for route in config.get('network', {}).values():
            NetworkRoute(route)
            if 'credential' in route and route['credential'] not in credentials:
                deny('Network route uses an undeclared credential alias.')
        for command in config.get('processes', {}).values():
            object_fields(command, ('argv', 'credentials', 'timeout_seconds', 'max_calls', 'output_bytes'), 'plugin process', ('argv',))
            argv = command['argv']
            if (not isinstance(argv, list) or not 1 <= len(argv) <= 32 or
                    any(not isinstance(arg, str) or not arg or '\0' in arg or len(arg) > 4096 for arg in argv)
                    or not argv[0].startswith('/') or '..' in argv[0].split('/')):
                deny('A process requires a pinned absolute executable and bounded argv prefix.')
            aliases = command.get('credentials', [])
            if not isinstance(aliases, list) or any(not isinstance(alias, str) or alias not in credentials for alias in aliases) or len(set(aliases)) != len(aliases):
                deny('Process credentials must select declared aliases.')
            for index, arg in enumerate(argv):
                if '{credential:' in arg and (index == 0 or arg not in {'{credential:' + alias + '}' for alias in aliases}):
                    deny('Credential placeholders must be complete arguments for approved aliases.')
            for key, default, maximum in (('timeout_seconds', 30, 120), ('max_calls', 4, 16), ('output_bytes', 32768, 32768)):
                limit(command.get(key, default), maximum, key)


def select_services(deployment, worker_spec, policy):
    """Intersect consent with a contribution-scoped, reviewed deployment."""
    identifier = worker_spec['binding']['contribution']['id']
    config = copy.deepcopy(deployment.to_document()['spec'].get('plugin_services', {}).get(identifier, {}))
    grants, raw = worker_spec['grants'], policy.policy.to_document()
    network = config.get('network', {}) if grants['network'] == 'policy_controlled' else {}
    credentials = config.get('credentials', {})
    if not set(grants['credentials']) <= set(credentials):
        deny('Every requested credential needs a deployment-owned alias mapping.')
    credentials = {alias: credentials[alias] for alias in grants['credentials']}
    if not set(credentials.values()) <= set(raw['secrets']):
        deny('Credential references exceed the effective secret grants.')
    processes = config.get('processes', {})
    if not set(grants['process']) <= set(processes) or not set(grants['process']) <= set(raw['tools']):
        deny('Every requested process must be approved by deployment and effective tool policy.')
    processes = {ref: processes[ref] for ref in grants['process']}
    files = {item['target']: item for item in worker_spec['image']['files']}
    for ref, command in processes.items():
        pin = worker_spec['external_bindings'].get(ref)
        file = files.get(command['argv'][0])
        if not pin or not file or not file['executable'] or pin != {'path': file['target'], 'sha256': file['sha256']}:
            deny('Process prerequisite must match its approved executable hash.')
        if not set(command.get('credentials', [])) <= set(credentials):
            deny('Process credential delivery exceeds package consent.')
    for route in network.values():
        endpoint = NetworkRoute(route).endpoint
        if raw['network']['mode'] != 'restricted' or endpoint not in raw['network']['endpoints']:
            deny('Network routes require matching restricted endpoint grants.')
        if route.get('credential') is not None and route['credential'] not in credentials:
            deny('Network authentication exceeds package credential consent.')
    if grants['network'] == 'policy_controlled' and not network:
        deny('Network permission needs an approved HTTPS route for this contribution.')
    return dict(network=network, processes=processes, credentials=credentials)
