"""Bounded, data-only QAVPLUG and nested ZIP validation. Never imports payloads."""

import hashlib
import io
import json
import os
import re
import stat
import unicodedata
import zipfile
from dataclasses import dataclass

from qavector_sdk import ContractError, validate_contract
from qavector_sdk.validation import validate_schema
from .registry import PluginRegistry


def reject(message, code='PACKAGE_INVALID'):
    raise ContractError(code, message)


def json_document(raw):
    if len(raw) > 2 * 1024 ** 2:
        reject('Metadata exceeds 2 MiB')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                reject('Duplicate JSON key')
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs,
                          parse_constant=lambda _: reject('Nonfinite JSON'))
    except (ValueError, UnicodeError, RecursionError):
        reject('Invalid JSON document')


@dataclass(frozen=True)
class ArchiveLimits:
    compressed: int = 256 * 1024 ** 2
    expanded: int = 512 * 1024 ** 2
    file_bytes: int = 128 * 1024 ** 2
    files: int = 20000
    depth: int = 16
    ratio: int = 200


LIMITS = ArchiveLimits()


def safe_path(name):
    if (not isinstance(name, str) or not name or len(name.encode('utf-8')) > 1024
            or unicodedata.normalize('NFC', name) != name or '\\' in name
            or any(ord(c) < 32 or c in ':<>"|?*' for c in name)):
        reject('Unsafe archive path')
    parts = name.split('/')
    for part in parts:
        if (part in ('', '.', '..') or part.endswith((' ', '.'))
                or re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', part)):
            reject('Unsafe or reserved archive path')
    return name


def zip_files(raw, limits=LIMITS):
    if len(raw) > limits.compressed:
        reject('Compressed archive limit exceeded')
    result, names, prefixes, total = {}, {}, {}, 0
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) > limits.files:
                reject('Archive entry limit exceeded')
            for entry in entries:
                original = entry.orig_filename
                directory = original.endswith('/')
                name = safe_path(original[:-1] if directory else original)
                if entry.filename != original or len(name.split('/')) > limits.depth:
                    reject('Unsafe archive name or depth')
                folded = name.casefold()
                if folded in names:
                    reject('Duplicate or case-colliding archive path')
                names[folded] = directory
                parts = name.split('/')
                for n in range(1, len(parts) + 1):
                    prefix = '/'.join(parts[:n])
                    if prefixes.get(prefix.casefold(), prefix) != prefix:
                        reject('Case-colliding archive directory')
                    prefixes[prefix.casefold()] = prefix
                mode = entry.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if (entry.flag_bits & 1 or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                        or kind not in (0, stat.S_IFDIR if directory else stat.S_IFREG)
                        or mode & (stat.S_ISUID | stat.S_ISGID)):
                    reject('Encrypted, special or unsupported ZIP entry')
                total += entry.file_size
                if (entry.file_size > limits.file_bytes or total > limits.expanded
                        or entry.file_size > max(entry.compress_size, 1) * limits.ratio):
                    reject('Expanded archive or compression-ratio limit exceeded')
                if directory:
                    if entry.file_size:
                        reject('Directory contains payload')
                    continue
                with archive.open(entry) as source:
                    data = source.read(entry.file_size + 1)
                if len(data) != entry.file_size:
                    reject('Truncated or oversized ZIP member')
                result[name] = data
            for name in names:
                parts = name.split('/')
                if any(names.get('/'.join(parts[:n])) is False for n in range(1, len(parts))):
                    reject('File/directory archive collision')
    except (zipfile.BadZipFile, NotImplementedError, OSError, RuntimeError):
        reject('Invalid ZIP archive')
    return result


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def schemas_for(files, manifest):
    schemas = {}
    for item in manifest['contributions']:
        for key in ('config_schema', 'input_schema', 'output_schema', 'ui_schema'):
            if key in item:
                name = safe_path(item[key])
                if not name.startswith('schemas/') or name not in files:
                    reject('Missing schema: ' + name)
                value = json_document(files[name])
                if key != 'ui_schema':
                    validate_schema(value, code='PACKAGE_INVALID')
                schemas[name] = value
    # v1 embeds self-contained schemas; no filesystem/network resolution or
    # cross-document mutation. Local JSON pointers are supported by the SDK.
    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ('$ref', '$dynamicRef') and (not isinstance(item, str) or not item.startswith('#')):
                    reject('Schemas must use document-local references')
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    for value in schemas.values():
        visit(value)
    return schemas


class Package:
    def __init__(self, raw, limits=LIMITS, *, check_compatibility=True):
        self.raw, self.digest = raw, sha256(raw)
        self.files = zip_files(raw, limits)
        for required in ('manifest.json', 'checksums.json'):
            if required not in self.files:
                reject('Missing ' + required)
        inventory = json_document(self.files['checksums.json'])
        expected = set(self.files) - {'checksums.json', 'signature.json'}
        if not isinstance(inventory, dict) or set(inventory) != expected:
            reject('Checksum inventory must cover every payload file exactly')
        for name, digest in inventory.items():
            if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest) or sha256(self.files[name]) != digest:
                reject('Payload checksum mismatch: ' + name)
        self.manifest = json_document(self.files['manifest.json'])
        validate_contract('qavplug-manifest', self.manifest)
        if check_compatibility:
            PluginRegistry._compatible(self.manifest)
        for name in self.files:
            if name not in ('manifest.json', 'checksums.json', 'signature.json', 'README.md', 'LICENSE',
                            self.manifest['dependencies']['python_lock']) and not name.startswith(
                                ('schemas/', 'wheels/', 'assets/', 'licenses/')):
                reject('Unsupported package layout: ' + name)
            if name.startswith('wheels/') and (not name.endswith('.whl') or name.count('/') != 1):
                reject('Only flat bundled wheels are supported')
        self.schemas = schemas_for(self.files, self.manifest)
        lock = self.manifest['dependencies']['python_lock']
        if lock not in self.files:
            reject('Missing dependency lock')
        self.lock = json_document(self.files[lock])
        if not isinstance(self.lock, dict) or set(self.lock) != {'format_version', 'targets'} or type(self.lock['format_version']) is not int or self.lock['format_version'] != 1:
            reject('Expected version 1 JSON dependency lock')
        if not isinstance(self.lock['targets'], dict) or set(self.lock['targets']) != set(self.manifest['runtime']['targets']):
            reject('Lock targets must match manifest targets')

    @classmethod
    def read(cls, path, limits=LIMITS, *, check_compatibility=True):
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                reject('Package input must be a regular file')
            raw = stream.read(limits.compressed + 1)
        return cls(raw, limits, check_compatibility=check_compatibility)
