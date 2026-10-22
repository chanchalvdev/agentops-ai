import json
import logging

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.core.errors import AuthenticationError, ProviderError, ProviderUnavailableError
from app.core.logging import JsonFormatter, redact
from app.core.retry import backoff_delay, retry_async
from app.core.security import ApiKeyAuthenticator, parse_api_keys


def test_prod_requires_api_keys_and_database_url() -> None:
    with pytest.raises(ValidationError, match="API_KEYS"):
        Settings(env="prod")
    with pytest.raises(ValidationError, match="DATABASE_URL"):
        Settings(env="prod", api_keys=SecretStr("k" * 20 + ":t"))


def test_overlap_must_be_smaller_than_chunk_size() -> None:
    with pytest.raises(ValidationError):
        Settings(chunk_size_chars=500, chunk_overlap_chars=500)


def test_api_keys_map_to_tenants() -> None:
    auth = ApiKeyAuthenticator(
        Settings(api_keys=SecretStr("a" * 16 + ":acme, " + "b" * 16 + ":globex"))
    )
    assert auth.authenticate("a" * 16).tenant_id == "acme"
    assert auth.authenticate("b" * 16).tenant_id == "globex"
    with pytest.raises(AuthenticationError):
        auth.authenticate("c" * 16)
    with pytest.raises(AuthenticationError):
        auth.authenticate(None)


@pytest.mark.parametrize("raw", ["nocolon", "short:tenant", "x" * 16 + ":"])
def test_malformed_api_keys_rejected(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_api_keys(raw)


def test_dev_keys_only_outside_prod() -> None:
    assert (
        ApiKeyAuthenticator(Settings(env="dev")).authenticate("dev-key-tenant-a").tenant_id
        == "tenant_a"
    )


def test_redaction() -> None:
    data = {
        "api_key": "x",
        "nested": {"Authorization": "Bearer abc"},
        "ok": "value",
        "msg": "token Bearer abc.def and sk-abcdefghijklmnop and "
        "postgresql+asyncpg://user:hunter2@db/x",
    }
    out = redact(data)
    assert out["api_key"] == "[REDACTED]" and out["nested"]["Authorization"] == "[REDACTED]"
    assert out["ok"] == "value"
    assert "abc" not in out["msg"] and "sk-abc" not in out["msg"] and "hunter2" not in out["msg"]
    assert "postgresql+asyncpg://user:[REDACTED]@db/x" in out["msg"]


def test_json_log_line_redacts_extras() -> None:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "hello", None, None)
    record.authorization = "Bearer secret"
    line = json.loads(JsonFormatter().format(record))
    assert line["msg"] == "hello" and line["authorization"] == "[REDACTED]"


def test_backoff_is_capped_and_jittered() -> None:
    delays = [backoff_delay(10, 0.5, 8.0) for _ in range(200)]
    assert all(0 <= d <= 8.0 for d in delays)
    assert len(set(delays)) > 100  # jitter, not a fixed schedule


async def test_retry_only_transient_and_bounded() -> None:
    calls = 0

    async def always_down() -> None:
        nonlocal calls
        calls += 1
        raise ProviderUnavailableError()

    with pytest.raises(ProviderUnavailableError):
        await retry_async(always_down, max_retries=3, base_delay_s=0)
    assert calls == 4

    calls = 0

    async def permanent() -> None:
        nonlocal calls
        calls += 1
        raise ProviderError()

    with pytest.raises(ProviderError):
        await retry_async(permanent, max_retries=3, base_delay_s=0)
    assert calls == 1
