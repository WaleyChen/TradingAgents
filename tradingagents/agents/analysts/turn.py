"""One turn of a tool-using analyst, and its last turn once its tool rounds are spent."""

import json
import re

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

WRAP_UP = (
    "You have used every tool round this report allows. Write your final report "
    "now from the tool results above, and say which data you could not retrieve."
)


_LEAD = re.compile(
    r"^\s*(?:(?:perfect|great|excellent|awesome|good|alright|okay|ok|sure|certainly|absolutely)\s*[!.,:]"
    r"|(?:now,?\s+)?i (?:now )?(?:have|will|can)\b|i'll\b|i've (?:gathered|collected|got)\b|let me\b"
    r"|here(?:'s| is) (?:the|my) (?:report|analysis|comprehensive|complete|full)\b)", re.IGNORECASE)
_TRAIL = re.compile(r"^\s*(?:let me know|i hope this|feel free|if you(?:'d| would) like|please let me know)",
                    re.IGNORECASE)
_MAX_CHATTER_WORDS = 40


def clean_report(text):
    """The report without the model's talk to itself ("Perfect! Now I have the data") or closing offers.

    Only short paragraphs at the very start or end are removed, so real content is never touched,
    and a text that is nothing but chatter is returned as it came rather than emptied.
    """
    if not isinstance(text, str):
        return text
    paragraphs = re.split(r"\n\s*\n", text.strip())
    start, end = 0, len(paragraphs)
    while start < end - 1 and _LEAD.match(paragraphs[start]) and len(paragraphs[start].split()) <= _MAX_CHATTER_WORDS:
        start += 1
    while end - 1 > start and _TRAIL.match(paragraphs[end - 1]) and len(paragraphs[end - 1].split()) <= _MAX_CHATTER_WORDS:
        end -= 1
    return text if (start, end) == (0, len(paragraphs)) else "\n\n".join(paragraphs[start:end])


def take_turn(prompt, llm, tools, messages):
    """The analyst's reply and its report, which stays empty while it calls tools.

    Told to wrap up, it answers without tools: a model offered tools may call one
    whatever it is told. Its earlier calls and their results reach it as text,
    since a provider may refuse a history holding tool calls when none are bound.
    """
    if messages and isinstance(messages[-1], HumanMessage) and messages[-1].content == WRAP_UP:
        # The prompt lists the tools it offers; a model told of tools it cannot
        # call may still try, and some providers then answer with nothing.
        prompt = prompt.partial(tool_names="none; your tool rounds are spent")
        result = (prompt | llm).invoke(_as_text(messages))
        return result, clean_report(result.content)
    result = (prompt | llm.bind_tools(tools)).invoke(messages)
    return result, "" if result.tool_calls else clean_report(result.content)


def _as_text(messages):
    """The history with its tool calls and tool results written out as plain messages."""
    written = []
    for message in messages:
        if isinstance(message, AIMessage) and message.tool_calls:
            said = message.content if isinstance(message.content, str) else ""
            calls = "; ".join(f"{c['name']}({json.dumps(c['args'], default=str)})" for c in message.tool_calls)
            written.append(AIMessage(content=f"{said}\n[Called {calls}]".strip()))
        elif isinstance(message, ToolMessage):
            written.append(HumanMessage(content=f"[{message.name or 'Tool'} returned]\n{message.content}"))
        else:
            written.append(message)
    return written
