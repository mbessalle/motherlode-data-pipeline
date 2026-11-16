"""The two LLM calls (classify location, extract deal terms), traced with GenAI semantic-convention attributes."""
import json

from pydantic import ValidationError

from . import telemetry
from .prompts import CLASSIFIER_SYSTEM_PROMPT, EXTRACTOR_SYSTEM_PROMPT, TRIAGE_SYSTEM_PROMPT
from .schema import TriageVerdict

# 25,000 characters keeps every value of the seed-7 sample (the latest analyst value sat at character 23,373);
# a longer document keeps its deal-term passages (deal_window).
ARTICLE_CHAR_LIMIT = 25000
TRIAGE_CHAR_LIMIT = 4000


DEAL_TERMS = ("consideration", "cash", "payment", "shares", "royalty", "nsr", "earn", "option", "interest", "expenditure",
              "acquire", "acquisition", "vendor", "hectares", "km2", "tenement", "agreement", "terms")
WINDOW_CHUNK = 1500
OPENING_CHARS = 3000


def deal_window(text: str, limit: int = ARTICLE_CHAR_LIMIT) -> tuple[str, bool]:
    """(text to send, whether it was cut). A document longer than `limit` keeps its opening and then the chunks
    with the most deal terms, in document order, marked "[...]" where text was left out. Cutting at the first
    `limit` characters lost deal terms written later (critic F9: five analyst values after character 15,000)."""
    if len(text) <= limit:
        return text, False
    opening, rest = text[:OPENING_CHARS], text[OPENING_CHARS:]
    chunks = [(i, rest[i:i + WINDOW_CHUNK]) for i in range(0, len(rest), WINDOW_CHUNK)]
    scored = sorted(chunks, key=lambda c: -sum(c[1].lower().count(term) for term in DEAL_TERMS))
    keep, size = [], len(opening)
    for i, chunk in scored:
        if size + len(chunk) + 7 > limit:
            break
        keep.append((i, chunk))
        size += len(chunk) + 7
    parts, last_end = [opening], 0
    for i, chunk in sorted(keep):
        parts.append(chunk if i == last_end else " [...] " + chunk)
        last_end = i + WINDOW_CHUNK
    return "".join(parts), True


def _chat_json(client, model: str, system: str, user: str, operation: str) -> dict:
    """One JSON-mode chat completion. Returns the parsed object, or {"_error": type} if the call or the parsing
    fails, so a caller can tell a failed call from a reply."""
    with telemetry.span(
        f"chat {model}",
        **{"gen_ai.operation.name": "chat", "gen_ai.provider.name": "openai", "gen_ai.request.model": model,
           "pipeline.llm.purpose": operation},
    ) as s:
        if telemetry.record_content():
            s.set_attribute("gen_ai.input.messages", telemetry.clip(json.dumps(
                [{"role": "system", "chars": len(system)}, {"role": "user", "content": user}], ensure_ascii=False)))
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                response_format={"type": "json_object"},
            )
            usage = getattr(response, "usage", None)
            if usage is not None:
                s.set_attribute("gen_ai.usage.input_tokens", usage.prompt_tokens)
                s.set_attribute("gen_ai.usage.output_tokens", usage.completion_tokens)
            s.set_attribute("gen_ai.response.model", getattr(response, "model", model) or model)
            content = response.choices[0].message.content
            if telemetry.record_content():
                s.set_attribute("gen_ai.output.messages", telemetry.clip(content or ""))
            return json.loads(content)
        except Exception as error:  # a failed call must not stop the batch, and must not look like a reply
            s.record_exception(error)
            s.set_attribute("error.type", type(error).__name__)
            return {"_error": type(error).__name__}


def classify_location(client, model: str, context: str) -> dict:
    """Country and continent for an article, or nulls when the text does not say."""
    if not context.strip():
        return {"country": None, "continent": None}
    data = _chat_json(client, model, CLASSIFIER_SYSTEM_PROMPT, context, "classify_location")
    if "_error" in data:      # a failed call is not "no continent" (critic N3): the run holds it for a retry
        return {"country": None, "continent": None, "error": data["_error"]}
    return {"country": data.get("country"), "continent": data.get("continent")}


def triage(client, model: str, title: str, company: str, announcement_text: str) -> dict:
    """Is this announcement a project transaction, and which project? A failed call or a reply that is not the
    agreed shape is "no" with an `error`, so the run reports it as a failure and retries it, not as a real "no"."""
    user = f"Headline: {title}\nCompany: {company}\nAnnouncement (start):\n---\n{announcement_text[:TRIAGE_CHAR_LIMIT]}\n---"
    data = _chat_json(client, model, TRIAGE_SYSTEM_PROMPT, user, "triage")
    if "_error" in data:
        return {**TriageVerdict().model_dump(), "error": data["_error"]}
    try:
        return TriageVerdict.model_validate(data).model_dump()
    except ValidationError:
        return {**TriageVerdict().model_dump(), "error": "malformed reply"}


def build_extraction_prompt(article_text: str, project_record: dict | None, feedback: str | None = None) -> str:
    record = project_record or {}
    existing = "\n".join(f"- {key}: {value}" for key, value in record.items() if value is not None)
    prompt = f"""
    **Primary Project of Interest:** '{record.get('project_name')}'
    **Existing Database Record (if any):**
    {existing}
    **News Article Content:**
    ---
    {deal_window(article_text)[0]}
    ---
    **Task:**
    Execute your cognitive workflow. Produce the mandatory two-part JSON object containing your reasoning and the final database updates.
    """
    if feedback:
        prompt += f"""
    **Correction required:** an automatic check could not find these values in the article above:
    {feedback}
    Re-read the article. Keep a value only if the article states it or it follows from stated numbers by a rule above; otherwise set the field to null.
    """
    return prompt


def extract_deal(client, model: str, article_text: str, project_record: dict | None, feedback: str | None = None) -> dict:
    """Deal terms for the project, as the `database_updates` object the prompt asks for."""
    data = _chat_json(client, model, EXTRACTOR_SYSTEM_PROMPT, build_extraction_prompt(article_text, project_record, feedback),
                      "extract_deal")
    if "_error" in data:
        return {"_error": data["_error"]}       # the graph tells a failed call from an empty reply
    return data.get("database_updates", {}) or {}
