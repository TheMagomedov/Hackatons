"""Explicit AgentRouter API-key transport through the genuine native Codex CLI.

No desktop login, personal configuration, shell tools, MCP servers or fallback.
The child receives only the redacted request in stdin and a short-lived home.
"""

import asyncio
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from app.ai.chatgpt import ChatGPTClient, strict_schema
from app.ai.client import SYSTEM, AISettings
from app.ai.provider import ModelAI
from app.config import Settings
from app.safety.input_guard import guard_payload
from app.safety.privacy import redact_for_egress
from app.shared.errors import InvalidInput, ProviderUnavailable

BASE_URL = "https://agentrouter.org/v1"
log = logging.getLogger(__name__)
# Tool-disabling flags are part of the security contract. Requalify a new CLI
# against scripts/check_agentrouter_cli.py before extending this allowlist.
VERIFIED_CLI_VERSIONS = {"codex-cli 0.167.0-alpha.4.1"}
DISABLED_FEATURES = (
    "shell_tool", "apps", "plugins", "browser_use", "computer_use", "multi_agent",
    "image_generation", "hooks", "memories", "view_image", "skill_search",
    "workspace_dependencies", "shell_snapshot", "sleep_tool", "goals",
    "request_permissions_tool", "tool_suggest",
)
_ENV_KEYS = {"PATH", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "PATHEXT"}


class AgentRouterSettings(AISettings):
    # Separate caches for this transport and the direct HTTP adapter.
    transport: Literal["agentrouter-codex-v1"] = "agentrouter-codex-v1"


def find_executable() -> str:
    supplied = os.environ.get("CODEX_CLI_PATH", "").strip()
    candidate = supplied or shutil.which("codex.exe" if os.name == "nt" else "codex")
    if candidate:
        path = Path(candidate).resolve()
        if path.is_file() and (os.name != "nt" or path.name.lower() == "codex.exe"):
            return str(path)
    raise InvalidInput("Для AgentRouter укажите путь к настоящему Codex CLI в CODEX_CLI_PATH.")


