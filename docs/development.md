# Development

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
```

You need Docker for the tests (below). The code has no build step: the browser app is plain HTML, CSS and ES modules. See [Architecture](architecture.md) for how the code is laid out and the rules each layer keeps.

## Tests

The tests run against a real PostgreSQL with PostGIS: each test gets its own database, cloned from one migrated template, on the server at `GEOLOGS_TEST_DATABASE_URL`. `scripts/test-db` starts a throwaway server for them in Docker (in memory, on port 55432) and prints that URL:

```bash
export GEOLOGS_TEST_DATABASE_URL=$(scripts/test-db)
.venv/bin/python -m unittest discover -s tests -v
for file in $(find src/geologs/presentation/web/js -name '*.js'); do node --check "$file"; done
docker rm -f geologs-test-db   # when you are done
```

To see how the app copes with a long history, fill an empty database with a believable routine and time what the browser asks for (three years is about 340,000 fixes and 500 MB, and takes about 20 minutes to fill):

```bash
docker exec geologs-test-db psql -h 127.0.0.1 -U geologs -d geologs -c "CREATE DATABASE geologs_load"
GEOLOGS_DATABASE_URL=postgresql://geologs:test@127.0.0.1:55432/geologs_load scripts/load-test --days 1095
GEOLOGS_DATABASE_URL=postgresql://geologs:test@127.0.0.1:55432/geologs_load scripts/load-test --skip-fill   # time it again later
```

The role in that URL must be able to create databases. `postgis/postgis` is published for amd64 only, so on Apple silicon and other arm64 machines the script uses the community multi-arch build `imresamu/postgis` of the same image, which runs the suite about twice as fast as emulation.

The Python tests also check that every file `index.html` loads is served, and that every module import names something its target exports.

### Browser tests

`tests/e2e` drives the real app in Chromium: signing in, one person's page never showing in the next person's session, the Home map fitting each day clear of its cards, import and export, quiet-tracker warnings, and opening offline. They start their own server on a fresh database filled by `scripts/demo-data` (a fictional person in Portland, Oregon; the same data the README's screenshots show), and need internet access for the map libraries. Without Playwright, `GEOLOGS_TEST_DATABASE_URL` or `scripts/demo-data` they skip themselves, so the command above runs them only when all three are there. To set up and run only them:

```bash
.venv/bin/pip install -e '.[browser-tests]' && .venv/bin/playwright install chromium
.venv/bin/python -m unittest discover -s tests -p 'test_browser.py' -v
```

`scripts/demo-data --url http://127.0.0.1:8799 --token "$GEOLOGS_TOKEN"` fills any empty server you can throw away, for trying the app or taking screenshots.

GitHub Actions (`.github/workflows/tests.yml`) runs the Python tests against PostGIS and checks every browser module parses, on each push and pull request.
