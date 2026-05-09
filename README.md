# NextResearcher

NextResearcher is an automated experiment orchestrator for coding agents.

## Local Configuration

`CodexConfig.toml` stores local experiment settings, including absolute paths to target and evaluation repositories. It is ignored by git so local machine paths are not committed.

The repository includes `CodexConfig.example.toml` as a safe starter template. On startup, `Main.py` calls `ensure_project_files()`, which creates `CodexConfig.toml` from the template when the local config file is missing.
