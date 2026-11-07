from __future__ import annotations

import unittest
from datetime import datetime

from errand_agent.clock import FakeClock, IST
from errand_agent.intent import IntentState, merge_intent, parse_user_message, sanitize_model_patch


class IntentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock(datetime(2026, 11, 7, 10, 0, tzinfo=IST))

    def test_parse_example_request_with_budget_time_and_date(self) -> None:
        patch = parse_user_message("Find a plumber in Jaipur this week, mornings only, under ₹1,500.", self.clock, "m1")
        state = merge_intent(IntentState(), patch, self.clock)
        self.assertEqual(state.action, "book")
        self.assertEqual(state.service, "plumber")
        self.assertEqual(state.area, "Jaipur")
        self.assertEqual(state.date_start, "2026-11-07")
        self.assertEqual(state.date_end, "2026-11-08")
        self.assertEqual(state.earliest_time, "06:00")
        self.assertEqual(state.latest_time, "12:00")
        self.assertEqual(state.budget_max_inr, 1500)
        self.assertEqual(state.missing_required(), [])

    def test_correction_changes_only_named_detail_and_invalidates_presented_choices(self) -> None:
        original = IntentState(
            action="book", service="electrician", area="Jaipur", date_start="2026-11-09",
            date_end="2026-11-09", earliest_time="10:00", budget_max_inr=2000,
            preferences={"min_rating": 4.5}, presented_options=[{"provider_id": "prov_001"}],
            provider_ids=["prov_001"], selection={"provider_id": "prov_001", "slot": "old"},
        )
        updated = merge_intent(original, parse_user_message("Actually use Koramangala instead.", self.clock, "m2"), self.clock)
        self.assertEqual(updated.area, "Koramangala")
        self.assertEqual(updated.service, "electrician")
        self.assertEqual(updated.date_start, "2026-11-09")
        self.assertEqual(updated.earliest_time, "10:00")
        self.assertEqual(updated.budget_max_inr, 2000)
        self.assertEqual(updated.preferences, {"min_rating": 4.5})
        self.assertEqual(updated.presented_options, [])
        self.assertEqual(updated.selection, {})

    def test_missing_required_fields_are_reported_in_order(self) -> None:
        state = merge_intent(IntentState(), parse_user_message("I need an electrician", self.clock, "m3"), self.clock)
        self.assertEqual(state.missing_required(), ["area", "date_range"])

    def test_typed_request_cannot_be_invented_by_model(self) -> None:
        state = IntentState()
        patch = sanitize_model_patch(
            {"action": "book", "service": "plumber", "area": "Jaipur", "preferences": {"min_rating": 5.0}},
            state,
            "I need help with a tap.",
        )
        self.assertEqual(patch.get("service"), "plumber")
        self.assertNotIn("area", patch)
        self.assertNotIn("preferences", patch)

    def test_reschedule_date_and_target_are_extracted(self) -> None:
        state = merge_intent(
            IntentState(),
            parse_user_message("Move my AC servicing booking book_000021 to Saturday.", self.clock, "m4"),
            self.clock,
        )
        self.assertEqual(state.action, "reschedule")
        self.assertEqual(state.service, "ac_service")
        self.assertEqual(state.target_booking_id, "book_000021")
        self.assertEqual(state.date_start, "2026-11-14")
        self.assertEqual(state.missing_required(), [])

    def test_provider_cancellation_with_replacement_request_is_not_user_cancel(self) -> None:
        patch = parse_user_message("My plumber cancelled, find another provider this week.", self.clock, "m5")
        self.assertEqual(patch["action"], "book")
        self.assertEqual(patch["service"], "plumber")

    def test_time_or_floor_counts_are_not_misread_as_price(self) -> None:
        for text in ("I need a plumber under 2 hours", "my apartment is on floor 3"):
            self.assertNotIn("budget_max_inr", parse_user_message(text, self.clock, "m6"))

    def test_model_patch_accepts_only_deterministically_grounded_date_time_and_budget(self) -> None:
        grounded = parse_user_message("Find a plumber in Jaipur this week, mornings, under ₹1,500.", self.clock, "m7")
        state = merge_intent(IntentState(), grounded, self.clock)
        model_patch = {key: grounded[key] for key in ("date_start", "date_end", "earliest_time", "latest_time", "budget_max_inr", "no_budget")}
        accepted = sanitize_model_patch(model_patch, state, "Find a plumber in Jaipur this week, mornings, under ₹1,500.", grounded)
        self.assertEqual(accepted, model_patch)
        ungrounded = sanitize_model_patch({"date_start": "2026-11-20", "budget_max_inr": 1}, state, "Find a plumber this week.", grounded)
        self.assertNotIn("date_start", ungrounded)
        self.assertNotIn("budget_max_inr", ungrounded)


if __name__ == "__main__":
    unittest.main()
