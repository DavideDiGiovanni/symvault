# Contributing to Symvault

Thanks for your interest in contributing! Here's how to get started.

## Development Setup

```bash
# Clone the repository
git clone https://github.com/DavideDiGiovanni/symvault.git
cd symvault

# Create a virtual environment
python -m venv .venv
source .venv/bin/activate

# Install in development mode with test dependencies
pip install -e ".[dev]"
```

## Branching Model

- `master` — stable release branch
- `dev` — integration branch for ongoing development
- `feature/*` — feature branches, created from `dev`

To contribute:

1. Create a feature branch from `dev`: `git checkout -b feature/my-feature dev`
2. Make your changes with focused, descriptive commits
3. Push your branch and open a Pull Request targeting `dev`

## Running Tests

Always run the test suite before submitting changes:

```bash
pytest
```

All tests must pass. If you're adding a new command or feature, add corresponding tests in `tests/test_vault.py`.

## Code Style

- **Code, comments, CLI output**: English
- **Documentation (README, ROADMAP)**: Italian (with English README as primary)
- Follow existing patterns in the codebase
- Use `click.style()` for colored CLI output (green=success, yellow=warning, red=error, blue=skip, cyan=untracked, magenta=orphan)

## Commit Messages

Use conventional commit format:

- `feat:` — new feature
- `fix:` — bug fix
- `docs:` — documentation
- `test:` — tests
- `ci:` — CI configuration
- `chore:` — maintenance, config
- `refactor:` — code restructuring

## Pull Request Process

1. Ensure all tests pass
2. Update documentation if needed
3. Use a descriptive PR title following commit conventions
4. Target the `dev` branch (never push directly to `master`)
