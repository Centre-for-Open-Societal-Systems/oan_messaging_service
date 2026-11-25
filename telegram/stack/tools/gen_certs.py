"""Generate the throwaway test CA and server certificates for the local Telegram stack.

Runs once, in the OAN Messaging image (it has `cryptography` and `certifi`), as root,
writing into the `certs` volume:

* ca.crt                    the test CA (trusted by OAN Messaging, Bot Services and the fakes)
* fake-telegram.{crt,key}   api.telegram.org (network alias of the fake server)
* fake-8028.{crt,key}       sms8028.example.invalid (the fake SMS gateway, mutual TLS)
* oan-8028-client.{crt,key} OAN Messaging's client certificate for the fake 8028; its key
                            is 0600, as the 8028 adapter requires
* tg-gateway.{crt,key}      the webhook gateway in front of OAN Bot Services
* oan-api.{crt,key}         OAN Messaging API (HTTPS, as in the SMS trial)
* bot-services.{crt,key}    offered to Bot Services if it serves HTTPS
* stubs.{crt,key}           offered to the stubs if they serve HTTPS
* trust-bundle.pem          certifi's public roots plus the test CA, for the OAN image,
                            whose HTTP client trusts certifi only (httpx, trust_env=False)

Keys are 0640, owned by uid/gid 10001 (every stack image runs as 10001). Valid 30 days.
Never use these anywhere but the local stack.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import os
import sys
from pathlib import Path

import certifi
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "/certs")
UID = 10001
SERVER = ExtendedKeyUsageOID.SERVER_AUTH
CLIENT = ExtendedKeyUsageOID.CLIENT_AUTH


def write(
    name: str, cert: x509.Certificate, key: ec.EllipticCurvePrivateKey | None, key_mode: int = 0o640
) -> None:
    crt = OUT / f"{name}.crt"
    crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    os.chmod(crt, 0o644)
    os.chown(crt, UID, UID)
    if key is not None:
        p = OUT / f"{name}.key"
        p.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        os.chmod(p, key_mode)
        os.chown(p, UID, UID)


def issue(
    cn: str,
    issuer: x509.Certificate | None,
    issuer_key,
    *,
    ca: bool = False,
    dns: list[str] | None = None,
    ips: list[str] | None = None,
    client: bool = False,
):
    k = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.UTC)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    b = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(issuer.subject if issuer else name)
        .public_key(k.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        # Python >= 3.13 verifies strictly: key identifiers and, for the CA, key usage.
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(k.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key((issuer_key or k).public_key()),
            critical=False,
        )
    )
    if ca:
        b = b.add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
    else:
        b = b.add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=True,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        b = b.add_extension(x509.ExtendedKeyUsage([CLIENT if client else SERVER]), critical=False)
    if dns or ips:
        names = [x509.DNSName(d) for d in dns or []]
        names += [x509.IPAddress(ipaddress.ip_address(i)) for i in ips or []]
        b = b.add_extension(x509.SubjectAlternativeName(names), critical=False)
    return b.sign(issuer_key or k, hashes.SHA256()), k


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    if (OUT / "ca.crt").exists():
        print("certificates already present")
        return
    ca, ca_key = issue("oan-tg-test-ca", None, None, ca=True)
    servers = {
        "fake-telegram": (["api.telegram.org", "fake-telegram", "localhost"], ["127.0.0.1"]),
        "fake-8028": (["sms8028.example.invalid", "fake-8028", "localhost"], ["127.0.0.1"]),
        "tg-gateway": (["tg-gateway", "webhook.example.invalid", "localhost"], ["127.0.0.1"]),
        "oan-api": (["oan-api", "localhost"], ["127.0.0.1"]),
        "bot-services": (["bot-services", "localhost"], ["127.0.0.1"]),
        "stubs": (["stubs", "localhost"], ["127.0.0.1"]),
    }
    for name, (dns, ips) in servers.items():
        write(name, *issue(name, ca, ca_key, dns=dns, ips=ips))
    write("oan-8028-client", *issue("oan-messaging", ca, ca_key, client=True), key_mode=0o600)
    # The CA last: its presence marks a complete set.
    ca_pem = ca.public_bytes(serialization.Encoding.PEM)
    bundle = OUT / "trust-bundle.pem"
    bundle.write_bytes(Path(certifi.where()).read_bytes() + b"\n" + ca_pem)
    os.chmod(bundle, 0o644)
    write("ca", ca, None)
    print("certificates written")


if __name__ == "__main__":
    main()
