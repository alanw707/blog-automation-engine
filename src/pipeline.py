"""Blog automation pipeline.

Multi-site, config-driven orchestration: research → brief → outline → draft → title → QA → publish.
Supports WordPress REST API, Azure Blob Storage, and local filesystem publishers.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import smtplib
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone, date, timedelta
from difflib import SequenceMatcher
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import requests
import yaml
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from .models import GeneratedPost, TopicCandidate
from .stages.publishing import create_publisher

try:
    import anthropic
except ImportError:
    anthropic = None  # type: ignore

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore


def _slugify(text: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in text)
    slug = "-".join(part for part in cleaned.split("-") if part)
    return slug.lower()[:80]


def _env_or_raise(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


class BlogPipeline:
    """Config-driven multi-site blog automation pipeline.

    Usage:
        pipeline = BlogPipeline(Path("configs/aiprofilephotomaker.yaml"), dry_run=True)
        pipeline.run(max_posts=1)
    """

    def __init__(self, config_path: Path, dry_run: bool = False) -> None:
        self.config_path = Path(config_path)
        self.config = self._load_config(self.config_path)
        self.base_dir = self.config_path.parent
        safety = self.config.get("safety", {})
        self.dry_run = dry_run or safety.get("dry_run", False)
        self.log = self._setup_logging()
        self.db = self._init_database()
        self.claude = self._init_claude_client()
        self.openai_client = self._init_openai_client()
        self.embedding_model = self.config.get("apis", {}).get("openai_embedding", "text-embedding-3-small")
        self.publisher = create_publisher(self.config, self.log, self.dry_run)
        self.log.info(
            "BlogPipeline ready: site=%s method=%s dry_run=%s",
            self.config.get("site_name", "?"),
            self.config.get("publishing", {}).get("method", "?"),
            self.dry_run,
        )

    # ------------------------------------------------------------------
    # Init helpers

    def _load_config(self, config_path: Path) -> Dict[str, Any]:
        if not config_path.exists():
            raise FileNotFoundError(f"Config not found: {config_path}")
        with config_path.open("r", encoding="utf-8") as fp:
            return yaml.safe_load(fp) or {}

    def _setup_logging(self) -> logging.Logger:
        log_cfg = self.config.get("monitoring", {})
        log_level = getattr(logging, log_cfg.get("log_level", "INFO"))
        logger = logging.getLogger(f"blog_pipeline.{self.config.get('site_name', 'site')}")
        logger.setLevel(log_level)
        logger.handlers.clear()

        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        ch = logging.StreamHandler()
        ch.setFormatter(formatter)
        logger.addHandler(ch)

        log_path = log_cfg.get("log_path")
        if log_path:
            path = self._resolve_path(log_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(path)
            fh.setFormatter(formatter)
            logger.addHandler(fh)
        return logger

    def _init_database(self) -> sqlite3.Connection:
        storage_cfg = self.config.get("storage", {})
        db_path = storage_cfg.get("db_path", "data/posts_history.db")
        resolved = self._resolve_path(db_path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(resolved)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS posts_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                slug TEXT,
                keyword TEXT,
                quality_score REAL,
                status TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                metadata TEXT,
                embedding BLOB
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS run_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_at TEXT NOT NULL,
                topics_found INTEGER,
                unique_topics INTEGER,
                posts_attempted INTEGER,
                posts_published INTEGER,
                dry_run INTEGER
            )
            """
        )
        conn.commit()
        return conn

    def _init_claude_client(self):
        if anthropic is None:
            self.log.warning("anthropic not installed; generation disabled")
            return None
        try:
            api_key = _env_or_raise("CLAUDE_API_KEY")
            return anthropic.Anthropic(api_key=api_key)
        except RuntimeError as exc:
            self.log.warning("Claude unavailable: %s", exc)
            return None

    def _init_openai_client(self):
        if OpenAI is None:
            self.log.warning("openai not installed; embeddings disabled")
            return None
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            self.log.warning("OPENAI_API_KEY not set; embeddings disabled")
            return None
        project = os.getenv("OPENAI_PROJECT")
        return OpenAI(api_key=api_key, project=project)

    # ------------------------------------------------------------------
    # Core run loop

    def run(self, max_posts: Optional[int] = None) -> None:
        """Main entry point: research → generate → publish."""
        if self._emergency_stop():
            return

        topics = self.research_topics()
        unique_topics = self.filter_duplicates(topics)
        self.log.info("Processing %s unique topics (max_posts=%s)", len(unique_topics), max_posts)

        publishing_cfg = self.config.get("publishing", {})
        weekly_cap = publishing_cfg.get("max_posts_per_week", 2)
        max_per_run = publishing_cfg.get("max_posts_per_run") or weekly_cap
        try:
            max_per_run = int(max_per_run)
        except (TypeError, ValueError):
            max_per_run = weekly_cap
        limit = max_posts if max_posts is not None else max_per_run

        attempts = published = staged = skipped_dupes = skipped_fuzzy = 0
        existing_slugs = self._existing_slugs()
        existing_titles = self._existing_titles()
        fuzzy_threshold = float(self.config.get("safety", {}).get("fuzzy_title_threshold", 0.82))
        min_score = self.config.get("quality", {}).get("min_score", 75)

        for topic in unique_topics[:limit]:
            proposed_slug = _slugify(topic.keyword)
            if proposed_slug in existing_slugs:
                self.log.info("Skipping '%s' — slug already exists", topic.keyword)
                skipped_dupes += 1
                continue

            attempts += 1
            post = self.generate_post(topic)

            if self._is_title_duplicate(post.title, existing_titles, fuzzy_threshold):
                self.log.info("Skipping '%s' — title too similar to existing", topic.keyword)
                skipped_fuzzy += 1
                continue

            score = self.validate_quality(post)
            if score < min_score:
                self.log.warning("Quality %.1f below threshold (%.0f) for '%s'", score, min_score, post.title)
                self._save_draft(post)
                continue

            if self.dry_run:
                self.log.info("[DRY] Would publish '%s' (score %.1f)", post.title, score)
                staged += 1
                existing_slugs.add(post.slug)
                existing_titles.append(post.title)
                continue

            if self.publisher.publish_post(post):
                record_status = str(post.frontmatter.get("wp_status") or "published").lower()
                published += 1
                self._record_post(post, status=record_status)
                existing_slugs.add(post.slug)
                existing_titles.append(post.title)
            else:
                self._save_draft(post)
                existing_slugs.add(post.slug)
                existing_titles.append(post.title)

        self.db.execute(
            "INSERT INTO run_log (run_at, topics_found, unique_topics, posts_attempted, posts_published, dry_run) VALUES (?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                len(topics),
                len(unique_topics),
                attempts,
                published,
                int(self.dry_run),
            ),
        )
        self.db.commit()

        stats = {
            "topics_found": len(topics),
            "unique_topics": len(unique_topics),
            "attempts": attempts,
            "published": published,
            "staged": staged,
            "skipped_dupes": skipped_dupes,
            "skipped_fuzzy": skipped_fuzzy,
            "dry_run": self.dry_run,
        }
        self._send_report(stats)

    # ------------------------------------------------------------------
    # Topic research

    def research_topics(self) -> List[TopicCandidate]:
        """Derive topic candidates from seed keywords and optional sources."""
        candidates = self._load_seed_keyword_candidates()
        gsc_candidates = self._load_gsc_candidates()
        candidates = self._merge_candidates([candidates, gsc_candidates])

        if not candidates:
            self.log.warning("No topic candidates found; using empty list")
            return []

        sorted_candidates = sorted(candidates, key=lambda c: c.metadata.get("score", 0), reverse=True)
        self.log.info("Loaded %s topic candidates", len(sorted_candidates))
        return sorted_candidates

    def _load_seed_keyword_candidates(self) -> List[TopicCandidate]:
        seeds = self.config.get("content_inputs", {}).get("seed_keywords", [])
        candidates = []
        for entry in seeds:
            keyword = entry.get("keyword")
            if not keyword:
                continue
            base_score = entry.get("base_score", 70)
            volume = entry.get("volume_estimate", 200)
            intent = entry.get("intent", "informational")
            meta = {
                "score": base_score,
                "intent": intent,
                "source": "seed",
                "topic_type": self._intent_to_topic_type(intent),
            }
            candidates.append(TopicCandidate(
                keyword=keyword,
                search_volume=volume,
                opportunity_score=min(base_score / 100, 0.99),
                metadata=meta,
            ))
        return candidates

    @staticmethod
    def _intent_to_topic_type(intent: str) -> str:
        mapping = {
            "comparison": "comparison",
            "how-to": "how-to",
            "informational": "pillar",
            "navigational": "pillar",
            "troubleshooting": "faq",
            "reputation": "pillar",
        }
        return mapping.get(intent, "pillar")

    def _load_gsc_candidates(self) -> List[TopicCandidate]:
        gsc_cfg = self.config.get("content_inputs", {}).get("gsc", {})
        if not gsc_cfg:
            return []
        gsc_path = gsc_cfg.get("path")
        if not gsc_path:
            return []
        source_path = self._resolve_path(gsc_path)
        if not source_path.exists():
            return []
        try:
            data = json.loads(source_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            self.log.warning("Failed to parse GSC JSON: %s", exc)
            return []
        candidates = []
        for entry in data:
            query = entry.get("query") or entry.get("keyword")
            if not query:
                continue
            impressions = float(entry.get("impressions", 0))
            ctr = float(entry.get("ctr", 0))
            position = float(entry.get("position", 99))
            score = self._score_gsc_entry(impressions, ctr, position) * 100
            meta = {"score": score, "source": "gsc", "topic_type": "pillar"}
            candidates.append(TopicCandidate(
                keyword=query,
                search_volume=int(impressions),
                opportunity_score=min(score / 100, 0.99),
                metadata=meta,
            ))
        return candidates

    @staticmethod
    def _score_gsc_entry(impressions: float, ctr: float, position: float) -> float:
        impression_component = min(impressions / 5000, 1.0)
        ctr_gap = max(0.1 - ctr, 0)
        ctr_component = min(ctr_gap * 5, 1.0)
        position_component = max((20 - position) / 20, 0)
        raw = (impression_component * 0.5) + (ctr_component * 0.3) + (position_component * 0.2)
        return min(raw, 1.0)

    @staticmethod
    def _merge_candidates(groups: List[List[TopicCandidate]]) -> List[TopicCandidate]:
        merged: Dict[str, TopicCandidate] = {}
        for candidate in [c for group in groups for c in group]:
            key = candidate.keyword.lower()
            existing = merged.get(key)
            if not existing or candidate.metadata.get("score", 0) > existing.metadata.get("score", 0):
                merged[key] = candidate
        return list(merged.values())

    # ------------------------------------------------------------------
    # Deduplication

    def filter_duplicates(self, topics: Iterable[TopicCandidate]) -> List[TopicCandidate]:
        topics_list = list(topics)
        existing_keywords = {
            row[0].lower()
            for row in self.db.execute("SELECT DISTINCT keyword FROM posts_history WHERE keyword IS NOT NULL")
            if row[0]
        }
        if not topics_list:
            return []

        embedding_threshold = self.config.get("quality", {}).get("duplicate_threshold", 0.85)
        embedding_cache: Dict[str, List[float]] = {}
        existing_embeddings = self._load_existing_embeddings()

        unique: List[TopicCandidate] = []
        dropped = 0
        for topic in topics_list:
            key = topic.keyword.lower()
            if key in existing_keywords:
                dropped += 1
                continue
            if existing_embeddings and self.openai_client:
                vec = embedding_cache.get(key) or self._generate_embedding_vector(topic.keyword)
                if vec:
                    embedding_cache[key] = vec
                    if self._is_duplicate_embedding(vec, existing_embeddings, embedding_threshold):
                        dropped += 1
                        continue
            unique.append(topic)

        if dropped:
            self.log.info("Dropped %s duplicate topics", dropped)
        return unique

    # ------------------------------------------------------------------
    # Post generation

    def generate_post(self, topic: TopicCandidate) -> GeneratedPost:
        """Full generation pipeline: brief → outline → draft → title."""
        brief = self._generate_brief(topic)
        outline = self._generate_outline(topic, brief)
        content = self._generate_full_post(topic, outline, brief)
        content = self._insert_internal_links(content, topic)
        title = self._generate_title(topic)
        slug = _slugify(topic.keyword)
        excerpt = self._plain_excerpt(content)

        frontmatter = {
            "keyword": topic.keyword,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "topic_type": (topic.metadata or {}).get("topic_type"),
            "tags": [],
        }

        return GeneratedPost(
            title=title,
            slug=slug,
            content=content,
            excerpt=excerpt,
            keyword=topic.keyword,
            quality_score=0.0,
            frontmatter=frontmatter,
        )

    def _render_prompt(self, template_key: str, **kwargs) -> str:
        """Render a prompt template from config with given kwargs."""
        templates = self.config.get("prompt_templates", {})
        template = templates.get(template_key, "")
        if not template:
            return ""
        quality = self.config.get("quality", {})
        brand = self.config.get("brand_voice", {})
        link_targets = self.config.get("content_inputs", {}).get("internal_link_targets", [])
        internal_links = "\n".join(
            f"- [{t.get('label')}]({t.get('url')}) — {t.get('description', '')}"
            for t in link_targets if t.get("label") and t.get("url")
        )
        defaults = {
            "site_name": self.config.get("site_name", ""),
            "site_url": self.config.get("site_url", ""),
            "min_length": quality.get("min_length", 1500),
            "brand_voice_tone": brand.get("tone", ""),
            "key_phrases": ", ".join(brand.get("key_phrases", [])),
            "internal_links": internal_links,
            "max_length": self.config.get("seo", {}).get("title", {}).get("max_length", 60),
            "content": "",
            "keyword": "",
        }
        defaults.update(kwargs)
        try:
            return template.format(**defaults)
        except KeyError as exc:
            self.log.warning("Prompt template '%s' missing key: %s", template_key, exc)
            return template

    def _claude_complete(self, model: str, system_prompt: str, user_prompt: str,
                         max_tokens: int = 2000, temperature: float = 0.3) -> str:
        if not self.claude:
            return ""
        response = self.claude.messages.create(
            model=model,
            system=system_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return "".join(
            block.text for block in response.content
            if getattr(block, "type", None) == "text"
        )

    def _generate_brief(self, topic: TopicCandidate) -> str:
        model = self.config.get("apis", {}).get("claude_model_brief")
        if self.claude and model:
            system = self._render_prompt("system_role")
            user = self._render_prompt("brief", keyword=topic.keyword)
            if system and user:
                try:
                    result = self._claude_complete(model, system, user, max_tokens=400, temperature=0.2)
                    if result:
                        return result.strip()
                except Exception as exc:
                    self.log.warning("Brief generation failed: %s", exc)
        return f"Brief for: {topic.keyword}"

    def _generate_outline(self, topic: TopicCandidate, brief: str) -> str:
        model = self.config.get("apis", {}).get("claude_model_outline")
        if self.claude and model:
            system = self._render_prompt("system_role")
            user = self._render_prompt("outline", keyword=topic.keyword)
            if system and user:
                try:
                    result = self._claude_complete(model, system, user, max_tokens=600, temperature=0.3)
                    if result:
                        return result.strip()
                except Exception as exc:
                    self.log.warning("Outline generation failed: %s", exc)
        return f"# Outline\n- Introduction\n- Main content about {topic.keyword}\n- Conclusion\n- FAQ"

    def _generate_full_post(self, topic: TopicCandidate, outline: str, brief: str) -> str:
        model = self.config.get("apis", {}).get("claude_model_full")
        if self.claude and model:
            system = self._render_prompt("system_role")
            user = self._render_prompt("user_article", keyword=topic.keyword)
            if system and user:
                try:
                    result = self._claude_complete(model, system, user, max_tokens=4000, temperature=0.35)
                    if result:
                        content = result.strip()
                        # Extend if too short
                        min_length = self.config.get("quality", {}).get("min_length", 1500)
                        word_count = len(content.split())
                        if word_count < min_length * 0.6:
                            extend_prompt = self._render_prompt("extend_article", keyword=topic.keyword, content=content)
                            if extend_prompt:
                                extended = self._claude_complete(model, system, extend_prompt, max_tokens=2000, temperature=0.3)
                                if extended:
                                    content = extended.strip()
                        return content
                except Exception as exc:
                    self.log.warning("Full post generation failed: %s", exc)
        # Fallback stub
        kw = topic.keyword
        return (
            f"# {kw}\n\n"
            f"This is a placeholder article about {kw}. "
            f"Replace this with real content by configuring the Claude API.\n\n"
            f"## Introduction\n\nContent goes here.\n\n"
            f"## Key Points\n\n- Point 1\n- Point 2\n- Point 3\n\n"
            f"## FAQ\n\n**Q: What is {kw}?**\nA: Placeholder answer.\n"
        )

    def _generate_title(self, topic: TopicCandidate) -> str:
        model = self.config.get("apis", {}).get("claude_model_brief")
        if self.claude and model:
            system = self._render_prompt("system_role")
            user = self._render_prompt("title", keyword=topic.keyword)
            if system and user:
                try:
                    result = self._claude_complete(model, system, user, max_tokens=200, temperature=0.4)
                    if result:
                        cleaned = result.strip().strip("`")
                        first_brace = cleaned.find("[")
                        last_brace = cleaned.rfind("]")
                        if first_brace >= 0 and last_brace > first_brace:
                            cleaned = cleaned[first_brace:last_brace + 1]
                        titles = json.loads(cleaned)
                        if titles and isinstance(titles, list):
                            return str(titles[0]).strip()
                except Exception as exc:
                    self.log.warning("Title generation failed: %s", exc)
        return topic.keyword

    # ------------------------------------------------------------------
    # Quality validation

    def validate_quality(self, post: GeneratedPost) -> float:
        model = self.config.get("apis", {}).get("claude_model_qa")
        if self.claude and model:
            score = self._qa_with_claude(post, model)
            if score is not None:
                post.quality_score = score
                return score

        # Fallback heuristic scoring
        content = post.content
        min_length = self.config.get("quality", {}).get("min_length", 1500)
        word_count = len(content.split())
        length_score = 40 if word_count >= min_length else (20 if word_count >= min_length * 0.6 else 5)
        structure_score = 25 if "##" in content else 10
        links = re.findall(r"https?://[^\s\)\]]+", content)
        links_score = min(len(links) * 5, 20)
        brand_terms = self.config.get("brand_voice", {}).get("key_phrases", [])
        brand_hits = sum(1 for t in brand_terms if t in content)
        brand_score = min(brand_hits * 5, 15)
        total = float(length_score + structure_score + links_score + brand_score)
        post.quality_score = total
        return total

    def _qa_with_claude(self, post: GeneratedPost, model: str) -> Optional[float]:
        system = self._render_prompt("system_role")
        content_sample = post.content[:10000]
        user = self._render_prompt("quality_check", content=content_sample)
        if not system or not user:
            return None
        for attempt in range(3):
            try:
                result = self._claude_complete(model, system, user, max_tokens=300, temperature=0.1)
                cleaned = result.strip().strip("`")
                if "```json" in cleaned:
                    cleaned = cleaned.split("```json")[1].split("```")[0].strip()
                first = cleaned.find("{")
                last = cleaned.rfind("}")
                if first >= 0 and last > first:
                    cleaned = cleaned[first:last + 1]
                data = json.loads(cleaned)
                score = float(data["score"])
                if 0 <= score <= 100:
                    post.frontmatter["qa_notes"] = data.get("notes", [])
                    self.log.info("QA score %.1f (attempt %s)", score, attempt + 1)
                    return score
            except Exception as exc:
                self.log.warning("QA attempt %s failed: %s", attempt + 1, exc)
        return None

    # ------------------------------------------------------------------
    # Internal links

    def _insert_internal_links(self, content: str, topic: TopicCandidate) -> str:
        targets = self.config.get("content_inputs", {}).get("internal_link_targets", [])
        if not targets:
            return content
        existing_urls = set(re.findall(r"https?://[^\s\)\]]+", content))
        chosen = []
        for t in targets:
            url = t.get("url")
            label = t.get("label")
            if not url or not label or url in existing_urls:
                continue
            desc = t.get("description", "")
            line = f"- [{label}]({url})"
            if desc:
                line += f" — {desc}"
            chosen.append(line)
            existing_urls.add(url)

        if not chosen:
            return content

        is_zh = bool(re.search(r"[\u4e00-\u9fff]", content))
        section_title = "延伸閱讀" if is_zh else "Related Links"
        block = f"\n\n## {section_title}\n" + "\n".join(chosen)
        return content + block

    # ------------------------------------------------------------------
    # Draft saving

    def _save_draft(self, post: GeneratedPost) -> None:
        draft_dir = self._resolve_path("drafts")
        draft_dir.mkdir(parents=True, exist_ok=True)
        path = draft_dir / f"{post.slug}.json"
        payload = {
            "title": post.title,
            "slug": post.slug,
            "excerpt": post.excerpt,
            "keyword": post.keyword,
            "quality_score": post.quality_score,
            "frontmatter": post.frontmatter,
            "content": post.content,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self.log.info("Saved draft: %s", path)
        self._record_post(post, status="draft")

    # ------------------------------------------------------------------
    # DB helpers

    def _record_post(self, post: GeneratedPost, status: str) -> None:
        embedding_blob = None
        vec = self._generate_embedding_vector(post.keyword or post.title)
        if vec:
            embedding_blob = json.dumps(vec).encode("utf-8")
        self.db.execute(
            "INSERT INTO posts_history (title, slug, keyword, quality_score, status, metadata, embedding) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                post.title,
                post.slug,
                post.keyword,
                post.quality_score,
                status,
                json.dumps(post.frontmatter, ensure_ascii=False),
                embedding_blob,
            ),
        )
        self.db.commit()

    def _existing_slugs(self) -> Set[str]:
        rows = self.db.execute("SELECT slug FROM posts_history WHERE slug IS NOT NULL")
        return {row[0] for row in rows if row[0]}

    def _existing_titles(self) -> List[str]:
        rows = self.db.execute("SELECT title FROM posts_history WHERE title IS NOT NULL")
        return [row[0] for row in rows if row[0]]

    @staticmethod
    def _is_title_duplicate(title: str, existing: List[str], threshold: float) -> bool:
        for t in existing:
            if SequenceMatcher(None, title.lower(), t.lower()).ratio() >= threshold:
                return True
        return False

    # ------------------------------------------------------------------
    # Embeddings

    def _load_existing_embeddings(self) -> List[Dict[str, Any]]:
        if not self.openai_client:
            return []
        embeddings = []
        cursor = self.db.execute("SELECT id, keyword, embedding FROM posts_history WHERE embedding IS NOT NULL")
        for row in cursor:
            stored = row[2]
            if stored:
                try:
                    if isinstance(stored, bytes):
                        stored = stored.decode("utf-8")
                    vector = json.loads(stored)
                    embeddings.append({"id": row[0], "keyword": row[1], "embedding": vector})
                except (ValueError, TypeError):
                    pass
        return embeddings

    def _generate_embedding_vector(self, text: str) -> Optional[List[float]]:
        if not self.openai_client or not text.strip():
            return None
        try:
            response = self.openai_client.embeddings.create(model=self.embedding_model, input=text.strip())
            return response.data[0].embedding  # type: ignore
        except Exception as exc:
            self.log.warning("Embedding generation failed: %s", exc)
            return None

    @staticmethod
    def _is_duplicate_embedding(candidate: List[float], existing: List[Dict], threshold: float) -> bool:
        def cosine(a: List[float], b: List[float]) -> float:
            dot = sum(x * y for x, y in zip(a, b))
            na = math.sqrt(sum(x * x for x in a))
            nb = math.sqrt(sum(x * x for x in b))
            return dot / (na * nb) if na and nb else 0.0

        return any(cosine(candidate, row["embedding"]) >= threshold for row in existing)

    # ------------------------------------------------------------------
    # Utility helpers

    def _plain_excerpt(self, content: str, length: int = 220) -> str:
        from bs4 import BeautifulSoup
        import markdown as md
        try:
            html = md.markdown(content, extensions=["extra"])
            text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
        except Exception:
            text = content
        return text[:length].strip()

    def _resolve_path(self, relative: str) -> Path:
        path = Path(relative)
        if not path.is_absolute():
            return (self.base_dir / path).resolve()
        return path

    def _emergency_stop(self) -> bool:
        stop_file = self.config.get("safety", {}).get("emergency_stop_file")
        if not stop_file:
            return False
        if self._resolve_path(stop_file).exists():
            self.log.warning("Emergency stop engaged")
            return True
        return False

    def _send_report(self, stats: Dict[str, Any]) -> None:
        site = self.config.get("site_name", "Unknown site")
        lines = [
            f"{site} — blog automation run summary",
            f"  Topics found:   {stats.get('topics_found', 0)}",
            f"  Unique topics:  {stats.get('unique_topics', 0)}",
            f"  Attempted:      {stats.get('attempts', 0)}",
            f"  Published:      {stats.get('published', 0)}",
            f"  Staged:         {stats.get('staged', 0)}",
            f"  Skipped dupes:  {stats.get('skipped_dupes', 0)}",
            f"  Dry run:        {stats.get('dry_run', False)}",
        ]
        self.log.info(" | ".join(lines))

    def close(self) -> None:
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
