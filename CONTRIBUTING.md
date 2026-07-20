# Contributing to AudioGraphy

Thank you for your interest in contributing to AudioGraphy! This document
covers setup, coding standards, and the pull request process.

## Development Environment

### Prerequisites

- Python 3.13+
- Docker & Docker Compose (for MySQL 8 and local dev stack)
- Git

### Setup

```bash
# Clone the repository
git clone https://github.com/your-org/audiography.git
cd audiography

# Create a virtual environment (or use the project venv)
python3.13 -m venv .venv
source .venv/bin/activate

# Install dev dependencies
cd backend
pip install -e ".[dev]"

# Start the local stack (MySQL 8 + Adminer)
cd ..
docker compose up -d
```

### Running Tests

```bash
# All tests (requires Docker for testcontainers)
cd backend
python -m pytest

# Only model integration tests
python -m pytest tests/models/ -v -m integration

# With coverage report
python -m pytest --cov=audio_graphy --cov-report=term-missing

# Lint and format
ruff format .
ruff check . --fix

# Type checking
mypy audio_graphy
```

## Coding Standards

### Style

- **Formatter**: `ruff format` (double quotes, 4 spaces, line length 100)
- **Linter**: `ruff check` (E, W, F, I, B, C4, UP, N, SIM, ASYNC, S, T20, RUF)
- **Type checking**: `mypy --strict` (100% type annotation coverage)
- **Import order**: isort-compatible (enforced by ruff)

### Conventions

- All ORM models must have explicit `__tablename__`.
- Constraint naming: `ux_` (UNIQUE), `ix_` (INDEX), `ck_` (CHECK), `fk_` (FK).
- Enums use `String(N)` + `CheckConstraint` (not SQL ENUM).
- Docstrings: Google style, English for module/class level.
- No hardcoded secrets — use environment variables with placeholder defaults.

### Database Migrations

```bash
# Generate a new migration after model changes
cd backend
alembic revision --autogenerate -m "description of change"

# Apply migrations
alembic upgrade head

# Rollback
alembic downgrade base
```

## Commit Conventions

We follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <subject>

<body>
```

Types: `feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`, `ci`

Example:
```
feat(models): add tag_facts append-only constraint test

Add integration test verifying that tag_facts rows cannot be updated,
only inserted (append-only semantics per PRD §5.6).
```

## Pull Request Process

1. **Fork & branch**: Create a feature branch from `main`.
2. **Write tests**: All new code must have test coverage (85% minimum).
3. **Run checks locally**:
   ```bash
   ruff format . && ruff check . --fix && mypy audio_graphy && pytest
   ```
4. **Open a PR**: Fill out the PR template (see `.github/PULL_REQUEST_TEMPLATE.md`).
5. **Code review**: Address reviewer feedback.
6. **CI must pass**: All GitHub Actions checks must be green.
7. **Squash merge**: Maintainers will squash-merge approved PRs.

## Reporting Issues

- Use the bug report or feature request templates in `.github/ISSUE_TEMPLATE/`.
- For security vulnerabilities, do NOT open a public issue — email the maintainers directly.

## License

By contributing, you agree that your contributions will be licensed under the
MIT License.
