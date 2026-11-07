const assert = require("node:assert/strict");
const test = require("node:test");

const { approvalOutcomeLabel } = require("../errand_agent/web/feedback.js");

test("completed approval labels report the verified outcome", () => {
  assert.equal(approvalOutcomeLabel("confirmed"), "Verified · booked");
  assert.equal(approvalOutcomeLabel("not_booked"), "Verified · not booked");
  assert.equal(approvalOutcomeLabel("cancelled"), "Verified · cancelled");
  assert.equal(approvalOutcomeLabel("rescheduled"), "Verified · rescheduled");
  assert.equal(approvalOutcomeLabel("unverified"), "Outcome unverified");
  assert.equal(approvalOutcomeLabel("verification_mismatch"), "Verification mismatch");
  assert.equal(approvalOutcomeLabel(undefined), "Result received");
});
