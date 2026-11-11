"""A scripted stand-in for Gemini over the orders example: hand-written answers, routed on the prompt text.
It tests the pipeline's plumbing, not the model."""
import json

DIMENSIONS = {
    "tables": [
        {"table": "staging.orders_trans",
         "dimensions": [{"dimension": "Uniqueness", "columns": ["order_id"], "rationale": "order_id names one order"}]},
        {"table": "analytic.fact_orders",
         "dimensions": [{"dimension": "Validity", "columns": ["status"], "rationale": "status is an open String"}]},
    ],
    "cross_table_checks": [],
}

SPECS = {
    "test_specifications": [
        {"id": "orders_trans_order_id_unique", "asset": "staging.orders_trans", "dimension": "Uniqueness",
         "assertion": {"type": "unique", "columns": ["order_id"]},
         "threshold": {"metric": "failed_rows", "operator": "equals", "value": 0},
         "evidence": [{"path": "columns.order_id.semantic_roles"}],
         "rationale": "order_id names one order", "status": "proposed"},
        {"id": "fact_orders_status_domain", "asset": "analytic.fact_orders", "dimension": "Validity",
         "assertion": {"type": "row_predicate", "condition": {
             "op": "in", "left": {"column": "status"},
             "values": ["pending", "shipped", "delivered", "cancelled", "unknown"]}},
         "threshold": {"metric": "failed_rows", "operator": "equals", "value": 0},
         "evidence": [{"path": "transformation.raw_sql"}],
         "rationale": "statuses written by the SQL", "status": "proposed"},
    ],
    "metric_thresholds": [],
}


def _fenced(obj) -> str:
    return "```json\n" + json.dumps(obj) + "\n```"


class FakeLLM:
    def __init__(self):
        self.prompts = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if "specializing in analytical pipelines" in prompt:
            return _fenced(DIMENSIONS)
        if "reading ONE SQL transformation" in prompt:
            return _fenced({"invariants": []})
        return _fenced(SPECS)
