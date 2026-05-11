# VCR cassettes for Anthropic Admin API tests

Cassettes recorded against the real Anthropic Admin API land in this
directory. Every recording MUST be made through
`tests/_vcr_config.py::vcr_for_anthropic`, which filters the
`x-api-key` header to the literal string `REDACTED`.

T3.1 ships the directory empty (only this README + `.gitkeep`). T3.2
records the first real cassettes against a test org for the typed
endpoints (`list_usage`, `list_members`, `list_workspaces`).

## Recording a new cassette

```python
from _vcr_config import vcr_for_anthropic
from vargate_telemetry.anthropic import AnthropicAdminClient

v = vcr_for_anthropic(record_mode="once")  # records on first run, replays after
with v.use_cassette("list_workspaces.yaml"):
    client = AnthropicAdminClient(api_key=os.environ["ANTHROPIC_ADMIN_KEY_TEST"])
    workspaces = list(client.paginate("/v1/organizations/workspaces"))
```

## Before committing a new cassette

Inspect the YAML and confirm:

- No string starting with `sk_live_` or `sk_test_` (a leaked key).
- The `x-api-key` request header reads `REDACTED`, not a real value.
- No tenant-identifying free-text in response bodies that would be
  awkward to ship publicly when the repo turns public.

A future CI check (tracked in T3.x follow-ups) will scan this
directory automatically; for now the convention is enforced by
review.
