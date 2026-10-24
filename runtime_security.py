"""Read-only runtime attestation and build-time CA distrust configuration.

The three root fingerprints match the roots removed by certifi's October 2026
eMudhra remediation. The unaffected emSign G1 root is deliberately retained.
No credentials, environment values or certificate contents are reported.
"""
import argparse
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import re
import ssl
import sys

REMOVED_CA_SHA256 = frozenset({
    '86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b',
    '125609aa301da0a249b97a8239cb6a34216f44dcac9f3954b14292f2e8c8608f',
    'bc4d809b15189d78db3e1d8cf4f9726a795da1643ca5f1358e1ddb0edc0d7eb3',
})
_PEM = re.compile(r'-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----', re.S)


def certificate_fingerprints(data: str) -> frozenset[str]:
    return frozenset(hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
                     for pem in _PEM.findall(data))


def bundle_status(path: str | Path) -> dict:
    data = Path(path).read_bytes()
    fingerprints = certificate_fingerprints(data.decode('ascii'))
    return {'certificate_count': len(fingerprints),
            'removed_root_count': len(fingerprints & REMOVED_CA_SHA256),
            'bundle_sha256': hashlib.sha256(data).hexdigest()}


def system_fingerprints() -> set[str]:
    roots = ssl.create_default_context().get_ca_certs(binary_form=True)
    fingerprints = {hashlib.sha256(cert).hexdigest() for cert in roots}
    # OpenSSL loads capath certificates lazily. get_ca_certs() alone can miss
    # a distrusted anchor that would be loaded during a later handshake.
    capath = os.getenv('SSL_CERT_DIR', ssl.get_default_verify_paths().openssl_capath)
    for directory in capath.split(os.pathsep):
        path = Path(directory)
        if not directory or not path.is_dir():
            continue
        for certificate in path.iterdir():
            if re.fullmatch(r'[a-fA-F0-9]{8}\.\d+', certificate.name) and certificate.is_file():
                fingerprints.update(certificate_fingerprints(certificate.read_text()))
    return fingerprints


def harden_system_config(config: Path, certificate_directory: Path) -> int:
    """Disable only exact known certificates in Debian/Ubuntu's existing list."""
    lines = config.read_text().splitlines(keepends=True)
    changed = 0
    for i, line in enumerate(lines):
        name = line.strip()
        if not name or name.startswith(('#', '!')):
            continue
        path = certificate_directory / name
        if not path.is_file():
            continue
        if certificate_fingerprints(path.read_text()) & REMOVED_CA_SHA256:
            lines[i] = '!' + line.lstrip()
            changed += 1
    if changed:
        config.write_text(''.join(lines))
    return changed


def runtime_status() -> dict:
    import certifi
    from botocore.httpsession import get_cert_path

    stores = {}
    try:
        fingerprints = system_fingerprints()
        stores['system'] = {'certificate_count': len(fingerprints),
                            'removed_root_count': len(fingerprints & REMOVED_CA_SHA256)}
    except (OSError, ValueError, ssl.SSLError):
        stores['system'] = {'available': False}
    for name, path in (
        ('certifi', certifi.where()),
        # Studio supplies storage credentials directly; diagnostics must not
        # resolve unrelated AWS profiles, credentials or instance metadata.
        ('botocore', get_cert_path(os.getenv('AWS_CA_BUNDLE') or True)),
    ):
        try:
            stores[name] = bundle_status(path)
        except (OSError, ValueError, UnicodeError):
            stores[name] = {'available': False}
    return {
        'python': sys.version.split()[0], 'openssl': ssl.OPENSSL_VERSION,
        'certifi': metadata.version('certifi'), 'botocore': metadata.version('botocore'),
        'stores': stores,
        'emudhra_root_remediation': all(s.get('certificate_count', 0) > 0 and
                                        s.get('removed_root_count') == 0
                                        for s in stores.values()),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--harden-system', action='store_true')
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    if args.harden_system:
        count = harden_system_config(Path('/etc/ca-certificates.conf'),
                                     Path('/usr/share/ca-certificates'))
        print(json.dumps({'event': 'ca_distrust_configuration', 'entries_disabled': count}))
    if args.verify:
        status = runtime_status()
        print(json.dumps({'event': 'runtime_security', **status}))
        if not status['emudhra_root_remediation']:
            raise SystemExit('Affected or unavailable certificate store; refusing build')


if __name__ == '__main__':
    main()
