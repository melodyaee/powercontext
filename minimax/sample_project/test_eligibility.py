import unittest

from eligibility import can_refund


class CanRefundTests(unittest.TestCase):
    def test_paid_order(self):
        self.assertTrue(can_refund({"status": "paid"}))

    def test_expired_order_must_not_refund(self):
        self.assertFalse(can_refund({"status": "paid", "expired": True}))

    def test_unknown_status_rejected(self):
        self.assertFalse(can_refund({"status": "unknown"}))


if __name__ == "__main__":
    unittest.main()
