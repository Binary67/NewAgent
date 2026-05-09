const configStatus = document.querySelector("#config-status");
const configContent = document.querySelector("#config-content");
const experimentButton = document.querySelector("#experiment-button");
const experimentButtonLabel = document.querySelector("#experiment-button-label");
const experimentStartIcon = experimentButton.querySelector(".experiment-start-icon");
const experimentStopIcon = experimentButton.querySelector(".experiment-stop-icon");
const resetButton = document.querySelector("#reset-button");
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
const evalCurrentTrial = document.querySelector("#eval-current-trial");
const evalCurrentScore = document.querySelector("#eval-current-score");
const evalCurrentComparison = document.querySelector("#eval-current-comparison");
const evalBestTrial = document.querySelector("#eval-best-trial");
const evalBestScore = document.querySelector("#eval-best-score");
const evalCount = document.querySelector("#eval-count");
const evalTrialsBody = document.querySelector("#eval-trials-body");

let evalResults = [];
let currentRunning = false;
let currentJobName = null;

function setRunningState(running, jobName) {
  currentRunning = running;
  currentJobName = jobName || null;

  const experimentRunning = running && jobName === "experiment";
  experimentButton.dataset.mode = experimentRunning ? "stop" : "start";
  experimentButton.classList.toggle("primary-button", !experimentRunning);
  experimentButton.classList.toggle("danger-button", experimentRunning);
  experimentButton.disabled = running && !experimentRunning;
  experimentButtonLabel.textContent = experimentRunning ? "Stop Experiment" : "Start Experiment";
  experimentStartIcon.hidden = experimentRunning;
  experimentStopIcon.hidden = !experimentRunning;

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

function numericScore(result) {
  const value = Number(result.score);
  return Number.isFinite(value) ? value : null;
}

function sortedEvalResults() {
  return [...evalResults].sort((first, second) => second.trialNumber - first.trialNumber);
}

function bestEvalResult(results) {
  return results.reduce((best, result) => {
    const score = numericScore(result);
    if (score === null) {
      return best;
    }
    if (!best || score > best.score) {
      return { result, score };
    }
    return best;
  }, null)?.result || null;
}

function renderEvalTable(results, current, best) {
  evalTrialsBody.replaceChildren();

  if (!results.length) {
    const row = document.createElement("tr");
    row.className = "eval-empty-row";

    const cell = document.createElement("td");
    cell.colSpan = 3;
    cell.textContent = "No evals recorded yet.";

    row.append(cell);
    evalTrialsBody.append(row);
    return;
  }

  const fragment = document.createDocumentFragment();
  for (const result of results) {
    const row = document.createElement("tr");
    const isCurrent = result.trialNumber === current.trialNumber;
    const isBest = best && result.trialNumber === best.trialNumber;
    row.classList.toggle("is-current", isCurrent);
    row.classList.toggle("is-best", isBest);
    row.title = result.comparison;

    const trial = document.createElement("td");
    trial.textContent = result.trial;

    const score = document.createElement("td");
    score.className = "eval-table-score";
    score.textContent = result.score;
    score.title = result.score;

    const status = document.createElement("td");
    if (isBest) {
      const bestLabel = document.createElement("span");
      bestLabel.className = "eval-best-label";
      bestLabel.textContent = "Best";
      status.append(bestLabel);
    } else if (isCurrent) {
      status.textContent = "Latest";
    }

    row.append(trial, score, status);
    fragment.append(row);
  }

  evalTrialsBody.append(fragment);
}

function renderEvalHistory() {
  const results = sortedEvalResults();
  const current = results[0] || null;
  const best = bestEvalResult(results);

  evalCount.textContent = `${results.length} recorded`;

  if (!current) {
    evalCurrentTrial.textContent = "No eval yet";
    evalCurrentScore.textContent = "--";
    evalCurrentComparison.textContent = "No eval result yet.";
    evalBestTrial.textContent = "--";
    evalBestScore.textContent = "--";
    renderEvalTable(results, current, best);
    return;
  }

  evalCurrentTrial.textContent = `Trial ${current.trial}`;
  evalCurrentScore.textContent = current.score;
  evalCurrentComparison.textContent = current.comparison;
  evalBestTrial.textContent = best ? `Trial ${best.trial}` : "--";
  evalBestScore.textContent = best ? best.score : "--";
  renderEvalTable(results, current, best);
}

function resetEvalHistory() {
  evalResults = [];
  renderEvalHistory();
}

function parseEvalResult(message) {
  const match = message.match(/^\s*\[Eval trial\s+(\d+)\]\s+Score:\s+(.+?)\s+\((Comparison:.+)\)\s*$/);
  if (!match) {
    return null;
  }

  return {
    trial: match[1],
    trialNumber: Number(match[1]),
    score: match[2],
    comparison: match[3],
  };
}

function updateEvalHistoryFromLog(event) {
  if (event.kind !== "log") {
    return;
  }

  const evalResult = parseEvalResult(event.message || "");
  if (!evalResult) {
    return;
  }

  evalResults = evalResults.filter((result) => result.trialNumber !== evalResult.trialNumber);
  evalResults.push(evalResult);
  renderEvalHistory();
}

function shouldResetEvalHistory(event) {
  return event.kind === "status" && ["Starting experiment.", "Starting reset."].includes(event.message || "");
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
  experimentButton.disabled = true;
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
      setRunningState(currentRunning, currentJobName);
    }
  } catch (error) {
    appendLog({
      kind: "error",
      message: error.message || "Request failed.",
      timestamp: new Date().toISOString().slice(0, 19),
      running: false,
    });
    setRunningState(currentRunning, currentJobName);
  }
}

