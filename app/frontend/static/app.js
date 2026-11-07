"use strict";

const form = document.querySelector("#run-form");
const report = document.querySelector("#report");
const submitButton = document.querySelector("#submit-run");
const dataFields = document.querySelector("#data-fields");
const rowTemplate = document.querySelector("#data-row-template");

function addDataRow() {
  const row = rowTemplate.content.firstElementChild.cloneNode(true);
  row.querySelector(".remove-data").addEventListener("click", () => row.remove());
  dataFields.append(row);
  row.querySelector(".data-key").focus();
}

document.querySelector("#add-data").addEventListener("click", addDataRow);
addDataRow();

function setStatus(message, outcome = "WAITING") {
  document.querySelector("#run-status").textContent = message;
  const badge = document.querySelector("#outcome-badge");
  badge.textContent = outcome;
  badge.className = "outcome-badge";
  if (outcome === "PASS") badge.classList.add("pass");
  if (outcome === "FAIL") badge.classList.add("fail");
  if (outcome === "INCONCLUSIVE") badge.classList.add("inconclusive");
}

function listItem(title, explanation, status) {
  const item = document.createElement("li");
  item.className = "result-item";
  const icon = document.createElement("span");
  icon.className = `result-icon ${String(status || "").toLowerCase()}`;
  icon.textContent = status === "PASS" ? "✓" : status === "FAIL" ? "!" : "·";
  const body = document.createElement("div");
  const heading = document.createElement("p");
  heading.className = "result-title";
  heading.textContent = title;
  const detail = document.createElement("p");
  detail.className = "result-copy";
  detail.textContent = explanation || "No additional details.";
  body.append(heading, detail);
  item.append(icon, body);
  return item;
}

function showItems(container, items, makeTitle) {
  container.replaceChildren();
  if (!items || items.length === 0) {
    const empty = document.createElement("li");
    empty.className = "empty-result";
    empty.textContent = "No entries to show.";
    container.append(empty);
    return;
  }
  items.forEach((entry) => container.append(
    listItem(makeTitle(entry), entry.explanation, entry.status),
  ));
}

function collectTestData() {
  const result = {};
  for (const row of dataFields.querySelectorAll(".data-row")) {
    const key = row.querySelector(".data-key").value.trim();
    const value = row.querySelector(".data-value").value;
    if (!key && !value) continue;
    if (!key || Object.hasOwn(result, key)) {
      throw new Error("Each test-data value needs a unique key.");
    }
    result[key] = value;
  }
  return result;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  let testData;
  try {
    testData = collectTestData();
  } catch (error) {
    report.hidden = false;
    setStatus(error.message, "INCONCLUSIVE");
    return;
  }

  const body = {
    target_url: document.querySelector("#target-url").value.trim(),
    instruction: document.querySelector("#instruction").value.trim(),
    test_data: testData,
  };
  report.hidden = false;
  document.querySelector("#run-summary").textContent = "";
  document.querySelector("#action-list").replaceChildren();
  document.querySelector("#assertion-list").replaceChildren();
  setStatus("Request submitted. Waiting for the backend to return a verified report…", "RUNNING");
  submitButton.disabled = true;

  try {
    const response = await fetch("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const contentType = response.headers.get("content-type") || "";
    const result = contentType.includes("application/json") ? await response.json() : null;
    if (!response.ok || !result) {
      throw new Error("The server could not accept this test run.");
    }
    const outcome = ["PASS", "FAIL", "INCONCLUSIVE"].includes(result.outcome)
      ? result.outcome
      : "INCONCLUSIVE";
    setStatus(`Run ${result.status || "COMPLETED"}`, outcome);
    document.querySelector("#run-summary").textContent = result.summary || "The run returned no summary.";
    showItems(
      document.querySelector("#action-list"),
      result.steps,
      (step) => {
        const occurrence = step.occurrence_id ? ` · ${step.occurrence_id}` : "";
        const attempt = step.attempt_number ? ` · attempt ${step.attempt_number}` : "";
        const diagnostic = step.fill_diagnostic;
        const details = [];
        if (diagnostic) {
          details.push(`target ${diagnostic.target_kind}`);
          if (diagnostic.target_role) details.push(`role ${diagnostic.target_role}`);
          details.push(`locator ${diagnostic.locator_strategy}`);
          if (diagnostic.snapshot_match) details.push(`snapshot ${diagnostic.snapshot_match}`);
          details.push(diagnostic.snapshot_truncated ? "snapshot truncated" : "snapshot complete");
          if (Number.isInteger(diagnostic.candidate_count)) {
            details.push(`candidates ${diagnostic.candidate_count}`);
          }
          if (Number.isInteger(diagnostic.visible_count)) {
            details.push(`visible ${diagnostic.visible_count}`);
          }
          details.push(diagnostic.reason_code);
        }
        const suffix = details.length ? ` (${details.join(", ")})` : "";
        return `${step.action_type || "Action"} · ${step.status || "UNKNOWN"}${occurrence}${attempt}${suffix}`;
      },
    );
    showItems(
      document.querySelector("#assertion-list"),
      result.verified_assertions,
      (assertion) => {
        const title = `${assertion.action_type || "Assertion"} · ${assertion.status || "UNKNOWN"}` +
          (assertion.reason_code ? ` · ${assertion.reason_code}` : "");
        const diagnostics = [];
        if (assertion.target_kind) diagnostics.push(`kind ${assertion.target_kind}`);
        if (assertion.snapshot_match) diagnostics.push(`snapshot ${assertion.snapshot_match}`);
        if (assertion.snapshot_truncated) diagnostics.push("snapshot truncated");
        if (Number.isInteger(assertion.candidate_count)) {
          diagnostics.push(`candidates ${assertion.candidate_count}`);
        }
        if (Number.isInteger(assertion.visible_count)) {
          diagnostics.push(`visible ${assertion.visible_count}`);
        }
        return diagnostics.length ? `${title} (${diagnostics.join(", ")})` : title;
      },
    );
  } catch (error) {
    const message = error.message === "Each test-data value needs a unique key."
      ? error.message
      : "The request failed; no verified result was returned.";
    setStatus(message, "INCONCLUSIVE");
  } finally {
    submitButton.disabled = false;
    for (const value of dataFields.querySelectorAll(".data-value")) value.value = "";
  }
});
