"""Brain — wraps Claude Code headless mode. Harvey's thinking engine."""

import asyncio
import json
import logging
import os
import re
import uuid
from pathlib import Path

from harvey.state import StateManager

logger = logging.getLogger("harvey.brain")

from harvey.paths import PROJECT_ROOT

PROMPTS_DIR = PROJECT_ROOT / "prompts"
SKILLS_DIR = PROJECT_ROOT / "skills"

# Subprocess safety limits
DEFAULT_TIMEOUT_SECONDS = 300  # a single Claude call should never hang forever
DEFAULT_MAX_RETRIES = 2  # retries on transient failures (non-zero exit, timeout)
RETRY_BASE_DELAY = 5.0  # seconds; doubles per attempt

# stderr fragments that indicate retrying is pointless
_NON_RETRYABLE_PATTERNS = (
    "not logged in",
    "please run `claude login`",
    "invalid api key",
    "unauthorized",
)


def _cli_env() -> dict:
    """Environment for the Claude CLI subprocess.

    The CLI refuses ``--dangerously-skip-permissions`` when it is running as
    root, unless IS_SANDBOX says the process is already confined. Harvey's
    own container image runs as an unprivileged user, so this never fires
    there -- but hosted runners and scheduled cloud containers are root, and
    there the container *is* the sandbox. Without this, Harvey's brain fails
    on every call in exactly the environments it is left alone to run in.

    The value has to be exactly "1". Some hosts already export a friendlier
    spelling (IS_SANDBOX=yes), which the CLI does not accept -- so overwrite
    rather than defaulting, or Harvey inherits a value that reads as correct
    and fails every call anyway.
    """
    env = os.environ.copy()
    if getattr(os, "geteuid", None) and os.geteuid() == 0:
        env["IS_SANDBOX"] = "1"
    return env


