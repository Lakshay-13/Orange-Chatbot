from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from errand_agent.clock import FakeClock, IST
from errand_agent.mock_provider import MockProvider, MockToolError


class MockProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "provider.sqlite3"
        self.clock = FakeClock(datetime(2026, 11, 7, 10, 0, tzinfo=IST))
        self.provider = MockProvider(self.path, self.clock, seed=29)
        self.range = {"start": "2026-11-08", "end": "2026-11-10"}

    def tearDown(self) -> None:
        self.provider.close()
        self.temp.cleanup()

    def _hold(self, outcome: str = "success") -> tuple[dict, str]:
        providers = self.provider.search_providers("plumber", "Jaipur", self.range)
        self.assertGreaterEqual(len(providers), 3)
        selected = providers[0]
        hold = self.provider.hold_slot(selected["provider_id"], selected["available_slots"][0], "u_test")
        self.provider.force_next("book", outcome)
        return hold, selected["provider_id"]

    def test_search_returns_required_provider_attributes_and_review_injection(self) -> None:
        results = self.provider.search_providers("plumber", "Jaipur", self.range)
        self.assertGreaterEqual(len(results), 3)
        self.assertLessEqual(len(results), 8)
        required = {"provider_id", "price_inr", "rating", "available_slots", "attributes"}
        for item in results:
            self.assertTrue(required <= item.keys())
            self.assertTrue(item["available_slots"])
        details = self.provider.get_provider("prov_001")
        self.assertTrue(any("ignore" in review.casefold() or "system:" in review.casefold() for review in details["reviews"]))

    def test_quote_drift_and_ten_minute_hold_expiry(self) -> None:
        results = self.provider.search_providers("plumber", "Jaipur", self.range)
        selected = results[0]
        self.provider.configure(price_drift="fixed", drift_value=250)
        hold = self.provider.hold_slot(selected["provider_id"], selected["available_slots"][0])
        self.assertEqual(hold["quoted_price_inr"], selected["price_inr"] + 250)
        self.assertEqual(hold["expires_at"] - self.clock.epoch(), 600)
        self.clock.advance(600)
        result = self.provider.book(hold["hold_id"], "book_expired")
        self.assertEqual(result["status"], "hold_expired")

    def test_timeout_after_possible_success_survives_process_restart(self) -> None:
        hold, _ = self._hold("timeout_booked")
        with self.assertRaises(MockToolError) as raised:
            self.provider.book(hold["hold_id"], "book_timeout_after_success")
        self.assertTrue(raised.exception.ambiguous)
        self.provider.close()
        self.provider = MockProvider(self.path, self.clock, seed=29)
        status = self.provider.get_booking_status(idempotency_key="book_timeout_after_success")
        self.assertEqual(status["status"], "booked")
        self.assertEqual(len(self.provider.list_bookings("u_test")), 1)
        replay = self.provider.book(hold["hold_id"], "book_timeout_after_success")
        self.assertEqual(replay["status"], "booked")
        self.assertEqual(len(self.provider.list_bookings("u_test")), 1)

    def test_timeout_not_booked_then_same_key_retry_creates_one_booking(self) -> None:
        hold, _ = self._hold("timeout_not_booked")
        with self.assertRaises(MockToolError):
            self.provider.book(hold["hold_id"], "book_timeout_not_booked")
        status = self.provider.get_booking_status(idempotency_key="book_timeout_not_booked")
        self.assertEqual(status["status"], "not_booked")
        result = self.provider.book(hold["hold_id"], "book_timeout_not_booked")
        self.assertEqual(result["status"], "booked")
        self.assertEqual(len(self.provider.list_bookings("u_test")), 1)

    def test_slot_taken_is_explicit_and_does_not_create_booking(self) -> None:
        hold, _ = self._hold("slot_taken")
        result = self.provider.book(hold["hold_id"], "book_slot_taken")
        self.assertEqual(result["status"], "slot_taken")
        self.assertEqual(self.provider.get_booking_status(idempotency_key="book_slot_taken")["status"], "not_booked")
        self.assertEqual(self.provider.list_bookings("u_test"), [])

    def test_cancel_503_then_retry_same_key_survives_restart(self) -> None:
        hold, _ = self._hold("success")
        result = self.provider.book(hold["hold_id"], "book_for_cancel")
        booking_id = result["booking_id"]
        with self.assertRaises(MockToolError) as raised:
            self.provider.cancel(booking_id, "cancel_once")
        self.assertEqual(raised.exception.code, "service_unavailable")
        self.provider.close()
        self.provider = MockProvider(self.path, self.clock, seed=29)
        result = self.provider.cancel(booking_id, "cancel_once")
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.provider.list_bookings("u_test")[0]["status"], "cancelled_by_user")

    def test_reschedule_conflict_and_provider_cancellation_event(self) -> None:
        hold, _ = self._hold("success")
        booking = self.provider.book(hold["hold_id"], "book_for_reschedule")
        new_slot = "2026-11-09T10:00+05:30"
        self.provider.configure(conflict_slots=[new_slot])
        conflict = self.provider.reschedule(booking["booking_id"], new_slot, "reschedule_conflict")
        self.assertEqual(conflict["status"], "slot_conflict")
        self.provider.simulate_provider_cancellation(booking["booking_id"])
        self.assertEqual(self.provider.list_bookings("u_test")[0]["status"], "cancelled_by_provider")

    def test_status_unavailable_is_a_retryable_failure(self) -> None:
        self.provider.configure(status_unavailable=True)
        with self.assertRaises(MockToolError) as raised:
            self.provider.get_booking_status(hold_id="unknown")
        self.assertTrue(raised.exception.retryable)


if __name__ == "__main__":
    unittest.main()
