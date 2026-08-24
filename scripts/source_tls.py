import hashlib
import ssl
from functools import lru_cache
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

SOGANG_SOURCE_TLS_HOSTS = frozenset({"sogang.ac.kr", "www.sogang.ac.kr"})
SOGANG_TLS_INTERMEDIATE_PATH = (
    Path(__file__).resolve().parent
    / "certificates"
    / "sectigo-public-server-authentication-ca-ov-r36.pem"
)
SOGANG_TLS_INTERMEDIATE_DER_SHA256 = (
    "6542d176bed50f193c0ce297ae44ecd8a0a86bec2ede682769344059b4e78530"
)


class SourceTLSConfigurationError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def build_sogang_source_ssl_context() -> ssl.SSLContext:
    try:
        certificate_pem = SOGANG_TLS_INTERMEDIATE_PATH.read_text(
            encoding="ascii"
        )
        if (
            certificate_pem.count("-----BEGIN CERTIFICATE-----") != 1
            or certificate_pem.count("-----END CERTIFICATE-----") != 1
        ):
            raise ValueError("unexpected certificate count")
        certificate_der = ssl.PEM_cert_to_DER_cert(certificate_pem)
    except (OSError, UnicodeError, ValueError) as exc:
        raise SourceTLSConfigurationError(
            "source TLS intermediate certificate is unavailable"
        ) from exc

    if (
        hashlib.sha256(certificate_der).hexdigest()
        != SOGANG_TLS_INTERMEDIATE_DER_SHA256
    ):
        raise SourceTLSConfigurationError(
            "source TLS intermediate certificate digest mismatch"
        )

    context = ssl.create_default_context()
    partial_chain = getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)
    if partial_chain:
        context.verify_flags &= ~partial_chain
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    try:
        context.load_verify_locations(cadata=certificate_pem)
    except ssl.SSLError as exc:
        raise SourceTLSConfigurationError(
            "source TLS intermediate certificate is invalid"
        ) from exc
    return context


def source_ssl_context_for_url(url: str) -> Optional[ssl.SSLContext]:
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return None
    hostname = (parsed.hostname or "").strip().lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or hostname not in SOGANG_SOURCE_TLS_HOSTS
    ):
        return None
    return build_sogang_source_ssl_context()
