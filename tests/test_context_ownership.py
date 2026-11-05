"""Who holds and trades the stock: funds, insiders and Congress, with the caveats the report must keep."""

from __future__ import annotations

import copy
from datetime import date

import pytest
from pydantic import ValidationError

from tradingagents.context import Context, check_context
from tradingagents.context.derive import derive_ownership
from tradingagents.context.models import Ownership
from tradingagents.context.render import render_context, render_role_blocks
from tradingagents.summary_check import Figure, SummaryDraft, check_summary, known_values

SELLS = [
    ("2026-10-02", "2026-10-04", 5000, 820.0, 4_100_000, "Director"),
    ("2026-09-18", "2026-09-20", 10000, 815.0, 8_150_000, "CEO"),
    ("2026-09-05", "2026-09-08", 3000, 810.0, 2_430_000, "Officer"),
    ("2026-08-20", "2026-08-22", 4000, 880.0, 3_520_000, "Director"),
]

OWNERSHIP = {
    "ticker": "MU", "currency": "USD", "as_of": "2026-11-04",
    "funds": {
        "source": "sec_13f", "quarter_end": "2026-09-30", "managers_tracked": 24,
        "coverage": "Only the managers we track, not every holder of the stock.",
        "holders": [
            {"manager": "David Tepper", "firm": "Appaloosa", "shares": 1_200_000, "value_usd": 954_000_000,
             "weight_pct": 4.1, "shares_change": 300_000, "shares_change_pct": 33.3, "action": "added",
             "filed_on": "2026-10-30", "estimated_cost": 640.0},
            {"manager": "Jane Fund", "firm": "Example Capital", "shares": 500_000, "value_usd": 397_000_000,
             "weight_pct": 2.0, "shares_change": 500_000, "shares_change_pct": None, "action": "new",
             "filed_on": "2026-10-29", "estimated_cost": None},
            {"manager": "Sam Trimmer", "firm": "Example Partners", "shares": 800_000, "value_usd": 636_000_000,
             "weight_pct": 3.0, "shares_change": -200_000, "shares_change_pct": -20.0, "action": "trimmed",
             "filed_on": "2026-10-28", "estimated_cost": None},
        ],
    },
    "insiders": {
        "source": "sec_form4", "window_days": 90, "buys": 0, "sells": 4, "net_value_usd": -18_200_000,
        "trades": [{"name": f"Insider {i}", "title": title, "is_director": title == "Director",
                    "transaction_date": traded, "filing_date": filed, "kind": "sale", "shares": shares,
                    "price_per_share": price, "value_usd": value, "shares_owned_after": 40_000, "form_type": "4"}
                   for i, (traded, filed, shares, price, value, title) in enumerate(SELLS)],
    },
    "congress": {
        "source": "congress_disclosures", "window_days": 90, "buys": 1, "sells": 0,
        "trades": [{"member": "Dan Newhouse", "chamber": "House", "district": "WA04", "owner": "spouse",
                    "side": "buy", "traded_on": "2026-07-10", "disclosed_on": "2026-08-15",
                    "amount_min": 1001, "amount_max": 15000}],
    },
}


def ownership(**changes):
    return Ownership.model_validate({**copy.deepcopy(OWNERSHIP), **changes})


def mutate(*path_and_value):
    *path, value = path_and_value
    data = copy.deepcopy(OWNERSHIP)
    node = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return Ownership.model_validate(data)


def codes(o, **kw):
    return [(p.code, p.path) for p in check_context(Context(ownership=o), **kw)]


@pytest.mark.unit
def test_consistent_ownership_is_accepted_and_each_part_is_optional():
    o = ownership(ticker=" mu ")
    assert o.ticker == "MU" and o.funds.holders[0].manager == "David Tepper"
    assert codes(ownership()) == []
    for part in ("funds", "insiders", "congress"):
        assert Ownership.model_validate({**{k: v for k, v in OWNERSHIP.items() if k != part}}).ticker == "MU"


