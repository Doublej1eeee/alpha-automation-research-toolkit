# Scripts

This directory contains the maintained script entrypoints for the quant mining
project. Prefer these documented entrypoints over ad hoc one-off helpers.

## Mainline Runtime

- [continuous_slot_miner.py](D:/StupidNight/工作/量化/learning/script/continuous_slot_miner.py)  
  Main 24x7 cloud miner entrypoint.
- [continuous_supply_engine.py](D:/StupidNight/工作/量化/learning/script/continuous_supply_engine.py)  
  Builds and refreshes the supply pool, inventories, and candidate jobs.
- [mobile_dashboard.py](D:/StupidNight/工作/量化/learning/script/mobile_dashboard.py)  
  Mobile-first runtime dashboard. Runtime truth comes from cloud state/logs;
  submit/submitted truth comes from the platform cache refreshed on page open.

## Alpha Submission And State Sync

- [submit_template_loop.py](D:/StupidNight/工作/量化/learning/script/submit_template_loop.py)  
  Batch submission loop for template-driven backtests.
- [recheck_alphas.py](D:/StupidNight/工作/量化/learning/script/recheck_alphas.py)  
  Re-fetch alpha state and refresh local result truth.
- [sync_platform_state_from_results.py](D:/StupidNight/工作/量化/learning/script/sync_platform_state_from_results.py)  
  Sync platform-facing properties from local results.
- [cache_platform_truth.py](D:/StupidNight/工作/量化/learning/script/cache_platform_truth.py)  
  Refresh platform tag/status truth cache used by the dashboard.

## Research And Raw Alpha

- [weekly_local_research_intake.py](D:/StupidNight/工作/量化/learning/script/weekly_local_research_intake.py)
- [fetch_us_research_resources.py](D:/StupidNight/工作/量化/learning/script/fetch_us_research_resources.py)
- [extract_research_alpha_families.py](D:/StupidNight/工作/量化/learning/script/extract_research_alpha_families.py)
- [ground_research_alpha_fields.py](D:/StupidNight/工作/量化/learning/script/ground_research_alpha_fields.py)
- [raw_alpha_rotation.py](D:/StupidNight/工作/量化/learning/script/raw_alpha_rotation.py)
- [raw_family_diversity_audit.py](D:/StupidNight/工作/量化/learning/script/raw_family_diversity_audit.py)

## Repair And Cleanup

- [high_grade_repair_engine.py](D:/StupidNight/工作/量化/learning/script/high_grade_repair_engine.py)  
  Bounded repair generation for high-grade failed source alphas.
- [sync_repair_wait_tags.py](D:/StupidNight/工作/量化/learning/script/sync_repair_wait_tags.py)  
  Sync platform-visible family and `1REPAIR` tags under the current repair rules.
- [clear_platform_repair_tags.py](D:/StupidNight/工作/量化/learning/script/clear_platform_repair_tags.py)  
  Cleanup tool that removes platform `1REPAIR` tags only.
- [clear_old_repair_queue.py](D:/StupidNight/工作/量化/learning/script/clear_old_repair_queue.py)  
  Cleanup helper for stale repair queue records.

## Validation And Field Selection

- [template_builder.py](D:/StupidNight/工作/量化/learning/script/template_builder.py)
- [template_validator.py](D:/StupidNight/工作/量化/learning/script/template_validator.py)
- [template_similarity.py](D:/StupidNight/工作/量化/learning/script/template_similarity.py)
- [field_selection_engine.py](D:/StupidNight/工作/量化/learning/script/field_selection_engine.py)
- [build_template_field_inventory.py](D:/StupidNight/工作/量化/learning/script/build_template_field_inventory.py)

## Current Main Run

```powershell
python script\continuous_slot_miner.py alpha_backtest\continuous_slot_miner.example.yaml
```

## Notes

- Do not add one-off maintenance scripts here unless they are safe to rerun and
  documented in project memory.
- Platform-visible truth for submission states must remain aligned with the
  dashboard rules in `memory/ai_learned_rules.md`.
