import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from app import netutil


def req(peer="10.0.0.1", **headers):
    return SimpleNamespace(client=SimpleNamespace(host=peer), headers=headers)


class NetUtilTests(unittest.TestCase):
    def setUp(self):
        netutil._FAILED.clear()

    def test_direct_connection_uses_socket_peer_and_ignores_forwarded_header(self):
        with patch.dict(os.environ, {"APPHORYN_TRUSTED_PROXY_HOPS": "0"}):
            self.assertEqual(netutil.client_ip(req(**{"x-forwarded-for": "1.2.3.4"})), "10.0.0.1")

    def test_behind_one_proxy_uses_address_added_by_proxy_not_spoofed_prefix(self):
        with patch.dict(os.environ, {"APPHORYN_TRUSTED_PROXY_HOPS": "1"}):
            r = req(**{"x-forwarded-for": "6.6.6.6, 203.0.113.9"})
            self.assertEqual(netutil.client_ip(r), "203.0.113.9")

    def test_unlimited_attempts_by_default_never_locks_out(self):
        r = req()
        with patch.dict(os.environ, {"APPHORYN_ADMIN_MAX_FAILED": "0"}), patch.object(netutil.time, "sleep"):
            for _ in range(25):
                with self.assertRaises(HTTPException) as e:
                    netutil.check_admin_token("wrong", "secret", r)
                self.assertEqual(e.exception.status_code, 401)
            netutil.check_admin_token("secret", "secret", r)

    def test_optional_lockout_is_per_real_client_not_shared_by_proxy(self):
        with patch.dict(os.environ, {"APPHORYN_TRUSTED_PROXY_HOPS": "1", "APPHORYN_ADMIN_MAX_FAILED": "6"}), \
                patch.object(netutil.time, "sleep"):
            attacker = req(**{"x-forwarded-for": "198.51.100.7"})
            owner = req(**{"x-forwarded-for": "203.0.113.9"})
            for _ in range(6):
                with self.assertRaises(HTTPException):
                    netutil.check_admin_token("wrong", "secret", attacker)
            with self.assertRaises(HTTPException) as blocked:
                netutil.check_admin_token("secret", "secret", attacker)
            self.assertEqual(blocked.exception.status_code, 429)
            netutil.check_admin_token("secret", "secret", owner)  # owner unaffected

    def test_success_clears_failures(self):
        r = req()
        with patch.object(netutil.time, "sleep"), self.assertRaises(HTTPException):
            netutil.check_admin_token("bad", "secret", r)
        netutil.check_admin_token("secret", "secret", r)
        self.assertNotIn("10.0.0.1", netutil._FAILED)
