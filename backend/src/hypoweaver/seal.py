from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _default_key_path() -> Path:
    project_root = Path(__file__).resolve().parents[3]
    return project_root / "backend" / "var" / "seal.key"


def get_seal_secret() -> bytes:
    configured = os.getenv("HYPOWEAVER_SEAL_SECRET")
    if configured:
        secret = configured.encode("utf-8")
        if len(secret) < 32:
            raise RuntimeError("HYPOWEAVER_SEAL_SECRET must contain at least 32 bytes")
        return secret

    path = Path(os.getenv("HYPOWEAVER_SEAL_KEY_PATH", _default_key_path()))
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        secret = path.read_bytes().strip()
        if len(secret) < 32:
            raise RuntimeError(f"seal key is too short: {path}")
        return secret
    except FileNotFoundError:
        secret = secrets.token_hex(32).encode("ascii")
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            secret = path.read_bytes().strip()
            if len(secret) < 32:
                raise RuntimeError(f"seal key is too short: {path}")
            return secret
        with os.fdopen(descriptor, "wb") as file:
            file.write(secret)
        return secret


def sign_manifest(manifest: Any) -> str:
    return hmac.new(get_seal_secret(), canonical_json(manifest), hashlib.sha256).hexdigest()


def verify_manifest(manifest: Any, signature: str) -> bool:
    return hmac.compare_digest(sign_manifest(manifest), signature)
