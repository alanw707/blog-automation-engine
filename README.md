# blog-automation-engine

AI-powered, config-driven blog post automation for multiple sites. One engine, many sites — each fully configured via a single YAML file.

## What it does

1. **Discovers** topics from seed keywords, Google Search Console data, and competitor scraping
2. **Generates** full blog posts via Claude: brief → outline → draft → title
3. **Validates** quality with Claude QA scoring (fallback to heuristics)
4. **Deduplicates** via slug matching + OpenAI embedding similarity
5. **Publishes** to:
   - **WordPress** REST API (svicloudtvbox.us)
   - **Azure Blob Storage** as Markdown files (aiprofilephotomaker.com)
   - **Local filesystem** (development / testing)
6. **Logs** every run to a SQLite database per site

## How to run

```bash
# Install deps
pip install -r requirements.txt

# Dry run (safe — no actual publishing)
python run.py --config configs/aiprofilephotomaker.yaml --dry-run --max-posts 1

# Real run
python run.py --config configs/svicloudtvbox.yaml --max-posts 1
```

### Required env vars

```bash
CLAUDE_API_KEY=sk-ant-...          # Anthropic API key
OPENAI_API_KEY=sk-...              # OpenAI (embeddings, optional but recommended)

# For Azure Blob publisher:
AZURE_STORAGE_CONNECTION_STRING=DefaultEndpointsProtocol=https;...

# For WordPress publisher:
WP_REST_PASSWORD=your-app-password
```

## Project structure

```
autoblogger/
├── run.py                  # CLI entry point
├── configs/
│   ├── aiprofilephotomaker.yaml   # English site, Azure Blob
│   └── svicloudtvbox.yaml         # Chinese site, WordPress
├── src/
│   ├── pipeline.py         # BlogPipeline orchestrator
│   ├── models.py           # GeneratedPost, TopicCandidate
│   └── stages/
│       ├── publishing.py   # Publisher classes + factory
│       ├── discovery.py    # Topic discovery helpers
│       ├── briefing.py     # Brief generation
│       ├── outlining.py    # Outline generation
│       ├── drafting.py     # Full post drafting
│       └── titling.py      # SEO title generation
├── data/                   # SQLite DBs, GSC cache (gitignored)
├── logs/                   # Run logs (gitignored)
├── drafts/                 # Failed QA posts for review (gitignored)
└── requirements.txt
```

## How to add a new site

1. **Copy a config:** `cp configs/aiprofilephotomaker.yaml configs/mysite.yaml`
2. **Edit `mysite.yaml`:** Set `site_name`, `site_url`, `language`, `publishing.method`, keywords, brand voice, and `storage.db_path`
3. **Dry-run test:** `python run.py --config configs/mysite.yaml --dry-run --max-posts 1`
4. **Flip the switch:** Set `safety.dry_run: false` when ready

### Publishing methods

| `method`     | What it does                                      | Extra config key |
|--------------|---------------------------------------------------|------------------|
| `wordpress`  | Posts to WordPress via REST API                   | `wordpress:`     |
| `azure_blob` | Uploads `.md` with YAML frontmatter to Azure Blob | `azure_blob:`    |
| `local_file` | Writes `.md` to local directory                   | `local_file:`    |

### Prompt templates

All prompts are in the site YAML under `prompt_templates:`. Available placeholders:

| Placeholder         | Source                              |
|---------------------|-------------------------------------|
| `{site_name}`       | `site_name`                         |
| `{site_url}`        | `site_url`                          |
| `{min_length}`      | `quality.min_length`                |
| `{keyword}`         | Current topic keyword               |
| `{brand_voice_tone}`| `brand_voice.tone`                  |
| `{key_phrases}`     | `brand_voice.key_phrases` (joined)  |
| `{internal_links}`  | `content_inputs.internal_link_targets` |
| `{content}`         | Post content (for QA/extend)        |
| `{max_length}`      | `seo.title.max_length`              |
