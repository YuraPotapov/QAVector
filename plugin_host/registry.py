"""Transactional metadata catalog and durable per-run contribution locks.

Only the trusted installer/bundled catalog may register validated metadata.
This module is not an archive installer and never loads an entrypoint.
"""

import copy
import json
import re
import sqlite3
from contextlib import contextmanager

from qavector_sdk import ContractError, validate_contract
from qavector_sdk.versions import satisfies, version

from .compatibility import API_VERSIONS, require as require_compatibility
STATES = ("installed_inactive", "active", "disabled", "quarantined", "incompatible")


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class PluginRegistry:
    def __init__(self, path):
        # Separate connections permit GUI/CLI contention; BEGIN IMMEDIATE
        # serializes ownership, pointer changes and run-lock creation.
        self.db = sqlite3.connect(str(path), timeout=60, isolation_level=None)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS packages (
                plugin_id TEXT NOT NULL, version TEXT NOT NULL, digest TEXT NOT NULL,
                manifest TEXT NOT NULL, state TEXT NOT NULL, publisher_identity TEXT NOT NULL,
                PRIMARY KEY(plugin_id, version));
            CREATE TABLE IF NOT EXISTS publisher_ownership (plugin_id TEXT PRIMARY KEY, identity TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ownership (id TEXT PRIMARY KEY, plugin_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS active (plugin_id TEXT PRIMARY KEY, version TEXT NOT NULL,
                FOREIGN KEY(plugin_id, version) REFERENCES packages(plugin_id, version));
            CREATE TABLE IF NOT EXISTS aliases (id TEXT PRIMARY KEY, contribution_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS run_locks (run_id TEXT PRIMARY KEY, document TEXT NOT NULL);
        """)

    def close(self):
        self.db.close()

    @contextmanager
    def _transaction(self):
        if self.db.in_transaction:
            yield
            return
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    @staticmethod
    def _compatible(manifest):
        return require_compatibility(manifest)

    def register(self, manifest, digest, *, publisher_identity, bundled=False, aliases=None,
                 activate_initial=False):
        """Record installer-verified metadata; publisher_identity is host-attested.

        Manifest publisher text is never an authority. Official IDs and legacy
        aliases are reserved for the bundled catalog in this increment.
        """
        manifest = copy.deepcopy(manifest)
        validate_contract("qavplug-manifest", manifest)
        self._compatible(manifest)
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ContractError("PACKAGE_INVALID", "Expected SHA-256 package digest")
        if not isinstance(publisher_identity, str) or not publisher_identity:
            raise ContractError("PACKAGE_INVALID", "Verified publisher identity is required")
        plugin_id, release = manifest["plugin_id"], manifest["version"]
        version(release)
        ids = {item["id"] for item in manifest["contributions"]}
        aliases = dict(aliases or {})
        if activate_initial and not bundled:
            raise ContractError("PACKAGE_INVALID", "Initial automatic activation is reserved for the bundled catalog")
        if (not bundled and (aliases or any(i == "qavector" or i.startswith("qavector.")
                                           for i in ids | {plugin_id}))):
            raise ContractError("PACKAGE_INVALID", "Official IDs and aliases require bundled ownership")
        if any(not isinstance(key, str) or not key or target not in ids or key in ids
               for key, target in aliases.items()):
            raise ContractError("PACKAGE_INVALID", "Invalid legacy alias")
        encoded = _json(manifest)
        with self._transaction():
            prior = self.db.execute("SELECT digest,manifest,publisher_identity FROM packages WHERE plugin_id=? AND version=?",
                                    (plugin_id, release)).fetchone()
            if prior:
                if prior != (digest, encoded, publisher_identity):
                    raise ContractError("PACKAGE_VERSION_CONFLICT", "Package version is already registered with different content or identity")
                return False
            publisher = self.db.execute("SELECT identity FROM publisher_ownership WHERE plugin_id=?", (plugin_id,)).fetchone()
            if publisher and publisher[0] != publisher_identity:
                raise ContractError("PACKAGE_VERSION_CONFLICT", "Publisher identity ownership is retained after removal")
            owners = self.db.execute("SELECT DISTINCT publisher_identity FROM packages WHERE plugin_id=?", (plugin_id,)).fetchall()
            if owners and owners != [(publisher_identity,)]:
                raise ContractError("PACKAGE_VERSION_CONFLICT", "Publisher identity changed")
            for identifier in ids | set(aliases):
                row = self.db.execute("SELECT plugin_id FROM ownership WHERE id=?", (identifier,)).fetchone()
                if row and row[0] != plugin_id:
                    raise ContractError("PACKAGE_VERSION_CONFLICT", "Contribution or alias ID has another owner")
                old_alias = self.db.execute("SELECT contribution_id FROM aliases WHERE id=?", (identifier,)).fetchone()
                if old_alias and aliases.get(identifier) != old_alias[0]:
                    raise ContractError("PACKAGE_VERSION_CONFLICT", "Legacy alias cannot be replaced")
                if identifier in aliases and row and not old_alias:
                    raise ContractError("PACKAGE_VERSION_CONFLICT", "Contribution cannot become an alias")
            self.db.execute("INSERT OR IGNORE INTO publisher_ownership VALUES (?,?)", (plugin_id, publisher_identity))
            initial = activate_initial and not owners
            self.db.execute("INSERT INTO packages VALUES (?,?,?,?,?,?)",
                            (plugin_id, release, digest, encoded,
                             "active" if initial else "installed_inactive", publisher_identity))
            if initial:
                self.db.execute("INSERT INTO active VALUES (?,?)", (plugin_id, release))
            for identifier in ids | set(aliases):
                self.db.execute("INSERT OR IGNORE INTO ownership VALUES (?,?)", (identifier, plugin_id))
            for identifier, target in aliases.items():
                self.db.execute("INSERT OR IGNORE INTO aliases VALUES (?,?)", (identifier, target))
        return True

    def set_state(self, plugin_id, release, state):
        if state not in STATES:
            raise ContractError("PACKAGE_INVALID", "Unknown package state")
        with self._transaction():
            record = self._package(plugin_id, release)
            if state == "active":
                self._compatible(record["manifest"])
                if record["state"] in ("quarantined", "incompatible"):
                    raise ContractError("PLUGIN_NOT_AVAILABLE", "Package must pass reassessment before activation")
                self.db.execute("UPDATE packages SET state='installed_inactive' WHERE plugin_id=? AND state='active'", (plugin_id,))
                self.db.execute("INSERT OR REPLACE INTO active VALUES (?,?)", (plugin_id, release))
            else:
                self.db.execute("DELETE FROM active WHERE plugin_id=? AND version=?", (plugin_id, release))
            self.db.execute("UPDATE packages SET state=? WHERE plugin_id=? AND version=?", (state, plugin_id, release))

    def _package(self, plugin_id, release):
        row = self.db.execute("SELECT digest,manifest,state FROM packages WHERE plugin_id=? AND version=?", (plugin_id, release)).fetchone()
        if row is None:
            raise ContractError("PLUGIN_NOT_AVAILABLE", "Required package version is missing")
        return {"digest": row[0], "manifest": json.loads(row[1]), "state": row[2]}

    def _resolve(self, reference, constraint, pin=None):
        alias = self.db.execute("SELECT contribution_id FROM aliases WHERE id=?", (reference,)).fetchone()
        reference = alias[0] if alias else reference
        owner = self.db.execute("SELECT plugin_id FROM ownership WHERE id=?", (reference,)).fetchone()
        if owner is None:
            raise ContractError("PLUGIN_NOT_AVAILABLE", "Contribution is not registered")
        plugin_id = owner[0]
        if pin is not None:
            if not isinstance(pin, dict) or set(pin) != {"plugin_id", "version", "digest"} or pin["plugin_id"] != plugin_id:
                raise ContractError("PLUGIN_NOT_AVAILABLE", "Invalid exact package pin")
            release = pin["version"]
        else:
            active = self.db.execute("SELECT version FROM active WHERE plugin_id=?", (plugin_id,)).fetchone()
            if active is None:
                raise ContractError("PLUGIN_NOT_AVAILABLE", "No active version; select an explicit installed pin")
            release = active[0]
        record = self._package(plugin_id, release)
        if record["state"] not in ("active", "installed_inactive"):
            raise ContractError("PLUGIN_NOT_AVAILABLE", "Required package version is unavailable")
        if pin and record["digest"] != pin["digest"]:
            raise ContractError("PLUGIN_NOT_AVAILABLE", "Required package digest is missing")
        self._compatible(record["manifest"])
        if not satisfies(release, constraint):
            raise ContractError("PLUGIN_API_INCOMPATIBLE", "Selected package does not satisfy version constraint")
        contributions = {item["id"]: item for item in record["manifest"]["contributions"]}
        if reference not in contributions:
            raise ContractError("PLUGIN_NOT_AVAILABLE", "Selected package version does not provide this contribution")
        return {"plugin_id": plugin_id, "version": release, "digest": record["digest"],
                "contribution": contributions[reference]}

    def resolve(self, reference, constraint=">=1.0.0,<2.0.0", *, pin=None):
        with self._transaction():
            return self._resolve(reference, constraint, pin)

    def lock_run(self, run_id, requirements, *, require_existing=False):
        """Atomically pin an entire run; retries/resume read exactly this lock.

        requirements maps contribution IDs/aliases to {constraint, pin?}.
        Changing requirements under an existing run ID is an error.
        """
        if not isinstance(run_id, str) or not run_id or not isinstance(requirements, dict) or not requirements:
            raise ContractError("PACKAGE_INVALID", "Run identity and contribution requirements are required")
        for reference, item in requirements.items():
            if (not isinstance(reference, str) or not reference or not isinstance(item, dict)
                    or set(item) - {"constraint", "pin"} or "constraint" not in item):
                raise ContractError("PACKAGE_INVALID", "Invalid run requirement")
        with self._transaction():
            row = self.db.execute("SELECT document FROM run_locks WHERE run_id=?", (run_id,)).fetchone()
            if row:
                lock = json.loads(row[0])
                if lock["requirements"] != requirements:
                    raise ContractError("PACKAGE_VERSION_CONFLICT", "Run requirements differ from the persisted lock")
                for binding in lock["bindings"].values():
                    record = self._package(binding["plugin_id"], binding["version"])
                    if record["digest"] != binding["digest"] or record["state"] in ("quarantined", "incompatible"):
                        raise ContractError("PLUGIN_NOT_AVAILABLE", "Locked package cannot resume")
                    self._compatible(record["manifest"])
                return lock
            if require_existing:
                raise ContractError("PLUGIN_NOT_AVAILABLE", "The original run package lock is missing")
            bindings = {ref: self._resolve(ref, item["constraint"], item.get("pin"))
                        for ref, item in requirements.items()}
            lock = {"schema_version": "1.0.0", "run_id": run_id,
                    "requirements": requirements, "bindings": bindings}
            self.db.execute("INSERT INTO run_locks VALUES (?,?)", (run_id, _json(lock)))
            return copy.deepcopy(lock)
