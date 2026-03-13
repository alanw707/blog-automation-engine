"""Quality validation stage for blog automation.

This module handles quality assessment using Claude AI with robust fallback
to heuristic-based scoring. All prompts and site-specific strings are read
from config['prompt_templates'].

PHASE 1 FIXES PRESERVED:
1. Token limit fix: Content truncation to 10K chars
2. JSON parsing: 3-strategy extraction with retry logic
3. Fallback scoring: Validated links, language, brand checks
"""

import json
import logging
import re
from typing import Any, Dict, Optional

from ..models import GeneratedPost


class QualityValidator:
    """Validates blog post quality with Claude AI and heuristic fallback.

    Primary validation uses Claude for comprehensive quality assessment.
    Fallback heuristic scoring validates: length, structure, brand voice,
    internal links, and language validation.

    All prompts and site-specific strings come from config.
    """

    def __init__(
        self,
        claude_client: Any,
        config: Dict[str, Any],
        logger: logging.Logger,
    ):
        """Initialize quality validator.

        Args:
            claude_client: Anthropic Claude API client instance
            config: Configuration dictionary with quality thresholds
            logger: Logger instance for operations tracking
        """
        self.claude = claude_client
        self.config = config
        self.log = logger

    # ------------------------------------------------------------------
    # Public API

    def validate_quality(self, post: GeneratedPost) -> float:
        """Validate post quality with Claude AI or fallback scoring.

        Attempts Claude-based quality assessment first, falling back to
        heuristic scoring if Claude unavailable or fails.

        Args:
            post: GeneratedPost with content, keyword, frontmatter

        Returns:
            Quality score (0-100) with post.quality_score updated
        """
        quality_cfg = self.config.get("quality", {})
        min_required = float(quality_cfg.get("min_score", 80))
        claude_weight = float(quality_cfg.get("claude_weight", 0.6))
        claude_weight = max(0.0, min(1.0, claude_weight))
        claude_floor = float(quality_cfg.get("claude_floor", 65))

        score = self._qa_with_claude(post)
        if score is not None:
            fallback_score = self._fallback_quality_score(post, log_details=False)
            combined = (score * claude_weight) + (fallback_score * (1 - claude_weight))

            if score < claude_floor and fallback_score >= min_required:
                combined = fallback_score
                self.log.info(
                    "QA: Claude score %.1f below floor %.1f, using fallback score %.1f",
                    score, claude_floor, fallback_score,
                )
            else:
                self.log.info(
                    "QA: combined score %.1f (claude=%.1f, fallback=%.1f, weight=%.2f)",
                    combined, score, fallback_score, claude_weight,
                )

            post.frontmatter["qa_components"] = {
                "claude_score": score,
                "fallback_score": fallback_score,
                "claude_weight": claude_weight,
            }
            post.quality_score = combined
            return combined

        return self._fallback_quality_score(post, log_details=True)

    # ------------------------------------------------------------------
    # Claude QA

    def _qa_with_claude(self, post: GeneratedPost) -> Optional[float]:
        """Perform Claude-based quality assessment.

        PHASE 1 FIXES:
        - Token limit fix: Truncate content to 10K chars
        - JSON parsing: 3-strategy extraction with retry
        - Retry logic: 3 attempts with detailed error logging

        Args:
            post: GeneratedPost to validate

        Returns:
            Quality score (0-100) or None if Claude QA fails
        """
        model = self.config.get("apis", {}).get("claude_model_qa")
        if not self.claude or not model:
            return None

        system = self._build_qa_system(post)

        # PHASE 1 FIX: Truncate content to avoid token limit
        content_sample = post.content[:10000] if len(post.content) > 10000 else post.content
        truncated_note = "(content truncated to first 10000 chars)" if len(post.content) > 10000 else ""

        user_prompt = self._build_qa_user(post, content_sample, truncated_note)

        for attempt in range(3):
            try:
                completion = self._claude_complete(
                    model=model,
                    system_prompt=system,
                    user_prompt=user_prompt,
                    max_tokens=500,
                    temperature=0.2,
                )

                # PHASE 1 FIX: 3-strategy JSON extraction
                cleaned = completion.strip()

                # Strategy 1: Extract from markdown code blocks
                if "```json" in cleaned:
                    json_start = cleaned.find("```json") + 7
                    json_end = cleaned.find("```", json_start)
                    if json_end > json_start:
                        cleaned = cleaned[json_start:json_end].strip()

                # Strategy 2: Remove backticks
                cleaned = cleaned.strip("`").strip()

                # Strategy 3: Find first { and last }
                first_brace = cleaned.find("{")
                last_brace = cleaned.rfind("}")
                if first_brace >= 0 and last_brace > first_brace:
                    cleaned = cleaned[first_brace:last_brace + 1]

                data = json.loads(cleaned)

                if "score" not in data:
                    self.log.warning("QA JSON missing 'score' field (attempt %s)", attempt + 1)
                    continue

                score = float(data.get("score", 0))
                if not (0 <= score <= 100):
                    self.log.warning("QA score out of range: %.1f (attempt %s)", score, attempt + 1)
                    continue

                notes = data.get("notes") or []
                post.frontmatter["qa_notes"] = notes
                self.log.info("Claude QA successful: score=%.1f (attempt %s)", score, attempt + 1)
                return score

            except json.JSONDecodeError as exc:
                self.log.warning("QA JSON parse failed (attempt %s): %s", attempt + 1, exc)
            except ValueError as exc:
                self.log.warning("QA score conversion failed (attempt %s): %s", attempt + 1, exc)
            except Exception as exc:
                self.log.warning("Claude QA failed (attempt %s): %s", attempt + 1, exc)
                if "token" in str(exc).lower() or "limit" in str(exc).lower():
                    self.log.error("Token limit exceeded — content may need further truncation")
                    break

        return None

    # ------------------------------------------------------------------
    # Prompt construction

    def _build_qa_system(self, post: GeneratedPost) -> str:
        """Build system prompt for QA from config template."""
        metadata = {
            "topic_type": post.frontmatter.get("topic_type"),
            "geo_target": post.frontmatter.get("geo_target"),
        }
        topic_type = metadata.get("topic_type", "pillar")
        geo = metadata.get("geo_target") or self._default_market()

        template = self._prompt("quality_check_system")
        if template:
            return template.format(
                site_name=self._site_name(),
                topic_type=topic_type,
                geo_target=geo,
            )

        # Generic English default
        return (
            f"You are a quality auditor for {self._site_name()}. "
            f"Return a JSON object with 'score' (0-100) and 'notes' array."
        )

    def _build_qa_user(self, post: GeneratedPost, content_sample: str, truncated_note: str) -> str:
        """Build user prompt for QA from config template."""
        template = self._prompt("quality_check_user")
        if template:
            return template.format(
                site_name=self._site_name(),
                content_sample=content_sample,
                truncated_note=truncated_note,
                keyword=post.keyword,
                min_length=self.config.get("quality", {}).get("min_length", 4000),
            )

        # Generic English default
        criteria = self._quality_criteria()
        return (
            'Reply with pure JSON (no markdown blocks): {"score": 85, "notes": ["clear structure"]}\n'
            f"Evaluate this article against: {criteria}\n"
            f"{truncated_note}\n"
            f"Article content:\n{content_sample}\n"
        )

    def _quality_criteria(self) -> str:
        """Build quality criteria string from config."""
        lang = self.config.get("language", "en")
        domain = self.config.get("site", {}).get("domain", "")
        min_length = self.config.get("quality", {}).get("min_length", 4000)

        parts = [
            f"Minimum {min_length} characters",
            "Proper heading structure (H2/H3)",
            "Brand voice and product terms naturally present",
        ]
        if domain:
            parts.append(f"Internal links to {domain}")
        if lang and lang != "en":
            parts.append(f"Written primarily in {lang}")
        return "; ".join(parts)

    # ------------------------------------------------------------------
    # Fallback heuristic scoring

    def _fallback_quality_score(self, post: GeneratedPost, log_details: bool = True) -> float:
        """Calculate heuristic quality score.

        PHASE 1 FIX: Actually validates criteria instead of just awarding points.

        Criteria:
        - Length: ≥min_length chars (25 points)
        - Structure: H2 headings present (20 points)
        - Brand voice: Product terms present (20 points)
        - Internal links: site domain URLs (15 points)
        - Language: Configured language validation (20 points)

        Args:
            post: GeneratedPost to score
            log_details: Whether to log the score breakdown

        Returns:
            Total quality score (0-100)
        """
        content = post.content
        min_length = self.config.get("quality", {}).get("min_length", 4000)

        # Length validation (25 points)
        if len(content) >= min_length:
            length_score = 25
        elif len(content) >= min_length * 0.7:
            length_score = 10
        else:
            length_score = 0

        # Structure validation (20 points)
        has_structure = "##" in content
        structure_score = 20 if has_structure else 10

        # Brand voice validation (20 points)
        brand_terms = self.config.get("brand_voice", {}).get("product_terms", [])
        brand_count = sum(1 for term in brand_terms if term in content)
        if brand_count >= 3:
            brand_score = 20
        elif brand_count >= 2:
            brand_score = 15
        elif brand_count >= 1:
            brand_score = 10
        else:
            brand_score = 0

        # Internal links validation (15 points)
        site_domain = self.config.get("site", {}).get("domain", "")
        if site_domain:
            internal_links = re.findall(rf"https?://{re.escape(site_domain)}/[^\s\)\]]+", content)
        else:
            internal_links = re.findall(r"https?://[^\s\)\]]+", content)
        links_count = len(internal_links)
        if links_count >= 3:
            links_score = 15
        elif links_count >= 2:
            links_score = 10
        elif links_count >= 1:
            links_score = 5
        else:
            links_score = 0

        # Language validation (20 points) — driven by config
        language_score = self._validate_language(content)

        scores = {
            "length": length_score,
            "structure": structure_score,
            "brand": brand_score,
            "links": links_score,
            "language": language_score,
        }
        total = float(sum(scores.values()))
        post.quality_score = total

        if log_details:
            self.log.info(
                "Fallback quality score: %.1f (length=%d, structure=%d, brand=%d, links=%d, language=%d)",
                total, length_score, structure_score, brand_score, links_score, language_score,
            )

        return total

    def _validate_language(self, content: str) -> float:
        """Validate content language based on config setting.

        Returns up to 20 points based on language correctness.
        For Chinese (zh-TW): checks for Traditional Chinese character density.
        For English: checks for adequate word count.
        """
        lang = (self.config.get("language") or "en").lower()

        if lang in ("zh-tw", "zh_tw", "zh"):
            # Traditional Chinese validation
            chinese_chars = re.findall(r"[\u4e00-\u9fff]", content)
            chinese_char_count = len(chinese_chars)

            # Simplified Chinese indicators
            simplified_indicators = ["国", "为", "这", "么", "们", "时", "个", "过"]
            simplified_count = sum(content.count(char) for char in simplified_indicators)

            if chinese_char_count >= 500:
                if simplified_count < (chinese_char_count * 0.05):
                    return 20.0
                return 10.0
            elif chinese_char_count >= 200:
                return 10.0
            return 0.0

        if lang in ("zh-cn", "zh_cn"):
            # Simplified Chinese validation
            chinese_chars = re.findall(r"[\u4e00-\u9fff]", content)
            return 20.0 if len(chinese_chars) >= 500 else (10.0 if len(chinese_chars) >= 200 else 0.0)

        # Default: English — check word count
        word_count = len(re.findall(r"\b[a-zA-Z]+\b", content))
        if word_count >= 600:
            return 20.0
        elif word_count >= 300:
            return 10.0
        return 0.0

    # ------------------------------------------------------------------
    # Helpers

    def _site_name(self) -> str:
        return self.config.get("site", {}).get("name") or self.config.get("brand_voice", {}).get("site_name", "our site")

    def _default_market(self) -> str:
        return self.config.get("site", {}).get("default_market", "global")

    def _prompt(self, name: str) -> str:
        templates = self.config.get("prompt_templates", {}) or {}
        return (templates.get(name) or "").strip()

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
