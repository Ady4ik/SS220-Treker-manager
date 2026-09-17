import asyncio
import time
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from bot.github import GitHubClient, GitHubError
from bot.github_app import InstallationAuth, auth_from_env


def make_auth(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path / "test.pem"
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
    return InstallationAuth("Iv1.example", 123, path), key


def test_sign_cache_refresh_and_use_for_rest_and_graphql(tmp_path):
    auth, key = make_auth(tmp_path)
    tokens = []

    def handle(request):
        if request.url.path.endswith("/access_tokens"):
            payload = jwt.decode(request.headers["Authorization"].removeprefix("Bearer "),
                                 key.public_key(), algorithms=["RS256"])
            assert payload["iss"] == "Iv1.example"
            assert payload["exp"] - payload["iat"] <= 600
            tokens.append(f"installation-{len(tokens)}")
            return httpx.Response(201, json={"token": tokens[-1], "expires_at":
                (datetime.now(UTC) + timedelta(hours=1)).isoformat()})
        assert request.headers["Authorization"] == f"Bearer {tokens[-1]}"
        return httpx.Response(200, json={"data": {"ok": True}})

    async def scenario():
        client = GitHubClient("ignored-old-secret", transport=httpx.MockTransport(handle), app_auth=auth)
        try:
            await asyncio.gather(*(client.request("GET", "/installation/repositories") for _ in range(5)))
            assert len(tokens) == 1
            assert await client.graphql("query { viewer { login } }", {}) == {"ok": True}
            auth.expires_at = time.time() + 30
            await client.request("GET", "/installation/repositories")
            assert len(tokens) == 2
        finally:
            await client.close()
    asyncio.run(scenario())


def test_error_sanitized_and_issue_not_sent(tmp_path):
    auth, _ = make_auth(tmp_path)
    paths = []

    def handle(request):
        paths.append(request.url.path)
        return httpx.Response(401, json={"message": "private secret should not appear"})

    async def scenario():
        client = GitHubClient(app_auth=auth, transport=httpx.MockTransport(handle))
        try:
            with pytest.raises(GitHubError) as error:
                await client.create_issue("owner/repo", "title", "body", [])
            assert "HTTP 401" in str(error.value)
            assert "private secret" not in str(error.value)
            assert paths == ["/app/installations/123/access_tokens"]
        finally:
            await client.close()
    asyncio.run(scenario())


def test_config_does_not_accept_client_secret_as_key(tmp_path):
    with pytest.raises(ValueError):
        auth_from_env({"GITHUB_AUTH_MODE": "app", "GITHUB_APP_ID": "1",
                       "GITHUB_INSTALLATION_ID": "2", "GITHUB_PRIVATE_KEY_PATH": str(tmp_path / "missing.pem")})
    assert auth_from_env({}) is None
