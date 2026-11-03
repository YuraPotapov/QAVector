"""Trust is host configuration; publisher text and embedded keys grant nothing."""

import base64
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .archive import json_document, reject, sha256

PREFIX = b'QAVPLUG-SIGNATURE-V1\n'


def assess(package, keys):
    signature = package.files.get('signature.json')
    if signature is None:
        return {'status': 'unsigned', 'publisher_identity': 'local:unsigned', 'key_id': None}
    value = json_document(signature)
    if not isinstance(value, dict) or set(value) != {'algorithm', 'key_id', 'signature'} or value['algorithm'] != 'Ed25519':
        reject('Unsupported signature envelope')
    key_id = value['key_id']
    if not isinstance(key_id, str) or key_id not in keys:
        reject('Signing key is not enrolled in the host trust store', 'PACKAGE_TRUST_REQUIRED')
    try:
        key = base64.b64decode(keys[key_id], validate=True)
        Ed25519PublicKey.from_public_bytes(key).verify(
            base64.b64decode(value['signature'], validate=True), PREFIX + package.files['checksums.json'])
    except (ValueError, TypeError, InvalidSignature):
        reject('Invalid Ed25519 package signature', 'PACKAGE_SIGNATURE_INVALID')
    return {'status': 'verified_signature', 'publisher_identity': 'ed25519:' + sha256(key), 'key_id': key_id}


def authorize(package, trust, decision):
    # Digest-bound on every install/update: no old permission consent is inherited.
    required = {'digest', 'publisher_identity', 'grants'}
    if not isinstance(decision, dict) or set(decision) != required:
        reject('Explicit digest, publisher identity and grants are required', 'PACKAGE_TRUST_REQUIRED')
    if decision['digest'] != package.digest or decision['publisher_identity'] != trust['publisher_identity']:
        reject('Approval belongs to a different archive or publisher', 'PACKAGE_TRUST_REQUIRED')
    if decision['grants'] != package.manifest['permissions_requested']:
        reject('Explicit grants must match the reviewed requested permissions', 'POLICY_UNSATISFIED')
    return dict(decision)
