from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "CodexConfig.toml"
PROMPTS_DIR = PROJECT_ROOT / "Prompts"

DEFAULT_CONFIG = """\
[Experiment]
target_repo = "D:/HousePricePrediction"
eval_command = "uv run D:/HiddenEval/hidden_evaluation.py"
eval_strategy = "minimize"
num_iterations = 1
role = "experiment"
"""

DEFAULT_PROMPTS = {
    "base.md": """\
You are a coding agent operating in an automated pipeline.
- Read and understand existing code before making changes.
- Make targeted, minimal changes that directly address the task.
- Do not refactor or reorganize code beyond what is required.
- If a task is ambiguous, use your best judgement and proceed.
""",
    "experiment.md": """\
You are conducting an experiment to improve a measurable objective.

Your workflow:
1. Analyze the existing codebase to understand its structure, data flow, and current approach.
2. Identify concrete opportunities for improvement that are likely to increase the evaluation score.
3. Implement changes and ensure the code remains runnable without errors.

Guidelines:
- Focus on changes with the highest expected impact on the objective.
- Prefer well-established techniques over speculative or exotic approaches.
- Do not remove or break existing functionality that is unrelated to your changes.
- Keep your changes cohesive — each modification should have a clear rationale tied to the objective.
- This project uses uv for dependency management. Use `uv add <package>` to install new dependencies and `uv run` to execute Python code.
""",
}


def ensure_codex_config() -> Path:
    if CONFIG_PATH.exists():
        print(f"Config found: {CONFIG_PATH}")
    else:
        CONFIG_PATH.write_text(DEFAULT_CONFIG, encoding="utf-8")
        print(f"Generated default config: {CONFIG_PATH}")

    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    for filename, content in DEFAULT_PROMPTS.items():
        prompt_path = PROMPTS_DIR / filename
        if not prompt_path.exists():
            prompt_path.write_text(content, encoding="utf-8")
            print(f"Generated default prompt: {prompt_path}")

    return CONFIG_PATH
