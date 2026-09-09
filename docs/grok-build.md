# Grok Build CLI provider

Install Grok Build and authenticate using its own `grok login` workflow. ReAgent uses the local CLI login; it does not read or store Grok credentials.

```yaml
llm:
  provider: grok-cli
  model: "" # Use the model configured in Grok Build, or supply a model from grok models.
  cli_path: grok # An absolute path to grok.exe also works on Windows.
  timeout_s: 600
```

Set `model` explicitly, including an empty string to use Grok's configured model. Omitting it inherits ReAgent's general default model, which is not a Grok model.

The same configuration can be used under `agents.reverser` or `agents.checker` for mixed-provider runs. Optional `effort` is forwarded to Grok's `--effort` argument.

The adapter uses `--prompt-file` for large evidence inputs, native session IDs for repair conversations, and native JSON output (`text`, `sessionId`, `stopReason`). It requires a completed `end_turn` response and rejects empty, malformed, errored, or incomplete results. Usage and cost fields are retained in call metadata when supplied by Grok.

Requests run in a temporary working directory with built-in tools disabled, a deny-all permission rule, web tools disabled, and subagents disabled. ReAgent supplies evidence and controls compilation/validation. The CLI still uses its own global configuration and stores its own sessions. Temporary prompt files are removed after each request; workspace cleanup does not delete Grok's persistent sessions.

The shared subprocess runner terminates the child process tree on timeout. ReAgent's `max_tokens`, `temperature`, and API-key settings are not forwarded to this CLI. `max_budget_usd` is rejected because the adapter cannot enforce it through the supported CLI flags. Configure account/model limits in Grok Build.

Validated with Grok Build 1.0.25 on Windows, including a live two-turn session. Unit tests run without a Grok installation or network access.

Official CLI documentation: https://docs.x.ai/build/cli/headless-scripting
