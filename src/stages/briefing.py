"""Content brief generation stage for blog automation.

This module handles generating strategic content briefs using Claude AI
or fallback to template-based briefs when Claude is unavailable.
All prompts and site-specific strings are read from config['prompt_templates'].
"""

import logging
from typing import Any, Dict, Optional

from ..models import TopicCandidate


class BriefGenerator:
    """Generates strategic content briefs for blog topics.

    Uses Claude AI for intelligent brief generation with fallback to
    template-based briefs. Briefs provide strategic direction including
    target audience, brand messaging, and topic-specific focus areas.

    All prompt templates are loaded from config['prompt_templates'] and
    formatted with str.format() substitution, making the stage fully
    site-agnostic.
    """

    def __init__(
        self,
        claude_client: Any,
        config: Dict[str, Any],
        logger: logging.Logger
    ):
        """Initialize brief generator.

        Args:
            claude_client: Anthropic Claude API client instance
            config: Configuration dictionary with API settings and prompt_templates
            logger: Logger instance for operations tracking
        """
        self.claude = claude_client
        self.config = config
        self.log = logger

    # ------------------------------------------------------------------
    # Public API

    def generate_brief(self, topic: TopicCandidate) -> str:
        """Generate strategic content brief for topic.

        Attempts Claude-powered brief generation first, falling back to
        template-based brief if Claude is unavailable or fails.

        Args:
            topic: TopicCandidate with keyword and metadata

        Returns:
            Strategic brief text (100-150 words) with target keyword,
            audience, brand messaging, and topic-specific focus areas.
        """
        metadata = topic.metadata or {}
        claude_model = self.config.get("apis", {}).get("claude_model_brief")

        if self.claude and claude_model:
            try:
                system = self._build_brief_system(topic)
                user_prompt = self._build_brief_user(topic)

                # Add competitor context if available
                competitor_note = self._competitor_context(metadata)
                if competitor_note:
                    user_prompt += f"\n- Competitor note: {competitor_note}"

                brief_text = self._claude_complete(
                    model=claude_model,
                    system_prompt=system,
                    user_prompt=user_prompt,
                    max_tokens=400,
                    temperature=0.2,
                )
                if brief_text:
                    return brief_text.strip()

            except Exception as exc:
                self.log.warning("Claude brief generation failed: %s", exc)

        return self._generate_fallback_brief(topic)

    # ------------------------------------------------------------------
    # Prompt construction

    def _build_brief_system(self, topic: TopicCandidate) -> str:
        """Build system prompt for brief generation from config template."""
        metadata = topic.metadata or {}
        topic_type = metadata.get("topic_type", "pillar")
        geo = metadata.get("geo_target") or self._default_market()

        template = self._prompt("brief_system")
        if template:
            return template.format(
                site_name=self._site_name(),
                topic_type=topic_type,
                geo_target=geo,
                keyword=topic.keyword,
            )

        # Generic English default
        return (
            f"You are a content strategist for {self._site_name()}. "
            f"Write a 100-word strategic brief focusing on {topic_type} content."
        )

    def _build_brief_user(self, topic: TopicCandidate) -> str:
        """Build user prompt for brief generation from config template."""
        metadata = topic.metadata or {}
        topic_type = metadata.get("topic_type", "pillar")
        geo = metadata.get("geo_target") or self._default_market()
        is_comparison = metadata.get("is_comparison", False)
        is_campaign = metadata.get("is_campaign", False)

        template = self._prompt("brief_user")
        if template:
            return template.format(
                site_name=self._site_name(),
                keyword=topic.keyword,
                topic_type=topic_type,
                geo_target=geo,
                is_comparison=is_comparison,
                is_campaign=is_campaign,
            )

        # Generic English default
        lines = [
            f"Write a 100-150 word strategic content brief:",
            f"- Keyword: {topic.keyword}",
            f"- Topic type: {topic_type}",
            f"- Target market: {geo}",
            f"- Comparison focus: {is_comparison}",
            f"- Seasonal/campaign: {is_campaign}",
        ]
        brand_points = self.config.get("brand_voice", {}).get("key_phrases", [])
        if brand_points:
            lines.append(f"- Emphasize: {'; '.join(brand_points[:3])}")
        return "\n".join(lines)

    def _generate_fallback_brief(self, topic: TopicCandidate) -> str:
        """Generate template-based brief when Claude unavailable."""
        metadata = topic.metadata or {}

        template = self._prompt("fallback_brief")
        if template:
            return template.format(
                site_name=self._site_name(),
                keyword=topic.keyword,
                topic_type=metadata.get("topic_type", "pillar"),
                geo_target=metadata.get("geo_target") or self._default_market(),
            )

        # Generic English fallback
        audience = self.config.get("brand_voice", {}).get("target_audience", "general audience")
        brand_points = self.config.get("brand_voice", {}).get("key_phrases", [])
        lines = [
            f"Target keyword: {topic.keyword}",
            f"Audience: {audience}",
        ]
        if brand_points:
            lines.append(f"Brand focus: {'; '.join(brand_points[:3])}")
        if metadata.get("is_geo"):
            lines.append(f"Geo focus: {metadata.get('geo_target')} — emphasize local service and delivery.")
        if metadata.get("is_comparison"):
            lines.append("Comparison focus: highlight advantages over competitors in quality, support, and warranty.")
        if metadata.get("is_campaign"):
            lines.append("Campaign context: tie in seasonal/event usage scenarios.")
        if metadata.get("topic_type") == "faq":
            lines.append("FAQ goal: answer most common installation, payment, and support questions.")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Helpers

    def _site_name(self) -> str:
        return self.config.get("site", {}).get("name") or self.config.get("brand_voice", {}).get("site_name", "our site")

    def _default_market(self) -> str:
        return self.config.get("site", {}).get("default_market", "global")

    def _prompt(self, name: str) -> str:
        """Fetch a prompt template from config by name. Returns '' if not set."""
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
