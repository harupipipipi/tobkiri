from __future__ import annotations

import subprocess
from typing import Any

from core_runtime.runtime_events import utc_now
from tobkiri_protocol.settings_state import SettingsOwnerPort
from .security import SchedulerPolicyError, validate_no_agent_argv


class SchedulerRunner:
    def __init__(self, *, settings_owner: SettingsOwnerPort | None = None) -> None:
        self._settings_owner = settings_owner

    def run(self, job: dict[str, Any]) -> dict[str, Any]:
        if job.get("no_agent"):
            return self._run_script(job)
        if job.get("target_conversation_id") or str(job.get("session_target") or "").strip().lower() in {"fresh", "current"}:
            return self._send_chat_task(job)
        return self._run_agent(job)

    def _run_script(self, job: dict[str, Any]) -> dict[str, Any]:
        try:
            argv = validate_no_agent_argv(job)
            completed = subprocess.run(
                argv,
                shell=False,
                capture_output=True,
                text=True,
                timeout=int(job.get("timeout_seconds") or 60),
            )
        except SchedulerPolicyError as exc:
            return {"status": "error", "error": str(exc), "created_at": utc_now()}
        except subprocess.TimeoutExpired as exc:
            return {
                "status": "failed",
                "error": "script timed out",
                "stdout": exc.stdout or "",
                "stderr": exc.stderr or "",
                "returncode": None,
                "created_at": utc_now(),
            }
        return {
            "status": "completed" if completed.returncode == 0 else "failed",
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "returncode": completed.returncode,
            "created_at": utc_now(),
        }

    def _run_agent(self, job: dict[str, Any]) -> dict[str, Any]:
        from domain.agent.engine import AgentEngine

        context = {
            "agent_id": job.get("agent_id", "main"),
            "runtime_profile_key": job.get("runtime_profile_key") or None,
            "session_key": f"cron:{job.get('job_id')}",
            "scheduler_job_id": job.get("job_id"),
        }
        engine = (
            AgentEngine(settings_owner=self._settings_owner)
            if self._settings_owner is not None
            else AgentEngine()
        )
        result = engine.execute(
            job.get("prompt", ""),
            [],
            job.get("model", "default"),
            job.get("system_prompt"),
            context,
        )
        return {"status": result.get("status"), "agent_result": result, "created_at": utc_now()}

    def _send_chat_task(self, job: dict[str, Any]) -> dict[str, Any]:
        from blocks.chat.send import run as send_chat
        from domain.chat.store import ChatStore

        store = ChatStore()
        target = str(job.get("target_conversation_id") or job.get("conversation_id") or "").strip()
        created = None
        if not target or str(job.get("session_target") or "").strip().lower() == "fresh":
            created = store.create_conversation(
                model=str(job.get("model") or "") or None,
                system_prompt_id=str(job.get("system_prompt_id") or "") or None,
                agent_id=str(job.get("agent_id") or "scheduler"),
                tags=["scheduler"],
                conversation_kind="scheduled",
                metadata={"scheduler_job_id": job.get("job_id"), "scheduler_job_name": job.get("name")},
            )
            target = str(created.get("id") or "")
        if not target:
            return {"status": "error", "error": "target_conversation_id is required", "created_at": utc_now()}
        prompt = str(job.get("prompt") or "").strip()
        if not prompt:
            return {"status": "error", "error": "prompt is required", "conversation_id": target, "created_at": utc_now()}
        payload = {
                "conversation_id": target,
                "message": {
                    "role": "user",
                    "content": prompt,
                    "metadata": {
                        "source": "scheduler",
                        "scheduler_job_id": job.get("job_id"),
                    },
                },
                "params": dict(job.get("params") if isinstance(job.get("params"), dict) else {}),
            }
        context = {"run_source": "scheduler", "scheduler_job_id": job.get("job_id")}
        result = (
            send_chat(payload, context, settings_owner=self._settings_owner)
            if self._settings_owner is not None
            else send_chat(payload, context)
        )
        return {
            "status": "completed" if isinstance(result, dict) and result.get("status") == "ok" else "failed",
            "conversation_id": target,
            "created_conversation": created,
            "chat_result": result,
            "created_at": utc_now(),
        }
