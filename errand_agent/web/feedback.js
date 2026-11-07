(function attachErrandAgentFeedback(root, factory) {
  "use strict";
  const feedback = factory();
  if (typeof module === "object" && module.exports) module.exports = feedback;
  else root.ErrandAgentFeedback = feedback;
})(globalThis, function createFeedback() {
  "use strict";

  function approvalOutcomeLabel(status) {
    const labels = {
      confirmed: "Verified · booked",
      not_booked: "Verified · not booked",
      cancelled: "Verified · cancelled",
      rescheduled: "Verified · rescheduled",
      unverified: "Outcome unverified",
      verification_mismatch: "Verification mismatch",
    };
    return labels[status] || "Result received";
  }

  return Object.freeze({ approvalOutcomeLabel });
});
