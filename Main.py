from ConfigGuard import ensure_project_files
from Orchestrator import ensure_evaluator_setup, run_experiment_loop

ensure_project_files()
experiment = ensure_evaluator_setup()

run_experiment_loop(
    target_repo=experiment["target_repo"],
    eval_command=experiment["eval_command"],
    eval_strategy=experiment["eval_strategy"],
    role=experiment["role"],
    num_iterations=experiment["num_iterations"],
    max_eval_calls=experiment.get("max_eval_calls", 3),
    eval_repo=experiment["eval_repo"],
    eval_overrides=experiment["eval_overrides"],
)
