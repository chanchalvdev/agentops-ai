"""API-key authentication mapping each key to exactly one tenant.

Every data access path takes a Principal and filters by its tenant_id; nothing reads tenant
identity from the request body.
"""

import hashlib
import hmac
import logging
from dataclasses import dataclass

from app.core.config import Settings
from app.core.errors import AuthenticationError

logger = logging.getLogger(__name__)

DEV_API_KEYS = {"dev-key-tenant-a": "tenant_a", "dev-key-tenant-b": "tenant_b"}


DOCUMENT_PERMISSIONS = frozenset({"documents:read"})
WORKSPACE_PERMISSIONS = frozenset({"workspace:read", "workspace:write", "workspace:exec"})


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    key_id: str  # short fingerprint of the key, safe to log
    permissions: frozenset[str] = DOCUMENT_PERMISSIONS


def _fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def parse_api_keys(raw: str) -> dict[str, str]:
    keys: dict[str, str] = {}
    for item in filter(None, (part.strip() for part in raw.split(","))):
        key, sep, tenant = item.partition(":")
        if not sep or not key.strip() or not tenant.strip():
            raise ValueError("AGENTOPS_API_KEYS entries must look like 'key:tenant'")
        if len(key.strip()) < 16:
            raise ValueError("API keys must be at least 16 characters")
        keys[key.strip()] = tenant.strip()
    return keys


class ApiKeyAuthenticator:
    def __init__(self, settings: Settings) -> None:
        keys = parse_api_keys(settings.api_keys.get_secret_value())
        if not keys and settings.env != "prod":
            logger.warning("no API keys configured; using built-in dev keys (never use in prod)")
            keys = dict(DEV_API_KEYS)
        # Store hashes so a heap dump or debug print does not reveal raw keys.
        self._digests = {hashlib.sha256(k.encode()).digest(): t for k, t in keys.items()}
        self._workspace_tenants = settings.workspace_tenant_ids

    def permissions_for(self, tenant_id: str) -> frozenset[str]:
        """Permissions come from server configuration, never from the request."""
        if tenant_id in self._workspace_tenants:
            return DOCUMENT_PERMISSIONS | WORKSPACE_PERMISSIONS
        return DOCUMENT_PERMISSIONS

    def authenticate(self, presented: str | None) -> Principal:
        if not presented:
            raise AuthenticationError("Missing X-API-Key header")
        digest = hashlib.sha256(presented.encode()).digest()
        tenant = None
        # Compare against every key so timing does not reveal which prefix matched.
        for known, known_tenant in self._digests.items():
            if hmac.compare_digest(known, digest):
                tenant = known_tenant
        if tenant is None:
            raise AuthenticationError("Invalid API key")
        return Principal(
            tenant_id=tenant,
            key_id=_fingerprint(presented),
            permissions=self.permissions_for(tenant),
        )
