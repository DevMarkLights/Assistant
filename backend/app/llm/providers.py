# Loads models.yaml: the LLM providers (one OpenAI-compatible client each), their models with
# capabilities and free-tier limits, and the ordered model list for each task. The router
# (router.py) picks from these; nothing else in the app names a provider or model.
import os
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import yaml
from openai import AsyncOpenAI

from app.core.config import BACKEND_DIR, LOCAL
from app.core.logs import get_logger

log = get_logger("llm")

CONFIG_PATH = Path(os.getenv("MODELS_CONFIG", BACKEND_DIR / "models.yaml"))
TIMEOUT = httpx.Timeout(120.0, connect=5.0)
CAPABILITIES = ("tools", "json_mode", "vision")


@dataclass
class Provider:
    name: str
    base_url: str
    key_env: str
    allow_personal_data: bool = False
    policy_url: str = ""
    api_key: str | None = None
    # `client` (chat) has no SDK retries: the chat loop handles errors and failover itself, since
    # the SDK quietly waits out a rate limit (up to a minute) before retrying. `background_client`
    # keeps the SDK's two retries for background JSON calls, as before the router (Stage 1).
    client: AsyncOpenAI | None = None
    background_client: AsyncOpenAI | None = None


@dataclass
class Model:
    key: str  # the name used in models.yaml, e.g. "gpt-oss-120b"
    provider: Provider
    id: str  # what the provider's API expects, e.g. "openai/gpt-oss-120b"
    capabilities: dict
    limits: dict
    options: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        """For logs: "groq/openai/gpt-oss-120b"."""
        return f"{self.provider.name}/{self.id}"

    def can(self, capability: str) -> bool:
        return bool(self.capabilities.get(capability))


@dataclass
class Task:
    name: str
    personal_data: bool
    needs: list[str]
    models: list[Model]


@dataclass
class Config:
    providers: dict[str, Provider]
    models: dict[str, Model]
    tasks: dict[str, Task]


def _require(mapping: dict, key: str, where: str):
    if key not in mapping:
        raise ValueError(f"{CONFIG_PATH.name}: {where} is missing {key!r}")
    return mapping[key]


def load(path: Path = CONFIG_PATH, env=os.environ) -> Config:
    """Parses and validates the config; raises ValueError naming the problem."""
    raw = yaml.safe_load(path.read_text()) or {}
    providers = {}
    for name, p in (raw.get("providers") or {}).items():
        provider = Provider(name, _require(p, "base_url", f"provider {name}"), _require(p, "key_env", f"provider {name}"),
                            bool(p.get("allow_personal_data", False)), p.get("policy_url", ""))
        provider.api_key = env.get(provider.key_env) or None
        providers[name] = provider
    models = {}
    for key, m in (raw.get("models") or {}).items():
        provider_name = _require(m, "provider", f"model {key}")
        if provider_name not in providers:
            raise ValueError(f"{path.name}: model {key} uses unknown provider {provider_name!r}")
        models[key] = Model(key, providers[provider_name], _require(m, "id", f"model {key}"),
                            m.get("capabilities") or {}, m.get("limits") or {}, m.get("options") or {})
    tasks = {}
    for name, t in (raw.get("tasks") or {}).items():
        needs = t.get("needs") or []
        if unknown := [n for n in needs if n not in CAPABILITIES]:
            raise ValueError(f"{path.name}: task {name} needs unknown capabilities {unknown}")
        listed = _require(t, "models", f"task {name}")
        if missing := [k for k in listed if k not in models]:
            raise ValueError(f"{path.name}: task {name} lists unknown models {missing}")
        tasks[name] = Task(name, bool(t.get("personal_data", True)), needs, [models[k] for k in listed])
    return Config(providers, models, tasks)


def connect(config: Config) -> None:
    """Creates each provider's clients; providers without a key are left unconnected (skipped)."""
    for provider in config.providers.values():
        if not provider.api_key:
            log.warning("provider %s has no key (%s is not set): its models won't be used",
                        provider.name, provider.key_env)
            continue
        provider.client = AsyncOpenAI(api_key=provider.api_key, base_url=provider.base_url,
                                      timeout=TIMEOUT, max_retries=0)
        provider.background_client = provider.client.with_options(max_retries=2)


config: Config | None = None
if not LOCAL:
    config = load()
    connect(config)
    for task in config.tasks.values():
        if not any(m.provider.client for m in task.models):
            raise RuntimeError(f"LOCAL=false, but task {task.name} has no model with an API key "
                               f"({', '.join(sorted({m.provider.key_env for m in task.models}))} in .env)")
