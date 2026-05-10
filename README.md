# Alpha Automation Research Toolkit

Alpha Automation Research Toolkit is a Python-based workflow for automated
alpha research. It focuses on generating candidate expressions, validating
templates, scheduling batch simulations, tracking experiment outcomes, and
closing the loop between feedback and the next search round.

## Main Components

- `brain_client.py`  
  API client and alpha submission/check utilities.

- `script/continuous_slot_miner.py`  
  Continuous multi-slot scheduler for long-running simulation queues.

- `script/continuous_supply_engine.py`  
  Candidate supply engine for generating and prioritizing experiment jobs.

- `script/raw_alpha_rotation.py` and `script/raw_alpha_builder.py`  
  Raw alpha family construction, diversity control, and seed expansion.

- `script/high_grade_repair_engine.py`  
  Repair workflow for promising candidates that fail submission checks.

- `script/mobile_dashboard.py`  
  Lightweight dashboard for monitoring channels, throughput, queues, and
  candidate status from a phone or browser.

- `alpha_generation/templates/`  
  Example templates and reusable expression patterns.

## Workflow

1. Define or import raw alpha ideas.
2. Expand ideas into structured candidate templates.
3. Validate expression syntax and field compatibility.
4. Schedule simulations across available channels.
5. Parse results and submission checks.
6. Feed performance, failure reasons, and family information back into the
   next generation round.

## Features

- template validation and lightweight expression parsing
- raw alpha family rotation and diversity governance
- continuous simulation scheduling
- result classification and tag synchronization helpers
- high-grade candidate repair workflow
- mobile-friendly runtime dashboard
- research-source ingestion and field-selection utilities

## Credential Setup

Credentials should be supplied through environment variables or an external
credentials file.

Use environment variables:

```powershell
$env:BRAIN_USERNAME="your_email@example.com"
$env:BRAIN_PASSWORD="your_password"
```

Or set an external file path:

```powershell
$env:BRAIN_CREDENTIALS_FILE="C:\path\outside\repo\brain_credentials.json"
```

The credentials file format is:

```json
["your_email@example.com", "your_password"]
```

Keep this file outside the repository.

## Notes

Before running large batches, review the target platform's terms, API behavior,
and rate limits. The toolkit is designed to support controlled experimentation,
not uncontrolled request bursts.
