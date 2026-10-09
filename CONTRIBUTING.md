# Contributing to Tendril

Thanks for wanting to help! Tendril is small on purpose, so it's a friendly place to start. Bug fixes, edge cases, docs and ideas are all welcome.

## Run it and the tests

You need Python 3.11. Docker is only needed if you want the full app with the local model.

```bash
git clone https://github.com/pyarchana/tendril.git
cd tendril
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt      # Windows: .venv\Scripts\pip
.venv/bin/python -m pytest -q                      # Windows: .venv\Scripts\python
```

The tests never touch the network or a real model. Open-Meteo is mocked with `respx`, the model with a scripted fake (`FakeLLM` in `tests/factories.py`), and whisper with a fake transcriber. The whole suite runs in under a minute.

To try the real thing, `docker compose up -d --build` starts the app and Ollama, and `docker compose exec app python -m scripts.demo` plans a demo garden with the local model. See the [README](README.md) for the full setup.

## How Tendril thinks

One rule shapes most of the code: **the model proposes, the rules decide.**

- The model (`app/services/planner.py`) suggests tasks and reads voice notes. Its replies are validated, retried once with feedback, and replaced by the rule-based planner if they still fail.
- The weather rules (`app/services/rules.py`) always have the last word: skip watering when rain is likely, shade on hot days, deep watering after a dry spell, treatment first when a problem is reported. They're plain, deterministic code.
- When Tendril isn't sure (which plant has pests, which task a note means), it asks instead of guessing.

If you change how plans or check-ins work, keep that split: let the model add variety, and let code make anything that has to be right.

## Making a change

1. **Open an issue first** for anything bigger than a small fix, so we can agree on the approach.
2. **Add a test** that fails without your change and passes with it. Bugs found in real use are best captured as a test with the exact case (see `tests/test_rules.py` for examples).
3. **Keep the suite green:** `pytest -q` should pass on your branch.
4. **Keep it focused:** one fix or feature per pull request, written like the code around it. Short commit messages are perfect, e.g. `fix similar plant names`.
5. **Stay open:** Tendril uses open-weight models and free services only. Please don't add paid or closed APIs.

## Reporting a bug

Open an issue with what you did, what you expected and what happened. For odd model behaviour, the model's raw reply from the logs helps a lot.

**Please never paste your Today link or check-in token in an issue.** It's the key to your garden. Your garden's exact coordinates aren't needed either.

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE), like the rest of Tendril.
