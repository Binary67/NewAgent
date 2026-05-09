const configForm = document.querySelector("#config-form");
const configStatus = document.querySelector("#config-status");
const startButton = document.querySelector("#start-button");
const resetButton = document.querySelector("#reset-button");
const saveButton = document.querySelector("#save-config");
const logOutput = document.querySelector("#log-output");
const jobPill = document.querySelector("#job-pill");
const connectionStatus = document.querySelector("#connection-status");
const backendInputForm = document.querySelector("#backend-input-form");
const backendInputPrompt = document.querySelector("#backend-input-prompt");
const backendQuestionRow = document.querySelector("#backend-question-row");
const backendRecommendationRow = document.querySelector("#backend-recommendation-row");
const backendQuestion = document.querySelector("#backend-question");
const backendRecommendation = document.querySelector("#backend-recommendation");
const backendResponse = document.querySelector("#backend-response");
const submitBackendInput = document.querySelector("#submit-backend-input");
const backendInputStatus = document.querySelector("#backend-input-status");

function setRunningState(running, jobName) {
  startButton.disabled = running;
  resetButton.disabled = running;
  jobPill.textContent = running ? `${jobName || "Job"} running` : "Idle";
  jobPill.classList.toggle("running", running);
}

function setConfigStatus(message, isError = false) {
  configStatus.textContent = message;
  configStatus.classList.toggle("error", isError);
}

function setBackendInputStatus(message, isError = false) {
  backendInputStatus.textContent = message;
  backendInputStatus.classList.toggle("error", isError);
}

function setInputDetail(row, target, value) {
  target.textContent = value || "";
  row.hidden = !value;
}

function showBackendInput(event) {
  backendInputPrompt.textContent = event.prompt || event.message || "Backend is waiting for input.";
  setInputDetail(backendQuestionRow, backendQuestion, event.question || "");
  setInputDetail(backendRecommendationRow, backendRecommendation, event.recommendation || "");
  backendResponse.value = "";
  submitBackendInput.disabled = false;
  setBackendInputStatus("Waiting for response.");
  backendInputForm.hidden = false;
  backendResponse.focus();
}

function hideBackendInput(message = "Input submitted.") {
  backendInputForm.hidden = true;
  submitBackendInput.disabled = false;
  setBackendInputStatus(message);
}

function appendLog(event) {
  const line = document.createElement("div");
  line.className = `log-line ${event.kind}-line`;

  const time = document.createElement("span");
  time.className = "log-time";
  time.textContent = event.timestamp ? event.timestamp.slice(11) : "--:--:--";

  const kind = document.createElement("span");
  kind.className = "log-kind";
  kind.textContent = event.kind || "log";

  const message = document.createElement("span");
  message.className = "log-message";
  message.textContent = event.message || "";

  line.append(time, kind, message);
  logOutput.append(line);
  logOutput.scrollTop = logOutput.scrollHeight;
}

async function postJob(path) {
  startButton.disabled = true;
  resetButton.disabled = true;

  try {
    const response = await fetch(path, { method: "POST" });
    const payload = await response.json();
    if (!response.ok) {
      appendLog({
        kind: "error",
        message: payload.message || "Request failed.",
        timestamp: new Date().toISOString().slice(0, 19),
        running: false,
      });
      setRunningState(false);
    }
  } catch (error) {
    appendLog({
      kind: "error",
      message: error.message || "Request failed.",
      timestamp: new Date().toISOString().slice(0, 19),
      running: false,
    });
    setRunningState(false);
  }
}

configForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  saveButton.disabled = true;
  setConfigStatus("Saving...");

  const response = await fetch("/config", {
    method: "POST",
    headers: { "Content-Type": "text/plain; charset=utf-8" },
    body: document.querySelector("#config-content").value,
  });
  const payload = await response.json();

  setConfigStatus(payload.message || "Config saved.", !response.ok);
  saveButton.disabled = false;
});

backendInputForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  submitBackendInput.disabled = true;
  setBackendInputStatus("Submitting...");

  try {
    const response = await fetch("/jobs/input", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ response: backendResponse.value }),
    });
    const payload = await response.json();
    if (!response.ok) {
      setBackendInputStatus(payload.message || "Input was rejected.", true);
      submitBackendInput.disabled = false;
      return;
    }
    hideBackendInput(payload.message || "Input submitted.");
  } catch (error) {
    setBackendInputStatus(error.message || "Input request failed.", true);
    submitBackendInput.disabled = false;
  }
});

startButton.addEventListener("click", () => {
  postJob("/jobs/start");
});

resetButton.addEventListener("click", () => {
  postJob("/jobs/reset");
});

const events = new EventSource("/events");

events.onopen = () => {
  connectionStatus.textContent = "Connected";
};

events.onmessage = (message) => {
  const event = JSON.parse(message.data);
  appendLog(event);
  if (event.kind === "input_request") {
    showBackendInput(event);
  }
  if (event.kind === "input_submitted") {
    hideBackendInput();
  }
  setRunningState(Boolean(event.running), event.job);
};

events.onerror = () => {
  connectionStatus.textContent = "Reconnecting";
};
