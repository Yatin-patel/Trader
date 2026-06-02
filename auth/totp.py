"""TOTP (RFC 6238) two-factor authentication helpers.

Workflow:
    generate_secret() -> base32 string, store on user.totp_secret
    provisioning_uri(email, secret) -> otpauth:// URI for QR
    qr_png_data_uri(uri) -> data:image/png;base64,... for inline display
    verify(secret, code) -> bool, accepts current ± 1 step (±30s)

NOTE — pricing policy: 2FA is free for all users, with no limits and no
plan gating. Security features should never be paywalled. This module
intentionally contains no plan/tier checks and never will. If commercial
tiers are introduced later, gate features like extra projects, SaaS
hosting, or higher loop frequencies — NOT security.
"""
from __future__ import annotations

import base64
import io

import pyotp
import qrcode
from qrcode.image.svg import SvgPathImage

ISSUER = "Autonomous Trader"


def generate_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(email: str, secret: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=ISSUER)


def verify(secret: str, code: str) -> bool:
    if not secret or not code:
        return False
    try:
        return pyotp.TOTP(secret).verify(code.strip(), valid_window=1)
    except Exception:
        return False


def qr_png_data_uri(uri: str) -> str:
    """Render the provisioning URI as a base64-encoded SVG data URI suitable
    for embedding directly in an <img src="...">.

    Uses qrcode's SVG backend so we don't pull in Pillow as a hard dep.
    The function name keeps `_png_` for historic callers — content is SVG.
    """
    qr = qrcode.QRCode(box_size=10, border=2)
    qr.add_data(uri)
    qr.make(fit=True)
    img = qr.make_image(image_factory=SvgPathImage)
    buf = io.BytesIO()
    img.save(buf)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/svg+xml;base64,{b64}"
