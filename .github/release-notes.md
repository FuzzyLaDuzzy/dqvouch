**An LLM proposes data quality tests, code validates them, you vouch for them.** The first release.

dqvouch reads what your pipeline already says about each table (schema, descriptions, the SQL that builds it, lineage), lets a language model propose data quality tests, checks every proposal in code against the real schema and data, and shows you the result before anything runs. Then it keeps running the tests you confirmed and tells you when one starts failing.

- **PostgreSQL and ClickHouse**, read-only, enforced in the code and in the database session.
- **The model never writes SQL**: it fills a closed catalog of five assertions, and every threshold has to cite evidence that resolves.
- **You decide** on the Review screen: confirm, edit or exclude each test; rules the data contradicts are flagged, not dropped.
- **Gemini or a model on your own machine** (Ollama, LM Studio, vLLM or any OpenAI-compatible server).
- **Keeps watch**: schedules in the app, or `dqvouch rerun` from cron, Task Scheduler, Airflow or CI, with alerts by webhook or email and each test's trend over time.
- **Export** the confirmed tests as one SQL file in your database's dialect.

On a real six-table pipeline, against 18 tests written by its data engineers, asking the model what each SQL transformation guarantees raised the share of their tests found from 39% to 67% (from the MSc dissertation behind it, University of Minho, 2026).

**Install** (Python 3.11 or newer), then open the step-by-step interface:

```bash
pip install https://github.com/FuzzyLaDuzzy/dqvouch/releases/download/v0.1.0/dqvouch-0.1.0-py3-none-any.whl
dqvouch ui
```

To try it on the fictional orders example, with its planted defects, follow the [Quickstart](https://github.com/FuzzyLaDuzzy/dqvouch#quickstart-fictional-orders-example).
