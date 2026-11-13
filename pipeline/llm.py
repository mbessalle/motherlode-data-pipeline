"""The two LLM calls (classify location, extract deal terms), traced with GenAI semantic-convention attributes."""
import json

from . import telemetry
from .prompts import CLASSIFIER_SYSTEM_PROMPT, EXTRACTOR_SYSTEM_PROMPT

ARTICLE_CHAR_LIMIT = 15000


def _chat_json(client, model: str, system: str, user: str, operation: str) -> dict:
    """One JSON-mode chat completion. Returns the parsed object, or {} if the call or the parsing fails."""
    with telemetry.span(
        f"chat {model}",
        **{"gen_ai.operation.name": "chat", "gen_ai.provider.name": "openai", "gen_ai.request.model": model,
           "pipeline.llm.purpose": operation},
    ) as s:
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
            return json.loads(response.choices[0].message.content)
        except Exception as error:  # a failed call must not stop the batch; the caller treats {} as "nothing extracted"
            s.record_exception(error)
            s.set_attribute("error.type", type(error).__name__)
            return {}


def classify_location(client, model: str, context: str) -> dict:
    """Country and continent for an article, or nulls when the text does not say."""
    if not context.strip():
        return {"country": None, "continent": None}
    data = _chat_json(client, model, CLASSIFIER_SYSTEM_PROMPT, context, "classify_location")
    return {"country": data.get("country"), "continent": data.get("continent")}


def build_extraction_prompt(article_text: str, project_record: dict | None, feedback: str | None = None) -> str:
    record = project_record or {}
    existing = "\n".join(f"- {key}: {value}" for key, value in record.items() if value is not None)
    prompt = f"""
    **Primary Project of Interest:** '{record.get('project_name')}'
    **Existing Database Record (if any):**
    {existing}
    **News Article Content:**
    ---
    {article_text[:ARTICLE_CHAR_LIMIT]}
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
    return data.get("database_updates", {}) or {}
