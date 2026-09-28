# Contributing

Contributions are welcome.

## Development setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt pytest
pytest -q
```

## Commit style

Use Conventional Commits:

- `fix: ...`
- `feat: ...`
- `docs: ...`
- `test: ...`
- `refactor: ...`
- `feat!: ...` for breaking changes

Release Please uses these commits to prepare version/release PRs.

## Pull requests

PRs should include:

1. What changed and why.
2. macOS/Linux implications.
3. Tests or a reason tests are not applicable.
4. No credentials, home-directory paths, private repository names, account IDs, or machine-specific data.

Keep provider-specific behavior behind bounded adapters where possible. Do not weaken destructive Git/cloud safety gates just to make a test pass.
