# Contributing

## Local setup

Install Python 3.12+, Docker Desktop, and `uv`. Then copy `.env.example` to
`.env`; do not commit real credentials.

```bash
uv sync --dev
make test
make lint
make smoke
```

Keep changes narrow, add or update tests for behavioural changes, and preserve
the durable state-machine and redaction boundaries. Run `make coverage` before
opening a pull request. `make smoke` is the Compose-level credentials-free demo;
it is separate from hosted CI and production validation.

## Security

Never commit API keys, provider keys, URLs containing credentials, prompts, or
provider responses. Report vulnerabilities privately as described in
[`SECURITY.md`](SECURITY.md).
