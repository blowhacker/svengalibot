"""ChatGPT manager integration for planning, review, and research."""

import json
import logging
import re
from pathlib import Path
from typing import Optional
from openai import OpenAI

logger = logging.getLogger(__name__)


class Manager:
    """Manager that uses ChatGPT for planning, review, and research."""

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o",
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
        # Find the first { and try to parse from there
        start = text.find('{')
        if start != -1:
            # Try parsing from each { to find valid JSON
            depth = 0
            for i, char in enumerate(text[start:], start):
                if char == '{':
                    depth += 1
                elif char == '}':
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start:i+1])
                        except json.JSONDecodeError:
                            # Try next opening brace
                            next_start = text.find('{', start + 1)
                            if next_start != -1:
                                start = next_start
                                depth = 0
                            else:
                                break

        raise ValueError(f"Could not extract JSON from response: {text[:200]}...")

    def _call_api(self, messages: list[dict], temperature: float = 1.0) -> str:
        """Make an API call to OpenAI."""
        # o1 models don't support temperature parameter
        kwargs = {
            "model": self.model,
            "messages": messages,
        }

        # Only add temperature for non-o1 models
        if not self.model.startswith("o1"):
            kwargs["temperature"] = temperature

        logger.info(f"Calling OpenAI API with model: {self.model}")
        response = self.client.chat.completions.create(**kwargs)
        content = response.choices[0].message.content
        logger.debug(f"OpenAI response (first 500 chars): {content[:500] if content else 'None'}")
        return content

    def plan_task(
        self,
        description: str,
        guide: dict,
        context: str = "",
    ) -> dict:
        """Break a task into implementable chunks."""
        prompt_template = self._load_prompt("plan")

        # Format guide as readable text
        guide_text = self._format_guide(guide)

        prompt = prompt_template.format(
            task_description=description,
            guide=guide_text,
            context=context or "No existing codebase context provided.",
        )

        messages = [{"role": "user", "content": prompt}]
        response = self._call_api(messages)

        return self._extract_json(response)

    def review_chunk(
        self,
        chunk_spec: dict,
        diff: str,
        worker_summary: str,
        guide: dict,
    ) -> dict:
        """Review code changes against specification and guide."""
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
        response = self._call_api(messages)

        return self._extract_json(response)

    def research(self, query: str, context: str = "") -> dict:
        """Research a topic for verification or information gathering."""
        prompt_template = self._load_prompt("research")

        prompt = prompt_template.format(
            query=query,
            context=context or "No additional context provided.",
        )

        messages = [{"role": "user", "content": prompt}]
        response = self._call_api(messages)

        return self._extract_json(response)

    def design_tests(
        self,
        chunk_spec: dict,
        implementation_summary: str,
        diff: str,
    ) -> dict:
        """Design tests for implemented code."""
        prompt_template = self._load_prompt("test")

        prompt = prompt_template.format(
            chunk_spec=json.dumps(chunk_spec, indent=2),
            implementation_summary=implementation_summary,
            diff=diff,
        )

        messages = [{"role": "user", "content": prompt}]
        response = self._call_api(messages)

        return self._extract_json(response)

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
