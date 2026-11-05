from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.analysts.turn import take_turn
from tradingagents.agents.context import get_instrument_context_from_state, get_language_instruction
from tradingagents.agents.structured import SOURCED_NUMBERS, UNEXPLAINED
from tradingagents.agents.tools import (
    get_balance_sheet,
    get_cashflow,
    get_fundamentals,
    get_income_statement,
    get_insider_transactions,
)

# The tools this analyst is offered; its tool node is built from the same tuple.
TOOLS = (
    get_fundamentals,
    get_balance_sheet,
    get_cashflow,
    get_income_statement,
    get_insider_transactions,
)


def _listed(items: list[str]) -> str:
    """'a', 'a and b' or 'a, b, and c'."""
    return items[0] if len(items) == 1 else f"{items[0]} and {items[1]}" if len(items) == 2 \
        else ", ".join(items[:-1]) + f", and {items[-1]}"


def fundamentals_system_message(tools) -> str:
    """The analyst's brief, naming only the tools it has and pointing to supplied data for the rest."""
    have = {tool.name for tool in tools}
    statements = [f"`{n}`" for n in ("get_balance_sheet", "get_cashflow", "get_income_statement") if n in have]
    offered = ([] if "get_fundamentals" not in have else ["`get_fundamentals` for comprehensive company analysis"]
               + ([f"{_listed(statements)} for specific financial statements"] if statements else [])
               + (["`get_insider_transactions` for recent insider buying and selling"]
                  if "get_insider_transactions" in have else []))
    notes = ""
    if not statements:
        notes += (" The company's financial statements, balance sheets and cash flows are in the supplied verified "
                  "data above: use them, and do not look for other copies of those figures.")
    if "get_insider_transactions" not in have:
        notes += " Insider and fund activity is in the supplied verified data above."
    return (
        "You are a researcher tasked with analyzing fundamental information over the past week about a company. Please write a comprehensive report of the company's fundamental information such as financial documents, company profile, basic company financials, and company financial history to gain a full view of the company's fundamental information to inform traders. Make sure to include as much detail as possible. Provide specific, actionable insights with supporting evidence to help traders make informed decisions."
        + " Make sure to append a Markdown table at the end of the report to organize key points in the report, organized and easy to read."
        + (f" Use the available tools: {_listed(offered)}." if offered else "")
        + notes
        + " " + SOURCED_NUMBERS
        + " " + UNEXPLAINED
        + get_language_instruction()
    )


def create_fundamentals_analyst(llm, tools=TOOLS):
    def fundamentals_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = get_instrument_context_from_state(state, "fundamentals")

        system_message = fundamentals_system_message(tools)

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Use the provided tools to progress towards answering the question."
                    " If you are unable to fully answer, that's OK; another assistant with different tools"
                    " will help where you left off. Execute what you can to make progress."
                    " Report what your tools support; another agent decides the trade."
                    " You have access to the following tools: {tool_names}."
                    " Today's date is {current_date}; treat it as 'now' for all analysis and tool-call date ranges. {instrument_context}\n"
                    "{system_message}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(tool_names=", ".join([tool.name for tool in tools]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        result, report = take_turn(prompt, llm, tools, state["messages"])

        return {
            "messages": [result],
            "fundamentals_report": report,
        }

    return fundamentals_analyst_node
