from alora_evv.ledger import Ledger


def test_submitting_clears_the_decision_and_undo_works():
    ledger = Ledger()
    ledger.decide("1000000001", "AUTH", "web: test")
    ledger.mark_submitted("1000000001")
    assert ledger.decisions() == {}
    assert "1000000001" in ledger.recently_submitted(1)
    ledger.unmark_submitted("1000000001")
    assert ledger.recently_submitted(1) == {}
