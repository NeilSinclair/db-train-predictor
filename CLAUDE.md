# CLAUDE.md

## Project overview

We are building an app that predicts **whether a Deutsche Bahn (DB) train will be late, and by how many minutes**.

The app has three parts:

1. **Data extraction**: pull timetable and real-time delay data from the Deutsche Bahn API (via the DB API Marketplace).
2. **Machine learning backend**: a Python service that trains a model on historical delay data and serves predictions: the probability that a train is late, plus the expected delay in minutes.
3. **Frontend**: a small, friendly ("cute") React app where users look up a train or connection and see the predicted delay.

## Tooling

- **Python package manager: [uv](https://docs.astral.sh/uv/).** Use `uv` for everything Python-related. Do not use `pip`, `poetry`, or `conda` directly.
  - Add dependencies: `uv add <package>` (dev-only: `uv add --dev <package>`)
  - Run commands: `uv run <command>` (e.g. `uv run pytest`)
  - Sync the environment: `uv sync`
- **Frontend:** React.

## Coding conventions

### Tests are required
- **Write tests for everything you build.** New code needs tests in the same change.
- Python tests use `pytest` and run with `uv run pytest`.
- Frontend tests use the React project's test runner (e.g. Vitest + React Testing Library).
- Tests must not call the real DB API. Mock HTTP calls and use fixtures, so tests are fast, deterministic, and don't spend API quota.

### Google-style docstrings
All Python modules, classes, and functions get [Google-style docstrings](https://google.github.io/styleguide/pyguide.html#38-comments-and-docstrings):

```python
def predict_delay(train_id: str, departure_time: datetime) -> DelayPrediction:
    """Predicts the delay for a given train departure.

    Args:
        train_id: The DB train identifier (e.g. "ICE 123").
        departure_time: The scheduled departure time.

    Returns:
        A DelayPrediction with the probability of being late and the
        expected delay in minutes.

    Raises:
        ValueError: If the train_id is not recognised.
    """
```

## Decision log

We record every project decision in [docs/decisions.md](docs/decisions.md).
- Read it before starting work, so you don't contradict or re-open a decision.
- When we make a decision (a choice of approach, data source, scope, schema,
  tool, etc.), add a numbered entry with the date, status, what we decided and
  why. Record findings that led to the decision too.
- Discussions that haven't concluded get an entry with status **Open**; update
  it once decided.
- Never rewrite an old decision. If one changes, add a new entry and mark the
  old one **Superseded**.

## To-do list

Open work, review findings and next steps live in [docs/todo.md](docs/todo.md).
Check it at the start of a session and tick items off as they're done.

## Secrets

- DB API credentials live in `.env` as `DB_CLIENT_ID` and `DB_API_KEY`.
- Never commit `.env` and never hard-code credentials. Load them from the environment at runtime.
