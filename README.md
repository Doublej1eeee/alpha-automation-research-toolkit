# Alpha Automation Research Toolkit

This repository contains a sanitized research automation toolkit for building,
validating, scheduling, and monitoring WorldQuant Brain-style alpha experiments.

The public release intentionally excludes:

- account credentials, tokens, cookies, SSH keys, and server addresses
- personal documents, resumes, competition papers, and private notes
- simulation results, alpha IDs, platform state caches, and submission history
- downloaded research PDFs, forum dumps, and reference repositories

## Main Components

- `brain_client.py`  
  API client and alpha submission/check utilities. Credentials are read only
  from environment variables or an external credentials file.

- `script/continuous_slot_miner.py`  
  Continuous multi-slot experiment scheduler.

- `script/continuous_supply_engine.py`  
  Candidate supply engine for generating and queuing alpha variants.

- `script/raw_alpha_rotation.py` and `script/raw_alpha_builder.py`  
  Raw alpha family rotation, diversity checks, and seed expansion.

- `script/high_grade_repair_engine.py`  
  Bounded repair engine for high-grade failed candidates.

- `script/mobile_dashboard.py`  
  Local/mobile dashboard for observing runtime status. Set
  `DASHBOARD_PASSWORD` before exposing it on any network.

- `alpha_generation/templates/`  
  Example alpha templates and template library.

## Credential Setup

Do not commit credentials.

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

## Safety Notes

This repository is a sanitized code release. It does not include the private
runtime state required to reproduce the original experiments. Before running
against any external platform, review the platform terms, rate limits, and
credential handling requirements.

