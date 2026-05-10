# Alpha Generation Module

This module stores the formal assets and build logic for:
- original alpha drafting
- template generation
- template optimization
- field-aware template expansion

## Formal Assets

- `templates/`
- `raw_alpha_human.md`
- `raw_alpha_ai.md`
- `optimization_strategies_human.md`
- `optimization_strategies_ai.md`

Raw-alpha source usage is archived in:
- `result_store/index/research_source_usage_catalog.jsonl`

## Current Structure

### 1. Raw Alpha Builder

Core steps:
- `idea parsing`
- `field grounding`
- `expression drafting`

Current entrypoint:
- `script/raw_alpha_builder.py`

Current reality:
- It is no longer only an archetype-based fallback builder.
- It now prefers extracted research families from papers/reports when a family match is strong enough.
- It also supports explicit family selection with `--research-family-key`.
- It is still a transitional `v2` stage, not the final research-grade original-alpha engine.
- `raw_alpha_ai.md` now uses structured `ALPHA:` lines so that source, delay judgement, family distinctness, and grounded field plan can be archived in a stable way.

### 2. Template Builder

Responsibilities:
- template abstraction
- optimization strategy application
- replaceable field slot identification
- template parameterization

Current entrypoint:
- `script/template_builder.py`

Current reality:
- The current version is usable, but it is still closer to a `v1` variant engine than a fully mature family-level template system.
- Template distinctness is not yet guaranteed at the family level.

### 3. Field Selection Engine

Current entrypoint:
- `script/field_selection_engine.py`

Current responsibilities:
- field type filtering
- coverage / update-frequency weighting
- semantic matching
- dataset / category constraints
- basic operator compatibility checks

## Current Original-Alpha Reality

What is already stronger:
- research-source ingestion
- research-family extraction
- family-level deduplication
- field grounding entrypoints
- integration between research families and raw alpha drafting

What is still unfinished:
- deep financial-mechanism extraction from papers/reports
- stronger LLM participation in true original-alpha drafting
- stable high-distinctness family generation across different domains
- final gating before a research-derived family becomes a formal production alpha source

## Current Intake Reality

- The original-alpha intake chain is now usable in a first practical sense:
  - weekly-style paper/report body fetching can run
  - paper bodies can be read
  - used sources can be archived
  - selected raw alphas can be added into the formal raw-alpha pool
- The strongest current full-text source is still `arXiv`.
- USA report full text is currently a supplement source, not yet a deep or broad institutional research source.

## Important Rule

Research-derived families must not be treated as formal production-ready original alphas until they pass:
- family-level deduplication
- field grounding
- non-overlap checks against existing families
