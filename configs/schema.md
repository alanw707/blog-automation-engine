# Config Schema Reference

All site-specific configuration for the autoblogger pipeline lives in a single YAML file.
This document describes every config key, its type, default, and which stage uses it.

---

## `site`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `name` | string | `"our site"` | Human-readable site name used in prompts |
| `domain` | string | `""` | Site domain for internal link validation (e.g., `svicloudtvbox.us`) |
| `url` | string | `""` | Full site URL |
| `default_market` | string | `"global"` | Default geographic market when topic has no geo target |

## `language`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `language` | string | `"en"` | Content language code. Used for quality validation. Options: `en`, `zh-TW`, `zh-CN` |

## `apis`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `claude_api_key_env` | string | `"CLAUDE_API_KEY"` | Env var name for Claude API key |
| `openai_api_key_env` | string | `"OPENAI_API_KEY"` | Env var name for OpenAI API key |
| `claude_model_brief` | string | — | Claude model ID for brief generation |
| `claude_model_outline` | string | — | Claude model ID for outline generation |
| `claude_model_full` | string | — | Claude model ID for full post drafting |
| `claude_model_qa` | string | — | Claude model ID for quality assessment |
| `openai_embedding` | string | `"text-embedding-3-small"` | OpenAI model for embedding dedup |

## `prompt_templates`

All prompt templates support `str.format()` substitution with these variables:

- `{site_name}` — from `site.name`
- `{keyword}` — current topic keyword
- `{topic_type}` — classified topic type (pillar, comparison, geo, campaign, faq)
- `{geo_target}` — geographic target from topic metadata
- `{brief}` — generated brief text
- `{outline}` — generated outline text
- `{retry_note}` — "Retry and improve" or "First draft"
- `{min_length}` — from `quality.min_length`
- `{content_sample}` — truncated article for QA
- `{truncated_note}` — "(content truncated...)" if applicable
- `{max_len}` — max title length
- `{disallow_clause}` — auto-generated clause for disallowed title tokens
- `{recent_clause}` — auto-generated list of recent titles to avoid

| Key | Stage | Description |
|-----|-------|-------------|
| `brief_system` | briefing | System prompt for Claude brief generation |
| `brief_user` | briefing | User prompt for brief generation |
| `fallback_brief` | briefing | Template for fallback brief (no Claude) |
| `outline_system` | outlining | System prompt for outline generation |
| `outline_user` | outlining | User prompt for outline generation |
| `fallback_outline_header` | outlining | Header for fallback outline |
| `fallback_outline_header_retry` | outlining | Header for retry fallback outline |
| `fallback_sections` | outlining, drafting | Dict of fallback section titles (see below) |
| `full_post_system` | drafting | System prompt for full post generation |
| `full_post_user` | drafting | User prompt for full post generation |
| `extend_article` | drafting | Prompt for extending short articles |
| `extend_article_system` | drafting | System prompt for article extension |
| `related_links_heading` | drafting | Heading for internal links block |
| `fallback_padding_sections` | drafting | List of section titles for length padding |
| `quality_check_system` | quality | System prompt for QA scoring |
| `quality_check_user` | quality | User prompt for QA scoring |
| `title_system` | titling | System prompt for title generation |
| `title_user` | titling | User prompt for title generation |

### `fallback_sections` (nested dict)

```yaml
fallback_sections:
  base:           # list of strings — core sections that appear in every outline
  comparison:     # string — section title for comparison topics
  geo:            # string — section title for geo topics
  campaign:       # string — section title for campaign topics
  faq:            # string — section title for FAQ closure
```

## `quality`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `min_score` | float | `80` | Minimum quality score to publish |
| `duplicate_threshold` | float | `0.85` | Cosine similarity threshold for embedding dedup |
| `min_length` | int | `4000` | Minimum content length in characters |
| `outline_score_threshold` | float | `0.75` | Minimum outline quality score |
| `claude_weight` | float | `0.6` | Weight for Claude QA score in combined score |
| `claude_floor` | float | `65` | Below this Claude score, fallback score wins |

