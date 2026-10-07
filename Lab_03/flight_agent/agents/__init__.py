from .hybrid import run_hybrid
from .plan_execute import run_plan_execute
from .react import run_react

DESIGNS = {"react": run_react, "pe": run_plan_execute, "hybrid": run_hybrid}
DESIGN_LABEL = {"react": "ReAct", "pe": "Plan-then-Execute", "hybrid": "Lai (ReAct + Plan)"}

__all__ = ["DESIGNS", "DESIGN_LABEL", "run_hybrid", "run_plan_execute", "run_react"]
