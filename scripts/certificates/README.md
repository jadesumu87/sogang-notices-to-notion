# Source TLS intermediates

This directory contains public intermediate CA certificates used to complete
an upstream TLS chain when the source server omits an intermediate certificate.
They supplement the operating system trust store; hostname validation and the
trusted-root requirement remain enabled.

## Sectigo Public Server Authentication CA OV R36

- File: `sectigo-public-server-authentication-ca-ov-r36.pem`
- Subject: `C=GB, O=Sectigo Limited, CN=Sectigo Public Server Authentication CA OV R36`
- Issuer: `C=GB, O=Sectigo Limited, CN=Sectigo Public Server Authentication Root R46`
- Validity: 2021-03-22 through 2036-03-21
- DER SHA-256: `6542d176bed50f193c0ce297ae44ecd8a0a86bec2ede682769344059b4e78530`
- AIA provenance (not fetched at runtime): `http://crt.sectigo.com/SectigoPublicServerAuthenticationCAOVR36.crt`

The crawler verifies the DER digest before loading this certificate. A missing,
malformed, or mismatched file stops source collection without weakening TLS.
