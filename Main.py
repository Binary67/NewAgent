from Orchestrator import run_experiment_loop

# === Configure these before running ===
TARGET_REPO = "D:/HousePricePrediction"
EVAL_COMMAND = "uv run D:/HiddenEval/hidden_evaluation.py"
EVAL_STRATEGY = "minimize"  # "maximize" or "minimize"
CODEX_INSTRUCTION = ""
NUM_ITERATIONS = 1

run_experiment_loop(
    target_repo=TARGET_REPO,
    eval_command=EVAL_COMMAND,
    eval_strategy=EVAL_STRATEGY,
    codex_instruction=CODEX_INSTRUCTION,
    num_iterations=NUM_ITERATIONS,
)
