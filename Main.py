import tomllib
from pathlib import Path

from ConfigGuard import ensure_codex_config
from Orchestrator import run_experiment_loop

CONFIG_PATH = Path(__file__).resolve().parent / "CodexConfig.toml"

ensure_codex_config()

config = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
experiment = config["Experiment"]

run_experiment_loop(
    target_repo=experiment["target_repo"],
    eval_command=experiment["eval_command"],
    eval_strategy=experiment["eval_strategy"],
    role=experiment["role"],
    num_iterations=experiment["num_iterations"],
    max_eval_calls=experiment.get("max_eval_calls", 3),
    eval_repo=experiment["eval_repo"],
    eval_overrides=experiment["eval_overrides"],
    prewarm_command=experiment.get("prewarm_command", ""),
    prewarm_watch_files=experiment.get("prewarm_watch_files", []),
)
