# Template Library V1

这是一版“有来源的模板库”，不是凭感觉乱写。

核心原则：

1. 优先收录来自官方教程/API 已抓取内容的模板。
2. 分清“现在就能批量生成”和“暂时只适合手工研究”的模板。
3. 模板本身只是原型，不等于最终高评级 Alpha。
4. 优化时要继续看颜色规则、相关性、提交顺序影响和 settings。

---

## 一、当前可直接批量生成的官方模板

这些模板已经落成 YAML，可以直接用现有脚本批量生成。

### 1. `official_ts_rank_252_template.yaml`

文件：
[official_ts_rank_252_template.yaml](d:/StupidNight/工作/量化/learning/alpha_generation/templates/official_ts_rank_252_template.yaml)

表达式原型：

```txt
ts_rank({{FIELD}}, 252)
```

来源：
- `Alpha Examples for Beginners`
- `Operating Earnings Yield`

适合字段：
- 有“当前值相对过去一年高低位置”意义的单字段
- 例如盈利、收入、质量、某些基本面水平字段

不要乱用在：
- 事件型 analyst 字段
- 枚举/分类字段
- 更新极稀疏且无时间排序意义的字段

---

### 2. `official_neg_ts_rank_126_template.yaml`

文件：
[official_neg_ts_rank_126_template.yaml](d:/StupidNight/工作/量化/learning/alpha_generation/templates/official_neg_ts_rank_126_template.yaml)

表达式原型：

```txt
-ts_rank({{FIELD}}, 126)
```

来源：
- `Alpha Examples for Beginners`
- `Appreciation of liabilities`

适合字段：
- “值越高越差”的单字段
- 利空型成本、负债、公允价值恶化、风险上升类字段

说明：
- 官方示例是 `252`
- 这里模板默认用 `126`
- 这是基于官方“试更短窗口”的提示，属于保守改写，不是无中生有

---

### 3. `official_group_rank_ratio_close_60_template.yaml`

文件：
[official_group_rank_ratio_close_60_template.yaml](d:/StupidNight/工作/量化/learning/alpha_generation/templates/official_group_rank_ratio_close_60_template.yaml)

表达式原型：

```txt
group_rank(ts_rank({{FIELD}} / close, 60), industry)
```

来源：
- `Alpha Examples for Beginners`
- `Earnings Yield Momentum`

适合字段：
- 可以和价格形成相对估值或相对收益率代理的字段
- 例如 EPS、现金流、股息、销售额、每股类字段

注意：
- 这类模板很常见，可能更容易撞自相关
- 但也常常是高质量起点

---

### 4. `official_neg_ts_std_dev_10_template.yaml`

文件：
[official_neg_ts_std_dev_10_template.yaml](d:/StupidNight/工作/量化/learning/alpha_generation/templates/official_neg_ts_std_dev_10_template.yaml)

表达式原型：

```txt
-ts_std_dev({{FIELD}}, 10)
```

来源：
- `Alpha Examples for Beginners`
- `Short-Term Sentiment Volume Stability`

适合字段：
- 讨论热度、成交活跃度、注意力、短期行为噪声类字段
- 含义是“越不稳定越差”

不要乱用在：
- 低频慢变量
- 长期水平类基本面字段

---

## 二、已经确认有官方来源，但当前不适合直接单字段批量生成的模板

这些模板值得保留，后面可以升级脚本或手工研究。

### 5. 现金流估值变化模板

来源：
- `Alpha Examples for Bronze Users`
- `Valuation based on cash flow`

思路：
- 用 EV/CF 一类比率
- 对变化做 `ts_zscore`
- 再用 `group_rank` 控 turnover

为什么暂不自动化：
- 需要明确分子、分母和具体 cash flow 口径
- 不是单字段模板

---

### 6. 双字段相关性模板

来源：
- `Alpha Examples for Bronze Users`
- `Overpriced stocks`

思路：
- `ts_corr(est_ptp, est_fcf, window)`

价值：
- 这是很重要的“双字段关系模板”原型
- 后面模板库升级时应优先支持

---

### 7. 波动率价差模板

来源：
- `Alpha Examples for Bronze Users`
- `Volatility arbitrage`
- `Alpha Examples for Silver Users`

思路：
- 隐含波动率 vs 历史波动率
- Call/Put skew
- 常配合 `ts_backfill`、`trade_when`、自定义 neutralization

为什么暂不自动化：
- 需要多个期权字段
- 通常还有过滤条件

---

### 8. 条件交易模板

来源：
- `Alpha Examples for Silver Users`
- `Implied Volatility Spread as a predictor`
- `5-Day Peer vs. Stock Performance Gap`

思路：
- `trade_when(condition, signal, exit)`

价值：
- 对降低 turnover、减少噪声很重要
- 是后续模板库必须支持的一类

---

### 9. 回归趋势模板

来源：
- `Alpha Examples for Silver Users`
- `Investing for the Future`

思路：
- `ts_regression(..., ts_step(1), 756, rettype=2)`

价值：
- 用来从低频字段里提取长期趋势，而不是只看水平值

---

## 三、从官方材料里提炼出的模板使用规则

来源：
- `Clear these tests before submitting an Alpha`
- `How to choose the Simulation Settings`
- `Must-read posts: How to improve your Alphas`

### 模板使用规则 1

模板只是研究起点，不要一过线就提交。

### 模板使用规则 2

低相关通常比“小幅提高绩效”更重要。

### 模板使用规则 3

如果模板天然高换手，优先考虑：
- `decay`
- `trade_when`
- 更稳健的窗口

### 模板使用规则 4

如果字段稀疏或低频，优先检查：
- `nanHandling`
- `ts_backfill`
- 覆盖率

### 模板使用规则 5

如果模板乘了明显的 size/liquidity 方向因子，要警惕 sub-universe test。

---

## 四、下一步该怎么扩模板库

优先级建议：

1. 先扩“单字段官方模板”的字段适配规则。
2. 然后升级生成器，支持双字段模板。
3. 再加入条件模板和 trend/regression 模板。
4. 最后把高评级样本沉淀回模板族经验。

---

## 五、当前最重要的现实提醒

即使一个模板当前 `Check Submission` 能过，也不代表以后一定还能过。

原因：
- 自相关池会变化
- 已提交相似 Alpha 会影响未提交候选
- 所以模板库只能提高研究效率，不能替代最终检查

这个经验已经沉淀在：
[submission_order_and_self_correlation.md](d:/StupidNight/工作/量化/learning/memory/manual/submission_order_and_self_correlation.md)
