"""In-memory installation tokens. Never log JWTs, PEMs, or token response bodies."""
import asyncio
import time
from datetime import datetime
from pathlib import Path

import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key


class AppAuthError(RuntimeError):
    pass


class InstallationAuth:
    def __init__(self, issuer, installation_id, private_key_path):
        if not issuer or not str(installation_id).isdigit() or int(installation_id) <= 0:
            raise ValueError("Укажите GitHub App Client ID/App ID и положительный Installation ID")
        try:
            key = load_pem_private_key(Path(private_key_path).read_bytes(), password=None)
            if not isinstance(key, RSAPrivateKey):
                raise TypeError("RSA required")
        except (OSError, ValueError, TypeError) as exc:
            raise ValueError("Не удалось прочитать RSA Private Key: проверьте GITHUB_PRIVATE_KEY_PATH") from exc
        self.key = key
        self.issuer = str(issuer)
        self.installation_id = int(installation_id)
        self.token = None
        self.expires_at = 0
        self.lock = asyncio.Lock()

    async def authorization(self, client):
        async with self.lock:
            now = time.time()
            if self.token and now < self.expires_at - 120:
                return f"Bearer {self.token}"
            signed = jwt.encode(
                {"iat": int(now) - 60, "exp": int(now) + 540, "iss": self.issuer},
                self.key, algorithm="RS256",
            )
            response = await client.post(
                f"/app/installations/{self.installation_id}/access_tokens",
                headers={"Authorization": f"Bearer {signed}"}, json={},
            )
            if response.is_error:
                raise AppAuthError(
                    f"GitHub App: получение installation token — HTTP {response.status_code}; "
                    "проверьте ID приложения, Installation ID, ключ и установку"
                )
            try:
                data = response.json()
                token = data["token"]
                expiration = datetime.fromisoformat(data["expires_at"])
                if not isinstance(token, str) or not token or expiration.tzinfo is None:
                    raise ValueError("Invalid token response")
                if expiration.timestamp() <= now + 120:
                    raise ValueError("Expired token")
            except (KeyError, TypeError, ValueError) as exc:
                raise AppAuthError("GitHub App: некорректный ответ получения токена") from exc
            self.token, self.expires_at = token, expiration.timestamp()
            return f"Bearer {token}"


def auth_from_env(env):
    mode = env.get("GITHUB_AUTH_MODE", "token").strip().lower()
    if mode == "token":
        return None
    if mode != "app":
        raise ValueError("GITHUB_AUTH_MODE должен быть app или token")
    return InstallationAuth(
        env.get("GITHUB_APP_CLIENT_ID") or env.get("GITHUB_APP_ID"),
        env.get("GITHUB_INSTALLATION_ID", ""),
        env.get("GITHUB_PRIVATE_KEY_PATH", ""),
    )
