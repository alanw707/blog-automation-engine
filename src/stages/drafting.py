"""Post drafting stage for blog automation.

This module handles generating full blog posts from outlines using Claude AI
or fallback to template-based generation, with content enhancement including
internal links, images, and call-to-action footers.

All prompts and site-specific strings are read from config['prompt_templates'].
"""

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from ..models import TopicCandidate


class PostDrafter:
    """Generates complete blog posts with content enhancements.

    Creates long-form blog posts from outlines using Claude AI with
    fallback to template-based generation. Handles content enhancement:
    - Brand voice enforcement
    - Internal link insertion
    - Image injection
    - CTA footer appending

    All site-specific strings (domain, CTA contact, prompts) are read
    from the config dictionary, making this stage fully site-agnostic.
    """

    def __init__(
        self,
        claude_client: Any,
        config: Dict[str, Any],
        logger: logging.Logger,
        image_generator: Optional[Any] = None,
    ):
        """Initialize post drafter.

        Args:
            claude_client: Anthropic Claude API client instance
            config: Configuration dictionary with content settings
            logger: Logger instance for operations tracking
            image_generator: Optional image generation client
        """
        self.claude = claude_client
        self.config = config
        self.log = logger
        self.image_generator = image_generator

    # ------------------------------------------------------------------
    # Public API

    def generate_full_post(
        self,
        topic: TopicCandidate,
        outline: str,
        brief: str,
    ) -> str:
        """Generate complete blog post from outline.

        Creates long-form blog post in Markdown format using Claude AI
        or fallback to template-based generation. Includes brand voice,
        internal links, images, and CTA footer.

        Args:
            topic: TopicCandidate with keyword and metadata
            outline: Structured outline from outlining stage
            brief: Strategic brief from briefing stage

        Returns:
            Complete blog post in Markdown with H2/H3 structure.
        """
        claude_model = self.config.get("apis", {}).get("claude_model_full")
        metadata = topic.metadata or {}
        content: str = ""

        if self.claude and claude_model:
            try:
                system = self._build_full_post_system(topic)
                user_prompt = self._build_full_post_user(topic, outline, brief)

                if metadata.get("official_summary"):
                    user_prompt += f"\n- Official feature highlights: {metadata['official_summary']}"

                competitor_note = self._competitor_context(metadata)
                if competitor_note:
                    user_prompt += f"\n- Competitor note: {competitor_note}"

                full_text = self._claude_complete(
                    model=claude_model,
                    system_prompt=system,
                    user_prompt=user_prompt,
                    max_tokens=4200,
                    temperature=0.35,
                )
                if full_text:
                    content = full_text.strip()

            except Exception as exc:
                self.log.warning("Claude full post generation failed: %s", exc)

        if not content:
            content = self._generate_fallback_post(topic)

        return self.ensure_minimum_length(topic, outline, brief, content)

    def enforce_requirements(self, topic: TopicCandidate, content: str) -> str:
        """Ensure brand voice and keyword placement in content.

        Adds required brand phrases to end of content if not present.

        Args:
            topic: TopicCandidate with metadata
            content: Blog post content

        Returns:
            Content with brand voice requirements enforced
        """
        voice_cfg = self.config.get("brand_voice", {}) or {}
        required_phrases = voice_cfg.get("key_phrases", []) or []
        max_required = int(voice_cfg.get("max_enforced_key_phrases", 2))
        keyword = topic.keyword if isinstance(topic.keyword, str) else ""
        missing: List[str] = []

        for phrase in required_phrases:
            if phrase in content:
                continue
            contextual = f"{keyword} | {phrase}" if keyword else phrase
            missing.append(contextual)

        if not missing or max_required <= 0:
            return content

        seed = abs(hash(keyword)) if keyword else 0
        selected: List[str] = []
        for idx in range(min(max_required, len(missing))):
            selected.append(missing[(seed + idx) % len(missing)])

        reminder_block = "\n\n".join(f"> {line}" for line in selected)
        return f"{content}\n\n{reminder_block}"

    def insert_internal_links(self, content: str) -> str:
        """Replace link placeholders with actual URLs from config."""
        replacements = self.config.get("content_inputs", {}).get("internal_links", {})
        for placeholder, url in replacements.items():
            marker = f"[{placeholder}]"
            if marker in content:
                content = content.replace(marker, f"[{placeholder}]({url})")
        return content

    def ensure_internal_links(self, content: str, min_links: int = 3) -> str:
        """Guarantee that the article surfaces enough internal links.

        Adds a recommended reading block if fewer than min_links internal
        URLs for the configured site domain exist in the content.

        Args:
            content: Blog post content
            min_links: Minimum number of internal links required

        Returns:
            Content with internal link block appended if needed
        """
        domain = self.config.get("site", {}).get("domain", "")
        if domain:
            existing_links = re.findall(rf"https?://{re.escape(domain)}/[^\s\)\]]+", content)
        else:
            existing_links = re.findall(r"https?://[^\s\)\]]+", content)

        if len(existing_links) >= min_links:
            return content

        targets = self.config.get("content_inputs", {}).get("internal_link_targets", [])
        if not targets:
            return content

        used_urls = set(existing_links)
        link_lines: List[str] = []

        for target in targets:
            url = target.get("url")
            if not url or url in used_urls:
                continue
            label = target.get("label") or target.get("text") or self._site_name()
            description = target.get("description")
            line = f"- [{label}]({url})"
            if description:
                line += f" — {description}"
            link_lines.append(line)
            used_urls.add(url)
            if len(used_urls) >= min_links:
                break

        if not link_lines:
            return content

        block_title_tmpl = self._prompt("related_links_heading")
        block_title = block_title_tmpl or "### Related links"
        block = "\n".join([block_title] + link_lines) if block_title not in content else "\n".join(link_lines)
        return f"{content.rstrip()}\n\n{block}\n"

    def inject_images(
        self,
        content: str,
        topic: TopicCandidate,
        brief: str,
        outline: str,
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        """Add hero images with optional AI generation."""
        hero_meta: Optional[Dict[str, Any]] = None

        if self.image_generator:
            hero_cfg = self.config.get("content_inputs", {}).get("hero_images", {})
            inline_hero = bool(hero_cfg.get("inline", False))
            hero_meta = self.image_generator.generate(topic, brief, outline)
            if hero_meta:
                if not inline_hero:
                    return content, hero_meta
                block = f"![{hero_meta['alt']}]({hero_meta['placeholder']})"
                if hero_meta.get("caption"):
                    block = f"{block}\n*{hero_meta['caption']}*"
                return f"{block}\n\n{content}", hero_meta

        images_cfg = self.config.get("content_inputs", {}).get("images", {})
        topic_type = (topic.metadata.get("topic_type") or "default").lower()
        image_entries = images_cfg.get(topic_type) or images_cfg.get("default") or []

        blocks = []
        for entry in image_entries:
            url = entry.get("url")
            alt = entry.get("alt") or self._site_name()
            caption = entry.get("caption")
            if not url:
                continue
            block = f"![{alt}]({url})"
            if caption:
                block = f"{block}\n*{caption}*"
            blocks.append(block)

        if not blocks:
            return content, hero_meta

        hero = "\n\n".join(blocks)
        if hero not in content:
            return f"{hero}\n\n{content}", hero_meta
        return content, hero_meta

    def append_cta(self, content: str, topic: TopicCandidate) -> str:
        """Add call-to-action footer to content.

        All CTA strings come from brand_voice config. No site-specific
        values are hardcoded in this method.

        Args:
            content: Blog post content
            topic: TopicCandidate with metadata

        Returns:
            Content with CTA footer appended
        """
        voice = self.config.get("brand_voice", {})
        title = voice.get("cta_title", f"Next steps: get started with {self._site_name()}")
        point_lines = voice.get("cta_points", [
            "Schedule a demo or consultation",
            "Get personalized setup advice",
            "Contact our support team",
        ])
        tagline = voice.get("cta_tagline", f"Reach out to {self._site_name()} for assistance.")

        # Contact details come from config — no hardcoded phone/email/URL
        contact_lines = voice.get("cta_contact", [])

        points = "\n".join(f"- {line}" for line in point_lines)
        contact = "\n".join(f"- {line}" for line in contact_lines) if contact_lines else ""

        cta_parts = [f"## {title}", points, tagline]
        if contact:
            cta_parts.append(contact)
        cta = "\n".join(cta_parts)

        if cta not in content:
            content = f"{content}\n\n{cta}"
        return content

    def ensure_minimum_length(
        self,
        topic: TopicCandidate,
        outline: str,
        brief: str,
        content: str,
    ) -> str:
        """Extend content until it satisfies minimum length requirements."""
        min_length = int(self.config.get("quality", {}).get("min_length", 4000))
        if len(content) >= min_length:
            return content

        claude_model = self.config.get("apis", {}).get("claude_model_full")

        if self.claude and claude_model:
            try:
                extend_template = self._prompt("extend_article")
                if extend_template:
                    extension_prompt = extend_template.format(
                        site_name=self._site_name(),
                        keyword=topic.keyword,
                        brief=brief[:800],
                        outline=outline[:1000],
                        min_length=min_length,
                    )
                    system_tmpl = self._prompt("extend_article_system")
                    system = system_tmpl.format(site_name=self._site_name()) if system_tmpl else (
                        f"You are a content editor for {self._site_name()}. Extend the given article."
                    )
                else:
                    extension_prompt = (
                        f"The article below is under {min_length} characters. "
                        f"Extend it with more examples, data, and FAQ sections. "
                        f"Keyword: {topic.keyword}\n"
                        f"Brief summary: {brief[:800]}\n"
                        f"Original outline:\n{outline[:1000]}\n"
                        "Write 3-4 additional paragraphs with sub-headings. Output pure Markdown:"
                    )
                    system = f"You are a content editor for {self._site_name()}. Extend the given article."

                extension = self._claude_complete(
                    model=claude_model,
                    system_prompt=system,
                    user_prompt=extension_prompt,
                    max_tokens=1600,
                    temperature=0.4,
                )
                if extension and extension.strip():
                    content = f"{content.rstrip()}\n\n{extension.strip()}"

            except Exception as exc:
                self.log.warning("Claude length extension failed: %s", exc)

        if len(content) >= min_length:
            return content

        filler = self._build_length_padding(topic)
        return f"{content.rstrip()}\n\n{filler}"

    # ------------------------------------------------------------------
    # Prompt construction

    def _build_full_post_system(self, topic: TopicCandidate) -> str:
        """Build system prompt for full post generation from config template."""
        metadata = topic.metadata or {}
        topic_type = metadata.get("topic_type", "pillar")
        geo = metadata.get("geo_target") or self._default_market()

        template = self._prompt("full_post_system")
        if template:
            return template.format(
                site_name=self._site_name(),
                topic_type=topic_type,
                geo_target=geo,
                keyword=topic.keyword,
            )

        # Generic English default
        return (
            f"You are a content consultant for {self._site_name()}. "
            f"Write long-form Markdown articles with examples and internal links."
        )

    def _build_full_post_user(self, topic: TopicCandidate, outline: str, brief: str) -> str:
        """Build user prompt for full post generation from config template."""
        metadata = topic.metadata or {}
        topic_type = metadata.get("topic_type", "pillar")
        geo = metadata.get("geo_target") or self._default_market()
        min_length = self.config.get("quality", {}).get("min_length", 4000)

        template = self._prompt("full_post_user")
        if template:
            return template.format(
                site_name=self._site_name(),
                keyword=topic.keyword,
                brief=brief,
                outline=outline,
                topic_type=topic_type,
                geo_target=geo,
                min_length=min_length,
            )

        # Generic English default
        return (
            f"Write a complete blog post (Markdown) based on the following:\n"
            f"- Keyword: {topic.keyword}\n"
            f"- Brief: {brief}\n"
            f"- Outline:\n{outline}\n"
            f"- Topic type: {topic_type}\n"
            f"- Target market: {geo}\n"
            f"Requirements:\n"
            f"1. Highlight {self._site_name()} service advantages naturally.\n"
            f"2. Include at least 3 internal links.\n"
            f"3. Add concrete examples per section.\n"
            f"4. End with a CTA directing readers to purchase/contact.\n"
            f"5. Minimum {min_length} characters.\n"
            f"Output: pure Markdown, no YAML frontmatter."
        )

    # ------------------------------------------------------------------
    # Fallback content generation (no Claude)

    def _generate_fallback_post(self, topic: TopicCandidate) -> str:
        """Generate template-based post when Claude unavailable."""
        sections = self._build_outline_sections(topic)
        paragraphs = []
        for section in sections:
            paragraph = self._build_section_paragraph(section, topic)
            paragraphs.append(paragraph)
        return "\n\n".join(paragraphs)

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

    def _build_section_paragraph(self, section_title: str, topic: TopicCandidate) -> str:
        """Build a template paragraph for a section."""
        metadata = topic.metadata or {}
        body = [
            f"## {section_title}",
            self._select_feature_statement(section_title, topic),
        ]
        if metadata.get("is_comparison") or "competitor" in section_title.lower():
            body.append(
                f"Compare processor speed, connectivity, app compatibility, and warranty to highlight "
                f"{self._site_name()}'s advantages."
            )
        if metadata.get("is_geo"):
            geo = metadata.get("geo_target", self._default_market())
            body.append(
                f"For customers in {geo}: share setup tips, local content options, and support availability."
            )
        if metadata.get("is_campaign"):
            body.append("Tie in seasonal or event context with practical recommendations.")
        if metadata.get("topic_type") == "faq" or "faq" in section_title.lower():
            body.append("Address common questions about payment, installation, warranty, and remote support.")
        return "\n\n".join(body)

    def _select_feature_statement(self, section_title: str, topic: TopicCandidate) -> str:
        """Select a brand feature statement from config pool."""
        voice = self.config.get("brand_voice", {})
        statements_cfg = voice.get("feature_statements", {})
        metadata = topic.metadata or {}
        pool: List[str] = []

        if metadata.get("official_summary"):
            pool.append(metadata["official_summary"])

        if metadata.get("is_comparison") or "competitor" in section_title.lower():
            pool.extend(statements_cfg.get("comparison", []) or [])

        if metadata.get("is_geo"):
            geo = metadata.get("geo_target") or self._default_market()
            for statement in statements_cfg.get("geo", []) or []:
                pool.append(statement.replace("{geo}", geo))

        pool.extend(statements_cfg.get("core", []) or [])

        if not pool:
            return f"{self._site_name()} delivers quality products with local service and support."

        index = abs(hash(f"{section_title}-{topic.keyword}")) % len(pool)
        return pool[index]

    def _build_length_padding(self, topic: TopicCandidate) -> str:
        """Generate deterministic fallback sections to pad content length."""
        site_name = self._site_name()
        filler_titles_tmpl = (
            self.config.get("prompt_templates", {}) or {}
        ).get("fallback_padding_sections") or [
            f"{site_name} customer stories and use cases",
            "After-sales support process",
            "Common questions and buying advice",
        ]
        site_name = self._site_name()
        filler_sections = []
        for t in filler_titles_tmpl:
            try:
                filler_sections.append(t.format(site_name=site_name, keyword=topic.keyword))
            except (KeyError, IndexError):
                filler_sections.append(t)

        paragraphs = [self._build_section_paragraph(title, topic) for title in filler_sections]

        support_url_targets = self.config.get("content_inputs", {}).get("internal_link_targets", [])
        support_link = ""
        for target in support_url_targets:
            if "contact" in (target.get("url") or "").lower() or "support" in (target.get("label") or "").lower():
                support_link = f"[{target.get('label', 'Support')}]({target['url']})"
                break

        if support_link:
            paragraphs.append(
                f"## Extended support\n"
                f"To arrange remote assistance, warranty service, or setup help, visit {support_link}."
            )

        return "\n\n".join(paragraphs)

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
