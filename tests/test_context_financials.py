"""Caller-supplied financial statements: schema, period checks and derived metrics."""

from __future__ import annotations

import copy
from datetime import date

import pytest
from pydantic import ValidationError

from tradingagents.context import Financials, check_financials, derive_metrics

B = 1_000_000_000


def d(billions):
    """Whole dollars from billions: amounts are integers."""
    return round(billions * B)


def _quarter(label, start, end, revenue):
    return {"fiscal_period": label, "period_start": start, "period_end": end,
            "kind": "standalone", "source": "sec_xbrl", "filed_at": end,
            "income": {"revenue": revenue}}


GOOD = {
    "ticker": "MU", "currency": "USD", "as_of": "2026-11-04", "fiscal_year_end": "2026-09-03",
    "annual": [{
        "fiscal_period": "FY2026", "period_start": "2025-09-05", "period_end": "2026-09-03",
        "kind": "annual", "source": "sec_xbrl", "filed_at": "2026-10-20",
        "income": {"revenue": d(133.188), "gross_profit": d(107.504),
                   "operating_income": d(99.340), "net_income": d(84.969)},
        "cash_flow": {"net_cash_flow_from_operations": d(89.7),
                      "capital_expenditure": d(30.7), "free_cash_flow": d(59.0)},
    }],
    "quarterly": [
        _quarter("Q1 FY2026", "2025-09-05", "2025-12-04", d(13.643)),
        _quarter("Q2 FY2026", "2025-12-05", "2026-03-05", d(23.860)),
        _quarter("Q3 FY2026", "2026-03-06", "2026-05-28", d(41.460)),
        _quarter("Q4 FY2026", "2026-05-29", "2026-09-03", d(54.225)),
    ],
    "balance": [{
        "fiscal_period": "FY2026", "as_of": "2026-09-03", "source": "sec_xbrl",
        "total_assets": d(195.888), "total_liabilities": d(57.510),
        "shareholders_equity": d(138.378), "cash_and_equivalents": d(38.4),
    }],
}


def financials(**changes):
    data = copy.deepcopy(GOOD)
    data.update(changes)
    return Financials.model_validate(data)


def problems(f):
    return [p.message for p in check_financials(f)]


def mutate(path, value):
    """GOOD with one nested value replaced; path like ('annual', 0, 'income', 'revenue')."""
    data = copy.deepcopy(GOOD)
    node = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return Financials.model_validate(data)


@pytest.mark.unit
def test_a_consistent_set_of_statements_passes():
    assert problems(financials()) == []


@pytest.mark.unit
@pytest.mark.parametrize("bad", [
    {"surprise": 1},
    {"annual": [{**GOOD["annual"][0], "extra": 1}]},
    {"as_of": "not a date"},
])
def test_the_schema_is_closed_and_typed(bad):
    with pytest.raises(ValidationError):
        financials(**bad)


@pytest.mark.unit
def test_unknown_is_null_and_nan_or_infinity_is_refused():
    f = mutate(("annual", 0, "income", "gross_profit"), None)
    assert f.annual[0].income.gross_profit is None
    for bad in (float("nan"), float("inf")):
        with pytest.raises(ValidationError):
            mutate(("annual", 0, "income", "revenue"), bad)


@pytest.mark.unit
def test_a_zero_that_looks_like_a_missing_value_is_refused():
    for path in (("annual", 0, "income", "gross_profit"),
                 ("annual", 0, "income", "operating_income"),
                 ("annual", 0, "income", "net_income"),
                 ("annual", 0, "cash_flow", "capital_expenditure"),
                 ("annual", 0, "cash_flow", "free_cash_flow"),
                 ("balance", 0, "total_liabilities"),
                 ("balance", 0, "shareholders_equity")):
        assert any("looks like a missing value" in p for p in problems(mutate(path, 0))), path


@pytest.mark.unit
def test_a_standalone_or_year_to_date_figure_needs_its_start_date():
    data = copy.deepcopy(GOOD)
    data["quarterly"][0]["period_start"] = None
    found = problems(Financials.model_validate(data))
    assert any("period_start" in p and "Q1 FY2026" in p for p in found)


@pytest.mark.unit
def test_a_figure_is_checked_against_the_length_of_the_period_it_claims():
    data = copy.deepcopy(GOOD)
    data["quarterly"][1]["period_start"] = "2025-09-05"      # nine months called standalone
    assert any("Q2 FY2026" in p and "days" in p for p in problems(Financials.model_validate(data)))
    data = copy.deepcopy(GOOD)
    data["annual"][0]["period_start"] = "2026-03-06"         # a quarter called annual
    assert any("FY2026" in p and "days" in p for p in problems(Financials.model_validate(data)))


@pytest.mark.unit
def test_an_annual_figure_without_a_start_date_is_accepted_for_now():
    data = copy.deepcopy(GOOD)
    data["annual"][0]["period_start"] = None
    assert problems(Financials.model_validate(data)) == []


