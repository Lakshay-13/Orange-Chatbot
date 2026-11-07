(() => {
  "use strict";

  let csrfToken = "";
  let sessionId = "";
  let demoControlsEnabled = false;
  let toastTimer = 0;
  let historySignature = "";
  let historyRefreshInFlight = false;

  const byId = (id) => document.getElementById(id);
  const chat = byId("chat");

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = String(text);
    return element;
  }

  function randomId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") return window.crypto.randomUUID();
    return `client-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (options.method && options.method !== "GET") {
      headers.set("Content-Type", "application/json");
      headers.set("X-CSRF-Token", csrfToken);
    }
    const response = await fetch(path, { ...options, headers, credentials: "same-origin" });
    let body;
    try { body = await response.json(); } catch (_) { body = {}; }
    if (!response.ok) throw new Error(body.message || `Request failed (${response.status})`);
    return body;
  }

  function showToast(message) {
    const toast = byId("toast");
    toast.textContent = message;
    toast.classList.add("show");
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => toast.classList.remove("show"), 3200);
  }

  function addMessage(role, content, className = "") {
    const row = node("div", `message-row ${role}${className ? ` ${className}` : ""}`);
    row.appendChild(node("div", "message-bubble", content));
    chat.appendChild(row);
    chat.scrollTop = chat.scrollHeight;
    return row;
  }

  function addAssistantContainer() {
    const row = node("div", "message-row assistant");
    const bubble = node("div", "message-bubble");
    row.appendChild(bubble);
    chat.appendChild(row);
    chat.scrollTop = chat.scrollHeight;
    return bubble;
  }

  function renderProviderOptions(providers) {
    const bubble = addAssistantContainer();
    bubble.appendChild(node("p", "", "Choose a provider and a slot. Selecting a slot creates a temporary hold and shows the exact quote for your approval."));
    const list = node("div", "provider-list");
    for (const provider of providers) {
      const card = node("article", "provider-card");
      const title = node("div", "provider-title");
      const heading = node("div");
      heading.appendChild(node("h3", "", provider.name || provider.provider_id));
      heading.appendChild(node("div", "provider-meta", `${provider.service.replaceAll("_", " ")} · ${provider.area}${provider.area_match === "nearby" ? " · nearby" : ""} · ★ ${provider.rating} (${provider.review_count} reviews)`));
      title.appendChild(heading);
      title.appendChild(node("span", "price-tag", `₹${provider.price_inr}`));
      card.appendChild(title);
      const attributes = provider.attributes || {};
      const attributeText = [
        attributes.verified ? "Verified" : "Not verified",
        attributes.years_experience ? `${attributes.years_experience} years’ experience` : "",
        Array.isArray(attributes.languages) ? attributes.languages.join(", ") : "",
      ].filter(Boolean).join(" · ");
      if (attributeText) card.appendChild(node("div", "provider-meta", attributeText));
      const slots = node("div", "slot-list");
      for (const slot of provider.available_slots || []) {
        const button = node("button", "slot-button", `${formatSlot(slot)} · review quote`);
        button.type = "button";
        button.addEventListener("click", () => selectProvider(provider, slot, button));
        slots.appendChild(button);
      }
      card.appendChild(slots);
      const detailsButton = node("button", "tiny-button", "View provider details and reviews");
      detailsButton.type = "button";
      detailsButton.addEventListener("click", () => showProviderDetails(provider.provider_id, card, detailsButton));
      card.appendChild(detailsButton);
      list.appendChild(card);
    }
    bubble.appendChild(list);
  }

  async function showProviderDetails(providerId, card, button) {
    button.disabled = true;
    button.textContent = "Loading details…";
    try {
      const result = await api(`/api/providers/${encodeURIComponent(providerId)}`);
      const details = result.provider;
      const reviews = node("div", "review-list");
      for (const review of details.reviews || []) {
        const item = node("div", "review-item");
        item.appendChild(node("span", "review-label", "UNTRUSTED PROVIDER REVIEW"));
        item.appendChild(node("div", "", review));
        reviews.appendChild(item);
      }
      card.appendChild(reviews);
      button.remove();
      await refreshTrace();
    } catch (error) {
      button.disabled = false;
      button.textContent = "View provider details and reviews";
      showToast(error.message);
    }
  }

  async function selectProvider(provider, slot, button) {
    button.disabled = true;
    button.textContent = "Holding slot…";
    try {
      const response = await api("/api/select", {
        method: "POST",
        body: JSON.stringify({ provider_id: provider.provider_id, slot }),
      });
      renderApproval(response.approval, response.message);
      await refreshTrace();
      await refreshBudget();
    } catch (error) {
      button.disabled = false;
      button.textContent = `${formatSlot(slot)} · review quote`;
      showToast(error.message);
      await refreshTrace();
    }
  }

  function renderApproval(approval, message) {
    const bubble = addAssistantContainer();
    bubble.appendChild(node("p", "", message || "Review these exact details before approving."));
    const card = node("article", "approval-card");
    const isBook = approval.card && approval.card.kind === "book";
    const details = approval.card || approval;
    const heading = isBook ? `Book ${details.service} with ${details.provider_name}` : details.kind === "cancel" ? `Cancel booking ${details.booking_id}` : `Reschedule booking ${details.booking_id}`;
    card.appendChild(node("h3", "", heading));
    if (details.slot) card.appendChild(node("p", "", `Slot: ${formatSlot(details.slot)}`));
    if (details.quoted_price_inr !== null && details.quoted_price_inr !== undefined) {
      card.appendChild(node("p", "", `Quoted price: ₹${details.quoted_price_inr} · Search price: ₹${details.search_price_inr} · Difference: ₹${details.price_delta_inr || 0}`));
    }
    if (details.approval_code) card.appendChild(node("span", "approval-code", `CODE ${details.approval_code}`));
    if (details.expires_at) card.appendChild(node("p", "", `Approval expires ${new Date(details.expires_at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}.`));
    const actions = node("div", "approval-actions");
    const approve = node("button", "approve-button", "Approve exact action");
    approve.type = "button";
    approve.dataset.outcome = "awaiting-approval";
    approve.addEventListener("click", () => approveAction(approval, approve));
    const reject = node("button", "reject-button", "Reject");
    reject.type = "button";
    reject.addEventListener("click", () => rejectAction(approval.action_id, reject));
    actions.append(approve, reject);
    card.appendChild(actions);
    bubble.appendChild(card);
  }

  async function approveAction(approval, button) {
    button.disabled = true;
    button.dataset.outcome = "in-progress";
    button.textContent = "Verifying action…";
    try {
      const result = await api("/api/approve", {
        method: "POST",
        body: JSON.stringify({ action_id: approval.action_id, fingerprint: approval.fingerprint, request_id: randomId() }),
      });
      button.dataset.outcome = result.status || "completed";
      button.textContent = window.ErrandAgentFeedback.approvalOutcomeLabel(result.status);
      addMessage("assistant", result.message || `Action status: ${result.status || "complete"}.`);
      await refreshTrace();
      await refreshBudget();
      await refreshBookings(false);
    } catch (error) {
      button.disabled = false;
      button.textContent = "Approve exact action";
      showToast(error.message);
      await refreshTrace();
    }
  }

  async function rejectAction(actionId, button) {
    button.disabled = true;
    try {
      const result = await api("/api/reject", { method: "POST", body: JSON.stringify({ action_id: actionId }) });
      addMessage("assistant", result.message);
      await refreshTrace();
    } catch (error) {
      button.disabled = false;
      showToast(error.message);
    }
  }

  function renderBookingList(bookings) {
    const list = byId("bookingList");
    list.replaceChildren();
    if (!bookings.length) {
      list.appendChild(node("div", "empty-state", "No mock bookings yet."));
      return;
    }
    for (const booking of bookings) {
      const card = node("article", "booking-card");
      const info = node("div", "booking-info");
      info.appendChild(node("strong", "", `${booking.service.replaceAll("_", " ")} · ${booking.booking_id}`));
      info.appendChild(node("span", "", `${formatSlot(booking.slot)} · ${booking.status.replaceAll("_", " ")}`));
      card.appendChild(info);
      const actions = node("div", "booking-actions");
      if (booking.status === "confirmed" && new Date(booking.slot).getTime() > Date.now()) {
        const cancel = node("button", "quiet-button", "Request cancellation");
        cancel.type = "button";
        cancel.addEventListener("click", () => {
          byId("bookingsDialog").close();
          sendPrompt(`Cancel my booking ${booking.booking_id}`);
        });
        const move = node("button", "quiet-button", "Request reschedule");
        move.type = "button";
        move.addEventListener("click", () => {
          byId("bookingsDialog").close();
          sendPrompt(`Reschedule my booking ${booking.booking_id} to Saturday`);
        });
        actions.append(cancel, move);
      }
      card.appendChild(actions);
      list.appendChild(card);
    }
  }

  function renderBookings(bookings) {
    const bubble = addAssistantContainer();
    if (!bookings.length) {
      bubble.appendChild(node("p", "", "You have no mock bookings."));
      return;
    }
    bubble.appendChild(node("p", "", `I found ${bookings.length} mock booking(s). You can request a cancellation or reschedule below. Each needs a fresh approval.`));
    const list = node("div", "provider-list");
    for (const booking of bookings) {
      const card = node("article", "booking-card");
      const info = node("div", "booking-info");
      info.appendChild(node("strong", "", `${booking.service.replaceAll("_", " ")} · ${booking.booking_id}`));
      info.appendChild(node("span", "", `${formatSlot(booking.slot)} · ${booking.status.replaceAll("_", " ")}`));
      card.appendChild(info);
      list.appendChild(card);
    }
    bubble.appendChild(list);
  }

  function renderRescheduleSlots(slots, booking) {
    const bubble = addAssistantContainer();
    bubble.appendChild(node("p", "", "Choose a replacement slot. The booking will not change until you approve the exact reschedule."));
    const list = node("div", "slot-list");
    for (const slot of slots) {
      const button = node("button", "slot-button", formatSlot(slot));
      button.type = "button";
      button.addEventListener("click", async () => {
        button.disabled = true;
        try {
          const result = await api("/api/reschedule/select", { method: "POST", body: JSON.stringify({ booking_id: booking.booking_id, slot }) });
          renderApproval(result.approval, result.message);
          await refreshTrace();
        } catch (error) {
          button.disabled = false;
          showToast(error.message);
        }
      });
      list.appendChild(button);
    }
    bubble.appendChild(list);
  }

  function renderResponse(response) {
    addMessage("assistant", response.message || "Done.", response.exit_reason === "blocked_by_security" ? "system-note" : "");
    if (response.providers && response.providers.length) renderProviderOptions(response.providers);
    if (response.approval) renderApproval(response.approval, response.message);
    if (response.bookings) renderBookings(response.bookings);
    if (response.reschedule_slots) renderRescheduleSlots(response.reschedule_slots, response.booking);
    if (response.security_labels && response.security_labels.length) showToast("The security layer filtered part of that request.");
  }

  async function sendPrompt(message) {
    addMessage("user", message);
    byId("messageInput").value = "";
    byId("sendButton").disabled = true;
    try {
      const response = await api("/api/chat", {
        method: "POST",
        body: JSON.stringify({ message, client_message_id: randomId() }),
      });
      renderResponse(response);
      await refreshTrace();
      await refreshBudget(response.budget);
      await refreshOutbox();
    } catch (error) {
      addMessage("assistant", error.message, "system-note");
    } finally {
      byId("sendButton").disabled = false;
      byId("messageInput").focus();
    }
  }

  async function refreshBookings(openDialog = true) {
    try {
      const result = await api("/api/bookings");
      renderBookingList(result.bookings || []);
      if (openDialog) byId("bookingsDialog").showModal();
      return result.bookings || [];
    } catch (error) {
      showToast(error.message);
      return [];
    }
  }

  async function renderMemories() {
    const list = byId("memoryList");
    list.replaceChildren();
    try {
      const result = await api("/api/memories");
      const memories = result.memories || [];
      if (!memories.length) list.appendChild(node("div", "empty-state", "No saved preferences."));
      for (const memory of memories) {
        const card = node("article", "memory-card");
        const top = node("div", "memory-top");
        const labels = node("div");
        labels.appendChild(node("div", "memory-key", `${memory.key} · ${memory.scope}`));
        labels.appendChild(node("div", "memory-meta", `${memory.kind} · ${memory.status}${memory.confidence ? ` · confidence ${memory.confidence}` : ""}`));
        top.appendChild(labels);
        card.appendChild(top);
        card.appendChild(node("div", "memory-value", memory.value === null ? "(value forgotten)" : JSON.stringify(memory.value)));
        const controls = node("div", "memory-controls");
        if (memory.status === "active" || memory.status === "pending") {
          const input = node("input");
          input.type = "text";
          input.value = memory.value === null ? "" : JSON.stringify(memory.value);
          input.setAttribute("aria-label", `Correct ${memory.key}`);
          const save = node("button", "", "Correct");
          save.type = "button";
          save.addEventListener("click", () => correctMemory(memory.id, input.value));
          controls.append(input, save);
          const forget = node("button", "forget-button", "Forget");
          forget.type = "button";
          forget.addEventListener("click", () => forgetMemory(memory.id));
          controls.appendChild(forget);
        }
        if (memory.status === "pending") {
          const confirm = node("button", "", "Confirm inference");
          confirm.type = "button";
          confirm.addEventListener("click", () => memoryDecision(memory.id, "confirm"));
          const reject = node("button", "", "Reject inference");
          reject.type = "button";
          reject.addEventListener("click", () => memoryDecision(memory.id, "reject"));
          controls.append(confirm, reject);
        }
        if (controls.childNodes.length) card.appendChild(controls);
        list.appendChild(card);
      }
      if (!byId("memoryDialog").open) byId("memoryDialog").showModal();
    } catch (error) {
      showToast(error.message);
    }
  }

  function parseMemoryValue(value) {
    try { return JSON.parse(value); } catch (_) { return value; }
  }

  async function correctMemory(memoryId, value) {
    try {
      await api("/api/memory/correct", { method: "POST", body: JSON.stringify({ memory_id: memoryId, value: parseMemoryValue(value) }) });
      await renderMemories();
      await refreshTrace();
      showToast("Preference updated.");
    } catch (error) { showToast(error.message); }
  }

  async function forgetMemory(memoryId) {
    try {
      await api("/api/memory/forget", { method: "POST", body: JSON.stringify({ memory_id: memoryId }) });
      await renderMemories();
      await refreshTrace();
      showToast("Preference forgotten and removed from stored context.");
    } catch (error) { showToast(error.message); }
  }

  async function memoryDecision(memoryId, decision) {
    try {
      await api(`/api/memory/${decision}`, { method: "POST", body: JSON.stringify({ memory_id: memoryId }) });
      await renderMemories();
      await refreshTrace();
    } catch (error) { showToast(error.message); }
  }

  async function refreshTrace() {
    try {
      const response = await api("/api/traces");
      const list = byId("traceList");
      list.replaceChildren();
      const events = response.events || [];
      byId("traceCount").textContent = `${events.length} event${events.length === 1 ? "" : "s"}`;
      for (const event of events) {
        const details = node("details", `trace-event ${traceClass(event.event)}`);
        const summary = node("summary");
        summary.appendChild(node("span", "event-dot"));
        summary.appendChild(node("span", "", event.event.replaceAll("_", " ")));
        summary.appendChild(node("span", "event-time", new Date(event.created_at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })));
        details.appendChild(summary);
        const payload = { run_id: event.run_id, data: event.data };
        details.appendChild(node("pre", "", JSON.stringify(payload, null, 2)));
        list.appendChild(details);
      }
      list.scrollTop = list.scrollHeight;
    } catch (_) { /* A temporary trace refresh failure does not interrupt chat. */ }
  }

  function traceClass(event) {
    if (event.includes("security")) return "security";
    if (event.includes("approval")) return "approval";
    if (event.includes("verification") || event.includes("execution_result")) return "verification";
    if (event.includes("error") || event.includes("denied")) return "error";
    return "";
  }

  async function refreshBudget(data) {
    try {
      const budget = data || (await api("/api/session")).budget;
      const spent = Number(budget.estimated_cost_usd || 0);
      byId("budgetValue").textContent = `$${spent.toFixed(5)} / $5.00 · ${budget.requests || 0} requests`;
    } catch (_) { /* Keep the last shown budget value. */ }
  }

  async function refreshOutbox() {
    try {
      const response = await api("/api/outbox");
      const container = byId("outbox");
      for (const notification of response.notifications || []) {
        container.appendChild(node("div", "notification", notification.content));
      }
    } catch (_) { /* No notification is preferable to interrupting the conversation. */ }
  }

  async function loadHistory() {
    if (historyRefreshInFlight) return;
    historyRefreshInFlight = true;
    try {
      const [response, approvalResponse] = await Promise.all([api("/api/history"), api("/api/approvals")]);
      const messages = response.messages || [];
      const approvals = approvalResponse.approvals || [];
      const options = response.current_options || [];
      const signature = JSON.stringify({
        messages: messages.map((item) => [item.id, item.role, item.content]),
        approvals: approvals.map((item) => [item.action_id, item.fingerprint]),
        options: options.map((item) => [item.provider_id, item.available_slots]),
      });
      if (signature === historySignature) return;
      historySignature = signature;
      const activeApprovalText = new Set(approvals.map((item) => item.text));
      chat.replaceChildren();
      for (const message of messages) {
        if (message.role === "assistant" && activeApprovalText.has(message.content)) continue;
        addMessage(message.role === "user" ? "user" : "assistant", message.content);
      }
      for (const approval of approvals) renderApproval(approval, approval.text);
      if (options.length) renderProviderOptions(options);
    } finally {
      historyRefreshInFlight = false;
    }
  }

  function formatSlot(slot) {
    const date = new Date(slot);
    if (Number.isNaN(date.getTime())) return slot;
    return date.toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  }

  async function applyDemoScenario() {
    const outcome = byId("scenarioSelect").value;
    try {
      await api("/api/demo/scenario", { method: "POST", body: JSON.stringify({ next_book: outcome }) });
      showToast(`Next mock booking: ${outcome.replaceAll("_", " ")}`);
    } catch (error) { showToast(error.message); }
  }

  async function simulateProviderCancellation() {
    const bookingId = byId("cancelBookingSelect").value;
    try {
      const result = await api("/api/demo/cancel_provider", {
        method: "POST", body: JSON.stringify({ booking_id: bookingId }),
      });
      await loadHistory();
      await refreshTrace();
      await refreshBookings(false);
      await refreshOutbox();
      showToast(result.events && result.events.length ? "Cancellation detected; review the replacement approval in chat." : "Cancellation event processed.");
    } catch (error) { showToast(error.message); }
  }

  async function bootstrap() {
    try {
      const session = await api("/api/session");
      sessionId = session.session_id;
      csrfToken = session.csrf_token;
      demoControlsEnabled = Boolean(session.dev_controls);
      byId("devControls").hidden = !demoControlsEnabled;
      const status = byId("modelStatus");
      if (session.model_ready) {
        status.textContent = `${session.model} · live`;
        status.classList.add("ready");
      } else if (session.llm_mode === "scripted") {
        status.textContent = "Scripted offline model";
        status.classList.add("offline");
      } else {
        status.textContent = `${session.model} · not configured`;
        status.classList.add("offline");
      }
      await loadHistory();
      await refreshTrace();
      await refreshBudget(session.budget);
      await refreshOutbox();
      if (!chat.childNodes.length) {
        addMessage("assistant", "Tell me the service, area, and date window. Add a budget or timing preference if you have one.");
      }
      window.setInterval(refreshTrace, 1700);
      window.setInterval(() => { void loadHistory(); void refreshOutbox(); }, 5000);
    } catch (error) {
      addMessage("assistant", `The local app could not start: ${error.message}`, "system-note");
    }
  }

  byId("composer").addEventListener("submit", (event) => {
    event.preventDefault();
    const input = byId("messageInput");
    const message = input.value.trim();
    if (message) sendPrompt(message);
  });
  byId("memoryButton").addEventListener("click", renderMemories);
  byId("closeMemory").addEventListener("click", () => byId("memoryDialog").close());
  byId("bookingsButton").addEventListener("click", () => refreshBookings(true));
  byId("closeBookings").addEventListener("click", () => byId("bookingsDialog").close());
  byId("scenarioButton").addEventListener("click", applyDemoScenario);
  byId("cancelProviderButton").addEventListener("click", simulateProviderCancellation);
  bootstrap();
})();
