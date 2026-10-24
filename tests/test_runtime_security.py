"""CPU-only TLS regression fixtures; never imports Torch or calls RunPod."""
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch
import certifi
import runtime_security as security

FIXTURES = Path(__file__).parent / 'fixtures' / 'security'


class TLSCertificateTests(unittest.TestCase):
    def test_exact_root_distrust_is_idempotent_and_preserves_unaffected_g1(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'affected.crt').write_text((FIXTURES / 'emsign_c1.pem').read_text())
            (root / 'safe.crt').write_text((FIXTURES / 'emsign_g1.pem').read_text())
            config = root / 'ca.conf'
            config.write_text('# retained\naffected.crt\nsafe.crt\n')
            self.assertEqual(security.harden_system_config(config, root), 1)
            self.assertEqual(config.read_text(), '# retained\n!affected.crt\nsafe.crt\n')
            self.assertEqual(security.harden_system_config(config, root), 0)

    def test_patched_bundle_retains_usable_roots_and_excludes_affected_anchors(self):
        status = security.bundle_status(certifi.where())
        self.assertEqual(status['removed_root_count'], 0)
        self.assertGreater(status['certificate_count'], 100)
        self.assertIn('40f6af0346a99aa1cd1d555a4e9cce62c7f9634603ee406615833dc8c8d00367',
                      security.certificate_fingerprints(Path(certifi.where()).read_text()))

    def test_explicit_lazy_capath_is_audited(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / '01234567.0').write_text((FIXTURES / 'emsign_c1.pem').read_text())
            with patch.dict(os.environ, {'SSL_CERT_FILE': certifi.where(), 'SSL_CERT_DIR': directory}):
                self.assertTrue(security.system_fingerprints() & security.REMOVED_CA_SHA256)


if __name__ == '__main__':
    unittest.main()
