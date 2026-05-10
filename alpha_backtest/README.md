# Alpha Backtest Module

这个模块负责当前项目的回测执行主干。

## Current Main Entry

- [script/continuous_slot_miner.py](D:/StupidNight/工作/量化/learning/script/continuous_slot_miner.py)
- [alpha_backtest/continuous_slot_miner.example.yaml](D:/StupidNight/工作/量化/learning/alpha_backtest/continuous_slot_miner.example.yaml)

## Responsibilities

- 登录 BRAIN
- 批量 simulation
- 获取 alpha 详情和 `/check`
- 本地颜色分级
- 平台 properties / color 同步
- checkpoint / resume
- 云服务器 24x7 连续运行

## Current Rules

1. 不自动正式提交 alpha。
2. 自动流程以 simulation 为核心。
3. 必须支持 checkpoint / resume。
4. 白色不是最终状态，但批量主流程不为了 pending 持续 `/check`。
5. 只要平台已有可用评级，就直接按评级同步颜色。

## Recommended Run

```powershell
python script\continuous_slot_miner.py alpha_backtest\continuous_slot_miner.example.yaml
```
