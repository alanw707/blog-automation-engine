# Autoblogger — Multi-Site Blog Automation Pipeline

Config-driven blog automation that generates SEO-optimized content using Claude AI.
Each site gets its own YAML config — all prompts, brand voice, and publishing targets
live in config, not in code.

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Copy and fill in environment variables
cp .env.example .env
# Edit .env with your API keys

# 3. Dry-run test with a config
python run.py --config configs/aiprofilephotomaker.yaml --dry-run

# 4. Production run (publishes for real)
python run.py --config configs/svicloudtvbox.yaml --max-posts 1
```

## Architecture

```
run.py                          ← CLI entry point
src/
  pipeline.py                   ← BlogPipeline orchestrator
  models.py                     ← TopicCandidate, GeneratedPost dataclasses
  stages/
    discovery.py                ← Topic discovery (keyword plan, GSC, competitors)
    briefing.py                 ← Strategic content brief generation
    outlining.py                ← Structured outline generation
    drafting.py                 ← Full post drafting with enhancements
    titling.py                  ← SEO title generation with dedup
    quality.py                  ← Quality validation (Claude + heuristic)
    publishing.py               ← WordPress, Azure Blob, Local File publishers
configs/
  svicloudtvbox.yaml            ← SVICLOUD Chinese config
  aiprofilephotomaker.yaml      ← AI Profile Photo Maker English config
  schema.md                     ← Config key reference
```

## Pipeline Flow

```
Discovery → Briefing → Outlining → Drafting → Titling → Quality → Publishing
     ↓          ↓          ↓           ↓          ↓         ↓          ↓
  Topics    Brief text   Outline    Full post   SEO title  Score    WordPress/
  (scored)  (100 words)  (5-7 pts)  (Markdown)  (deduped)  (0-100)  Azure/Local
```

Each stage reads prompts from `config["prompt_templates"]` with `str.format()` substitution.
No site-specific strings exist in the Python code.

## Supported Publishers

| Method | Config Key | Description |
|--------|-----------|-------------|
| `wordpress` | `publishing.method: wordpress` | WordPress REST API |
| `azure-blob` | `publishing.method: azure-blob` | Azure Blob Storage (.md with YAML frontmatter) |
| `local-file` | `publishing.method: local-file` | Local filesystem (.md with YAML frontmatter) |

## Adding a New Site

1. Copy `configs/aiprofilephotomaker.yaml` as a template
2. Update `site`, `language`, `brand_voice`, `content_inputs`, and `prompt_templates`
3. Set the appropriate `publishing.method`
4. Run: `python run.py --config configs/mysite.yaml --dry-run`

## Key Features

- **Config-driven**: All prompts, brand voice, and URLs in YAML
- **Multi-publisher**: WordPress, Azure Blob, local file
- **Deduplication**: SQLite history + OpenAI embeddings + fuzzy title matching
- **Quality scoring**: Claude AI + heuristic fallback with configurable weights
- **Competitor scraping**: Auto-refresh from competitor sitemaps
- **GSC integration**: Google Search Console data for topic discovery
- **Emergency stop**: Create `EMERGENCY_STOP` file to halt pipeline

## Environment Variables

See `.env.example` for the full list. Key variables:

| Variable | Required For |
|----------|-------------|
| `CLAUDE_API_KEY` | Content generation (all stages) |
| `OPENAI_API_KEY` | Embedding-based deduplication |
| `WP_REST_PASSWORD` | WordPress publishing |
| `AZURE_STORAGE_CONNECTION_STRING` | Azure Blob publishing |
