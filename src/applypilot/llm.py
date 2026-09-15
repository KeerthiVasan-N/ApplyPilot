"""
Unified LLM client for ApplyPilot.

Auto-detects provider from environment:
  GEMINI_API_KEY  -> Google Gemini (default: gemini-3.6-flash)
  OPENAI_API_KEY  -> OpenAI (default: gpt-4o-mini)
  LLM_URL         -> Local llama.cpp / Ollama compatible endpoint
  LLM_PROVIDER=claude -> Claude Code CLI (`claude -p`), no API key needed

LLM_MODEL env var overrides the model name for any provider.
"""

import logging
import os
import time

import httpx

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Provider detection
# ---------------------------------------------------------------------------

def _detect_provider() -> tuple[str, str, str]:
    """Return (base_url, model, api_key) based on environment variables.

    Reads env at call time (not module import time) so that load_env() called
    in _bootstrap() is always visible here.
    """
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    local_url = os.environ.get("LLM_URL", "")
    model_override = os.environ.get("LLM_MODEL", "")

    # Explicit choice wins: LLM_PROVIDER=claude routes every call through the
    # Claude Code CLI (`claude -p`), using its login instead of an API key.
    if os.environ.get("LLM_PROVIDER", "").strip().lower() in ("claude", "claude-cli", "claude-code"):
        return (CLAUDE_CLI_BASE, model_override, "")

    if gemini_key and not local_url:
        return (
            "https://generativelanguage.googleapis.com/v1beta/openai",
            model_override or "gemini-3.6-flash",  # 2.0/2.5-flash return 404 for new keys
            gemini_key,
        )

    if openai_key and not local_url:
        return (
            "https://api.openai.com/v1",
            model_override or "gpt-4o-mini",
            openai_key,
        )

    if local_url:
        return (
            local_url.rstrip("/"),
            model_override or "local-model",
            os.environ.get("LLM_API_KEY", ""),
        )

    raise RuntimeError(
        "No LLM provider configured. "
        "Set GEMINI_API_KEY, OPENAI_API_KEY, or LLM_URL in your environment."
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

_MAX_RETRIES = 5
_TIMEOUT = 120  # seconds

# Base wait on first 429/503 (doubles each retry, caps at 60s).
# Gemini free tier is 15 RPM = 4s minimum between requests; 10s gives headroom.
_RATE_LIMIT_BASE_WAIT = 10


CLAUDE_CLI_BASE = "claude-cli"  # sentinel base_url: shell out to the Claude Code CLI
_CLAUDE_CLI_TIMEOUT = 600  # seconds; a full resume rewrite can take a while
_GEMINI_COMPAT_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
_GEMINI_NATIVE_BASE = "https://generativelanguage.googleapis.com/v1beta"


class LLMClient:
    """Thin LLM client supporting OpenAI-compatible and native Gemini endpoints.

    For Gemini keys, starts on the OpenAI-compat layer. On a 403 (which
    happens with preview/experimental models not exposed via compat), it
    automatically switches to the native generateContent API and stays there
    for the lifetime of the process.
    """

    def __init__(self, base_url: str, model: str, api_key: str) -> None:
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self._client = httpx.Client(timeout=_TIMEOUT)
        # True once we've confirmed the native Gemini API works for this model
        self._use_native_gemini: bool = False
        self._is_gemini: bool = base_url.startswith(_GEMINI_COMPAT_BASE)
        self._is_claude_cli: bool = base_url == CLAUDE_CLI_BASE

    # -- Claude Code CLI ----------------------------------------------------

    def _chat_claude_cli(self, messages: list[dict]) -> str:
        """Run `claude -p` with tools disabled; system messages go to --system-prompt, the rest to stdin."""
        import subprocess

        exe = _resolve_claude_exe()
        if not exe:
            raise RuntimeError("LLM_PROVIDER=claude but the Claude Code CLI ('claude') is not on PATH.")

        system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system")
        rest = [m for m in messages if m.get("role") != "system"]
        if len(rest) == 1:
            user = rest[0]["content"]
        else:
            user = "\n\n".join(f"[{m.get('role', 'user').upper()}]\n{m['content']}" for m in rest)

        cmd = [exe, "-p", "--output-format", "text", "--no-session-persistence", "--tools", ""]
        if self.model:
            cmd += ["--model", self.model]
        if system:
            if exe.lower().endswith((".cmd", ".bat")):
                # cmd.exe batch shims mangle multi-line/percent-laden arguments: send the
                # instructions through stdin instead and keep argv ASCII-only.
                short_system = (
                    "Follow the INSTRUCTIONS block in the message exactly. Output only what it asks for, nothing else."
                )
                cmd += ["--system-prompt", short_system]
                user = f"=== INSTRUCTIONS ===\n{system}\n\n=== INPUT ===\n{user}"
            else:
                cmd += ["--system-prompt", system]

        env = os.environ.copy()
        env.pop("CLAUDECODE", None)            # allow spawning from inside a Claude Code session
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)

        t0 = time.time()
        proc = subprocess.run(
            cmd, input=user, capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=_CLAUDE_CLI_TIMEOUT, check=False,
        )
        out = (proc.stdout or "").strip()
        log.info("Claude CLI: %d chars in, %d chars out, %.1fs", len(user) + len(system), len(out), time.time() - t0)

        if proc.returncode != 0 or not out or out.startswith("Not logged in"):
            detail = (proc.stderr or out or "").strip().splitlines()
            detail = detail[-1] if detail else f"exit {proc.returncode}"
            hint = "  Run `claude` once in a terminal and /login." if "logged in" in detail.lower() else ""
            raise RuntimeError(f"Claude CLI failed: {detail}{hint}")
        return out

    # -- Native Gemini API --------------------------------------------------

    def _chat_native_gemini(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Call the native Gemini generateContent API.

        Used automatically when the OpenAI-compat endpoint returns 403,
        which happens for preview/experimental models not exposed via compat.

        Converts OpenAI-style messages to Gemini's contents/systemInstruction
        format transparently.
        """
        contents: list[dict] = []
        system_parts: list[dict] = []

        for msg in messages:
            role = msg["role"]
            text = msg.get("content", "")
            if role == "system":
                system_parts.append({"text": text})
            elif role == "user":
                contents.append({"role": "user", "parts": [{"text": text}]})
            elif role == "assistant":
                # Gemini uses "model" instead of "assistant"
                contents.append({"role": "model", "parts": [{"text": text}]})

        payload: dict = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if system_parts:
            payload["systemInstruction"] = {"parts": system_parts}

        url = f"{_GEMINI_NATIVE_BASE}/models/{self.model}:generateContent"
        resp = self._client.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            params={"key": self.api_key},
        )
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]

    # -- OpenAI-compat API --------------------------------------------------

    def _chat_compat(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Call the OpenAI-compatible endpoint."""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        resp = self._client.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers=headers,
        )

        # 403 on Gemini compat = model not available on compat layer.
        # Raise a specific sentinel so chat() can switch to native API.
        if resp.status_code == 403 and self._is_gemini:
            raise _GeminiCompatForbidden(resp)

        return self._handle_compat_response(resp)

    @staticmethod
    def _handle_compat_response(resp: httpx.Response) -> str:
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    # -- public API ---------------------------------------------------------

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        """Send a chat completion request and return the assistant message text."""
        # Qwen3 optimization: prepend /no_think to skip chain-of-thought
        # reasoning, saving tokens on structured extraction tasks.
        if "qwen" in self.model.lower() and messages:
            first = messages[0]
            if first.get("role") == "user" and not first["content"].startswith("/no_think"):
                messages = [{"role": first["role"], "content": f"/no_think\n{first['content']}"}] + messages[1:]

        if self._is_claude_cli:
            return self._chat_claude_cli(messages)

        for attempt in range(_MAX_RETRIES):
            try:
                # Route to native Gemini if we've already confirmed it's needed
                if self._use_native_gemini:
                    return self._chat_native_gemini(messages, temperature, max_tokens)

                return self._chat_compat(messages, temperature, max_tokens)

            except _GeminiCompatForbidden as exc:
                # Model not available on OpenAI-compat layer — switch to native.
                log.warning(
                    "Gemini compat endpoint returned 403 for model '%s'. "
                    "Switching to native generateContent API. "
                    "(Preview/experimental models are often compat-only on native.)",
                    self.model,
                )
                self._use_native_gemini = True
                # Retry immediately with native — don't count as a rate-limit wait
                try:
                    return self._chat_native_gemini(messages, temperature, max_tokens)
                except httpx.HTTPStatusError as native_exc:
                    raise RuntimeError(
                        f"Both Gemini endpoints failed. Compat: 403 Forbidden. "
                        f"Native: {native_exc.response.status_code} — "
                        f"{native_exc.response.text[:200]}"
                    ) from native_exc

            except httpx.HTTPStatusError as exc:
                resp = exc.response
                if resp.status_code == 404 and self._is_gemini:
                    # Google retires models; its 404 body names the replacement.
                    try:
                        detail = resp.json()
                        detail = (detail[0] if isinstance(detail, list) else detail)["error"]["message"]
                    except Exception:  # noqa: BLE001 - fall back to raw text
                        detail = resp.text[:300]
                    raise RuntimeError(
                        f"Gemini model '{self.model}' not found: {detail}\n"
                        "Set LLM_MODEL=<model> in your .env to pick an available model."
                    ) from exc
                if resp.status_code in (429, 503) and attempt < _MAX_RETRIES - 1:
                    # Respect Retry-After header if provided (Gemini sends this).
                    retry_after = (
                        resp.headers.get("Retry-After")
                        or resp.headers.get("X-RateLimit-Reset-Requests")
                    )
                    if retry_after:
                        try:
                            wait = float(retry_after)
                        except (ValueError, TypeError):
                            wait = _RATE_LIMIT_BASE_WAIT * (2 ** attempt)
                    else:
                        wait = min(_RATE_LIMIT_BASE_WAIT * (2 ** attempt), 60)

                    log.warning(
                        "LLM rate limited (HTTP %s). Waiting %ds before retry %d/%d. "
                        "Tip: Gemini free tier = 15 RPM. Consider a paid account "
                        "or switching to a local model.",
                        resp.status_code, wait, attempt + 1, _MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                raise

            except httpx.TimeoutException:
                if attempt < _MAX_RETRIES - 1:
                    wait = min(_RATE_LIMIT_BASE_WAIT * (2 ** attempt), 60)
                    log.warning(
                        "LLM request timed out, retrying in %ds (attempt %d/%d)",
                        wait, attempt + 1, _MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                raise

        raise RuntimeError("LLM request failed after all retries")

    def ask(self, prompt: str, **kwargs) -> str:
        """Convenience: single user prompt -> assistant response."""
        return self.chat([{"role": "user", "content": prompt}], **kwargs)

    def close(self) -> None:
        self._client.close()


def _resolve_claude_exe() -> str | None:
    """Path to the Claude CLI, preferring the real executable over a Windows .CMD shim.

    On Windows `shutil.which("claude")` returns `claude.CMD`, a cmd.exe batch wrapper that
    corrupts multi-line arguments. The npm package ships a native `claude.exe` next to it.
    """
    import shutil
    from pathlib import Path

    found = shutil.which("claude")
    if not found:
        return None
    if found.lower().endswith((".cmd", ".bat")):
        native = Path(found).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        if native.exists():
            return str(native)
    return found


class _GeminiCompatForbidden(Exception):
    """Sentinel: Gemini OpenAI-compat returned 403. Switch to native API."""
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        super().__init__(f"Gemini compat 403: {response.text[:200]}")


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: LLMClient | None = None


def get_client() -> LLMClient:
    """Return (or create) the module-level LLMClient singleton."""
    global _instance
    if _instance is None:
        base_url, model, api_key = _detect_provider()
        log.info("LLM provider: %s  model: %s", base_url, model)
        _instance = LLMClient(base_url, model, api_key)
    return _instance
