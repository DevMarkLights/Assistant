# Chooses which model serves each request. Every cloud LLM call names a task (chat_tools,
# background_json, titles, morning_brief, from models.yaml); the router returns that task's models
# in order of preference, keeping only those that:
#   - have the capabilities the task needs (tools, json_mode, ...);
#   - may receive the task's data (a provider gets personal data only with allow_personal_data);
#   - belong to a provider with an API key.
# Every request it serves is logged with its provider and model.
from app.core.logs import get_logger
from app.llm import providers
from app.llm.providers import Model

log = get_logger("llm")


class NoModelAvailable(Exception):
    """No configured model can serve the task (missing capabilities, keys, or data permission)."""


def candidates(task_name: str) -> list[Model]:
    task = providers.config.tasks[task_name]
    usable = [m for m in task.models
              if all(m.can(need) for need in task.needs)
              and (m.provider.allow_personal_data or not task.personal_data)
              and m.provider.client is not None]
    if not usable:
        raise NoModelAvailable(f"no model can serve {task_name}: check its models' capabilities, "
                               "API keys and allow_personal_data in models.yaml")
    return usable


def served(task_name: str, model: Model, purpose: str, usage) -> int:
    """Logs one finished request: which provider and model served which task, and its tokens.
    Returns the request's total tokens (0 if the provider didn't report them)."""
    prompt, completion, total = _counts(usage)
    log.info("router: %s (%s) served by %s: %d in + %d out = %d tokens",
             task_name, purpose, model.label, prompt, completion, total)
    return total


def _counts(usage) -> tuple[int, int, int]:
    """Token counts from a usage object or dict (Groq's streamed x_groq.usage arrives as a dict)."""
    if not usage:
        return 0, 0, 0
    get = usage.get if isinstance(usage, dict) else (lambda k: getattr(usage, k, 0))
    prompt, completion = get("prompt_tokens") or 0, get("completion_tokens") or 0
    return prompt, completion, get("total_tokens") or prompt + completion
