**An LLM proposes data quality tests, code validates them, you vouch for them.**

dqvouch reads what your pipeline already says about each table (schema, descriptions, the SQL that builds it, lineage), lets a language model propose data quality tests, checks every proposal in code against the real schema and data, and shows you the result before anything runs. Then it keeps running the tests you confirmed and tells you when one starts failing.

**New in this version: it notices when your database changes**
- **After every scheduled execution**, once the tests ran, dqvouch checks each table's structure (columns, types, descriptions, the SQL that builds it, the tables it reads from or feeds) against the run you reviewed. It only reads metadata: no model call when nothing changed.
- **New tests only for what changed**: a new run in which the model only looks at the changed and new tables. Every test you confirmed is kept exactly as you reviewed it; a test whose column or table is gone is set aside, with the reason.
- **Nothing new runs until you confirm it.** You get one alert per change, and the old tests keep running every day meanwhile.
- **The schedule follows**: once you review the new run, the schedule and the history move to it, and a cron or Task Scheduler line that names the old run keeps working.
- **Only want to be told?** Set `propose_on_change = false` and a schedule never calls the model.
- **A model outage no longer holds a schedule**: proposing gives up after a few attempts, the tests' results and exit code stand, and dqvouch tries again at the next execution.

**Install** (Python 3.11 or newer), then open the step-by-step interface:

```bash
pip install https://github.com/FuzzyLaDuzzy/dqvouch/releases/download/v0.2.0/dqvouch-0.2.0-py3-none-any.whl
dqvouch ui
```

To try it on the fictional orders example, with its planted defects, follow the [Quickstart](https://github.com/FuzzyLaDuzzy/dqvouch#quickstart-fictional-orders-example). Upgrading from 0.1.0 keeps your runs: install over it.
