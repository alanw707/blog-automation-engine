"""Data models for SVICLOUD blog automation."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass
class TopicCandidate:
    """Represents a potential blog topic for content generation.

    Attributes:
        keyword: The primary keyword/topic for the blog post
        search_volume: Estimated monthly search volume for the keyword
        opportunity_score: Calculated score indicating content opportunity (0.0-1.0)
        metadata: Additional context about the topic (source, category, etc.)
    """
    keyword: str
    search_volume: int = 0
    opportunity_score: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GeneratedPost:
    """Represents a generated blog post with all metadata.

    Attributes:
        title: Post title optimized for SEO and user engagement
        slug: URL-friendly version of the title
        content: Full HTML content of the blog post
        excerpt: Brief summary for previews and meta descriptions
        keyword: Primary keyword this post targets
        quality_score: Automated quality assessment score (0-100)
        frontmatter: Additional metadata (categories, tags, featured image, etc.)
    """
    title: str
    slug: str
    content: str
    excerpt: str
    keyword: str
    quality_score: float
    frontmatter: Dict[str, Any] = field(default_factory=dict)