@pytest.mark.unit
def test_four_quarters_must_add_up_to_the_year():
    found = problems(mutate(("quarterly", 3, "income", "revenue"), d(40)))
    assert any("quarters add up to" in p for p in found)


@pytest.mark.unit
def test_the_quarters_may_not_exceed_the_year():
    data = copy.deepcopy(GOOD)
    data["quarterly"] = data["quarterly"][:3]
    data["quarterly"][2]["income"]["revenue"] = d(120)       # three quarters already over the year
    assert any("more than the annual" in p for p in problems(Financials.model_validate(data)))


@pytest.mark.unit
def test_fewer_than_four_quarters_that_fit_under_the_year_pass():
    data = copy.deepcopy(GOOD)
    data["quarterly"] = data["quarterly"][:3]
    assert problems(Financials.model_validate(data)) == []


@pytest.mark.unit
def test_the_balance_sheet_must_balance():
    found = problems(mutate(("balance", 0, "total_assets"), d(250)))
    assert any("assets" in p and "liabilities" in p for p in found)


@pytest.mark.unit
def test_free_cash_flow_must_match_operating_cash_flow_less_capex():
    assert any("free cash flow" in p for p in problems(mutate(("annual", 0, "cash_flow", "free_cash_flow"), d(80))))
    negative_capex = mutate(("annual", 0, "cash_flow", "capital_expenditure"), -d(30.7))
    assert problems(negative_capex) == []                    # either sign convention is fine


@pytest.mark.unit
def test_income_lines_must_nest_properly():
    assert any("gross profit" in p for p in problems(mutate(("annual", 0, "income", "gross_profit"), d(200))))
    assert any("operating income" in p
               for p in problems(mutate(("annual", 0, "income", "operating_income"), d(110))))


@pytest.mark.unit
def test_duplicate_and_future_periods_are_refused():
    data = copy.deepcopy(GOOD)
    data["quarterly"][1]["period_end"] = data["quarterly"][0]["period_end"]
    assert any("twice" in p for p in problems(Financials.model_validate(data)))
    data = copy.deepcopy(GOOD)
    data["as_of"] = "2026-05-01"
    assert any("after as_of" in p for p in problems(Financials.model_validate(data)))
    data = copy.deepcopy(GOOD)
    data["annual"][0]["filed_at"] = "2026-08-01"
    assert any("filed before" in p for p in problems(Financials.model_validate(data)))


@pytest.mark.unit
def test_derived_metrics_come_from_the_values_not_from_the_caller():
    m = derive_metrics(financials())
    assert m["gross_margin_pct"] == pytest.approx(80.71, abs=0.01)
    assert m["operating_margin_pct"] == pytest.approx(74.58, abs=0.01)
    assert m["net_margin_pct"] == pytest.approx(63.80, abs=0.01)
    assert m["fcf_margin_pct"] == pytest.approx(44.30, abs=0.01)
    assert m["capex_to_revenue_pct"] == pytest.approx(23.05, abs=0.01)
    assert m["liabilities_to_equity"] == pytest.approx(0.4156, abs=0.0001)
    assert m["ttm_revenue"] == pytest.approx(d(133.188))
    assert "debt_to_equity" not in m                          # no debt figure is supplied


@pytest.mark.unit
def test_what_cannot_be_computed_is_left_out_not_guessed():
    data = copy.deepcopy(GOOD)
    data["annual"][0]["income"]["gross_profit"] = None
    data["quarterly"] = data["quarterly"][:3]
    data["balance"] = []
    m = derive_metrics(Financials.model_validate(data))
    for missing in ("gross_margin_pct", "ttm_revenue", "liabilities_to_equity"):
        assert missing not in m
    assert m["net_margin_pct"] == pytest.approx(63.80, abs=0.01)


@pytest.mark.unit
def test_revenue_growth_needs_the_prior_year():
    data = copy.deepcopy(GOOD)
    prior = copy.deepcopy(GOOD["annual"][0])
    prior.update(fiscal_period="FY2025", period_start="2024-08-30", period_end="2025-08-28")
    prior["income"] = {"revenue": d(37.378)}
    prior["cash_flow"] = None
    data["annual"].append(prior)
    assert derive_metrics(Financials.model_validate(data))["revenue_growth_yoy_pct"] == pytest.approx(256.3, abs=0.1)
    assert "revenue_growth_yoy_pct" not in derive_metrics(financials())


@pytest.mark.unit
def test_dates_are_real_dates():
    assert financials().annual[0].period_end == date(2026, 9, 3)


def codes(f):
    return [(p.code, p.path) for p in check_financials(f)]