## `seo.title`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `claude_model` | string | — | Claude model for title generation |
| `max_length` | int | `60` | Max title length in characters |
| `min_length_zh` | int | `30` | Min title length for CJK content |
| `recent_window` | int | `30` | Number of recent titles to check for similarity |
| `recent_similarity_threshold` | float | `0.82` | SequenceMatcher threshold |
| `normalize_drop_tokens` | list | `[]` | Tokens to strip before similarity comparison |
| `overused_tokens` | list | `[]` | Tokens penalized in title ranking |
| `disallowed_tokens` | list | `[]` | Tokens blocked from titles |
| `allow_disallowed_for_topic_types` | list | `["comparison", "campaign"]` | Topic types exempt from disallowed tokens |
| `unsafe_trailing_chars` | list | `[]` | CJK chars that shouldn't end a title |
| `fallback_templates` | list | — | Fallback title templates with `{keyword}`, `{geo}`, `{benefit}`, `{descriptor}` |
| `benefits` | list | — | Benefit phrases for fallback titles |
| `descriptors` | list | — | Descriptor phrases for fallback titles |

## `publishing`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `method` | string | `"wordpress"` | Publishing backend: `wordpress`, `azure-blob`, `local-file` |
| `max_posts_per_week` | int | `2` | Weekly publishing cap |
| `max_posts_per_run` | int | `2` | Per-run cap (overridden by `--max-posts`) |
| `timezone` | string | `"UTC"` | Publishing timezone |
| `azure_blob.connection_string_env` | string | `"AZURE_STORAGE_CONNECTION_STRING"` | Env var for Azure connection |
| `azure_blob.container_name` | string | `"blog-posts"` | Azure Blob container |
| `azure_blob.path_prefix` | string | `""` | Blob path prefix |
| `local_file.output_dir` | string | `"output/posts"` | Local output directory |

## `wordpress`

See existing WordPress REST API config keys. All preserved from original pipeline.

## `brand_voice`

| Key | Type | Description |
|-----|------|-------------|
| `site_name` | string | Site name (fallback for `site.name`) |
| `target_audience` | string | Target audience description |
| `tone` | string | Writing tone description |
| `key_phrases` | list | Required phrases enforced in content |
| `max_enforced_key_phrases` | int | Max key phrases to inject if missing |
| `product_terms` | list | Product terms checked in quality scoring |
| `feature_statements` | dict | `core`, `comparison`, `geo` lists of statements |
| `cta_title` | string | CTA section heading |
| `cta_points` | list | CTA bullet points |
| `cta_tagline` | string | CTA closing tagline |
| `cta_contact` | list | Contact info lines for CTA footer |

## `content_inputs`

| Key | Type | Description |
|-----|------|-------------|
| `keyword_source` | string | Path to Markdown keyword plan file |
| `keyword_brand_terms` | list | Brand terms for keyword validation |
| `seed_keywords` | list | Curated seed keywords with metadata |
| `diversity_seeds` | list | Additional diversity seed topics |
| `fallback_topics` | list | Fallback topics when no other source available |
| `campaign_tokens` | list | Tokens that indicate campaign/seasonal content |
| `service_tokens` | list | Tokens that indicate service/support content |
| `trouble_tokens` | list | Tokens that indicate troubleshooting/FAQ content |
| `overused_tokens` | list | Overused marketing tokens to penalize |
| `gsc` | dict | Google Search Console configuration |
| `locale_targets` | list | Geographic target strings for geo classification |
| `angle_rules` | dict | Token lists for content angle classification |
| `images` | dict | Image config by topic type |
| `internal_links` | dict | Placeholder → URL mapping |
| `internal_link_targets` | list | Internal links with label, url, description |
| `competitors` | dict | Competitor scraping configuration |
| `keyword_filters` | dict | `allow_patterns` and `block_patterns` regex lists |

## `storage`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `db_path` | string | `"posts_history.db"` | SQLite database path |

## `monitoring`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `log_level` | string | `"INFO"` | Python logging level |
| `log_path` | string | — | Optional log file path |

## `safety`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `dry_run` | bool | `false` | Simulate all operations |
| `emergency_stop_file` | string | `"EMERGENCY_STOP"` | File that triggers pipeline halt |
| `fuzzy_title_threshold` | float | `0.82` | Title similarity threshold for dedup |