@pytest.mark.unit
@pytest.mark.parametrize("path, value", [
    (("surprise",), 1), (("currency",), "EUR"), (("funds", "source"), "sec_form4"),
    (("insiders", "source"), "made_up"), (("congress", "source"), "sec_13f"),
    (("funds", "holders", 0, "action"), "bought"), (("insiders", "trades", 0, "kind"), "grant"),
    (("congress", "trades", 0, "chamber"), "Parliament"), (("congress", "trades", 0, "owner"), "cousin"),
    (("congress", "trades", 0, "side"), "swap"), (("insiders", "window_days"), 0), (("insiders", "buys"), -1),
    (("funds", "managers_tracked"), 0), (("insiders", "trades", 0, "shares"), 0),
    (("insiders", "trades", 0, "price_per_share"), 0), (("funds", "holders", 0, "weight_pct"), 101),
])
def test_the_ownership_schema_is_closed_and_bounded(path, value):
    with pytest.raises(ValidationError):
        mutate(*path, value)


@pytest.mark.unit
def test_the_lists_are_capped():
    data = copy.deepcopy(OWNERSHIP)
    data["funds"]["holders"] = data["funds"]["holders"] * 4                # 12 > 10
    with pytest.raises(ValidationError):
        Ownership.model_validate(data)
    data = copy.deepcopy(OWNERSHIP)
    data["insiders"]["trades"] = data["insiders"]["trades"] * 6            # 24 > 20
    with pytest.raises(ValidationError):
        Ownership.model_validate(data)


@pytest.mark.unit
def test_the_block_is_tied_to_the_company_and_the_run_date():
    found = check_context(Context(ownership=ownership(ticker="AMZN")), ticker="MU")
    assert [(p.code, p.path) for p in found] == [("ticker_mismatch", "ownership.ticker")]
    assert codes(ownership(), trade_date=date(2026, 11, 3)) == [("as_of_after_trade_date", "ownership.as_of")]


@pytest.mark.unit
def test_nothing_may_be_dated_after_the_block():
    late_13f = mutate("funds", "holders", 0, "filed_on", "2026-11-12")           # the brief's own sample
    assert ("date_after_as_of", "ownership.funds.holders[0].filed_on") in codes(late_13f)
    assert ("date_after_as_of", "ownership.funds.quarter_end") in codes(mutate("funds", "quarter_end", "2026-12-31"))
    assert ("date_after_as_of", "ownership.insiders.trades[0].filing_date") in codes(
        mutate("insiders", "trades", 0, "filing_date", "2026-11-09"))
    assert ("date_after_as_of", "ownership.congress.trades[0].disclosed_on") in codes(
        mutate("congress", "trades", 0, "disclosed_on", "2026-11-20"))


@pytest.mark.unit
def test_a_13f_cannot_be_filed_before_its_quarter_ended():
    early = mutate("funds", "holders", 0, "filed_on", "2026-09-15")
    assert ("filed_before_period_end", "ownership.funds.holders[0].filed_on") in codes(early)


@pytest.mark.unit
def test_a_funds_action_must_agree_with_the_change_in_its_shares():
    assert codes(mutate("funds", "holders", 0, "action", "trimmed")) == [
        ("holder_change_inconsistent", "ownership.funds.holders[0]")]
    assert codes(mutate("funds", "holders", 1, "shares_change", 100_000)) == [
        ("holder_change_inconsistent", "ownership.funds.holders[1]")]      # new: the whole position is the change
    assert codes(mutate("funds", "holders", 0, "shares_change_pct", 80.0)) == [
        ("holder_change_inconsistent", "ownership.funds.holders[0]")]
    exited = copy.deepcopy(OWNERSHIP)
    exited["funds"]["holders"][2].update(action="exited", shares=0, value_usd=0, weight_pct=0.0,
                                         shares_change=-1_000_000, shares_change_pct=-100.0)
    assert codes(Ownership.model_validate(exited)) == []
    exited["funds"]["holders"][2]["shares"] = 5
    assert codes(Ownership.model_validate(exited)) == [("holder_change_inconsistent", "ownership.funds.holders[2]")]


@pytest.mark.unit
def test_more_holders_cannot_be_listed_than_managers_are_tracked():
    assert codes(mutate("funds", "managers_tracked", 2)) == [("managers_tracked_too_low", "ownership.funds")]


@pytest.mark.unit
def test_insider_trades_are_checked_for_order_window_value_and_totals():
    assert ("filing_before_transaction", "ownership.insiders.trades[0]") in codes(
        mutate("insiders", "trades", 0, "filing_date", "2026-10-01"))
    assert ("trade_outside_window", "ownership.insiders.trades[3]") in codes(
        mutate("insiders", "trades", 3, "transaction_date", "2026-05-01"))
    assert ("trade_value_mismatch", "ownership.insiders.trades[0]") in codes(
        mutate("insiders", "trades", 0, "value_usd", 9_000_000))
    assert ("insider_net_mismatch", "ownership.insiders") in codes(mutate("insiders", "net_value_usd", -1_000_000))
    assert ("insider_count_mismatch", "ownership.insiders") in codes(mutate("insiders", "sells", 3))