async function startExperiment() {
  experimentButton.disabled = true;
  resetButton.disabled = true;
  setConfigStatus("Saving config...");

  try {
    const response = await fetch("/jobs/start", {
      method: "POST",
      headers: { "Content-Type": "text/plain; charset=utf-8" },
      body: configContent.value,
    });
    const payload = await response.json();
    setConfigStatus(payload.message || "Config saved. Experiment started.", !response.ok);
    if (!response.ok) {
      appendLog({
        kind: "error",
        message: payload.message || "Start request failed.",
        timestamp: new Date().toISOString().slice(0, 19),
        running: false,
      });
      setRunningState(currentRunning, currentJobName);
    }
  } catch (error) {
    const message = error.message || "Start request failed.";
    setConfigStatus(message, true);
    appendLog({
      kind: "error",
      message,
      timestamp: new Date().toISOString().slice(0, 19),
      running: false,
    });
    setRunningState(currentRunning, currentJobName);
  }
}

async function stopExperiment() {
  experimentButton.disabled = true;

  try {
    const response = await fetch("/jobs/stop", { method: "POST" });
    const payload = await response.json();
    if (!response.ok) {
      appendLog({
        kind: "error",
        message: payload.message || "Stop request failed.",
        timestamp: new Date().toISOString().slice(0, 19),
        running: false,
      });
      setRunningState(currentRunning, currentJobName);
    }
  } catch (error) {
    appendLog({
      kind: "error",
      message: error.message || "Stop request failed.",
      timestamp: new Date().toISOString().slice(0, 19),
      running: false,
    });
    setRunningState(currentRunning, currentJobName);
  }
}

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

resetButton.addEventListener("click", () => {
  postJob("/jobs/reset");
});

experimentButton.addEventListener("click", () => {
  if (experimentButton.dataset.mode === "stop") {
    stopExperiment();
    return;
  }

  startExperiment();
});

const events = new EventSource("/events");

events.onopen = () => {
  connectionStatus.textContent = "Connected";
};

events.onmessage = (message) => {
  const event = JSON.parse(message.data);
  appendLog(event);
  if (shouldResetEvalHistory(event)) {
    resetEvalHistory();
  }
  updateEvalHistoryFromLog(event);
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
