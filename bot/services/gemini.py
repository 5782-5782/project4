import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import aiohttp

from bot.config import get_settings
from bot.db.database import Database

logger = logging.getLogger(__name__)


class RateLimitExhausted(Exception):
    """All models and projects exhausted for today."""


class GeminiAuthError(Exception):
    """Invalid or unsupported Gemini API credentials."""


class ModelUnavailableError(Exception):
    """Temporary or plan-specific model unavailability (503, overloaded)."""


class RPMThrottle(Exception):
    """Per-minute limit hit — caller should retry later."""


@dataclass
class QueueItem:
    prompt: str
    future: asyncio.Future[str]
    admin_user_id: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class GeminiService:
  GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

  def __init__(self, db: Database, http_session: aiohttp.ClientSession | None = None) -> None:
      self.db = db
      self.settings = get_settings()
      self._http_session = http_session
      self._owns_session = http_session is None
      self._queue: asyncio.Queue[QueueItem | None] = asyncio.Queue()
      self._worker_task: asyncio.Task | None = None
      self._rpm_timestamps: list[float] = []
      self._lock = asyncio.Lock()
      self._exhausted: set[tuple[int, str]] = set()
      self._model_notes: dict[tuple[int, str], str] = {}
      self._refresh_task: asyncio.Task | None = None

  def start(self) -> None:
      if self._worker_task is None:
          self._worker_task = asyncio.create_task(self._worker())
      if self._refresh_task is None:
          self._refresh_task = asyncio.create_task(self._refresh_limits_loop())

  async def stop(self) -> None:
      if self._refresh_task:
          self._refresh_task.cancel()
          try:
              await self._refresh_task
          except asyncio.CancelledError:
              pass
          self._refresh_task = None
      if self._worker_task:
          await self._queue.put(None)
          await self._worker_task
          self._worker_task = None
      if self._owns_session and self._http_session and not self._http_session.closed:
          await self._http_session.close()

  async def generate(self, prompt: str, timeout: float = 120.0, admin_user_id: int | None = None) -> str:
      loop = asyncio.get_running_loop()
      future: asyncio.Future[str] = loop.create_future()
      item = QueueItem(prompt=prompt, future=future, admin_user_id=admin_user_id)
      await self._queue.put(item)
      return await asyncio.wait_for(future, timeout=timeout)

  async def _worker(self) -> None:
      while True:
          item = await self._queue.get()
          if item is None:
              break
          try:
              await self._wait_for_rpm_slot()
              result = await self._call_with_fallback(item.prompt, item.admin_user_id)
              if not item.future.done():
                  item.future.set_result(result)
          except Exception as exc:
              if not item.future.done():
                  item.future.set_exception(exc)
          finally:
              self._queue.task_done()

  async def _refresh_limits_loop(self) -> None:
      interval_sec = max(self.settings.limits_refresh_minutes, 5) * 60
      while True:
          await asyncio.sleep(interval_sec)
          try:
              await self.refresh_model_limits()
          except Exception:
              logger.exception("Gemini periodic limits refresh failed")

  async def refresh_model_limits(self) -> int:
      """Re-check exhausted models; clear those whose daily quota recovered."""
      usage = await self.db.get_gemini_usage_stats()
      cleared = 0
      for key in list(self._exhausted):
          used = usage.get(key, 0)
          if used < self.settings.rpd_per_model:
              self._exhausted.discard(key)
              self._model_notes.pop(key, None)
              cleared += 1
      for key in list(self._model_notes.keys()):
          if key not in self._exhausted:
              used = usage.get(key, 0)
              if used < self.settings.rpd_per_model:
                  self._model_notes.pop(key, None)
      if cleared:
          logger.info("Gemini limits refresh: %s model(s) available again", cleared)
      return cleared

  async def _wait_for_rpm_slot(self) -> None:
      async with self._lock:
          now = datetime.now(timezone.utc).timestamp()
          self._rpm_timestamps = [t for t in self._rpm_timestamps if now - t < 60]
          if len(self._rpm_timestamps) >= self.settings.rpm_limit:
              wait = 60 - (now - self._rpm_timestamps[0]) + 0.05
              await asyncio.sleep(max(wait, 0.1))
              now = datetime.now(timezone.utc).timestamp()
              self._rpm_timestamps = [t for t in self._rpm_timestamps if now - t < 60]
          self._rpm_timestamps.append(datetime.now(timezone.utc).timestamp())

  def _api_keys(self) -> list[str]:
      return self.settings.gemini_api_keys

  async def _call_with_fallback(self, prompt: str, admin_user_id: int | None = None) -> str:
      keys = self._api_keys()
      if not keys:
          raise GeminiAuthError("Gemini API keys are not configured in config/secrets.json")

      usage = await self.db.get_gemini_usage_stats()
      last_error: Exception | None = None
      auth_errors = 0

      for project_idx, api_key in enumerate(keys):
          for model in self.settings.gemini_models:
              if (project_idx, model) in self._exhausted:
                  continue
              used = usage.get((project_idx, model), 0)
              if used >= self.settings.rpd_per_model:
                  self._exhausted.add((project_idx, model))
                  continue
              try:
                  logger.info("Gemini try project=%s model=%s", project_idx, model)
                  text = await self._request(api_key, model, prompt)
                  await self.db.record_gemini_usage(project_idx, model, admin_user_id)
                  self._model_notes.pop((project_idx, model), None)
                  return text
              except GeminiAuthError as exc:
                  auth_errors += 1
                  last_error = exc
                  self._model_notes[(project_idx, model)] = "неверный ключ"
                  logger.warning("Gemini auth error project=%s model=%s: %s", project_idx, model, exc)
                  break
              except ModelUnavailableError as exc:
                  last_error = exc
                  self._model_notes[(project_idx, model)] = str(exc)
                  logger.warning("Gemini unavailable project=%s model=%s: %s", project_idx, model, exc)
              except RateLimitExhausted as exc:
                  self._exhausted.add((project_idx, model))
                  self._model_notes[(project_idx, model)] = "лимит исчерпан"
                  last_error = exc
              except Exception as exc:
                  if _is_daily_limit_error(exc):
                      self._exhausted.add((project_idx, model))
                      self._model_notes[(project_idx, model)] = "дневная квота"
                  last_error = exc
                  logger.warning("Gemini error project=%s model=%s: %s", project_idx, model, exc)

      if auth_errors >= len(keys):
          raise GeminiAuthError(
              "Неверный или просроченный ключ Gemini API. "
              "Создайте новый на https://aistudio.google.com/apikey "
              "и обновите config/secrets.json → gemini_api_keys"
          ) from last_error

      raise RateLimitExhausted("All Gemini models and projects exhausted") from last_error

  def _get_session(self):
      if self._http_session is not None:
          return _SessionCtx(self._http_session)
      return aiohttp.ClientSession()

  async def _request(self, api_key: str, model: str, prompt: str) -> str:
      url = self.GEMINI_URL.format(model=model)
      payload = {
          "contents": [{"parts": [{"text": prompt}]}],
          "generationConfig": {
              "temperature": 0.2,
              "responseMimeType": "application/json",
          },
      }
      async with self._get_session() as session:
          async with session.post(
              url,
              headers={"x-goog-api-key": api_key},
              json=payload,
              timeout=aiohttp.ClientTimeout(total=90),
          ) as resp:
              body = await resp.json()
              if resp.status in (401, 403):
                  raise GeminiAuthError(f"Gemini HTTP {resp.status}: {body}")
              if resp.status == 429:
                  err = body.get("error", {}) if isinstance(body, dict) else {}
                  msg = str(err.get("message", "")).lower()
                  if "quota" in msg or "resource_exhausted" in str(err.get("status", "")).lower():
                      raise RateLimitExhausted(f"Quota on {model}: {err.get('message', body)}")
                  raise ModelUnavailableError(f"Временный лимит {model}")
              if resp.status == 503:
                  raise ModelUnavailableError(f"Модель {model} перегружена (503)")
              if resp.status == 400:
                  err = body.get("error", {}) if isinstance(body, dict) else {}
                  msg = str(err.get("message", ""))
                  if "location is not supported" in msg.lower():
                      raise GeminiAuthError(f"Регион не поддерживается: {msg}")
              if resp.status != 200:
                  raise RuntimeError(f"Gemini HTTP {resp.status}: {body}")
              try:
                  return body["candidates"][0]["content"]["parts"][0]["text"]
              except (KeyError, IndexError) as exc:
                  raise RuntimeError(f"Unexpected Gemini response: {body}") from exc

  async def get_limits_dashboard(self) -> str:
      from bot.ui.emoji import E, bar

      usage = await self.db.get_gemini_usage_stats()
      keys = self._api_keys()
      lines = [
          f"━━━━━━━━━━━━━━━━━━━━",
          f"{E['crown']} <b>AI MODERATION BOT</b> {E['sparkles']}",
          f"━━━━━━━━━━━━━━━━━━━━",
          "",
          f"{E['robot']} <b>Статус системы</b>",
          f"  {E['queue']} Очередь: <b>{self._queue.qsize()}</b> запросов",
          f"  {E['clock']} RPM лимит: <b>{self.settings.rpm_limit}</b>/мин",
          f"  {E['shield']} Проектов API: <b>{len(keys)}</b>",
          "",
          f"{E['chart']} <b>Квоты моделей (сегодня)</b>",
          f"<i>Лимит: {self.settings.rpd_per_model} запросов / модель / проект</i>",
          "",
      ]
      for project_idx, _ in enumerate(keys):
          lines.append(f"{E['key']} <b>Проект {project_idx + 1}</b>")
          for model in self.settings.gemini_models:
              used = usage.get((project_idx, model), 0)
              total = self.settings.rpd_per_model
              pct = int(used / total * 100) if total else 0
              status = E["check"] if used < total else E["ban"]
              exhausted = (project_idx, model) in self._exhausted or used >= total
              marker = "🔴" if exhausted else "🟢"
              short = model.replace("gemini-", "").replace("-preview", "")
              note = self._model_notes.get((project_idx, model), "")
              note_str = f" — <i>{note}</i>" if note else ""
              lines.append(
                  f"  {marker} <code>{short}</code> {bar(used, total)} "
                  f"<b>{used}</b>/{total} ({pct}%) {status}{note_str}"
              )
          lines.append("")
      lines.append(f"{E['info']} Сброс RPD: полночь PT (08:00 UTC)")
      lines.append(f"{E['clock']} Проверка лимитов: каждые <b>{self.settings.limits_refresh_minutes}</b> мин")
      return "\n".join(lines)


class _SessionCtx:
    """Reuse shared session without closing it."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def __aenter__(self) -> aiohttp.ClientSession:
        return self._session

    async def __aexit__(self, *args: object) -> None:
        return None


def _is_daily_limit_error(exc: Exception) -> bool:
      text = str(exc).lower()
      return "429" in text or "quota" in text or "resource_exhausted" in text or "rate limit" in text


def parse_moderation_response(raw: str) -> dict[str, Any]:
      text = raw.strip()
      if text.startswith("```"):
          text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
      data = json.loads(text)
      if not isinstance(data, dict):
          raise ValueError("Response is not a JSON object")
      return data
