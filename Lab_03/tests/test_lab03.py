"""Kiểm thử nhanh harness và 3 agent. Chạy: python -m pytest -q  (hoặc python tests/test_lab03.py)"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flight_agent.evaluate import run_one  # noqa: E402
from flight_agent.harness import FlightHarness, LoopDetector, StopKind  # noqa: E402
from flight_agent.scenarios import SCENARIOS  # noqa: E402
from flight_agent.tools_mock import FlightBackend  # noqa: E402


def _h(sid="co-ban", **kw):
    sc = SCENARIOS[sid]
    return FlightHarness(FlightBackend(faults=sc.faults), sc.constraints, **kw)


def test_permission_denies_hallucinated_flight():
    h = _h()
    h.call("search_flights", {"origin": "SGN", "destination": "DAD", "date": "2026-10-07"})
    obs = h.call("book_seat", {"flight_no": "VN999", "passenger": "NGUYEN VAN A"})
    assert obs["status"] == "denied" and not h.backend.bookings


def test_permission_denies_constraint_violation():
    h = _h()
    h.call("search_flights", {"origin": "SGN", "destination": "DAD", "date": "2026-10-07"})
    h.call("check_seat", {"flight_no": "QH118"})
    obs = h.call("book_seat", {"flight_no": "QH118", "passenger": "NGUYEN VAN A"})
    assert obs["status"] == "denied" and "sai giờ" in obs["reason"]


def test_needs_approval_without_human_stops_and_hands_off():
    h = _h("can-duyet")
    h.call("search_flights", {"origin": "HAN", "destination": "SGN", "date": "2026-10-10"})
    h.call("check_seat", {"flight_no": "VN210"})
    obs = h.call("book_seat", {"flight_no": "VN210", "passenger": "NGUYEN VAN A"})
    assert obs["status"] == "pending_approval" and h.stop_kind == StopKind.NEEDS_HUMAN
    assert h.handoff and "Duyệt" in h.handoff.question and not h.backend.bookings


def test_loop_detector_fires_on_third_identical_call():
    d = LoopDetector(window=6, repeat_k=3)
    assert d.check("check_seat", {"flight_no": "VN122"}, 0) is None
    assert d.check("check_seat", {"flight_no": "VN122"}, 0) is None
    assert d.check("check_seat", {"flight_no": "VN122"}, 0) == "LOOP"
    assert LoopDetector().check("get_booking", {"booking_code": "X"}, 0) is None  # polling hợp lệ


def test_completion_is_checked_by_code_not_by_model():
    h = _h()
    h.call("search_flights", {"origin": "SGN", "destination": "DAD", "date": "2026-10-07"})
    h.call("check_seat", {"flight_no": "VN122"})
    held = h.call("book_seat", {"flight_no": "VN122", "passenger": "NGUYEN VAN A"})
    assert h.is_done()[0] is False  # mới held, chưa trả tiền
    assert any("is_done() = False" in p for p in h.verify_answer("Đã đặt vé thành công VN122"))
    h.call("pay", {"booking_code": held["booking_code"], "method": "corp_card"})
    assert h.is_done()[0] is True and h.stop_kind == StopKind.GOAL


def test_clean_runs_match_expected():
    expected_fail = {("het-cho", "pe")}  # plan-then-execute không lập lại kế hoạch -> bàn giao
    for sid, sc in SCENARIOS.items():
        for d in ("react", "pe", "hybrid"):
            r, _h2, _a = run_one(d, sc)
            assert r.outcome not in ("violation", "false_claim", "error"), (sid, d, r)
            assert r.passed == ((sid, d) not in expected_fail), (sid, d, r.outcome, r.detail)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
