"""Hand-computed cases and invariant checks for the reference tracer.

Amounts are written in rupees and converted to paise with R().
"""

import random

import pytest

from reference.tracer import (
    MODES,
    Tracer,
    allocate,
    cashout,
    fraud,
    income,
    split_largest_remainder,
    transfer,
)
from sim.simulate import generate_case


def R(rupees: int) -> int:
    return rupees * 100


def eighty_twenty_events():
    # A holds 80,000 of its own money, then receives 20,000 stolen from V1,
    # then sends 50,000 to B.
    return [
        income(1, 1, "A", R(80_000)),
        fraud(2, 2, "V1", "A", R(20_000)),
        transfer(3, 3, "A", "B", R(50_000)),
    ]


def test_80k_20k_proportional_example():
    t = Tracer("proportional").run(eighty_twenty_events())
    # The 50,000 carries 20% stolen money: 10,000 stolen + 40,000 own.
    assert t.stolen("B") == {"V1": R(10_000)}
    assert t.own("B") == R(40_000)
    # A keeps 40,000 own + 10,000 stolen.
    assert t.own("A") == R(40_000)
    assert t.stolen("A") == {"V1": R(10_000)}
    assert t.lien("A") == R(10_000)
    assert t.lien("B") == R(10_000)
    t.check_invariants()


def test_80k_20k_fifo_differs_from_proportional():
    t = Tracer("fifo").run(eighty_twenty_events())
    # FIFO spends the oldest money first: the 50,000 is all of A's own money.
    assert t.stolen("B") == {}
    assert t.own("B") == R(50_000)
    assert t.lien("A") == R(20_000)
    assert t.lien("B") == 0
    t.check_invariants()


def test_80k_20k_full_freeze_freezes_whole_balances():
    t = Tracer("full_freeze").run(eighty_twenty_events())
    assert t.lien("A") == R(50_000)
    assert t.lien("B") == R(50_000)
    assert t.legit_frozen("A") == R(40_000)
    assert t.legit_frozen("B") == R(40_000)


def test_full_freeze_ignores_accounts_that_never_received_stolen_money():
    events = eighty_twenty_events() + [income(4, 4, "C", R(1_00_000))]
    t = Tracer("full_freeze").run(events)
    assert t.lien("C") == 0


def test_two_victim_split_and_allocation():
    events = [
        fraud(1, 1, "V1", "M", R(30_000)),
        fraud(2, 2, "V2", "M", R(10_000)),
        transfer(3, 3, "M", "N", R(20_000)),
    ]
    t = Tracer("proportional").run(events)
    # M holds 30k (V1) + 10k (V2); half its balance moves, so half of each share moves.
    assert t.stolen("N") == {"V1": R(15_000), "V2": R(5_000)}
    assert t.stolen("M") == {"V1": R(15_000), "V2": R(5_000)}
    # 8,000 recovered from N is split 3:1.
    assert t.allocate("N", R(8_000)) == {"V1": R(6_000), "V2": R(2_000)}
    t.check_invariants()


def test_cashout_removes_money_from_the_system():
    events = [
        income(1, 1, "A", R(20_000)),
        fraud(2, 2, "V1", "A", R(20_000)),
        cashout(3, 3, "A", R(10_000)),
    ]
    t = Tracer("proportional").run(events)
    # Half the balance is stolen, so half of the 10,000 cash-out is V1's money.
    assert t.cashed_out == {"V1": R(5_000)}
    assert t.stolen("A") == {"V1": R(15_000)}
    assert t.own("A") == R(15_000)
    assert t.lien("A") == R(15_000)
    assert t.recoverable("V1") == R(15_000)
    t.check_invariants()


def test_allocation_rounding_is_exact_and_deterministic():
    # Three equal shares of 1 paisa, 2 paise recovered: ties go by sorted victim id.
    assert allocate({"V3": 1, "V1": 1, "V2": 1}, 2) == {"V1": 1, "V2": 1, "V3": 0}
    split = allocate({"V1": 7, "V2": 3}, 5)
    assert split == {"V1": 4, "V2": 1}  # 3.5 -> 3 + tie paisa to V1; 1.5 -> 1
    assert sum(split.values()) == 5


def test_allocation_cannot_exceed_traced_money():
    with pytest.raises(ValueError):
        allocate({"V1": 100}, 101)


def test_proportional_rounding_conserves_paise():
    events = [
        income(1, 1, "A", 1),
        fraud(2, 2, "V1", "A", 1),
        fraud(3, 3, "V2", "A", 1),
        transfer(4, 4, "A", "B", 2),
    ]
    t = Tracer("proportional").run(events)
    # Three 1-paisa components, 2 paise out: ties go to V1 then V2, own last.
    assert t.stolen("B") == {"V1": 1, "V2": 1}
    assert t.own("A") == 1
    t.check_invariants()


def test_split_largest_remainder_never_exceeds_weights():
    rng = random.Random(7)
    for _ in range(2_000):
        weights = [rng.randint(0, 1_000) for _ in range(rng.randint(1, 6))]
        total = rng.randint(0, sum(weights))
        parts = split_largest_remainder(total, weights)
        assert sum(parts) == total
        assert all(0 <= p <= w for p, w in zip(parts, weights))


def test_overdraft_is_rejected():
    with pytest.raises(ValueError):
        Tracer().run([income(1, 1, "A", 100), transfer(2, 2, "A", "B", 101)])


def test_events_are_applied_in_timestamp_order():
    events = eighty_twenty_events()
    a = Tracer().run(events)
    b = Tracer().run(list(reversed(events)))
    assert a.stolen("B") == b.stolen("B") and a.own("B") == b.own("B")


@pytest.mark.parametrize("mode", MODES)
def test_invariants_on_simulated_cases(mode):
    for cid in range(1, 101):
        case = generate_case(cid, random.Random(cid))
        t = Tracer(mode).run(case.events)
        # Never negative, never above the original loss, and exact conservation.
        t.check_invariants()
        for v, loss in t.losses.items():
            remaining = t.recoverable(v)
            assert 0 <= remaining <= loss
            assert remaining + t.cashed_out.get(v, 0) == loss
        # Allocation sums exactly and respects each victim's traced share.
        for acc in t.accounts():
            stolen = t.stolen(acc)
            if stolen:
                recovered = sum(stolen.values()) * 2 // 3
                split = t.allocate(acc, recovered)
                assert sum(split.values()) == recovered
                assert all(split[v] <= stolen[v] for v in split)


@pytest.mark.parametrize("mode", MODES)
def test_determinism(mode):
    for cid in range(1, 21):
        case1 = generate_case(cid, random.Random(cid))
        case2 = generate_case(cid, random.Random(cid))
        t1, t2 = Tracer(mode).run(case1.events), Tracer(mode).run(case2.events)
        assert case1.events == case2.events
        for acc in t1.accounts():
            assert (t1.own(acc), t1.stolen(acc), t1.lien(acc)) == (t2.own(acc), t2.stolen(acc), t2.lien(acc))


def test_traced_modes_never_freeze_legitimate_money():
    for cid in range(1, 51):
        case = generate_case(cid, random.Random(cid))
        for mode in ("proportional", "fifo"):
            t = Tracer(mode).run(case.events)
            assert all(t.legit_frozen(a) == 0 for a in t.accounts())