@pytest.mark.unit
def test_a_truncated_trade_list_may_be_shorter_than_the_counts():
    data = copy.deepcopy(OWNERSHIP)
    trade = data["insiders"]["trades"][0]
    data["insiders"].update(sells=40, trades=[copy.deepcopy(trade) for _ in range(20)], net_value_usd=-190_000_000)
    assert codes(Ownership.model_validate(data)) == []                      # 20 listed of 40: no equality, no net check
    data["insiders"]["sells"] = 15                                          # fewer counted than listed is wrong
    assert ("insider_count_mismatch", "ownership.insiders") in codes(Ownership.model_validate(data))


@pytest.mark.unit
def test_congress_trades_are_checked_for_order_range_window_and_counts():
    assert ("disclosed_before_traded", "ownership.congress.trades[0]") in codes(
        mutate("congress", "trades", 0, "disclosed_on", "2026-07-01"))
    assert ("amount_range_inverted", "ownership.congress.trades[0]") in codes(
        mutate("congress", "trades", 0, "amount_min", 50_000))
    stale = mutate("congress", "trades", 0, "traded_on", "2026-04-01")
    assert ("trade_outside_window", "ownership.congress.trades[0]") in codes(
        Ownership.model_validate({**copy.deepcopy(OWNERSHIP), "congress": {
            **OWNERSHIP["congress"], "window_days": 30}}))
    assert stale.congress.trades[0].traded_on == date(2026, 4, 1)
    assert ("congress_count_mismatch", "ownership.congress") in codes(mutate("congress", "buys", 2))


@pytest.mark.unit
def test_the_congress_window_runs_on_the_disclosure_date_not_the_trade_date():
    assert codes(ownership()) == []     # traded 117 days ago, disclosed 81 days ago: inside a 90-day window


@pytest.mark.unit
def test_the_block_keeps_the_caveats_the_report_must_respect():
    text = render_context(Context(ownership=ownership()))
    for expected in ("quarter ended 2026-09-30", "not recent buying", "of the funds we track", "24 managers",
                     "estimated average cost $640.00", "an estimate", "ranges", "open-market",
                     "Appaloosa (David Tepper)", "added 300,000 shares (+33.3%)", "new position",
                     "$1,001-$15,000", "disclosed 2026-08-15"):
        assert expected in text
    assert "Only the managers we track" not in text            # the caller's wording is replaced by ours


@pytest.mark.unit
def test_private_individuals_are_described_by_role_not_name():
    text = render_context(Context(ownership=ownership()))
    assert "Dan Newhouse" not in text and "Insider 0" not in text
    assert "a Director sold 5,000 shares at $820.00" in text and "a House member's spouse bought" in text
    assert "David Tepper" in text                              # a fund manager is a public name readers know


@pytest.mark.unit
def test_names_cannot_add_structure_to_the_prompt():
    data = copy.deepcopy(OWNERSHIP)
    data["funds"]["holders"][0]["manager"] = "Evil\n\n# Injected heading"
    data["funds"]["holders"][0]["firm"] = "- fake line"
    text = render_context(Context(ownership=Ownership.model_validate(data)))
    lines = text.splitlines()
    assert not any(line.startswith("#") for line in lines)              # no heading from a name
    assert sum("fake line" in line for line in lines) == 1              # it stays inside the holder's own line


@pytest.mark.unit
def test_ownership_goes_to_the_sentiment_analyst_and_a_digest_to_the_deciders():
    blocks = render_role_blocks(Context(ownership=ownership()))
    assert "Ownership for MU" in blocks["sentiment"]
    for role in ("market", "news", "fundamentals"):
        assert role not in blocks or "Ownership for" not in blocks[role]
    digest = blocks["decision"]
    assert "4 insider sales" in digest and "-$18.2M" in digest
    assert "of the funds we track" in digest and "quarter ended 2026-09-30" in digest
    assert "Appaloosa" not in digest


