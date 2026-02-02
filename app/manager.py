"""ChatGPT manager integration for planning, review, and research."""

import json
import logging
import re
import time
from pathlib import Path
from typing import Optional
from openai import OpenAI

logger = logging.getLogger(__name__)

# Retry configuration
MAX_API_RETRIES = 3
RETRY_DELAY_SECONDS = 5
RETRY_BACKOFF_MULTIPLIER = 2


class QuotaExhaustedException(Exception):
    """Raised when API quota/billing is exhausted."""
    pass


class Manager:
    """Manager that uses ChatGPT for planning, review, and research."""

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-5.2",
        prompts_dir: Optional[Path] = None,
    ):
        self.client = OpenAI(api_key=api_key)
        self.model = model
        self.prompts_dir = prompts_dir or Path(__file__).parent.parent / "prompts" / "manager"

    def _load_prompt(self, name: str) -> str:
        """Load a prompt template from file."""
        prompt_path = self.prompts_dir / f"{name}.txt"
        if prompt_path.exists():
            return prompt_path.read_text()
        raise FileNotFoundError(f"Prompt template not found: {prompt_path}")

    def _extract_json(self, text: str) -> dict:
        """Extract JSON from response text."""
        if not text:
            raise ValueError("Empty response from API")

        # Try to find JSON in code blocks first
        json_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
        if json_match:
            try:
                return json.loads(json_match.group(1))
            except json.JSONDecodeError:
                pass  # Try other methods

        # Try to parse the whole text as JSON
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Try to find JSON object - match balanced braces
        # Handle strings properly to avoid counting braces inside string values
        start = text.find('{')
        if start != -1:
            depth = 0
            in_string = False
            escape_next = False

            for i, char in enumerate(text[start:], start):
                if escape_next:
                    escape_next = False
                    continue

                if char == '\\' and in_string:
                    escape_next = True
                    continue

                if char == '"' and not escape_next:
                    in_string = not in_string
                    continue

                if not in_string:
                    if char == '{':
                        depth += 1
                    elif char == '}':
                        depth -= 1
                        if depth == 0:
                            try:
                                return json.loads(text[start:i+1])
                            except json.JSONDecodeError as e:
                                logger.warning(f"JSON parse failed at position {i}: {e}")
                                # Try next opening brace
                                next_start = text.find('{', start + 1)
                                if next_start != -1:
                                    start = next_start
                                    depth = 0
                                    in_string = False
                                else:
                                    break

        # Last resort: try to repair common issues
        # Sometimes responses get truncated - try to close open structures
        if start != -1:
            logger.warning(f"JSON appears truncated (depth={depth}, in_string={in_string}), attempting repair")
            partial = text[start:]

            # Try multiple repair strategies
            repairs_to_try = []

            if in_string:
                # We're inside an unclosed string - close it first
                # Strategy 1: Close string, close all braces
                repairs_to_try.append(partial + '"' + ('}' * max(depth, 1)))
                # Strategy 2: Close string with empty value continuation
                repairs_to_try.append(partial + '..."' + ('}' * max(depth, 1)))
                # Strategy 3: Truncate to last complete key-value and close
                last_comma = partial.rfind('",')
                if last_comma > 0:
                    repairs_to_try.append(partial[:last_comma + 1] + ('}' * max(depth, 1)))

            if depth > 0:
                # Just need closing braces
                repairs_to_try.append(partial + ('}' * depth))

            # Also try closing any open arrays
            repairs_to_try.append(partial + (']' * partial.count('[')) + ('}' * max(depth, 1)))

            for repaired in repairs_to_try:
                try:
                    result = json.loads(repaired)
                    logger.info(f"JSON repair successful")
                    return result
                except json.JSONDecodeError:
                    continue

        raise ValueError(f"Could not extract JSON from response: {text[:200]}...")

    def _call_api(self, messages: list[dict], temperature: float = 1.0) -> tuple[str, dict]:
        """Make an API call to OpenAI with automatic retry on transient failures.

        Returns:
            Tuple of (content, usage_info) where usage_info contains token counts and cost.
        """
        # o1 models don't support temperature parameter
        kwargs = {
            "model": self.model,
            "messages": messages,
        }

        # Only add temperature for non-o1 models
        if not self.model.startswith("o1"):
            kwargs["temperature"] = temperature

        delay = RETRY_DELAY_SECONDS
        last_error = None

        for attempt in range(MAX_API_RETRIES):
            try:
                logger.info(f"Calling OpenAI API with model: {self.model} (attempt {attempt + 1}/{MAX_API_RETRIES})")
                response = self.client.chat.completions.create(**kwargs)
                content = response.choices[0].message.content
                logger.debug(f"OpenAI response (first 500 chars): {content[:500] if content else 'None'}")

                # Extract usage info
                usage_info = {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "cost": 0.0,
                }
                if response.usage:
                    usage_info["prompt_tokens"] = response.usage.prompt_tokens or 0
                    usage_info["completion_tokens"] = response.usage.completion_tokens or 0
                    usage_info["total_tokens"] = response.usage.total_tokens or 0
                    # Estimate cost (approximate rates for GPT-4 class models)
                    # These rates should be configured but using defaults for now
                    prompt_cost = usage_info["prompt_tokens"] * 0.00003  # $0.03 per 1K
                    completion_cost = usage_info["completion_tokens"] * 0.00006  # $0.06 per 1K
                    usage_info["cost"] = prompt_cost + completion_cost

                return content, usage_info

            except Exception as e:
                last_error = e
                error_str = str(e).lower()

                # Check for quota/billing errors - these are NOT retryable
                is_quota_error = any(term in error_str for term in [
                    'quota', 'billing', 'insufficient_quota', 'exceeded your current quota',
                    'usage limit', 'spending limit', 'out of credits', 'payment required',
                    'account_deactivated', 'plan limit'
                ])

                if is_quota_error:
                    logger.error(f"API quota exhausted: {e}")
                    raise QuotaExhaustedException(f"OpenAI API quota exhausted: {e}")

                # Check if this is a retryable error
                is_retryable = any(term in error_str for term in [
                    'rate limit', 'timeout', 'connection', 'server error',
                    '503', '502', '500', '429', 'overloaded', 'capacity',
                    'temporarily', 'try again'
                ])

                if is_retryable and attempt < MAX_API_RETRIES - 1:
                    logger.warning(f"API call failed (attempt {attempt + 1}): {e}. Retrying in {delay}s...")
                    time.sleep(delay)
                    delay *= RETRY_BACKOFF_MULTIPLIER
                else:
                    # Non-retryable error or last attempt
                    logger.error(f"API call failed after {attempt + 1} attempts: {e}")
                    raise

        # Should not reach here, but just in case
        raise last_error or Exception("API call failed after all retries")

    def plan_task(
        self,
        description: str,
        guide: dict,
        context: str = "",
    ) -> tuple[dict, dict]:
        """Break a task into implementable chunks.

        Returns:
            Tuple of (plan_dict, usage_info)
        """
        prompt_template = self._load_prompt("plan")

        # Format guide as readable text
        guide_text = self._format_guide(guide)

        prompt = prompt_template.format(
            task_description=description,
            guide=guide_text,
            context=context or "No existing codebase context provided.",
        )

        messages = [{"role": "user", "content": prompt}]
        response, usage = self._call_api(messages)

        return self._extract_json(response), usage

    def review_chunk(
        self,
        chunk_spec: dict,
        diff: str,
        worker_summary: str,
        guide: dict,
    ) -> tuple[dict, dict]:
        """Review code changes against specification and guide.

        Returns:
            Tuple of (review_dict, usage_info)
        """
        prompt_template = self._load_prompt("review")

        guide_text = self._format_guide(guide)
        criteria_text = "\n".join(f"- {c}" for c in chunk_spec.get("acceptance_criteria", []))

        prompt = prompt_template.format(
            chunk_spec=json.dumps(chunk_spec, indent=2),
            acceptance_criteria=criteria_text,
            guide=guide_text,
            diff=diff,
            worker_summary=worker_summary,
        )

        messages = [{"role": "user", "content": prompt}]
        response, usage = self._call_api(messages)

        return self._extract_json(response), usage

    def research(self, query: str, context: str = "") -> tuple[dict, dict]:
        """Research a topic for verification or information gathering.

        Returns:
            Tuple of (research_dict, usage_info)
        """
        prompt_template = self._load_prompt("research")

        prompt = prompt_template.format(
            query=query,
            context=context or "No additional context provided.",
        )

        messages = [{"role": "user", "content": prompt}]
        response, usage = self._call_api(messages)

        return self._extract_json(response), usage

    def design_tests(
        self,
        chunk_spec: dict,
        implementation_summary: str,
        diff: str,
    ) -> tuple[dict, dict]:
        """Design tests for implemented code.

        Returns:
            Tuple of (tests_dict, usage_info)
        """
        prompt_template = self._load_prompt("test")

        prompt = prompt_template.format(
            chunk_spec=json.dumps(chunk_spec, indent=2),
            implementation_summary=implementation_summary,
            diff=diff,
        )

        messages = [{"role": "user", "content": prompt}]
        response, usage = self._call_api(messages)

        return self._extract_json(response), usage

    def reconsider_with_rebuttal(
        self,
        original_review: dict,
        worker_response: str,
    ) -> tuple[dict, dict]:
        """Reconsider a review decision after hearing the worker's perspective.

        Returns:
            Tuple of (reconsideration_dict, usage_info)
        """
        prompt_template = self._load_prompt("reconsider")

        issues_text = "\n".join(
            f"- [{i.get('severity', 'issue').upper()}] {i.get('description', '')}"
            for i in original_review.get("issues", [])
        )

        prompt = prompt_template.format(
            original_decision=original_review.get("decision", "rejected"),
            issues=issues_text or "No specific issues listed.",
            worker_response=worker_response,
        )

        messages = [{"role": "user", "content": prompt}]
        response, usage = self._call_api(messages)

        return self._extract_json(response), usage

    def give_feedback(
        self,
        chunk_spec: dict,
        diff: str,
        worker_summary: str,
        guide: dict,
    ) -> tuple[dict, dict]:
        """Give constructive feedback without approve/reject decision.

        This is used in iterative flows where we want improvement suggestions
        without gate-keeping. The focus is on helping refine the work.

        Returns:
            Tuple of (feedback_dict, usage_info)
        """
        prompt_template = self._load_prompt("feedback")

        guide_text = self._format_guide(guide)
        criteria_text = "\n".join(f"- {c}" for c in chunk_spec.get("acceptance_criteria", []))

        prompt = prompt_template.format(
            chunk_spec=json.dumps(chunk_spec, indent=2),
            acceptance_criteria=criteria_text,
            guide=guide_text,
            diff=diff,
            worker_summary=worker_summary,
        )

        messages = [{"role": "user", "content": prompt}]
        response, usage = self._call_api(messages)

        return self._extract_json(response), usage

    def summarize_feedback(self, review: dict) -> str:
        """Create a concise summary of review feedback for retry attempts."""
        if review.get("decision") == "approved":
            return "Previous attempt was approved."

        issues = review.get("issues", [])
        violations = review.get("guide_violations", [])
        feedback = review.get("feedback_for_retry", "")

        parts = []

        if feedback:
            parts.append(f"Feedback: {feedback}")

        if issues:
            issues_text = "\n".join(
                f"- [{i.get('severity', 'issue')}] {i.get('description', '')}"
                for i in issues
            )
            parts.append(f"Issues found:\n{issues_text}")

        if violations:
            violations_text = "\n".join(
                f"- {v.get('rule', '')}: {v.get('details', '')}"
                for v in violations
            )
            parts.append(f"Guide violations:\n{violations_text}")

        return "\n\n".join(parts) if parts else "Review rejected without specific feedback."

    def get_detailed_remediation(
        self,
        chunk_spec: dict,
        attempt_history: list[dict],
        guide: dict,
    ) -> tuple[str, dict]:
        """Get detailed step-by-step remediation after multiple failures.

        This is called after 3+ failed attempts to get very specific
        instructions for what needs to change.

        Returns:
            Tuple of (remediation_text, usage_info)
        """
        guide_text = self._format_guide(guide)
        criteria_text = "\n".join(f"- {c}" for c in chunk_spec.get("acceptance_criteria", []))

        # Format attempt history
        history_text = ""
        for attempt in attempt_history:
            history_text += f"\n### Attempt {attempt['attempt']}\n"
            if attempt.get('diff'):
                # Truncate diff if too long
                diff = attempt['diff']
                if len(diff) > 2000:
                    diff = diff[:2000] + "\n... (truncated)"
                history_text += f"**Diff:**\n```\n{diff}\n```\n"
            if attempt.get('issues'):
                history_text += "**Issues found:**\n"
                for issue in attempt['issues']:
                    history_text += f"- [{issue.get('severity', 'issue')}] {issue.get('description', '')}\n"
            if attempt.get('review', {}).get('feedback_for_retry'):
                history_text += f"**Feedback:** {attempt['review']['feedback_for_retry']}\n"

        prompt = f"""You are a senior software architect. A junior developer has tried to implement a task {len(attempt_history)} times and failed each time.

## Task Specification
**Title:** {chunk_spec.get('title', 'Untitled')}
**Description:** {chunk_spec.get('description', '')}

**Acceptance Criteria:**
{criteria_text}

## Code Guide
{guide_text}

## Attempt History
{history_text}

## Your Job
Analyze the pattern of failures and provide VERY SPECIFIC, step-by-step instructions that will definitely succeed.

Do NOT be vague. Instead of saying "handle errors properly", say exactly:
- "In file X, line Y, add a try/except block around Z"
- "Change function A to return Optional[B] instead of B"
- "Add validation: if not param: raise ValueError('param required')"

Provide:
1. Root cause analysis (what pattern of mistakes is being repeated?)
2. EXACT changes needed (file names, function names, specific code changes)
3. A checklist the developer can follow step-by-step

Be extremely specific and actionable. The next attempt MUST succeed."""

        messages = [{"role": "user", "content": prompt}]
        response, usage = self._call_api(messages)

        return response, usage

    def _format_guide(self, guide: dict) -> str:
        """Format guide dict as readable text."""
        lines = []
        for section, rules in guide.items():
            lines.append(f"## {section.replace('_', ' ').title()}")
            if isinstance(rules, list):
                for rule in rules:
                    lines.append(f"- {rule}")
            elif isinstance(rules, dict):
                for key, value in rules.items():
                    lines.append(f"- {key}: {value}")
            else:
                lines.append(f"- {rules}")
            lines.append("")
        return "\n".join(lines)

    def collaborate(
        self,
        collaboration_prompt: str,
        conversation_history: list[dict],
        iteration: int,
        max_iterations: int,
    ) -> tuple[dict, dict]:
        """Respond to a collaboration turn based on the collaboration goal.

        This is for general-purpose collaboration (debates, brainstorming, review, etc.)
        NOT just code review. The response is freeform based on the collaboration prompt.

        Args:
            collaboration_prompt: The user's description of how agents should collaborate
            conversation_history: List of {role, content, provider} messages
            iteration: Current iteration number
            max_iterations: Maximum iterations allowed

        Returns:
            Tuple of (response_dict, usage_info)
            response_dict contains:
                - response: The actual response text
                - consensus_reached: bool - whether to stop iterating
                - reasoning: Why consensus was/wasn't reached
        """
        # Format conversation history
        history_text = ""
        for msg in conversation_history:
            role = msg.get("role", "unknown")
            provider = msg.get("provider", role)
            content = msg.get("content", "")

            if provider == "claude" or role == "worker":
                history_text += f"\n**Claude:**\n{content}\n"
            elif provider == "openai" or role == "reviewer":
                history_text += f"\n**OpenAI (You previously):**\n{content}\n"
            elif role == "system":
                history_text += f"\n*[System: {content}]*\n"

        prompt = f"""You are OpenAI, participating in a collaboration with Claude (Anthropic's AI).

## Collaboration Goal
{collaboration_prompt}

## Current State
- Iteration: {iteration} of {max_iterations}
- Your role: Provide independent critique, feedback, or responses as described in the collaboration goal

## Conversation So Far
{history_text if history_text else "(This is the start of the collaboration)"}

## Your Task
Based on the collaboration goal above, respond to Claude's latest contribution. Be:
- **Independent**: Give your genuine perspective, not just agreement
- **Critical**: Challenge assumptions, find weaknesses, push for improvement
- **Constructive**: Offer specific suggestions, not just criticism
- **Direct**: State your position clearly

At the end, assess whether consensus has been reached or if more iteration is needed.

Respond in JSON format:
```json
{{
    "response": "Your detailed response to Claude...",
    "consensus_reached": false,
    "reasoning": "Why consensus was or wasn't reached"
}}
```

If the collaboration goal has been achieved or you've reached genuine agreement, set consensus_reached to true."""

        messages = [{"role": "user", "content": prompt}]
        response_text, usage = self._call_api(messages)

        try:
            result = self._extract_json(response_text)
        except ValueError:
            # If JSON extraction fails, treat the whole response as the response text
            result = {
                "response": response_text,
                "consensus_reached": False,
                "reasoning": "Could not parse structured response"
            }

        return result, usage

    def decide_research_needed(self, plan: dict, chunk: dict) -> list[str]:
        """Decide what research is needed for a chunk."""
        # Check if plan already identified research needs
        research_topics = plan.get("research_needed", [])

        # Check chunk description for technical terms that might need verification
        description = chunk.get("description", "").lower()
        title = chunk.get("title", "").lower()

        # Keywords that often benefit from research
        research_keywords = [
            "algorithm", "protocol", "encryption", "hash", "auth",
            "oauth", "jwt", "api", "specification", "standard",
            "cusum", "statistical", "ml", "model", "neural",
        ]

        for keyword in research_keywords:
            if keyword in description or keyword in title:
                research_topics.append(f"Verify implementation approach for: {keyword}")

        return list(set(research_topics))
