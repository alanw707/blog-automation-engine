"""SEO title generation stage.

All prompts and site-specific strings are read from config['prompt_templates'].
"""

from __future__ import annotations

import json
import logging
import random
import re
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional

from ..models import TopicCandidate


class TitleGenerator:
    """Generates SEO-friendly titles for each post.

    All title prompts and configuration are read from the config dictionary,
    making this stage fully site-agnostic.
    """

    def __init__(self, claude_client: Any, config: Dict[str, Any], logger: logging.Logger):
        """Initialize title generator.

        Args:
            claude_client: Anthropic Claude API client instance
            config: Configuration dictionary with SEO and prompt_templates settings
            logger: Logger instance for operations tracking
        """
        self.claude = claude_client
        self.config = config
        self.log = logger
        self.recent_titles: List[str] = []

    # ------------------------------------------------------------------
    # Public API

    def set_recent_titles(self, titles: List[str]) -> None:
        """Set the recent title history for deduplication."""
        limit = int(self.config.get("seo", {}).get("title", {}).get("recent_window", 30))
        trimmed = [t for t in titles if isinstance(t, str) and t.strip()]
        self.recent_titles = trimmed[:limit] if limit > 0 else trimmed

    def add_recent_title(self, title: str) -> None:
        """Add a newly generated title to the recent history."""
        if isinstance(title, str) and title.strip():
            self.recent_titles.insert(0, title.strip())
            limit = int(self.config.get("seo", {}).get("title", {}).get("recent_window", 30))
            if limit > 0:
                self.recent_titles = self.recent_titles[:limit]

    def generate_title(self, topic: TopicCandidate, brief: str, outline: str) -> Dict[str, Any]:
        """Generate a headline from Claude or fallback templates.

        Args:
            topic: TopicCandidate with keyword and metadata
            brief: Strategic brief text
            outline: Outline text

        Returns:
            Dict with 'title', 'candidates', and 'slug_source' keys
        """
        seo_cfg = self.config.get("seo", {}).get("title", {})
        claude_model = seo_cfg.get("claude_model")
        candidates: List[str] = []

        configured_max = int(seo_cfg.get("max_length", 60))
        min_len = int(seo_cfg.get("min_length", 30))

        def has_cjk(text: str) -> bool:
            return any("\u4e00" <= ch <= "\u9fff" for ch in (text or ""))

        max_len = configured_max
        if has_cjk(topic.keyword):
            min_zh = int(seo_cfg.get("min_length_zh", 30))
            max_len = max(max_len, min_zh)

        if self.claude and claude_model:
            try:
                response = self._claude_titles(topic, brief, outline, claude_model, max_len=max_len)
                if response:
                    candidates = response
            except Exception as exc:
                self.log.warning("Claude title generation failed: %s", exc)

        if not candidates:
            candidates = self._fallback_titles(topic, seo_cfg)

        overused_tokens = seo_cfg.get("overused_tokens") or []
        disallowed_tokens = seo_cfg.get("disallowed_tokens") or []
        allowed_topic_types = {
            (t or "").lower()
            for t in (seo_cfg.get("allow_disallowed_for_topic_types") or ["comparison", "campaign"])
        }
        topic_type = ((topic.metadata or {}).get("topic_type") or "pillar").lower()

        if disallowed_tokens and topic_type not in allowed_topic_types:
            filtered = [
                t for t in candidates
                if not any(tok and tok in t for tok in disallowed_tokens)
            ]
            if filtered:
                candidates = filtered

        def overused_count(text: str) -> int:
            return sum(1 for tok in overused_tokens if tok and tok in text)

        def finalize(text: str) -> str:
            text = text.strip()
            if not text:
                return text
            if len(text) > max_len:
                text = self._trim_safely(text, max_len)
            text = self._sanitize_title(text)
            text = self._fix_dangling_suffix(text, seo_cfg)
            return text.strip()

        def is_unsafe_end(text: str) -> bool:
            if not text:
                return True
            unsafe_chars = set(seo_cfg.get("unsafe_trailing_chars") or [])
            default_unsafe = {"電", "視", "盒", "保", "固", "售", "後", "免", "運", "安", "裝", "+", "＋", "-", "—", "–"}
            unsafe = unsafe_chars or default_unsafe
            return text[-1] in unsafe

        finalized = [finalize(t) for t in candidates]
        finalized = [t for t in finalized if t]
        if not finalized:
            fallback = finalize(topic.keyword or self._site_name())
            finalized = [fallback] if fallback else [self._site_name()]

        # Deduplicate against recent titles
        recent_titles = [t for t in self.recent_titles if t]
        if recent_titles:
            threshold = float(seo_cfg.get("recent_similarity_threshold", 0.82))
            drop_tokens = seo_cfg.get("normalize_drop_tokens") or []
            normalized_recent = [self._normalize_title(t, drop_tokens) for t in recent_titles]

            def is_similar(candidate: str) -> bool:
                norm = self._normalize_title(candidate, drop_tokens)
                if not norm:
                    return False
                return any(
                    SequenceMatcher(None, norm, ref).ratio() >= threshold
                    for ref in normalized_recent if ref
                )

            filtered = [t for t in finalized if not is_similar(t)]
            if filtered:
                finalized = filtered
            else:
                self.log.warning("All title candidates too similar to recent titles; keeping best available.")

        seed = abs(hash(topic.keyword or "")) ^ int(datetime.utcnow().strftime("%Y%m%d"))
        rng = random.Random(seed)
        finalized.sort(key=lambda t: (is_unsafe_end(t), overused_count(t), len(t), rng.random()))
        title = finalized[0]

        return {
            "title": title,
            "candidates": candidates,
            "slug_source": title,
        }

    # ------------------------------------------------------------------
    # Claude title generation

    def _claude_titles(
        self,
        topic: TopicCandidate,
        brief: str,
        outline: str,
        model: str,
        *,
        max_len: int,
    ) -> Optional[List[str]]:
        """Generate title candidates using Claude."""
        metadata = topic.metadata or {}
        geo = metadata.get("geo_target") or self._default_market()
        topic_type = metadata.get("topic_type") or "pillar"
        seo_cfg = self.config.get("seo", {}).get("title", {})

        disallowed_tokens = seo_cfg.get("disallowed_tokens") or []
        allowed_topic_types = {
            (t or "").lower()
            for t in (seo_cfg.get("allow_disallowed_for_topic_types") or ["comparison", "campaign"])
        }
        disallow_clause = ""
        if disallowed_tokens and str(topic_type).lower() not in allowed_topic_types:
            disallow_clause = f"Avoid these tokens in titles: {', '.join(map(str, disallowed_tokens))}. "

        recent_clause = ""
        recent_titles = [t for t in self.recent_titles if t][:6]
        if recent_titles:
            recent_list = "\n".join(f"- {t}" for t in recent_titles)
            recent_clause = f"\nAvoid titles too similar to these recent ones:\n{recent_list}\n"

        # Try config template first
        template = self._prompt("title_user")
        if template:
            try:
                prompt = template.format(
                    site_name=self._site_name(),
                    keyword=topic.keyword,
                    topic_type=topic_type,
                    geo_target=geo,
                    brief=brief[:800],
                    outline=outline[:800],
                    max_len=max_len,
                    disallow_clause=disallow_clause,
                    recent_clause=recent_clause,
                )
            except KeyError:
                prompt = template
        else:
            # Generic English default
            prompt = (
                f"Generate 3 SEO-friendly titles for a blog post about: {topic.keyword}\n"
                f"Topic type: {topic_type}\n"
                f"Target market: {geo}\n"
                f"Brief: {brief[:800]}\n"
                f"Max {max_len} characters per title. {disallow_clause}"
                f"{recent_clause}"
                'Return JSON: {"titles": [{"text": "...", "reason": "..."}]}'
            )

        system_template = self._prompt("title_system")
        system = system_template.format(site_name=self._site_name()) if system_template else (
            "You are an SEO copywriter. Output JSON data with titles focused on reader intent."
        )

        completion = self.claude.messages.create(
            model=model,
            system=system,
            max_tokens=400,
            temperature=0.4,
            messages=[{"role": "user", "content": prompt}],
        )

        text = "".join(block.text for block in completion.content if getattr(block, "type", "") == "text")
        cleaned = text.strip()
        if "```" in cleaned:
            start = cleaned.find("```")
            fence = cleaned[start + 3:]
            if fence.startswith("json"):
                fence = fence[4:]
            end = fence.find("```")
            if end > -1:
                cleaned = fence[:end].strip()
        cleaned = cleaned.strip("`").strip()
        first = cleaned.find("{")
        last = cleaned.rfind("}")
        if first >= 0 and last > first:
            cleaned = cleaned[first:last + 1]

        try:
            data = json.loads(cleaned)
            titles = [item.get("text") for item in data.get("titles", []) if item.get("text")]
            return [t for t in titles if isinstance(t, str)]
        except json.JSONDecodeError:
            self.log.warning("Title JSON parse failed: %s", cleaned[:200])
            return None

    # ------------------------------------------------------------------
    # Fallback title generation

    def _fallback_titles(self, topic: TopicCandidate, seo_cfg: Dict[str, Any]) -> List[str]:
        """Generate titles from fallback templates in config."""
        metadata = topic.metadata or {}
        geo = metadata.get("geo_target") or self._default_market()
        topic_type = metadata.get("topic_type") or ""

        templates = seo_cfg.get("fallback_templates") or [
            "{keyword} | {geo} Guide",
            "{keyword}: Complete {descriptor}",
            "{keyword} — {benefit}",
        ]
        benefits = seo_cfg.get("benefits") or ["quality", "value", "support"]
        descriptors = seo_cfg.get("descriptors") or ["Guide", "Review", "Overview"]

        seed = abs(hash(f"{topic.keyword}|{geo}|{topic_type}")) ^ int(datetime.utcnow().strftime("%Y%m%d"))

        def pick(seq: List[str], idx: int) -> str:
            if not seq:
                return ""
            return seq[(seed + idx) % len(seq)]

        def render(template: str, idx: int) -> str:
            benefit = pick(benefits, idx)
            descriptor = pick(descriptors, idx * 3 + 1)
            try:
                return template.format(
                    keyword=topic.keyword,
                    geo=geo,
                    topic_type=topic_type or "",
                    benefit=benefit,
                    descriptor=descriptor,
                    site_name=self._site_name(),
                )
            except KeyError:
                return template

        titles = [render(tpl, idx) for idx, tpl in enumerate(templates)]
        unique = []
        for title in titles:
            if title not in unique:
                unique.append(title)
        return unique

    # ------------------------------------------------------------------
    # Text processing utilities

    def _normalize_title(self, title: str, drop_tokens: Optional[List[str]] = None) -> str:
        if not title:
            return ""
        text = title.lower()
        text = re.sub(r"<[^>]+>", "", text)
        if drop_tokens:
            for token in drop_tokens:
                if not token:
                    continue
                text = text.replace(str(token).lower(), "")
        text = re.sub(r"[\s|｜:：,，。.\-—–]+", "", text)
        return text.strip()

    @staticmethod
    def _sanitize_title(title: str) -> str:
        """Remove explicit delivery/time promises from titles."""
        text = (title or "").strip()
        if not text:
            return text
        text = re.sub(
            r"\b\d+\s*(?:days?|hours?|hrs?)\s*(?:delivery|shipping|arrival)?",
            "fast",
            text,
            flags=re.IGNORECASE,
        )
        return text.strip()

    @staticmethod
    def _fix_dangling_suffix(title: str, seo_cfg: Dict[str, Any]) -> str:
        """Remove awkward dangling fragments at end of title."""
        text = (title or "").strip()
        if not text:
            return text

        text = re.sub(r"[\s+\-–—|｜：:,，、]+$", "", text).strip()
        if not text:
            return ""

        unsafe_chars = set(seo_cfg.get("unsafe_trailing_chars") or [])
        default_unsafe = {"電", "視", "盒", "保", "固", "售", "後", "免", "運", "安", "裝"}
        unsafe = unsafe_chars or default_unsafe

        if text and text[-1] in unsafe:
            for sep in ("、", "，", " ", ":", "：", "|", "｜"):
                if sep in text:
                    head, tail = text.rsplit(sep, 1)
                    head = head.rstrip(sep).strip()
                    if head and (len(tail) <= 3 or (tail and tail[-1] in unsafe)):
                        text = head
                        break

        while text and text[-1] in unsafe:
            text = text[:-1].strip()
        return text

    @staticmethod
    def _trim_safely(title: str, max_len: int) -> str:
        """Trim title at safe boundaries."""
        if len(title) <= max_len:
            return title
        cutoff = title[:max_len]
        for sep in ("｜", "|", "：", ":", "，", " ", "、"):
            if sep in cutoff:
                candidate = cutoff.rsplit(sep, 1)[0].strip()
                if candidate:
                    return candidate
        cleaned = re.sub(r"[^\w\u4e00-\u9fff]+$", "", cutoff)
        return cleaned.strip() or cutoff.strip()

    # ------------------------------------------------------------------
    # Helpers

    def _site_name(self) -> str:
        return self.config.get("site", {}).get("name") or self.config.get("brand_voice", {}).get("site_name", "our site")

    def _default_market(self) -> str:
        return self.config.get("site", {}).get("default_market", "global")

    def _prompt(self, name: str) -> str:
        templates = self.config.get("prompt_templates", {}) or {}
        return (templates.get(name) or "").strip()
