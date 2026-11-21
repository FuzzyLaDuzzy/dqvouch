**An LLM proposes data quality tests, code validates them, you vouch for them.**

dqvouch reads what your pipeline already says about each table (schema, descriptions, the SQL that builds it, lineage), lets a language model propose data quality tests, checks every proposal in code against the real schema and data, and shows you the result before anything runs. Then it keeps running the tests you confirmed and tells you when one starts failing.

**New in this version: it is on PyPI**
- Install it with `pip install dqvouch`; upgrade with `pip install -U dqvouch`. Your runs are kept.
- The sidebar shows the version you have installed (0.2.0 still said 0.1.0).

What 0.2.0 brought, change detection on scheduled runs, is in its [release notes](https://github.com/FuzzyLaDuzzy/dqvouch/releases/tag/v0.2.0).

**Install** (Python 3.11 or newer), then open the step-by-step interface:

```bash
pip install dqvouch
dqvouch ui
```

To try it on the fictional orders example, with its planted defects, follow the [Quickstart](https://github.com/FuzzyLaDuzzy/dqvouch#quickstart-fictional-orders-example).
