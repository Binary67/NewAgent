## Coding Contracts

### Keep Changes Problem-Agnostic and Language-Agnostic
- Do not design changes, proposals, prompts, workflows, schemas, or logic around a single problem type unless the user explicitly requests that scope.
- Do not make implementations depend on a specific programming language, framework, ecosystem, file extension, or toolchain unless that dependency is explicitly required.
- Default to general mechanisms that can work across different problem domains and across codebases written in different languages.
- If an approach would make NextResearcher biased toward one category of task or one language, stop and ask the user before implementing it.
- Avoid hardcoded assumptions such as:
  - logic that only applies to one problem type
  - prompts or pipelines tailored only to Python, JavaScript, or any other single language
  - language-specific heuristics presented as universal behavior
- When proposing or implementing changes, preserve NextResearcher's role as a general-purpose system that should work on any type of problem regardless of programming language.