class Brain:
    def __init__(self, state: StateManager):
        self.state = state
        # Lazy import avoids a cycle (quota -> usage -> state).
        from harvey.integrations.quota import QuotaClient
        self.quota = QuotaClient()

    async def think(
        self,
        prompt: str,
        session_id: str | None = None,
        expect_json: bool = False,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        agent: str = "",
        task: str = "",
    ) -> str:
        """Send a prompt to Claude Code headless mode and return the response.

        Retries transient failures with exponential backoff and enforces a
        hard timeout so a hung CLI call can never stall the heartbeat.
        Returns "" on unrecoverable failure (callers already handle empty).

        ``agent``/``task`` label the call in the usage_events ledger so the
        dashboard can attribute tokens and cost to specific work.
        """
        cmd = [
            "claude", "-p", prompt,
            # JSON output carries exact token/cost accounting per call.
            "--output-format", "json",
            "--dangerously-skip-permissions",
        ]
        if session_id:
            # The CLI only accepts UUIDs for --session-id; callers pass
            # friendly names ("harvey-scout") purely as a debug label, and
            # one-shot -p calls get no continuity from a session id anyway.
            try:
                uuid.UUID(session_id)
                cmd.extend(["--session-id", session_id])
            except ValueError:
                pass

        logger.debug(f"Brain call (session={session_id}): {prompt[:100]}...")

        last_error = ""
        for attempt in range(max_retries + 1):
            if attempt > 0:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.info(
                    f"Retrying brain call in {delay:.0f}s "
                    f"(attempt {attempt + 1}/{max_retries + 1})..."
                )
                await asyncio.sleep(delay)

            process = None
            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    env=_cli_env(),
                    # DEVNULL: the CLI reads inherited stdin as prompt input,
                    # stealing the terminal (and any piped answers) from Harvey.
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(
                        process.communicate(), timeout=timeout
                    )
                except asyncio.TimeoutError:
                    logger.error(f"Claude call timed out after {timeout:.0f}s. Killing process.")
                    try:
                        process.kill()
                        await process.wait()
                    except ProcessLookupError:
                        pass
                    last_error = "timeout"
                    continue  # retry

                if process.returncode != 0:
                    error = stderr.decode(errors="replace").strip()
                    logger.error(
                        f"Claude exited with code {process.returncode}: {error[:300]}"
                    )
                    last_error = error
                    if any(p in error.lower() for p in _NON_RETRYABLE_PATTERNS):
                        logger.error(
                            "Non-retryable Claude error (auth). "
                            "Run 'claude login' and restart Harvey."
                        )
                        return ""
                    continue  # retry transient failures

                raw = stdout.decode(errors="replace").strip()
                response, payload = self._parse_result_payload(raw)

                # Usage accounting must never break the response path.
                try:
                    await self.state.increment_usage()
                except Exception as e:
                    logger.warning(f"Failed to record usage counter: {e}")
                try:
                    await self._record_usage(payload, agent=agent, task=task,
                                             label=session_id or "")
                except Exception as e:
                    logger.warning(f"Failed to record usage event: {e}")

                logger.debug(f"Brain response: {response[:200]}...")
                return response

            except FileNotFoundError:
                logger.error(
                    "Claude CLI not found. Install it: https://claude.com/download"
                )
                return ""  # not retryable
            except asyncio.CancelledError:
                # Shutting down — kill the child so it doesn't orphan
                if process is not None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                raise
            except Exception as e:
                logger.error(f"Brain error: {e}")
                last_error = str(e)
                continue

        logger.error(
            f"Brain call failed after {max_retries + 1} attempts. "
            f"Last error: {str(last_error)[:200]}"
        )
        return ""

    @staticmethod
    def _parse_result_payload(raw: str) -> tuple[str, dict | None]:
        """Split CLI JSON output into (response_text, result_payload).

        With ``--output-format json`` stdout is one JSON object whose
        ``result`` field is the model's text. If parsing fails (older CLI,
        truncated output), fall back to treating stdout as plain text so a
        formatting change can never break Harvey's pipeline.
        """
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return raw, None
        if not isinstance(payload, dict) or payload.get("type") != "result":
            return raw, None
        return str(payload.get("result") or ""), payload

    async def _record_usage(
        self, payload: dict | None, agent: str, task: str, label: str
    ):
        """Write per-model usage rows from a CLI result payload."""
        if not payload:
            return

        if not agent and label.startswith("harvey-"):
            # Callers historically pass labels like "harvey-scout-score".
            agent = label[len("harvey-"):].split("-")[0]

        session = str(payload.get("session_id") or "")
        duration_ms = int(payload.get("duration_ms") or 0)
        num_turns = int(payload.get("num_turns") or 0)
        is_error = bool(payload.get("is_error"))

        # modelUsage includes subagent spend; the flat `usage` field does not.
        model_usage = payload.get("modelUsage")
        rows = []
        if isinstance(model_usage, dict) and model_usage:
            for model, mu in model_usage.items():
                if not isinstance(mu, dict):
                    continue
                rows.append({
                    "model": str(model),
                    "input_tokens": int(mu.get("inputTokens") or 0),
                    "output_tokens": int(mu.get("outputTokens") or 0),
                    "cache_read_tokens": int(mu.get("cacheReadInputTokens") or 0),
                    "cache_creation_tokens": int(mu.get("cacheCreationInputTokens") or 0),
                    "cost_usd": float(mu.get("costUSD") or 0.0),
                })
        else:
            usage = payload.get("usage")
            if isinstance(usage, dict):
                rows.append({
                    "model": "",
                    "input_tokens": int(usage.get("input_tokens") or 0),
                    "output_tokens": int(usage.get("output_tokens") or 0),
                    "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0),
                    "cache_creation_tokens": int(usage.get("cache_creation_input_tokens") or 0),
                    "cost_usd": float(payload.get("total_cost_usd") or 0.0),
                })

        for row in rows:
            await self.state.record_usage_event(
                agent=agent,
                task=task or label,
                session_id=session,
                duration_ms=duration_ms,
                num_turns=num_turns,
                is_error=is_error,
                source="result_json",
                **row,
            )

    async def think_json(
        self,
        prompt: str,
        session_id: str | None = None,
        agent: str = "",
        task: str = "",
    ) -> dict | list | None:
        """Send a prompt and parse the response as JSON."""
        full_prompt = (
            prompt
            + "\n\nRespond ONLY with valid JSON. No markdown, no explanation."
        )
        response = await self.think(
            full_prompt, session_id=session_id, agent=agent, task=task
        )
        if not response:
            return None
        parsed = self._extract_json(response)
        if parsed is None:
            logger.error(f"Failed to parse JSON from brain: {response[:200]}")
        return parsed

    @staticmethod
    def _extract_json(text: str) -> dict | list | None:
        """Best-effort JSON extraction: strips code fences, then falls back to
        locating the outermost JSON object/array in surrounding prose."""
        cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip())
        cleaned = re.sub(r"\s*```$", "", cleaned)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass
        # Fall back: model wrapped the JSON in explanation text
        for open_ch, close_ch in (("{", "}"), ("[", "]")):
            start = cleaned.find(open_ch)
            end = cleaned.rfind(close_ch)
            if start != -1 and end > start:
                try:
                    return json.loads(cleaned[start : end + 1])
                except json.JSONDecodeError:
                    continue
        return None

    async def check_usage(self) -> float:
        """Check current Claude daily usage (our own call count as proxy).

        Returns the number of calls made today. Errors are treated as
        "over budget is unknown" and return a safe 0.0 so a broken DB read
        doesn't crash the heartbeat.
        """
        try:
            return await self.state.get_usage_today()
        except Exception as e:
            logger.warning(f"Could not read usage from state: {e}")
            return 0.0

    async def is_within_budget(
        self, max_daily_calls: int = 200, max_percent: float | None = None
    ) -> bool:
        """Check whether Harvey may spend more Claude quota right now.

        Preferred signal: the account's REAL utilization windows (the same
        numbers `/usage` shows), compared against max_percent so Harvey
        always leaves the remainder for the user's own interactive work.
        Fallback when that endpoint is unavailable: Harvey's own call
        counter against max_daily_calls.
        """
        if max_percent is not None:
            try:
                windows = await self.quota.get_utilization()
            except Exception as e:
                logger.debug(f"Quota check errored: {e}")
                windows = None
            if windows:
                worst = max(
                    (w["utilization"] for w in windows.values()), default=0.0
                )
                within = worst < max_percent
                if not within:
                    resets = ", ".join(
                        f"{name} resets {w['resets_at']}"
                        for name, w in windows.items()
                        if w.get("resets_at")
                    )
                    logger.info(
                        f"Quota gate: utilization {worst:.0f}% >= "
                        f"{max_percent:.0f}% limit. {resets}"
                    )
                return within

        calls = await self.check_usage()
        return calls < max_daily_calls

    def load_prompt(self, prompt_name: str, **kwargs) -> str:
        """Load a prompt template from the prompts/ directory and fill in variables."""
        prompt_file = PROMPTS_DIR / f"{prompt_name}.md"
        try:
            template = prompt_file.read_text()
        except FileNotFoundError:
            logger.warning(f"Prompt file not found: {prompt_file}")
            return ""
        except OSError as e:
            logger.error(f"Could not read prompt file {prompt_file}: {e}")
            return ""
        for key, value in kwargs.items():
            template = template.replace(f"{{{{{key}}}}}", str(value))
        # Surface templating mistakes early instead of sending {{foo}} to Claude
        leftover = re.findall(r"\{\{(\w+)\}\}", template)
        if leftover:
            logger.warning(
                f"Prompt '{prompt_name}' has unfilled variables: {sorted(set(leftover))}"
            )
        return template

    def load_skill(self, skill_name: str) -> str:
        """Load a skill knowledge file from the skills/ directory."""
        skill_file = SKILLS_DIR / f"{skill_name}.md"
        try:
            return skill_file.read_text()
        except FileNotFoundError:
            logger.warning(f"Skill file not found: {skill_file}")
            return ""
        except OSError as e:
            logger.error(f"Could not read skill file {skill_file}: {e}")
            return ""

    def load_skills_for_agent(self, agent_name: str) -> str:
        """Load all relevant skills for a specific sub-agent.

        Returns concatenated skill content that should be injected
        into the agent's prompts as foundational knowledge.
        """
        # product_knowledge and competitive_intel are auto-generated by the trainer
        # and loaded by every agent that needs product context
        skill_map = {
            "scout": ["prospecting_tactics", "lead_qualification", "account_navigation", "signal_playbook", "product_knowledge"],
            "writer": ["email_frameworks", "signal_playbook", "sales_methodology", "offer_strategy", "product_knowledge", "competitive_intel"],
            "handler": ["objection_handling", "sales_methodology", "offer_strategy", "product_knowledge", "competitive_intel"],
            "sender": ["email_frameworks", "product_knowledge"],
            "linkedin": ["linkedin_outreach", "prospecting_tactics", "product_knowledge"],
        }

        skill_names = skill_map.get(agent_name, [])
        if not skill_names:
            return ""

        sections = []
        for name in skill_names:
            content = self.load_skill(name)
            if content:
                sections.append(content)

        if not sections:
            return ""

        return (
            "\n\n---\n## FOUNDATIONAL KNOWLEDGE\n"
            "Use the following frameworks and best practices to guide your work:\n\n"
            + "\n\n---\n\n".join(sections)
        )
