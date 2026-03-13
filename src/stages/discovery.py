"""Topic discovery stage for blog automation.

This module handles discovering blog topic candidates from multiple sources:
- Manual keyword plan (Markdown file with structured sections)
- Google Search Console (GSC) API data
- Competitor blog scraping

All site-specific strings are read from config. This stage is fully
site-agnostic: keyword brand terms, fallback topics, and diversity seeds
all come from config['content_inputs'].
"""

import json
import logging
import os
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

from ..models import TopicCandidate

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
except ImportError:
    service_account = None  # type: ignore
    build = None  # type: ignore

GSC_SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]


class TopicDiscovery:
    """Discovers and prioritizes blog topic candidates.

    Integrates multiple discovery sources with intelligent scoring:
    - Manual planning: Structured keyword lists with section priorities
    - GSC data: Search performance metrics (impressions, CTR, position)
    - Competitor analysis: Scraped topics from competitor blogs

    All site-specific strings (brand terms, fallback topics, seed topics)
    come from the config dictionary.
    """

    def __init__(
        self,
        config: Dict[str, Any],
        logger: logging.Logger,
        base_dir: Path,
    ):
        """Initialize topic discovery.

        Args:
            config: Configuration dictionary with content_inputs settings
            logger: Logger instance for operations tracking
            base_dir: Base directory for resolving relative paths
        """
        self.config = config
        self.log = logger
        self.base_dir = base_dir
        self.keyword_filters = self._compile_keyword_filters()
        self.angle_rules = (self.config.get("content_inputs", {}) or {}).get("angle_rules", {}) or {}

    # ------------------------------------------------------------------
    # Public API

    def discover_topics(self) -> List[TopicCandidate]:
        """Discover topics from all sources with fallback.

        Returns:
            List of TopicCandidate sorted by opportunity score (high to low)
        """
        self.auto_refresh_sources()

        plan_candidates = self._load_keyword_plan_candidates()
        gsc_candidates = self._load_gsc_candidates()
        competitor_candidates = self._load_competitor_candidates()
        official_candidates = self._load_official_feature_candidates()
        seed_candidates = self._load_seed_keyword_candidates()
        diversity_candidates = self._load_diversity_seed_topics()

        merged_candidates = self._merge_candidates([
            plan_candidates,
            gsc_candidates,
            competitor_candidates,
            official_candidates,
            seed_candidates,
            diversity_candidates,
        ])

        merged_candidates = [
            c for c in merged_candidates
            if not self._should_drop_overused(c.keyword, c.metadata)
        ]

        if not merged_candidates:
            return self._get_fallback_topics()

        sorted_candidates = sorted(
            merged_candidates,
            key=lambda c: c.metadata.get("score", 0),
            reverse=True,
        )

        self.log.info(
            "Loaded %s topic candidates (plan: %s, gsc: %s, competitor: %s, official: %s, seed: %s)",
            len(sorted_candidates),
            len(plan_candidates),
            len(gsc_candidates),
            len(competitor_candidates),
            len(official_candidates),
            len(seed_candidates),
        )

        return sorted_candidates

    def auto_refresh_sources(self) -> None:
        """Auto-refresh GSC and competitor data if configured."""
        self.refresh_gsc_topics()
        self.refresh_competitor_topics()
        self.refresh_official_features()

    def refresh_gsc_topics(self) -> None:
        """Refresh Google Search Console topic data."""
        gsc_cfg = self.config.get("content_inputs", {}).get("gsc", {})

        if not gsc_cfg.get("auto_refresh"):
            return

        if service_account is None or build is None:
            self.log.warning("Skipping GSC refresh: google-api-python-client not installed")
            return

        site = gsc_cfg.get("site")
        cred_setting = gsc_cfg.get("credentials") or os.getenv("GSC_CREDENTIALS_PATH")

        if not site or not cred_setting:
            self.log.warning("Skipping GSC refresh: missing site or credentials path")
            return

        cred_path = self._resolve_path(cred_setting)
        if not cred_path.exists():
            self.log.warning("GSC credentials not found at %s", cred_path)
            return

        target_path = self._resolve_path(gsc_cfg.get("path", "data/gsc_topics.json"))
        lookback = int(gsc_cfg.get("lookback_days", 30))
        min_impressions = float(gsc_cfg.get("min_impressions", 1))
        max_position = float(gsc_cfg.get("max_position", 50))
        limit = int(gsc_cfg.get("limit", 200))

        try:
            creds = service_account.Credentials.from_service_account_file(
                str(cred_path), scopes=GSC_SCOPES
            )
            service = build("searchconsole", "v1", credentials=creds, cache_discovery=False)

            end_date = date.today() - timedelta(days=1)
            start_date = end_date - timedelta(days=lookback)

            request_body = {
                "startDate": start_date.isoformat(),
                "endDate": end_date.isoformat(),
                "dimensions": ["query"],
                "rowLimit": limit,
                "type": "web",
                "dataState": "all",
            }

            response = service.searchanalytics().query(siteUrl=site, body=request_body).execute()
            rows = response.get("rows", [])

            filtered = []
            for row in rows:
                query = row.get("keys", [""])[0]
                impressions = row.get("impressions", 0)
                position = row.get("position", 99)
                ctr = row.get("ctr", 0)

                if impressions < min_impressions or position > max_position:
                    continue

                filtered.append({
                    "query": query,
                    "impressions": impressions,
                    "position": position,
                    "ctr": ctr,
                })

            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(json.dumps(filtered, ensure_ascii=False, indent=2), encoding="utf-8")
            self.log.info("Refreshed GSC topics (%s rows)", len(filtered))

        except Exception as exc:
            self.log.warning("GSC refresh failed: %s", exc)

    def refresh_competitor_topics(self) -> None:
        """Refresh competitor topic data by scraping blogs."""
        comp_cfg = self.config.get("content_inputs", {}).get("competitors", {})

        if not comp_cfg.get("auto_refresh"):
            return

        sites = comp_cfg.get("sites") or {}
        if not sites:
            self.log.warning("Competitor auto-refresh enabled but no sites configured")
            return

        min_length = int(comp_cfg.get("min_length", 4))
        entries = []

        for domain, site_cfg in sites.items():
            urls = self._expand_competitor_urls(domain, site_cfg)
            for url in urls:
                keywords = self._scrape_competitor_keywords(url, min_length)
                for keyword in keywords:
                    entries.append({
                        "query": keyword,
                        "source": domain,
                        "source_url": url,
                        "impressions": comp_cfg.get("base_volume", 150),
                        "position": 15,
                        "ctr": 0.01,
                    })

        if not entries:
            self.log.warning("Competitor refresh yielded no keywords")
            return

        target_path = self._resolve_path(comp_cfg.get("path", "data/competitor_topics.json"))
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
        self.log.info("Refreshed competitor topics (%s rows)", len(entries))

    def refresh_official_features(self) -> None:
        """Refresh official site feature topics."""
        cfg = self.config.get("content_inputs", {}).get("official_features", {})
        if not cfg.get("auto_refresh"):
            return

        url = cfg.get("url")
        if not url:
            return

        target_path = self._resolve_path(cfg.get("path", "data/official_features.json"))
        entries = self._scrape_official_features(url)
        if not entries:
            self.log.warning("Official site scrape produced no entries")
            return

        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
        self.log.info("Refreshed official feature topics (%s rows)", len(entries))

    # ------------------------------------------------------------------
    # Candidate loaders

    def _load_seed_keyword_candidates(self) -> List[TopicCandidate]:
        """Load curated seed keywords from config.content_inputs.seed_keywords."""
        content_cfg = self.config.get("content_inputs", {})
        raw_seeds = content_cfg.get("seed_keywords") or []
        if not raw_seeds:
            return []

        locale_targets = content_cfg.get("locale_targets", [])
        candidates: List[TopicCandidate] = []

        for entry in raw_seeds:
            if isinstance(entry, str):
                keyword = entry.strip()
                base_score = 92
                volume_estimate = 220
                extra_meta: Dict[str, Any] = {}
            elif isinstance(entry, dict):
                keyword = (entry.get("keyword") or entry.get("query") or "").strip()
                base_score = int(entry.get("base_score", 92))
                volume_estimate = int(entry.get("volume_estimate", 220))
                extra_meta = {k: v for k, v in entry.items() if k not in {"keyword", "query", "base_score", "volume_estimate"}}
            else:
                continue

            if not keyword or not self._keyword_allowed(keyword):
                continue

            candidate = self._build_topic_candidate(
                keyword=keyword,
                base_score=base_score,
                section="seed",
                volume_estimate=volume_estimate,
                locale_targets=locale_targets,
                source="config:content_inputs.seed_keywords",
            )
            if extra_meta:
                candidate.metadata["seed"] = extra_meta
                candidate.metadata["score"] = max(candidate.metadata.get("score", base_score), base_score)
            candidates.append(candidate)

        return candidates

    def _load_diversity_seed_topics(self) -> List[TopicCandidate]:
        """Load diversity seed topics from config.content_inputs.diversity_seeds.

        Falls back to empty list if not configured. No hardcoded Chinese strings here.
        """
        content_cfg = self.config.get("content_inputs", {})
        raw_seeds = content_cfg.get("diversity_seeds") or []
        if not raw_seeds:
            return []

        locale_targets = content_cfg.get("locale_targets", [])
        candidates: List[TopicCandidate] = []

        for entry in raw_seeds:
            if isinstance(entry, str):
                keyword = entry.strip()
                base_score = 85
            elif isinstance(entry, dict):
                keyword = (entry.get("keyword") or "").strip()
                base_score = int(entry.get("base_score", 85))
            else:
                continue

            if not keyword:
                continue

            candidate = self._build_topic_candidate(
                keyword=keyword,
                base_score=base_score,
                section="diversity-seed",
                volume_estimate=220,
                locale_targets=locale_targets,
                source="config:content_inputs.diversity_seeds",
            )
            candidates.append(candidate)

        return candidates

    def _load_keyword_plan_candidates(self) -> List[TopicCandidate]:
        """Load topics from manual keyword plan (Markdown file)."""
        content_cfg = self.config.get("content_inputs", {})
        keyword_source = content_cfg.get("keyword_source")

        if not keyword_source:
            return []

        source_path = self._resolve_path(keyword_source)
        if not source_path.exists():
            self.log.warning("Keyword plan source %s does not exist", source_path)
            return []

        section_weights = {
            "target keywords not ranking": 90,
            "primary keywords (target now)": 88,
            "secondary keywords (3-6 months)": 72,
            "create chinese blog posts": 84,
            "create english blog posts": 84,
            "create location-specific pages": 78,
            "keyword strategy expansion": 70,
        }
        base_volumes = {
            "target keywords not ranking": 550,
            "primary keywords (target now)": 500,
            "secondary keywords (3-6 months)": 360,
            "create chinese blog posts": 420,
            "create english blog posts": 420,
            "create location-specific pages": 300,
            "keyword strategy expansion": 320,
        }

        locale_targets = content_cfg.get("locale_targets", [])
        current_section = ""
        candidates: List[TopicCandidate] = []

        with source_path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                if line.startswith("##"):
                    current_section = line.strip("# ").lower()
                    continue
                if line.startswith("###"):
                    current_section = line.strip("# ").lower()
                    continue
                if line.startswith("**") and line.endswith("**"):
                    current_section = line.strip("* ").lower()
                    continue
                if not line.startswith("- "):
                    continue

                keyword = self._extract_keyword_from_line(line)
                if not keyword or not self._looks_like_keyword(keyword):
                    continue
                if not self._keyword_allowed(keyword):
                    continue

                base_score = section_weights.get(current_section, 65)
                volume_estimate = base_volumes.get(current_section, 300)

                candidate = self._build_topic_candidate(
                    keyword=keyword,
                    base_score=base_score,
                    section=current_section or "plan",
                    volume_estimate=volume_estimate,
                    locale_targets=locale_targets,
                    source=str(source_path),
                )
                candidates.append(candidate)

        return candidates

    def _load_gsc_candidates(self) -> List[TopicCandidate]:
        """Load topics from Google Search Console JSON data."""
        gsc_config = self.config.get("content_inputs", {}).get("gsc")
        if not gsc_config:
            return []

        gsc_path = gsc_config.get("path")
        if not gsc_path:
            return []

        source_path = self._resolve_path(gsc_path)
        if not source_path.exists():
            self.log.warning("GSC data file %s not found", source_path)
            return []

        try:
            data = json.loads(source_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            self.log.warning("Failed to parse GSC JSON: %s", exc)
            return []

        locale_targets = self.config.get("content_inputs", {}).get("locale_targets", [])
        candidates: List[TopicCandidate] = []

        for entry in data:
            query = entry.get("query") or entry.get("keyword")
            if not query or not self._keyword_allowed(query):
                continue

            impressions = float(entry.get("impressions", 0))
            ctr = float(entry.get("ctr", 0))
            position = float(entry.get("position", 99))
            score = self._score_gsc_entry(impressions, ctr, position)

            candidate = self._build_topic_candidate(
                keyword=query,
                base_score=int(score * 100),
                section="gsc",
                volume_estimate=int(impressions),
                locale_targets=locale_targets,
                source=str(source_path),
            )
            candidate.metadata["gsc"] = {"impressions": impressions, "ctr": ctr, "position": position}
            candidate.metadata["score"] = score * 100
            candidates.append(candidate)

        return candidates

    def _load_competitor_candidates(self) -> List[TopicCandidate]:
        """Load topics from competitor blog scraping JSON."""
        content_cfg = self.config.get("content_inputs", {})
        competitor_cfg = content_cfg.get("competitors", {})
        path = competitor_cfg.get("path", "data/competitor_topics.json")

        source_path = self._resolve_path(path)
        if not source_path.exists():
            return []

        try:
            entries = json.loads(source_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            self.log.warning("Failed to parse competitor topics JSON: %s", exc)
            return []

        locale_targets = content_cfg.get("locale_targets", [])
        base_score = competitor_cfg.get("base_score", 75)
        base_volume = competitor_cfg.get("base_volume", 150)
        candidates: List[TopicCandidate] = []

        for entry in entries:
            keyword = entry.get("query")
            if not keyword or not self._passes_competitor_quality_gate(keyword, min_length=4):
                continue
            if not self._keyword_allowed(keyword):
                continue

            impressions = int(entry.get("impressions", base_volume))
            score = base_score + min(int(impressions / 20), 10)

            candidate = self._build_topic_candidate(
                keyword=keyword,
                base_score=score,
                section="competitor",
                volume_estimate=impressions,
                locale_targets=locale_targets,
                source=str(source_path),
            )
            candidate.metadata["source_domain"] = entry.get("source")
            candidate.metadata["competitor_query"] = keyword
            candidate.metadata["score"] = score
            candidates.append(candidate)

        return candidates

    def _load_official_feature_candidates(self) -> List[TopicCandidate]:
        """Load official site feature topics from JSON."""
        cfg = self.config.get("content_inputs", {}).get("official_features", {})
        path = cfg.get("path")
        if not path:
            return []

        source_path = self._resolve_path(path)
        if not source_path.exists():
            return []

        try:
            entries = json.loads(source_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            self.log.warning("Failed to parse official feature JSON: %s", exc)
            return []

        base_score = cfg.get("base_score", 90)
        base_volume = cfg.get("base_volume", 420)
        locale_targets = self.config.get("content_inputs", {}).get("locale_targets", [])
        candidates: List[TopicCandidate] = []

        for entry in entries:
            keyword = entry.get("title") or entry.get("keyword")
            if not keyword or not self._passes_competitor_quality_gate(keyword, min_length=4):
                continue
            if not self._keyword_allowed(keyword):
                continue

            candidate = self._build_topic_candidate(
                keyword=keyword,
                base_score=base_score,
                section="official",
                volume_estimate=base_volume,
                locale_targets=locale_targets,
                source=str(source_path),
            )
            candidate.metadata["topic_type"] = "feature"
            candidate.metadata["is_feature"] = True
            candidate.metadata["official_summary"] = entry.get("summary")
            candidate.metadata["source_domain"] = entry.get("source")
            candidate.metadata["score"] = max(candidate.metadata.get("score", base_score), base_score)
            candidates.append(candidate)

        return candidates

    # ------------------------------------------------------------------
    # Keyword filtering

    def _compile_keyword_filters(self) -> Dict[str, List[re.Pattern]]:
        """Compile allow/block regex patterns from config."""
        filters = self.config.get("content_inputs", {}).get("keyword_filters", {})
        compiled: Dict[str, List[re.Pattern]] = {"allow": [], "block": []}

        for pattern in filters.get("allow_patterns", []) or []:
            try:
                compiled["allow"].append(re.compile(pattern, re.IGNORECASE))
            except re.error:
                self.log.warning("Invalid allow pattern: %s", pattern)

        for pattern in filters.get("block_patterns", []) or []:
            try:
                compiled["block"].append(re.compile(pattern, re.IGNORECASE))
            except re.error:
                self.log.warning("Invalid block pattern: %s", pattern)

        return compiled

    def _keyword_allowed(self, keyword: str) -> bool:
        """Check if keyword passes allow/block filters."""
        if not keyword:
            return False
        keyword = keyword.strip()

        for pattern in self.keyword_filters.get("block", []):
            if pattern.search(keyword):
                return False

        allow_patterns = self.keyword_filters.get("allow", [])
        if allow_patterns:
            return any(pattern.search(keyword) for pattern in allow_patterns)

        return True

    def _passes_competitor_quality_gate(self, keyword: str, min_length: int = 4) -> bool:
        """Basic quality check for competitor-scraped keywords."""
        kw = (keyword or "").strip()
        if len(kw) < min_length:
            return False

        has_cjk = bool(re.search(r"[\u4e00-\u9fff]", kw))
        if has_cjk:
            return True

        words = kw.split()
        if len(words) < 2 or len(words) > 8:
            return False
        if any(len(w) > 24 for w in words):
            return False
        if any(re.search(r"[a-z][A-Z]", w) for w in words):
            return False
        return True

    # ------------------------------------------------------------------
    # Topic classification and scoring

    def _build_topic_candidate(
        self,
        keyword: str,
        base_score: int,
        section: str,
        volume_estimate: int,
        locale_targets: List[str],
        source: str,
    ) -> TopicCandidate:
        """Build TopicCandidate with classification and scoring."""
        classification = self._classify_topic(keyword, locale_targets)
        angle = self._classify_angle(keyword, classification)

        topic_score = base_score
        if classification["is_geo"]:
            topic_score += 8
        if classification["is_comparison"]:
            topic_score += 6
        if classification["is_campaign"]:
            topic_score += 4
        if classification.get("is_service"):
            topic_score += 5
        if classification["topic_type"] == "faq":
            topic_score += 3

        novelty_bonus = 3 if any(str(year) in keyword for year in (2025, 2026, 2027)) else 0
        length_modifier = min(len(keyword), 32) % 7
        final_score = topic_score + novelty_bonus + length_modifier

        metadata = {
            "section": section,
            "topic_type": classification["topic_type"],
            "is_geo": classification["is_geo"],
            "geo_target": classification.get("geo_target"),
            "is_comparison": classification["is_comparison"],
            "is_campaign": classification["is_campaign"],
            "is_service": classification.get("is_service", False),
            "topic_angle": angle,
            "score": final_score,
            "source": source,
        }

        estimated_volume = volume_estimate + classification.get("volume_bonus", 0)
        opportunity = round(min(final_score / 100, 0.99), 2)

        return TopicCandidate(
            keyword=keyword,
            search_volume=estimated_volume,
            opportunity_score=opportunity,
            metadata=metadata,
        )

    def _classify_topic(self, keyword: str, locale_targets: List[str]) -> Dict[str, Any]:
        """Classify topic by type and attributes."""
        lower_kw = keyword.lower()

        classification: Dict[str, Any] = {
            "topic_type": "pillar",
            "is_geo": False,
            "is_comparison": False,
            "is_campaign": False,
            "is_service": False,
            "volume_bonus": 0,
        }

        # Geo-targeting
        for region in locale_targets:
            if region and region in keyword:
                classification["is_geo"] = True
                classification["geo_target"] = region
                classification["topic_type"] = "geo"
                classification["volume_bonus"] += 40
                break

        # Comparison content
        if "vs" in lower_kw or "比較" in keyword or "對比" in keyword or " vs " in lower_kw:
            classification["is_comparison"] = True
            classification["topic_type"] = "comparison"
            classification["volume_bonus"] += 20

        # Campaign/seasonal content — tokens come from config or defaults
        campaign_tokens = self._campaign_tokens()
        if any(token in keyword for token in campaign_tokens):
            classification["is_campaign"] = True
            if classification["topic_type"] == "pillar":
                classification["topic_type"] = "campaign"

        # FAQ content
        if "常見問題" in keyword or "FAQ" in keyword.upper():
            classification["topic_type"] = "faq"

        # Service/support content — tokens from config or defaults
        service_tokens = self._service_tokens()
        if any(tok in keyword for tok in service_tokens) or any(tok in lower_kw for tok in service_tokens):
            classification["is_service"] = True
            if classification["topic_type"] == "pillar":
                classification["topic_type"] = "service"
            classification["volume_bonus"] += 16

        # Troubleshooting/FAQ heuristics — from config or defaults
        trouble_tokens = self._trouble_tokens()
        if any(tok in keyword for tok in trouble_tokens) or any(tok in lower_kw for tok in trouble_tokens):
            classification["topic_type"] = "faq"
            classification["volume_bonus"] += 20

        # Overused token penalty
        overused_tokens = self._overused_tokens()
        has_overused = any(tok in lower_kw for tok in overused_tokens)
        has_new_angle = (
            classification["is_geo"]
            or classification["is_comparison"]
            or classification["is_campaign"]
            or classification["is_service"]
            or classification["topic_type"] == "faq"
        )
        if has_overused and not has_new_angle:
            classification["volume_bonus"] -= 20
        if not has_overused:
            classification["volume_bonus"] += 8

        return classification

    def _classify_angle(self, keyword: str, classification: Dict[str, Any]) -> str:
        """Classify content angle for rotation."""
        lower_kw = (keyword or "").lower()

        def has_tokens(tokens: Iterable[str]) -> bool:
            for token in tokens:
                if not token:
                    continue
                token_str = str(token)
                if token_str.lower() in lower_kw or token_str in keyword:
                    return True
            return False

        if has_tokens(self.angle_rules.get("legal", [])):
            return "legal"
        if classification.get("is_comparison") or has_tokens(self.angle_rules.get("comparison", [])):
            return "comparison"
        if has_tokens(self.angle_rules.get("upgrade", [])):
            return "upgrade"
        if classification.get("topic_type") == "faq" or has_tokens(self.angle_rules.get("usage", [])):
            return "usage"
        if classification.get("is_service") or has_tokens(self.angle_rules.get("service", [])):
            return "service"
        if classification.get("is_geo"):
            return "geo"
        if classification.get("is_campaign"):
            return "campaign"
        return "pillar"

    @staticmethod
    def _should_drop_overused(keyword: str, metadata: Dict[str, Any]) -> bool:
        """Drop topics that are repetitive with no new angle."""
        lower_kw = keyword.lower()
        overused_tokens = ("4k", "hdr", "dolby", "wi-fi 6", "wifi 6")
        has_overused = any(tok in lower_kw for tok in overused_tokens)
        has_new_angle = (
            metadata.get("is_geo")
            or metadata.get("is_comparison")
            or metadata.get("is_campaign")
            or metadata.get("is_service")
            or metadata.get("topic_type") == "faq"
        )
        return bool(has_overused and not has_new_angle)

    # ------------------------------------------------------------------
    # Config-driven token lists

    def _campaign_tokens(self) -> List[str]:
        """Campaign tokens from config or language-agnostic defaults."""
        return self.config.get("content_inputs", {}).get("campaign_tokens") or [
            "新年", "春節", "派對", "節", "2025", "2026", "holiday", "festival", "sale",
        ]

    def _service_tokens(self) -> List[str]:
        """Service/support tokens from config or defaults."""
        return self.config.get("content_inputs", {}).get("service_tokens") or [
            "美國倉", "現貨", "快速配送", "快遞", "免運", "運費", "退換", "退貨", "換貨",
            "保固", "維修", "售後", "客服", "中文客服", "雙語", "支援", "遠端協助",
            "warranty", "support", "return", "refund", "shipping", "delivery",
        ]

    def _trouble_tokens(self) -> List[str]:
        """Troubleshooting tokens from config or defaults."""
        return self.config.get("content_inputs", {}).get("trouble_tokens") or [
            "不能看", "看不到", "連不上", "連線問題", "故障", "排除", "排解", "錯誤碼",
            "troubleshooting", "not working", "no signal", "cannot connect", "can't connect",
            "error", "fix", "problem",
        ]

    def _overused_tokens(self) -> List[str]:
        """Overused marketing tokens to penalize (from config or defaults)."""
        return self.config.get("content_inputs", {}).get("overused_tokens") or [
            "4k", "hdr", "dolby", "wi-fi 6", "wifi 6",
        ]

    # ------------------------------------------------------------------
    # Fallbacks

    def _get_fallback_topics(self) -> List[TopicCandidate]:
        """Get fallback topics from config, or generic English defaults."""
        content_cfg = self.config.get("content_inputs", {})
        fallback_cfg = content_cfg.get("fallback_topics") or []
        locale_targets = content_cfg.get("locale_targets", [])

        if fallback_cfg:
            candidates = []
            for entry in fallback_cfg:
                if isinstance(entry, str):
                    keyword = entry
                    base_score = 70
                    volume_estimate = 300
                elif isinstance(entry, dict):
                    keyword = entry.get("keyword", "")
                    base_score = int(entry.get("base_score", 70))
                    volume_estimate = int(entry.get("volume_estimate", 300))
                else:
                    continue
                if keyword:
                    candidates.append(self._build_topic_candidate(
                        keyword=keyword,
                        base_score=base_score,
                        section="fallback",
                        volume_estimate=volume_estimate,
                        locale_targets=locale_targets,
                        source="fallback",
                    ))
            if candidates:
                return candidates

        # Generic English fallback when nothing configured
        site_name = self._site_name()
        generic = [
            f"{site_name} complete guide 2025",
            f"{site_name} review and comparison",
            f"{site_name} setup and support guide",
        ]
        return [
            self._build_topic_candidate(
                keyword=kw,
                base_score=70,
                section="fallback",
                volume_estimate=300,
                locale_targets=locale_targets,
                source="fallback",
            )
            for kw in generic
        ]

    # ------------------------------------------------------------------
    # Web scraping helpers

    def _scrape_official_features(self, url: str) -> List[Dict[str, str]]:
        """Scrape official site for feature keywords."""
        try:
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
        except requests.RequestException as exc:
            self.log.warning("Failed to fetch official site %s: %s", url, exc)
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        # Load feature keywords from config or use generic terms
        keywords = self.config.get("content_inputs", {}).get("official_feature_keywords") or [
            ("4k", "4K HDR"),
            ("hdr", "HDR"),
            ("wi-fi", "Wi-Fi"),
            ("voice", "Voice Remote"),
            ("app", "App"),
            ("storage", "Storage"),
            ("warranty", "Warranty"),
        ]

        results: List[Dict[str, str]] = []
        seen = set()
        for node in soup.find_all(["h1", "h2", "h3", "h4", "li", "p"]):
            text = node.get_text(strip=True)
            if not text or len(text) < 24:
                continue
            lowered = text.lower()
            matched = None
            for token, label in keywords:
                if token in lowered:
                    matched = label
                    break
            if not matched:
                continue
            title = text[:80]
            if title in seen:
                continue
            seen.add(title)
            results.append({"title": title, "summary": text, "category": matched, "source": url})

        return results

    def _scrape_competitor_keywords(self, url: str, min_length: int) -> List[str]:
        """Scrape keywords from competitor blog page."""
        tokens: List[str] = []
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            for container in soup.find_all(["nav", "footer", "aside", "header"]):
                container.decompose()

            excluded_exact = {
                "categories", "tags", "recent comments", "recent posts", "search",
                "product", "item", "lifestyle", "seller", "collection",
                "best product", "new product", "hot selling",
            }

            seen = set()
            for tag in soup.find_all(["h1", "h2", "h3", "h4", "strong"]):
                text = tag.get_text(strip=True)
                if not text or text.strip().lower() in excluded_exact:
                    continue
                for token in re.findall(r"[\u4e00-\u9fffA-Za-z0-9＋+\- ]{2,40}", text):
                    cleaned = token.strip()
                    if not self._passes_competitor_quality_gate(cleaned, min_length=min_length):
                        continue
                    if not self._keyword_allowed(cleaned):
                        continue
                    if cleaned in seen:
                        continue
                    seen.add(cleaned)
                    tokens.append(cleaned)

        except requests.RequestException as exc:
            self.log.warning("Competitor fetch failed for %s: %s", url, exc)

        return tokens

    def _expand_competitor_urls(self, domain: str, site_cfg: Any) -> List[str]:
        """Expand competitor config into a list of URLs to scrape."""
        if isinstance(site_cfg, list):
            return [u for u in site_cfg if isinstance(u, str) and u.strip()]

        if not isinstance(site_cfg, dict):
            return []

        urls: List[str] = []
        for u in site_cfg.get("urls", []) or []:
            if isinstance(u, str) and u.strip():
                urls.append(u.strip())

        sitemap = site_cfg.get("sitemap")
        if isinstance(sitemap, str) and sitemap.strip():
            include_patterns = site_cfg.get("include_patterns")
            exclude_patterns = site_cfg.get("exclude_patterns")
            max_urls = int(site_cfg.get("max_urls", 80))
            try:
                urls.extend(self._scrape_sitemap_urls(
                    sitemap_url=sitemap.strip(),
                    include_patterns=include_patterns,
                    exclude_patterns=exclude_patterns,
                    max_urls=max_urls,
                ))
            except Exception as exc:
                self.log.warning("Sitemap scrape failed for %s (%s): %s", domain, sitemap, exc)

        seen = set()
        out: List[str] = []
        for u in urls:
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out

    def _scrape_sitemap_urls(
        self,
        sitemap_url: str,
        include_patterns: Optional[List[str]],
        exclude_patterns: Optional[List[str]],
        max_urls: int,
    ) -> List[str]:
        """Fetch URLs from a sitemap (supports sitemapindex + nested sitemaps)."""
        import xml.etree.ElementTree as ET

        include = include_patterns or []
        exclude = exclude_patterns or []

        include_re = re.compile("|".join(re.escape(p) for p in include), re.IGNORECASE) if include else None
        exclude_re = re.compile("|".join(re.escape(p) for p in exclude), re.IGNORECASE) if exclude else None

        def fetch(url: str) -> str:
            resp = requests.get(url, timeout=20, headers={"User-Agent": "autoblogger/1.0"})
            resp.raise_for_status()
            return resp.text

        def parse_locs(xml_text: str) -> Tuple[List[str], bool]:
            try:
                root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
            except ET.ParseError:
                return ([], False)
            is_index = root.tag.lower().endswith("sitemapindex")
            locs: List[str] = []
            for node in root.findall(".//{*}loc"):
                if node.text and node.text.strip():
                    locs.append(node.text.strip())
            return (locs, is_index)

        xml_text = fetch(sitemap_url)
        sitemap_locs, is_index = parse_locs(xml_text)

        urls: List[str] = []
        if is_index:
            for loc in sitemap_locs:
                if len(urls) >= max_urls:
                    break
                try:
                    child_xml = fetch(loc)
                except requests.RequestException:
                    continue
                child_locs, _ = parse_locs(child_xml)
                for u in child_locs:
                    if len(urls) >= max_urls:
                        break
                    if include_re and not include_re.search(u):
                        continue
                    if exclude_re and exclude_re.search(u):
                        continue
                    urls.append(u)
        else:
            for u in sitemap_locs:
                if len(urls) >= max_urls:
                    break
                if include_re and not include_re.search(u):
                    continue
                if exclude_re and exclude_re.search(u):
                    continue
                urls.append(u)

        return urls

    # ------------------------------------------------------------------
    # Merge

    def _merge_candidates(
        self,
        candidate_groups: List[List[TopicCandidate]],
    ) -> List[TopicCandidate]:
        """Merge candidates from multiple sources, deduplicating by keyword."""
        merged: Dict[str, TopicCandidate] = {}
        for candidate in [c for group in candidate_groups for c in group]:
            key = candidate.keyword.lower()
            existing = merged.get(key)
            if not existing:
                merged[key] = candidate
                continue
            if candidate.metadata.get("score", 0) > existing.metadata.get("score", 0):
                merged[key] = candidate
        return list(merged.values())

    # ------------------------------------------------------------------
    # Utilities

    def _looks_like_keyword(self, text: str) -> bool:
        """Check if text looks like a valid keyword.

        CJK text is always considered valid. For Latin text, we check against
        config brand_terms (if provided) or fall back to accepting any multi-word phrase.
        """
        has_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in text)
        if has_cjk:
            return True

        # Check configured brand terms
        brand_terms = self.config.get("content_inputs", {}).get("keyword_brand_terms") or []
        lowered = text.lower()
        if brand_terms:
            return any(term.lower() in lowered for term in brand_terms)

        # Generic fallback: accept any phrase with 2+ words
        words = text.strip().split()
        return len(words) >= 2

    @staticmethod
    def _extract_keyword_from_line(line: str) -> Optional[str]:
        """Extract keyword from Markdown bullet line."""
        candidate = line[2:].strip()
        if not candidate:
            return None

        quoted = re.search(r'"([^"]+)"', candidate)
        if quoted:
            candidate = quoted.group(1).strip()
        elif ":" in candidate:
            label, remainder = candidate.split(":", 1)
            if remainder.strip() and not any("\u4e00" <= ch <= "\u9fff" for ch in label):
                candidate = remainder.strip()

        candidate = candidate.split("(")[0].strip()
        return candidate or None

    @staticmethod
    def _score_gsc_entry(impressions: float, ctr: float, position: float) -> float:
        """Calculate opportunity score for GSC entry."""
        impression_component = min(impressions / 5000, 1.0)
        ctr_gap = max(0.1 - ctr, 0)
        ctr_component = min(ctr_gap * 5, 1.0)
        position_component = max((20 - position) / 20, 0)
        raw = (impression_component * 0.5) + (ctr_component * 0.3) + (position_component * 0.2)
        return min(raw, 1.0)

    def _site_name(self) -> str:
        return self.config.get("site", {}).get("name") or self.config.get("brand_voice", {}).get("site_name", "our site")

    def _resolve_path(self, path_str: str) -> Path:
        """Resolve relative path from base directory."""
        path = Path(path_str)
        if path.is_absolute():
            return path
        return self.base_dir / path
