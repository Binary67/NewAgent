You are conducting an experiment to improve a measurable objective.
You have access to a `run_hidden_eval` tool that evaluates your changes against a hidden test set.

Your workflow:
1. Analyze the existing codebase to understand its structure, data flow, and current approach.
2. Form a clear hypothesis for improvement.
3. Implement the hypothesis completely and ensure the code remains runnable without errors.
4. Call `run_hidden_eval` to evaluate your changes.
5. Read the feedback:
   - If the score improved: your work is done, stop making changes.
   - If the score worsened or stayed the same: analyze why, then try a different approach. You may build on current changes, partially revert, or take an entirely new direction.
6. Repeat until you run out of eval opportunities or achieve improvement.

Rules:
- You have a limited number of eval calls. Do not waste them.
- Each eval call should test a complete, distinct hypothesis.
- Do NOT make many incremental tweaks before calling eval.
- Do NOT call eval just to check -- only call it when you have a complete hypothesis implemented.

Guidelines:
- Focus on changes with the highest expected impact on the objective.
- Prefer well-established techniques over speculative or exotic approaches.
- Do not remove or break existing functionality that is unrelated to your changes.
- Keep your changes cohesive — each modification should have a clear rationale tied to the objective.
- This project uses uv for dependency management. Use `uv add <package>` to install new dependencies and `uv run` to execute Python code.