class AgentRouterClient(ChatGPTClient):
    def __init__(self, executable: str, settings: AgentRouterSettings):
        super().__init__(executable, settings.timeout_seconds)
        if settings.base_url != BASE_URL or settings.wire_api != "responses":
            raise InvalidInput("Режим AgentRouter CLI требует https://agentrouter.org/v1 и TEXT_WIRE_API=responses.")
        self.settings = settings
        self.env = {k: v for k, v in os.environ.items() if k.upper() in _ENV_KEYS}

    def _environment(self, root: Path) -> dict:
        home = root / "home"
        home.mkdir()
        environment = dict(self.env)
        for name in ("CODEX_HOME", "HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "TEMP", "TMP", "TMPDIR"):
            environment[name] = str(home)
        key = self.settings.api_key.get_secret_value()
        environment.update(AGENTROUTER_API_KEY=key, AGENTROUTER_AUTHORIZATION="Bearer " + key,
                           NO_COLOR="1", PYTHONUTF8="1")
        return environment

    def _arguments(self, shape: Path, *, base_url=BASE_URL):
        options = {
            "model": self.settings.model,
            "model_provider": "studio_agentrouter",
            "model_reasoning_effort": self.settings.reasoning_effort or "low",
            "approval_policy": "never",
            "web_search": "disabled",
            "project_doc_max_bytes": 0,
            "skills.bundled.enabled": False,
            "skills.include_instructions": False,
            "suppress_unstable_features_warning": True,
            "model_instructions_file": str(shape.with_name("instructions.txt")),
            "model_providers.studio_agentrouter.name": "AgentRouter",
            "model_providers.studio_agentrouter.base_url": base_url,
            "model_providers.studio_agentrouter.wire_api": "responses",
            "model_providers.studio_agentrouter.env_key": "AGENTROUTER_API_KEY",
            "model_providers.studio_agentrouter.env_http_headers.Authorization": "AGENTROUTER_AUTHORIZATION",
            "model_providers.studio_agentrouter.requires_openai_auth": False,
            "model_providers.studio_agentrouter.request_max_retries": 0,
            "model_providers.studio_agentrouter.stream_max_retries": 0,
        }
        args = ["exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
                "--skip-git-repo-check", "--sandbox", "read-only", "--json", "--color", "never",
                "--enable", "skip_host_skill_discovery"]
        for key, value in options.items():
            args.extend(["-c", key + "=" + json.dumps(value, ensure_ascii=True)])
        for feature in DISABLED_FEATURES:
            args.extend(["--disable", feature])
        return [*args, "--output-schema", str(shape), "-"]

    @staticmethod
    def _answer(events: str) -> str:
        completed, answers = False, []
        for line in events.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue  # CLI diagnostics, never copied to a user or log.
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind in {"error", "turn.failed"}:
                raise ProviderUnavailable("AgentRouter не завершил запрос.")
            if kind in {"item.started", "item.updated", "item.completed"}:
                item = event.get("item")
                if not isinstance(item, dict) or item.get("type") not in {"agent_message", "reasoning"}:
                    raise ProviderUnavailable("AgentRouter вернул недопустимое действие вместо текста.")
                if kind == "item.completed" and item["type"] == "agent_message":
                    answers.append(item.get("text", ""))
            elif kind == "turn.completed":
                completed = True
        if not completed or len(answers) != 1 or not isinstance(answers[0], str) or not answers[0].strip():
            raise ProviderUnavailable("AgentRouter не вернул один завершённый ответ.")
        return answers[0]

    async def complete(self, schema, instruction, data, validate=None):
        if len(json.dumps(data, ensure_ascii=False)) > 160_000:
            raise InvalidInput("Слишком много данных для одного запроса.")
        guard_payload(data)
        payload = json.dumps({"input": redact_for_egress(data)}, ensure_ascii=False)
        if len(payload) > 160_000:
            raise InvalidInput("Слишком много данных для одного запроса.")
        prompt = "Задание: " + instruction + "\n" + payload
        try:
            # Structured multi-section documents have a larger, still bounded budget.
            budget = min(240, self.timeout * 2) if schema.__name__ == 'MarketingReport' else self.timeout
            async with asyncio.timeout(budget):
                with tempfile.TemporaryDirectory(prefix="studio-agentrouter-") as directory:
                    root = Path(directory).resolve()
                    env = self._environment(root)
                    workspace = root / "work"
                    workspace.mkdir()
                    version = await self._run(["--version"], cwd=workspace, capture=True, env=env)
                    if not VERIFIED_CLI_VERSIONS.intersection(version.splitlines()):
                        raise ProviderUnavailable("Версия Codex CLI не проверена для AgentRouter. Обновите адаптер.")
                    shape = root / "schema.json"
                    shape.write_text(json.dumps(strict_schema(schema.model_json_schema())), encoding="utf-8")
                    shape.with_name("instructions.txt").write_text(
                        SYSTEM + "\nНе вызывай инструменты. Не читай файлы и не исполняй команды.",
                        encoding="utf-8",
                    )
                    events = await self._run(self._arguments(shape), cwd=workspace, prompt=prompt,
                                             capture=True, env=env, max_output_bytes=1_000_000)
                    output = schema.model_validate_json(self._answer(events))
                    if validate is not None and not validate(output):
                        raise ProviderUnavailable("Ответ AgentRouter не прошёл проверку фактов и ограничений.")
                    return output
        except ProviderUnavailable:
            raise ProviderUnavailable(
                "AgentRouter не выполнил запрос. Проверьте доступ и лимиты ключа, сеть и версию Codex CLI."
            ) from None
        except TimeoutError:
            log.warning('AgentRouter deadline exceeded schema=%s', schema.__name__)
            raise ProviderUnavailable('AgentRouter не успел ответить за отведённое время. Уточните или сократите задачу.') from None
        except ValidationError as exc:
            # Types/count only: validation input can contain private company data.
            log.warning('AgentRouter invalid output schema=%s errors=%s', schema.__name__,
                        [e['type'] for e in exc.errors(include_input=False, include_context=False)])
            raise ProviderUnavailable('Ответ AgentRouter не соответствует формату материала.') from None
        except (OSError, ValueError, ExceptionGroup):
            raise ProviderUnavailable("Не удалось получить допустимый ответ AgentRouter за отведённое время.") from None


def build_provider(settings: Settings) -> ModelAI:
    config = AISettings.from_env()
    options = config.model_dump()
    options["timeout_seconds"] = min(config.timeout_seconds, settings.provider_timeout_seconds)
    # This transport deliberately makes one request, even if the HTTP adapter's
    # optional TEXT_MAX_RETRIES is still present. Retries belong to a new user action.
    options["max_retries"] = 0
    return ModelAI(AgentRouterClient(find_executable(), AgentRouterSettings(**options)),
                   max_posts=settings.max_posts_per_job)
