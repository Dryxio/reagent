# Grok Build CLI provider

Install Grok Build and authenticate using its own `grok login` workflow. ReAgent uses the local CLI login; it does not read or store Grok credentials.

```yaml
llm:
  provider: grok-cli
  model: "" # Use Grok's isolated default, or supply a model from grok models.
  cli_path: grok # An absolute path to grok.exe also works on Windows.
  timeout_s: 600
```

Set `model` explicitly, including an empty string to use Grok's built-in default. Omitting it inherits ReAgent's general default model, which is not a Grok model.

The same configuration can be used under `agents.reverser` or `agents.checker` for mixed-provider runs. Optional `effort` is forwarded to Grok's `--effort` argument.

The adapter uses `--prompt-file` for large evidence inputs, native session IDs for repair conversations, and native JSON output (`text`, `sessionId`, `stopReason`). It requires a completed `end_turn` response and rejects empty, malformed, errored, or incomplete results. Usage and cost fields are retained in call metadata when supplied by Grok.

Requests run in a temporary working directory and a separate `GROK_HOME` per provider.
`GROK_AUTH_PATH` points Grok at the existing login store (respecting an explicit override);
ReAgent does not read, copy, or log credentials. Global user plugins, MCP configuration,
and compatibility discovery are not inherited. Local `requirements.toml` and
`managed_config.toml` are preserved, and system policies still apply. Custom user
model/default/endpoint and authentication-helper configuration is not copied; select
an explicit supported model and use supported environment-based endpoint/auth settings
when necessary. An enforced managed integration may still initialize.

The adapter selects `read_file` with `--tools`, then removes it together with the
always-on MCP meta-tools using `--disallowed-tools read_file,search_tool,use_tool`.
This deliberately avoids the empty allowlist behavior observed in Grok Build 1.0.25,
which left built-ins available. A deny rule, disabled web search, and disabled
subagents remain additional restrictions. `--max-turns 1` is unchanged. In a native
reconstruction trace, the old filter produced tool calls and hit that limit; the
explicit filter completed with `end_turn` and one model turn.

Grok SDK retries are disabled in the isolated configuration. Native error JSON retains
stop reason, turn count, and reported usage in call metadata, even on nonzero exit.
Provider cleanup removes its temporary home and sessions; ReAgent's configured call
logs and reports persist separately. Original Grok sessions and user settings are unchanged.

References: [headless tool filtering](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/14-headless-mode.md),
[configuration](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/05-configuration.md),
and [native auth path resolution](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-login/src/storage.rs).


The shared subprocess runner terminates the child process tree on timeout. ReAgent's `max_tokens`, `temperature`, and API-key settings are not forwarded to this CLI. `max_budget_usd` is rejected because the adapter cannot enforce it through the supported CLI flags. Configure account/model limits in Grok Build.

Validated with Grok Build 1.0.25 on Windows, including a live two-turn session. Unit tests run without a Grok installation or network access.

Official CLI documentation: https://docs.x.ai/build/cli/headless-scripting
