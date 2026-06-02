"""Argon2 password hashing. Argon2id won the PHC competition; this is the
modern default for password storage."""
from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHash

# Default parameters are OWASP-recommended (time_cost=2, memory_cost=65536,
# parallelism=1). We pin them here to be explicit.
_PH = PasswordHasher(
    time_cost=2,
    memory_cost=65536,
    parallelism=1,
    hash_len=32,
    salt_len=16,
)


def hash_password(plain: str) -> str:
    if not plain or len(plain) < 1:
        raise ValueError("password cannot be empty")
    return _PH.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Constant-time verification. Returns False on any failure (including
    malformed hash) — never raises."""
    if not hashed or not plain:
        return False
    try:
        _PH.verify(hashed, plain)
        return True
    except (VerifyMismatchError, InvalidHash):
        return False
    except Exception:
        return False


def needs_rehash(hashed: str) -> bool:
    """True if argon2 parameters have changed since this hash was stored.
    Call after a successful verify to opportunistically upgrade."""
    try:
        return _PH.check_needs_rehash(hashed)
    except Exception:
        return False