@pytest.mark.unit
def test_the_data_names_its_company_and_currency():
    assert financials(ticker=" mu ").ticker == "MU"
    for bad in ({"ticker": ""}, {"currency": "EUR"}):
        with pytest.raises(ValidationError):
            financials(**bad)
    data = copy.deepcopy(GOOD)
    del data["currency"]
    with pytest.raises(ValidationError):
        Financials.model_validate(data)


@pytest.mark.unit
def test_amounts_are_whole_dollars():
    assert mutate(("annual", 0, "income", "revenue"), 133_188_000_000.0).annual[0].income.revenue == 133_188_000_000
    with pytest.raises(ValidationError):
        mutate(("annual", 0, "income", "revenue"), 133_188_000_000.5)


@pytest.mark.unit
def test_sources_come_from_a_closed_list_at_each_level():
    with pytest.raises(ValidationError):
        mutate(("annual", 0, "source"), "made_up")
    with pytest.raises(ValidationError):
        financials(source="sec_xbrl")             # one source per statement, none for the whole block
    assert mutate(("annual", 0, "source"), "financial_datasets").annual[0].source == "financial_datasets"


@pytest.mark.unit
def test_a_quarter_may_leave_out_its_start_when_the_one_before_it_fixes_its_length():
    data = copy.deepcopy(GOOD)
    data["quarterly"][1]["period_start"] = None                 # Q1 ended 2025-12-04: 91 days earlier
    assert problems(Financials.model_validate(data)) == []
    data["quarterly"][0]["period_start"] = None                 # nothing before Q1 to measure from
    assert any("Q1 FY2026" in p and "period_start" in p for p in problems(Financials.model_validate(data)))


@pytest.mark.unit
def test_a_start_derived_from_the_previous_quarter_is_still_checked():
    data = copy.deepcopy(GOOD)
    data["quarterly"] = [data["quarterly"][0], data["quarterly"][3]]    # Q1 and Q4: 272 days apart
    data["quarterly"][1]["period_start"] = None
    assert any("Q4 FY2026" in p and "period_start" in p for p in problems(Financials.model_validate(data)))


@pytest.mark.unit
def test_a_year_to_date_figure_always_needs_its_start():
    data = copy.deepcopy(GOOD)
    data["quarterly"].append({**data["quarterly"][2], "fiscal_period": "9M FY2026",
                              "kind": "ytd", "period_start": None, "period_end": "2026-05-28"})
    data["quarterly"][2]["period_end"] = "2026-05-27"
    assert any("9M FY2026" in p and "period_start" in p for p in problems(Financials.model_validate(data)))


def _with_balance(**changes):
    data = copy.deepcopy(GOOD)
    data["balance"].append({**data["balance"][0], "fiscal_period": "Q3 FY2026", "as_of": "2026-05-28", **changes})
    return Financials.model_validate(data)


@pytest.mark.unit
def test_one_balance_sheet_per_date_and_its_label_must_match_the_statement_it_sits_with():
    assert problems(_with_balance()) == []
    data = copy.deepcopy(GOOD)
    data["balance"].append(copy.deepcopy(data["balance"][0]))
    assert any("appears twice" in p for p in problems(Financials.model_validate(data)))
    assert any("Q3 FY2026" in p and "Q2 FY2026" in p
               for p in problems(_with_balance(fiscal_period="Q2 FY2026")))


@pytest.mark.unit
def test_eps_and_current_balances_are_accepted_and_working_capital_is_derived():
    data = copy.deepcopy(GOOD)
    data["annual"][0]["income"].update(eps_basic=75.62, eps_diluted=74.91)
    data["balance"][0].update(current_assets=d(90), current_liabilities=d(25))
    f = Financials.model_validate(data)
    assert f.annual[0].income.eps_diluted == 74.91
    assert derive_metrics(f)["working_capital"] == d(65)
    assert "working_capital" not in derive_metrics(financials())


@pytest.mark.unit
def test_every_problem_has_a_stable_code_and_a_path_to_the_value():
    data = copy.deepcopy(GOOD)
    data["quarterly"][3]["income"]["revenue"] = d(40)
    data["annual"][0]["income"]["gross_profit"] = 0
    data["balance"][0]["total_assets"] = d(250)
    found = codes(Financials.model_validate(data))
    assert ("quarters_mismatch_year", "financials.annual[0]") in found
    assert ("zero_placeholder", "financials.annual[0].income.gross_profit") in found
    assert ("balance_mismatch", "financials.balance[0]") in found


@pytest.mark.unit
def test_other_checks_carry_codes_too():
    data = copy.deepcopy(GOOD)
    data["quarterly"][1]["period_start"] = "2025-09-05"
    data["quarterly"][2]["period_end"] = "2026-12-01"
    data["annual"][0]["filed_at"] = "2026-08-01"
    found = {code for code, _ in codes(Financials.model_validate(data))}
    assert {"period_length", "period_after_as_of", "filed_before_period_end"} <= found
