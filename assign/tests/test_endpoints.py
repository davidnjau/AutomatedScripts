#!/usr/bin/env python3
"""
Unit tests for endpoints.py — the single source of truth for every
Ardhisasa API endpoint path used across this bot.

These are intentionally light: endpoints.py has no logic, just string
constants, so the only thing worth asserting is that each constant is
built from the expected base URL and path — i.e. that a future edit to
one doesn't silently change the other, and that every constant is a
non-empty string.

Run with: python3 -m unittest discover -s assign/tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import endpoints
from ardhisasa_auth import AUTH_BASE_URL
from common import BASE_URL


class TestEndpointConstants(unittest.TestCase):
    def test_auth_endpoints_built_from_auth_base_url(self):
        self.assertEqual(endpoints.AUTH_LOGIN_URL, f"{AUTH_BASE_URL}/login")
        self.assertEqual(endpoints.AUTH_OTP_VERIFY_URL, f"{AUTH_BASE_URL}/otpverify")
        self.assertEqual(endpoints.AUTH_REFRESH_TOKEN_URL, f"{AUTH_BASE_URL}/refresh-token")

    def test_service_endpoints_built_from_base_url(self):
        self.assertEqual(
            endpoints.ACCOUNTS_LIST_URL, f"{BASE_URL}/acl/api/v1/accounts/list-user-accounts"
        )
        self.assertEqual(
            endpoints.STAMP_DUTY_APPLICATION_LIST_URL,
            f"{BASE_URL}/valuationservice/api/v1/stamp-duty/application",
        )
        self.assertEqual(
            endpoints.STAMP_DUTY_APPLICATION_DETAIL_URL,
            f"{BASE_URL}/valuationservice/api/v1/stamp-duty/application/detail-view",
        )
        self.assertEqual(
            endpoints.STAMP_DUTY_FIX_APPLICATION_URL,
            f"{BASE_URL}/valuationservice/api/v1/stamp-duty/fix_application_details",
        )
        self.assertEqual(
            endpoints.ASSESSOR_STAGE_LIST_URL,
            f"{BASE_URL}/stampdutyservice/api/v1/stamp-duty/hod-or-clr",
        )
        self.assertEqual(
            endpoints.ASSESSOR_STAGE_DETAIL_URL,
            f"{BASE_URL}/stampdutyservice/api/v1/stamp-duty/detail-view",
        )
        self.assertEqual(
            endpoints.COUNTY_TRANSFER_DETAIL_URL,
            f"{BASE_URL}/registrationservice/api/v1/transfer/transfer-request-staff-detailed-view",
        )

    def test_every_constant_is_a_non_empty_string(self):
        for name in dir(endpoints):
            if name.isupper():
                value = getattr(endpoints, name)
                self.assertIsInstance(value, str, msg=name)
                self.assertTrue(value.startswith("https://"), msg=name)


if __name__ == "__main__":
    unittest.main()
