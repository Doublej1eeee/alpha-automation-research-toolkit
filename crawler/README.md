# Crawler Module

这个模块负责把外部知识和平台元信息抓回本地，供原始 alpha 研究与字段选择使用。

## Two Classes

### 1. Learning Content

- 论文
- 官方报告 / 公司新闻
- BRAIN 学习资料
- 工具文档

### 2. Data Content

- datasets
- datafields
- 字段元信息与 description
- coverage / 更新频率相关信息

## Current Formal Directories

- `datafields/`
- `usa_research/`

## Current USA Research Intake Reality

正式抓取结果写入：

- `memory/learning_sources/quant_papers/usa_equity/`
- `memory/learning_sources/research_reports/usa_equity/`

当前状态：

- `arXiv`：metadata、PDF、正文文本已基本成立
- `OpenAlex`：发现层，只作辅助元数据源
- `SEC`：metadata 已有，正文链未打通
- `company_news`：正文补源已接入，但仍属于辅助报告源

## Core Principle

不要全量抓，不要长期囤积巨大的无差别字段池。

优先：

- 基于模板语义抓
- 基于 category / dataset 抓
- 基于 operator compatibility 筛
- 基于 coverage / 更新频率筛

## Daily Incremental Entry

```powershell
python script\fetch_us_research_resources.py --loop-seconds 86400
```
