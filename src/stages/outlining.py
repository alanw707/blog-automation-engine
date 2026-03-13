"""Outline generation and validation stage for blog automation.

This module handles creating structured content outlines using Claude AI
or fallback to template-based outlines when Claude is unavailable.
All prompts and site-specific strings are read from config['prompt_templates'].
"""

import logging
from typing import Any, Dict, List

from ..models import TopicCandidate


class OutlineGenerator:
    """Generates and validates structured content outlines.

    Creates 5-7 section outlines for blog posts using Claude AI with
    fallback to template-based outlines. Validates outline completeness
    and quality before passing to drafting stage.

    All prompt templates are loaded from config['prompt_templates'] and
    formatted with str.format() substitution.
    """

    def __init__(
        self,
        claude_client: Any,
        config: Dict[str, Any],
        logger: logging.Logger
    ):
        """Initialize outline generator.

        Args:
            claude_client: Anthropic Claude API client instance
            config: Configuration dictionary with API and quality settings
            logger: Logger instance for operations tracking
        """
        self.claude = claude_client
        self.config = config
        self.log = logger

    # ------------------------------------------------------------------
    # Public API

    def generate_outline(
        self,
        topic: TopicCandidate,
        brief: str,
        retry: bool = False,
    ) -> str:
        """Generate structured content outline.

        Creates numbered list outline (5-7 sections) based on topic and brief.
        Attempts Claude-powered generation first, falling back to template-based
        outline if Claude unavailable or fails.

        Args:
            topic: TopicCandidate with keyword and metadata
            brief: Strategic brief text from briefing stage
            retry: Whether this is a retry after failed validation

        Returns:
            Numbered outline text with 5-7 sections.
        """
        claude_model = self.config.get("apis", {}).get("claude_model_outline")
        metadata = topic.metadata or {}

        if self.claude and claude_model:
            try:
                system = self._build_outline_system(topic)
                user_prompt = self._build_outline_user(topic, brief, retry)

                competitor_note = self._competitor_context(metadata)
                if competitor_note:
                    user_prompt += f"\nCompetitor note: {competitor_note}"

                outline_text = self._claude_complete(
                    model=claude_model,
                    system_prompt=system,
                    user_prompt=user_prompt,
                    max_tokens=600,
                    temperature=0.3,
                )
                if outline_text:
                    return outline_text.strip()

            except Exception as exc:
                self.log.warning("Claude outline generation failed: %s", exc)

        return self._generate_fallback_outline(topic, retry)

    def validate_outline(self, outline: str) -> bool:
        """Validate outline structure and completeness.

        Args:
            outline: Outline text to validate

        Returns:
            True if outline meets quality threshold.
        """
        threshold = self.config.get("quality", {}).get("outline_score_threshold", 0.75)
        score = min(outline.count("\n"), 4) / 4
        return score >= threshold

    # ------------------------------------------------------------------
    # Prompt construction

    def _build_outline_system(self, topic: TopicCandidate) -> str:
        """Build system prompt for outline generation from config template."""
        metadata = topic.metadata or {}
        topic_type = metadata.get("topic_type", "pillar")
        geo = metadata.get("geo_target") or self._default_market()

        template = self._prompt("outline_system")
        if template:
            return template.format(
                site_name=self._site_name(),
                topic_type=topic_type,
                geo_target=geo,
                keyword=topic.keyword,
            )

        # Generic English default
        return (
            f"You are editorial director for {self._site_name()}. "
            f"Create 5-7 section outlines covering services, comparisons, and FAQ."
        )

    def _build_outline_user(self, topic: TopicCandidate, brief: str, retry: bool) -> str:
        """Build user prompt for outline generation from config template."""
        metadata = topic.metadata or {}
        topic_type = metadata.get("topic_type", "pillar")
        geo = metadata.get("geo_target") or self._default_market()
        retry_note = "Retry and improve" if retry else "First draft"

        template = self._prompt("outline_user")
        if template:
            return template.format(
                site_name=self._site_name(),
                keyword=topic.keyword,
                brief=brief,
                topic_type=topic_type,
                geo_target=geo,
                retry_note=retry_note,
            )

        # Generic English default
        lines = [
            f"{retry_note}: create a numbered outline based on this brief:",
            f"\nBrief:\n{brief}",
            f"\nTopic type: {topic_type}",
            f"Target market: {geo}",
            "Output: numbered list, each item ≤40 chars, cover service, support, FAQ.",
        ]
        return "\n".join(lines)

    def _generate_fallback_outline(self, topic: TopicCandidate, retry: bool = False) -> str:
        """Generate template-based outline when Claude unavailable."""
        sections = self._build_outline_sections(topic)

        header_template = self._prompt("fallback_outline_header_retry" if retry else "fallback_outline_header")
        if header_template:
            header = header_template.format(site_name=self._site_name(), keyword=topic.keyword)
        else:
            header = "## Content Outline (Revised)" if retry else "## Content Outline"

        outline_lines = [header]
        for idx, section in enumerate(sections, 1):
            outline_lines.append(f"{idx}. {section}")
        return "\n".join(outline_lines)

    def _build_outline_sections(self, topic: TopicCandidate) -> List[str]:
        """Build outline sections from config or generic defaults."""
        metadata = topic.metadata or {}
        fs = (self.config.get("prompt_templates", {}) or {}).get("fallback_sections", {}) or {}
        site_name = self._site_name()
        geo = metadata.get("geo_target") or self._default_market()

        def _render(template: str) -> str:
            try:
                return template.format(site_name=site_name, geo_target=geo, keyword=topic.keyword)
            except (KeyError, IndexError):
                return template

        base_raw = fs.get("base") or [
            "{site_name} service advantages",
            "Product models and target audience",
            "Setup and support process",
        ]
        sections = [_render(s) for s in base_raw]

        if metadata.get("is_comparison"):
            comp_raw = fs.get("comparison", "{site_name} vs competitors")
            sections.insert(1, _render(comp_raw))

        if metadata.get("is_geo"):
            geo_raw = fs.get("geo", "{geo_target} local customer experience")
            sections.append(_render(geo_raw))

        if metadata.get("is_campaign"):
            camp_raw = fs.get("campaign", "Seasonal / campaign use cases")
            sections.append(_render(camp_raw))

        faq_raw = fs.get("faq", "FAQ and next steps")
        sections.append(_render(faq_raw))

        return sections

    # ------------------------------------------------------------------
    # Helpers

    def _site_name(self) -> str:
        return self.config.get("site", {}).get("name") or self.config.get("brand_voice", {}).get("site_name", "our site")

    def _default_market(self) -> str:
        return self.config.get("site", {}).get("default_market", "global")

    def _prompt(self, name: str) -> str:
        templates = self.config.get("prompt_templates", {}) or {}
        return (templates.get(name) or "").strip()

    def _competitor_context(self, metadata: Dict[str, Any]) -> str:
        domain = metadata.get("source_domain")
        query = metadata.get("competitor_query")
        if not domain or not query:
            return ""
        return f"Competitor {domain} recently covered '{query}'; differentiate on our advantages."

    def _claude_complete(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        """Call Claude API for completion."""
        if not self.claude:
            raise RuntimeError("Claude client not available")
        response = self.claude.messages.create(
            model=model,
            system=system_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[{"role": "user", "content": user_prompt}],
        )
        chunks = []
        for block in response.content:
            if getattr(block, "type", None) == "text":
                chunks.append(block.text)
        return "".join(chunks)