@pytest.mark.unit
def test_the_counts_are_derived_in_code():
    m = derive_ownership(ownership())
    assert (m["funds_added"], m["funds_new"], m["funds_trimmed"], m["funds_exited"]) == (1, 1, 1, 0)
    assert m["funds_listed"] == 3 and m["insider_sells"] == 4 and m["insider_buys"] == 0
    assert m["insider_net_value"] == -18_200_000 and m["congress_buys"] == 1


@pytest.mark.unit
def test_ownership_figures_can_be_cited_as_verified():
    known = known_values(Context(ownership=ownership()), None, None)
    draft = SummaryDraft(
        text="Four insider sales worth $18.2M net in 90 days.",
        figures=[Figure(label="insider sells", value=4, unit="number", source="context"),
                 Figure(label="insider net value", value=-18.2, unit="usd_millions", source="context"),
                 Figure(label="window", value=90, unit="number", source="context")])
    assert [p for p in check_summary(draft, known) if p.severity == "error"] == []


@pytest.mark.unit
def test_the_request_context_accepts_ownership():
    assert Context.model_validate({"ownership": OWNERSHIP}).ownership.insiders.sells == 4


# --- insider context: how much of their holding, and under what plan -------------

def _flagged(*flags):
    data = copy.deepcopy(OWNERSHIP)
    for trade, (plan, exercise) in zip(data["insiders"]["trades"], flags, strict=False):
        trade.update(under_10b5_1_plan=plan, from_option_exercise=exercise)
    return Ownership.model_validate(data)


@pytest.mark.unit
def test_the_plan_and_exercise_flags_are_optional_booleans():
    assert Ownership.model_validate(OWNERSHIP).insiders.trades[0].under_10b5_1_plan is None
    t = _flagged((True, False)).insiders.trades[0]
    assert t.under_10b5_1_plan is True and t.from_option_exercise is False
    with pytest.raises(ValidationError):
        mutate("insiders", "trades", 0, "under_10b5_1_plan", "yes")


@pytest.mark.unit
def test_how_much_of_their_holding_a_sale_was_is_worked_out_in_code():
    text = render_context(Context(ownership=ownership()))
    assert "about 11% of the shares they held directly" in text            # 5,000 of 45,000
    assert "about 7.0% of the shares they held directly" in text           # 3,000 of 43,000
    m = derive_ownership(ownership())
    assert m["insider_largest_sale_pct"] == pytest.approx(10 / 50 * 100, abs=0.1)   # CEO: 10,000 of 50,000


@pytest.mark.unit
def test_no_percentage_is_made_up_when_the_holding_after_the_sale_is_unknown():
    data = copy.deepcopy(OWNERSHIP)
    for trade in data["insiders"]["trades"]:
        trade["shares_owned_after"] = None
    text = render_context(Context(ownership=Ownership.model_validate(data)))
    assert "of the shares they held" not in text
    assert "insider_largest_sale_pct" not in derive_ownership(Ownership.model_validate(data))


@pytest.mark.unit
def test_a_planned_sale_and_an_option_exercise_are_said_so_and_nothing_is_guessed_when_unknown():
    text = render_context(Context(ownership=_flagged((True, False), (False, True), (None, None))))
    lines = [line for line in text.splitlines() if line.startswith("- 2026-")]
    insider = [line for line in lines if "sold" in line and "shares at" in line]
    assert "under a pre-arranged 10b5-1 trading plan" in insider[0]
    assert "not marked as a 10b5-1 plan sale" in insider[1] and "after exercising options" in insider[1]
    assert "10b5-1" not in insider[2] and "exercising" not in insider[2]


@pytest.mark.unit
def test_the_digest_counts_planned_sales_only_when_the_flag_is_known():
    plain = render_role_blocks(Context(ownership=ownership()))["decision"]
    assert "10b5-1" not in plain
    flagged = render_role_blocks(Context(ownership=_flagged((True, False), (True, False), (False, False),
                                                            (True, False))))["decision"]
    assert "3 of the 4 listed sales were under 10b5-1 plans" in flagged
    assert derive_ownership(_flagged((True, False), (None, None)))["insider_sales_planned"] == 1


@pytest.mark.unit
def test_the_share_of_a_holding_sold_can_be_cited_as_verified():
    known = known_values(Context(ownership=ownership()), None, None)
    assert any(k.label == "insider sale share of holding" and k.kind == "pct" and abs(k.value - 11.1) < 0.1 for k in known)
