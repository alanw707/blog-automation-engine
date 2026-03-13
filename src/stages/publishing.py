"""Publishing stage for blog automation.

This module handles publishing blog posts via multiple backends:
- WordPress REST API (existing)
- Azure Blob Storage (new)
- Local file system (new)

The publisher is selected via config['publishing']['method']:
  - "wordpress" or "rest-api" → WordPressPublisher
  - "azure-blob" → AzureBlobPublisher
  - "local-file" → LocalFilePublisher

All site-specific strings come from config.
"""

import json
import logging
import mimetypes
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import markdown
import requests
from bs4 import BeautifulSoup

from ..models import GeneratedPost


# ======================================================================
# Publisher interface
# ======================================================================

class BasePublisher:
    """Abstract base for all publishers."""

    def __init__(self, config: Dict[str, Any], logger: logging.Logger, dry_run: bool = False):
        self.config = config
        self.log = logger
        self.dry_run = dry_run

    def publish_post(self, post: GeneratedPost) -> bool:
        raise NotImplementedError

    def save_draft(self, post: GeneratedPost, draft_dir: Path) -> None:
        """Save post as draft JSON file for manual review."""
        draft_dir.mkdir(parents=True, exist_ok=True)
        path = draft_dir / f"{post.slug}.md"
        payload = {
            "title": post.title,
            "excerpt": post.excerpt,
            "keyword": post.keyword,
            "frontmatter": post.frontmatter,
            "content": post.content,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self.log.info("Saved draft to %s", path)


# ======================================================================
# WordPress Publisher
# ======================================================================

class WordPressPublisher(BasePublisher):
    """Publishes blog posts to WordPress via REST API."""

    def publish_post(self, post: GeneratedPost) -> bool:
        """Publish post to WordPress using configured method.

        Args:
            post: GeneratedPost with content and metadata

        Returns:
            True if publishing succeeded
        """
        wp_method = self.config.get("wordpress", {}).get("method", "rest-api")
        if wp_method == "wp-cli":
            return self._publish_via_wpcli(post)
        return self._publish_via_rest_api(post)

    def publish_translation(
        self,
        post: GeneratedPost,
        locale: str,
        lang_field: str = "lang",
        translated_title: Optional[str] = None,
        translated_slug: Optional[str] = None,
        translated_content: Optional[str] = None,
        translated_excerpt: Optional[str] = None,
    ) -> Optional[int]:
        """Publish a translated variant of a post."""
        rest_cfg = self.config.get("wordpress", {}).get("rest", {})
        endpoint = rest_cfg.get("endpoint")
        username = rest_cfg.get("username")
        password_env = rest_cfg.get("password_env")
        password = os.getenv(password_env) if password_env else rest_cfg.get("password")
        status = rest_cfg.get("status", "draft")

        if not endpoint or not username or not password:
            self.log.error("REST API not fully configured")
            return None

        title = translated_title or post.title
        slug = translated_slug or f"{post.slug}-{locale.lower()}"
        markdown_source = translated_content or post.content
        html_content = self._markdown_to_html(markdown_source)
        excerpt_text = translated_excerpt or self._excerpt_from_html(html_content, keyword=post.keyword)

        payload = {
            "title": title,
            "slug": slug,
            "content": html_content,
            "excerpt": excerpt_text,
            "status": status,
            lang_field: locale,
        }

        if rest_cfg.get("author"):
            payload["author"] = rest_cfg["author"]

        selected_categories = self._select_categories_for_post(post)
        if selected_categories:
            payload["categories"] = selected_categories

        try:
            response = requests.post(endpoint, auth=(username, password), json=payload, timeout=30)
            if 200 <= response.status_code < 300:
                data = response.json()
                self.log.info("Published translation '%s' (%s) (WP ID %s)", title, locale, data.get("id"))
                return data.get("id")
            self.log.error("WP REST API error %s for locale %s: %s", response.status_code, locale, response.text[:500])
            return None
        except requests.RequestException as exc:
            self.log.error("WP REST API failed for locale %s: %s", locale, exc)
            return None

    def update_translation_meta(
        self,
        post_id: Optional[int],
        locale: str,
        translation: Dict[str, str],
    ) -> bool:
        """Store translated content into meta fields on the base post."""
        if post_id is None:
            self.log.warning("Cannot update translation meta without post_id")
            return False

        suffix = self._locale_suffix(locale)
        if not suffix:
            self.log.warning("Unsupported locale for translation meta: %s", locale)
            return False

        rest_cfg = self.config.get("wordpress", {}).get("rest", {})
        endpoint = rest_cfg.get("endpoint")
        username = rest_cfg.get("username")
        password_env = rest_cfg.get("password_env")
        password = os.getenv(password_env) if password_env else rest_cfg.get("password")
        if not endpoint or not username or not password:
            self.log.error("REST API not fully configured")
            return False

        meta_prefix = self.config.get("wordpress", {}).get("translation_meta_prefix", "_svic")
        meta_payload = {
            f"{meta_prefix}_content_{suffix}": translation.get("content", ""),
            f"{meta_prefix}_title_{suffix}": translation.get("title", ""),
            f"{meta_prefix}_description_{suffix}": translation.get("excerpt", ""),
        }

        url = f"{endpoint.rstrip('/')}/{post_id}"
        try:
            resp = requests.post(url, auth=(username, password), json={"meta": meta_payload}, timeout=30)
            if 200 <= resp.status_code < 300:
                self.log.info("Updated translation meta for post %s locale %s", post_id, locale)
                return True
            self.log.warning("Failed to update translation meta (%s): %s", resp.status_code, resp.text[:200])
            return False
        except requests.RequestException as exc:
            self.log.error("Translation meta update failed: %s", exc)
            return False

    @staticmethod
    def _locale_suffix(locale: str) -> Optional[str]:
        mapping = {"zh_TW": "zh_tw", "zh_CN": "zh_cn", "en_US": "en_us"}
        return mapping.get(locale)

    def _publish_via_rest_api(self, post: GeneratedPost) -> bool:
        """Publish post via WordPress REST API."""
        rest_cfg = self.config.get("wordpress", {}).get("rest", {})
        endpoint = rest_cfg.get("endpoint")
        username = rest_cfg.get("username")
        password_env = rest_cfg.get("password_env")
        password = os.getenv(password_env) if password_env else rest_cfg.get("password")
        status = rest_cfg.get("status", "draft")

        if not endpoint or not username or not password:
            self.log.error("REST API not fully configured (endpoint/username/password missing)")
            return False

        content_markdown, featured_media_id = self._prepare_media_assets(
            post, post.content, rest_cfg, auth=(username, password),
        )
        html_content = self._markdown_to_html(content_markdown)
        excerpt_text = self._excerpt_from_html(html_content, keyword=post.keyword)

        payload: Dict[str, Any] = {
            "title": post.title,
            "slug": post.slug,
            "content": html_content,
            "excerpt": excerpt_text,
            "status": status,
        }

        base_locale = self.config.get("publishing", {}).get("base_locale")
        lang_field = self.config.get("publishing", {}).get("locale_param", "lang")
        if base_locale:
            payload[lang_field] = base_locale

        if rest_cfg.get("author"):
            payload["author"] = rest_cfg["author"]

        selected_categories = self._select_categories_for_post(post)
        if selected_categories:
            payload["categories"] = selected_categories

        if featured_media_id:
            payload["featured_media"] = featured_media_id

        if self.dry_run:
            self.log.info("[DRY] Would POST to %s with status '%s'", endpoint, status)
            return True

        try:
            response = requests.post(endpoint, auth=(username, password), json=payload, timeout=30)
            if 200 <= response.status_code < 300:
                data = response.json()
                post.frontmatter["wp_post_id"] = data.get("id")
                post.frontmatter["wp_status"] = data.get("status")
                self.log.info("Published '%s' (WP ID %s)", post.title, data.get("id"))
                return True
            self.log.error("WordPress REST API error %s: %s", response.status_code, response.text[:500])
            return False
        except requests.RequestException as exc:
            self.log.error("Failed to call WordPress REST API: %s", exc)
            return False

    def _publish_via_wpcli(self, post: GeneratedPost) -> bool:
        if self.dry_run:
            self.log.info("[DRY] WP-CLI publish stub for '%s'", post.title)
            return True
        self.log.info("WP-CLI publishing not yet implemented")
        return False

    def _markdown_to_html(self, markdown_text: str) -> str:
        try:
            return markdown.markdown(markdown_text, extensions=["extra", "sane_lists"])
        except Exception as exc:
            self.log.warning("Markdown conversion failed: %s", exc)
            return markdown_text

    def _excerpt_from_html(self, html_text: str, length: int = 220, keyword: Optional[str] = None) -> str:
        soup = BeautifulSoup(html_text, "html.parser")
        text = soup.get_text(separator=" ", strip=True)
        if not text:
            return ""

        keyword = (keyword or "").strip()
        sentences = [s.strip() for s in re.split(r"(?<=[。！？!?\.])\s*", text) if s.strip()]
        sentences = [
            re.sub(r"^[✅✔\s]+", "", s) for s in sentences
            if not re.search(r"chinese title\s*[:：]", s, re.IGNORECASE)
        ]

        ordered: List[str] = []
        if keyword:
            ordered.extend([s for s in sentences if keyword in s])
        ordered.extend(sentences)

        result_parts: List[str] = []
        for sentence in ordered:
            if sentence in result_parts:
                continue
            result_parts.append(sentence)
            if len("".join(result_parts)) >= length:
                break

        excerpt = "".join(result_parts).strip() or text
        return excerpt[:length]

    def _prepare_media_assets(
        self, post: GeneratedPost, markdown_text: str,
        rest_cfg: Dict[str, Any], auth: Tuple[str, str],
    ) -> Tuple[str, Optional[int]]:
        hero = (post.frontmatter or {}).get("hero_image") or {}
        image_path = hero.get("path")
        placeholder = hero.get("placeholder")
        if not image_path:
            return markdown_text, None

        upload = self._upload_media(image_path, hero.get("alt") or post.title, rest_cfg, auth)
        if not upload:
            return markdown_text, None

        source_url = upload.get("source_url")
        media_id = upload.get("id")
        if source_url and placeholder:
            markdown_text = markdown_text.replace(placeholder, source_url, 1)
        hero.update({"uploaded_url": source_url, "wp_media_id": media_id})
        return markdown_text, media_id

    def _upload_media(
        self, file_path: str, title: str,
        rest_cfg: Dict[str, Any], auth: Tuple[str, str],
    ) -> Optional[Dict[str, Any]]:
        media_endpoint = rest_cfg.get("media_endpoint") or self._derive_media_endpoint(rest_cfg.get("endpoint"))
        if not media_endpoint:
            return None

        path = Path(file_path)
        if not path.exists():
            self.log.warning("Hero image path does not exist: %s", path)
            return None

        content_type, _ = mimetypes.guess_type(path.name)
        files = {"file": (path.name, path.read_bytes(), content_type or "application/octet-stream")}
        data = {"title": title}

        try:
            response = requests.post(media_endpoint, auth=auth, files=files, data=data, timeout=60)
            if 200 <= response.status_code < 300:
                return response.json()
            self.log.warning("Media upload failed (%s): %s", response.status_code, response.text[:200])
        except requests.RequestException as exc:
            self.log.warning("Media upload error: %s", exc)
        return None

    @staticmethod
    def _derive_media_endpoint(endpoint: Optional[str]) -> Optional[str]:
        if not endpoint:
            return None
        clean = endpoint.rstrip("/")
        if clean.endswith("/posts"):
            return clean.rsplit("/posts", 1)[0] + "/media"
        return clean + "/media"

    def _select_categories_for_post(self, post: GeneratedPost) -> List[int]:
        category_map = self.config.get("wordpress", {}).get("category_map", {})
        topic_type = (post.frontmatter.get("topic_type") or "default").lower()
        selected = category_map.get(topic_type) or category_map.get("default") or []
        normalized: List[int] = []
        for value in selected:
            try:
                normalized.append(int(value))
            except (ValueError, TypeError):
                continue
        return normalized

    def fetch_remote_slugs(self) -> set:
        """Gather existing slugs from remote WP to prevent duplicates."""
        rest_cfg = self.config.get("wordpress", {}).get("rest", {})
        if not rest_cfg.get("dedupe_remote", True):
            return set()
        return self._fetch_remote_field(rest_cfg, "slug")

    def fetch_remote_titles(self) -> List[str]:
        """Gather existing titles from remote WP."""
        rest_cfg = self.config.get("wordpress", {}).get("rest", {})
        if not rest_cfg.get("dedupe_remote", True):
            return []

        endpoint = rest_cfg.get("endpoint")
        if not endpoint:
            return []

        per_page = 100
        max_pages = int(rest_cfg.get("dedupe_pages", 3))
        titles: List[str] = []
        for page in range(1, max_pages + 1):
            try:
                resp = requests.get(
                    endpoint,
                    params={"per_page": per_page, "page": page, "_fields": "title", "status": "publish", "orderby": "date", "order": "desc"},
                    timeout=20,
                )
                if resp.status_code != 200:
                    break
                rows = resp.json()
                if not rows:
                    break
                for row in rows:
                    title_obj = row.get("title") or {}
                    rendered = title_obj.get("rendered")
                    if rendered:
                        titles.append(rendered)
            except requests.RequestException:
                break
        return titles

    def _fetch_remote_field(self, rest_cfg: Dict[str, Any], field: str) -> set:
        endpoint = rest_cfg.get("endpoint")
        if not endpoint:
            return set()

        per_page = 100
        max_pages = int(rest_cfg.get("dedupe_pages", 3))
        collected: set = set()
        for page in range(1, max_pages + 1):
            try:
                resp = requests.get(
                    endpoint,
                    params={"per_page": per_page, "page": page, "_fields": field, "status": "publish", "orderby": "date", "order": "desc"},
                    timeout=20,
                )
                if resp.status_code != 200:
                    break
                rows = resp.json()
                if not rows:
                    break
                for row in rows:
                    val = row.get(field)
                    if val:
                        collected.add(val)
            except requests.RequestException:
                break
        return collected


# ======================================================================
# Azure Blob Publisher
# ======================================================================

class AzureBlobPublisher(BasePublisher):
    """Publishes blog posts as Markdown files with YAML frontmatter to Azure Blob Storage.

    Config keys (under publishing.azure_blob):
      connection_string_env: env var name for Azure connection string
      container_name: blob container name
      path_prefix: optional prefix for blob paths (e.g. "blog/")
    """

    def publish_post(self, post: GeneratedPost) -> bool:
        """Upload .md file with YAML frontmatter to Azure Blob Storage.

        Args:
            post: GeneratedPost to publish

        Returns:
            True if upload succeeded
        """
        import yaml

        # Look in publishing.azure_blob first, then top-level azure_blob
        blob_cfg = (
            self.config.get("publishing", {}).get("azure_blob")
            or self.config.get("azure_blob")
            or {}
        )
        conn_env = blob_cfg.get("connection_string_env", "AZURE_STORAGE_CONNECTION_STRING")
        connection_string = os.getenv(conn_env)
        # Accept both "container_name" and "container" as key names
        container_name = blob_cfg.get("container_name") or blob_cfg.get("container") or "blog-posts"
        # Accept both "path_prefix" and "prefix" as key names
        path_prefix = blob_cfg.get("path_prefix") or blob_cfg.get("prefix") or ""

        if not connection_string:
            self.log.error("Azure connection string missing (env: %s)", conn_env)
            return False

        # Build standardised frontmatter
        author = self.config.get("brand_voice", {}).get("author") or self.config.get("site_name", "")
        tags = (post.frontmatter or {}).get("tags") or []
        frontmatter = {
            "title": post.title,
            "description": post.excerpt,
            "author": author,
            "publishedAt": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "published": True,
            "tags": tags,
            "slug": post.slug,
        }

        # Build markdown with YAML frontmatter
        yaml_block = yaml.dump(frontmatter, allow_unicode=True, default_flow_style=False, sort_keys=False)
        full_content = f"---\n{yaml_block}---\n\n{post.content}"

        blob_name = f"{path_prefix}{post.slug}.md"

        if self.dry_run:
            self.log.info("[DRY] Would upload to Azure Blob: %s/%s (%d bytes)", container_name, blob_name, len(full_content))
            return True

        try:
            from azure.storage.blob import BlobServiceClient

            blob_service = BlobServiceClient.from_connection_string(connection_string)
            container_client = blob_service.get_container_client(container_name)

            # Create container if it doesn't exist
            try:
                container_client.create_container()
            except Exception:
                pass  # Container already exists

            blob_client = container_client.get_blob_client(blob_name)
            blob_client.upload_blob(
                full_content.encode("utf-8"),
                overwrite=True,
                content_settings={"content_type": "text/markdown; charset=utf-8"},
            )

            post.frontmatter["azure_blob_name"] = blob_name
            post.frontmatter["azure_container"] = container_name
            self.log.info("Uploaded to Azure Blob: %s/%s", container_name, blob_name)
            return True

        except ImportError:
            self.log.error("azure-storage-blob package not installed; run: pip install azure-storage-blob")
            return False
        except Exception as exc:
            self.log.error("Azure Blob upload failed: %s", exc)
            return False


# ======================================================================
# Local File Publisher
# ======================================================================

class LocalFilePublisher(BasePublisher):
    """Publishes blog posts as Markdown files with YAML frontmatter to local directory.

    Config keys (under publishing.local_file):
      output_dir: directory to write files (default: "output/posts")
    """

    def publish_post(self, post: GeneratedPost) -> bool:
        """Write .md file with YAML frontmatter to local directory.

        Args:
            post: GeneratedPost to publish

        Returns:
            True if write succeeded
        """
        import yaml

        # Look in publishing.local_file first, then top-level local_file
        local_cfg = (
            self.config.get("publishing", {}).get("local_file")
            or self.config.get("local_file")
            or {}
        )
        output_dir = local_cfg.get("output_dir", "output/posts")
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        # Build standardised frontmatter
        author = self.config.get("brand_voice", {}).get("author") or self.config.get("site_name", "")
        tags = (post.frontmatter or {}).get("tags") or []
        frontmatter = {
            "title": post.title,
            "description": post.excerpt,
            "author": author,
            "publishedAt": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "published": True,
            "tags": tags,
            "slug": post.slug,
        }

        yaml_block = yaml.dump(frontmatter, allow_unicode=True, default_flow_style=False, sort_keys=False)
        full_content = f"---\n{yaml_block}---\n\n{post.content}"

        file_path = output_path / f"{post.slug}.md"

        if self.dry_run:
            self.log.info("[DRY] Would write to %s (%d bytes)", file_path, len(full_content))
            return True

        try:
            file_path.write_text(full_content, encoding="utf-8")
            post.frontmatter["local_file_path"] = str(file_path)
            self.log.info("Published to local file: %s", file_path)
            return True
        except Exception as exc:
            self.log.error("Local file write failed: %s", exc)
            return False


# ======================================================================
# Publisher factory
# ======================================================================

def create_publisher(config: Dict[str, Any], logger: logging.Logger, dry_run: bool = False) -> BasePublisher:
    """Create the appropriate publisher based on config['publishing']['method'].

    Supported methods:
      - "wordpress", "rest-api" → WordPressPublisher
      - "azure-blob" → AzureBlobPublisher
      - "local-file" → LocalFilePublisher

    Args:
        config: Full pipeline config dict
        logger: Logger instance
        dry_run: If True, simulate operations

    Returns:
        A BasePublisher subclass instance
    """
    method = config.get("publishing", {}).get("method", "wordpress").lower()

    if method in ("wordpress", "rest-api"):
        return WordPressPublisher(config, logger, dry_run)
    elif method in ("azure-blob", "azure_blob"):
        return AzureBlobPublisher(config, logger, dry_run)
    elif method in ("local-file", "local_file"):
        return LocalFilePublisher(config, logger, dry_run)
    else:
        logger.warning("Unknown publishing method '%s', defaulting to local-file", method)
        return LocalFilePublisher(config, logger, dry_run)
