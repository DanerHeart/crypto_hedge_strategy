# 对冲策略交易机器人

一个基于币安期货API的多空双开对冲策略交易机器人。

## 策略思路

本策略采用**多空双开对冲**的交易思路，核心逻辑如下：

1. **市场中性开仓**：同时对同一交易对开多单和空单，形成对冲持仓。这样无论市场上涨还是下跌，多空双方的盈亏会相互抵消，理论上可以实现市场中性。

2. **亏损方止损**：当市场出现单边行情时，一方会盈利，另一方会亏损。当亏损方的亏损超过设定的止损阈值（默认1%）时，立即止损平仓，控制风险。

3. **盈利方移动止盈**：当亏损方止损后，盈利方进入移动止盈模式。系统设置了11档移动止盈机制，根据盈利幅度动态调整止盈比例：
   - 盈利较小时（0.2%-1.5%），使用固定回撤或较小比例回撤
   - 盈利较大时（2.0%-10.0%），使用比例回撤，允许更大的回撤空间以捕捉更大涨幅

4. **风险控制**：通过设置累计总收益的亏损阈值，当整体亏损超过设定值时自动停止交易，避免持续亏损。

**策略优势**：
- 市场中性，不依赖方向判断
- 通过移动止盈最大化盈利方的收益
- 严格的止损机制控制单次亏损
- 整体风险可控，有明确的停止条件

**适用场景**：
- 震荡市场或波动较大的市场
- 适合捕捉短期价格波动
- 需要严格控制风险的交易环境

## 功能特性

- ✅ **多空双开**: 同时开多单和空单，实现市场中性策略
- ✅ **智能止损**: 当一方亏损超过1%时，亏损方自动止损
- ✅ **移动止盈**: 盈利方进入11档移动止盈模式，最大化收益
- ✅ **单边监控**: 支持单边持仓的移动止盈监控
- ✅ **钉钉通知**: 支持交易通知和持仓状态推送
- ✅ **日志记录**: 完整的日志记录系统，支持日志轮转

## 项目结构

```
hedge_strategy/
├── main_hedge.py                    # 主程序入口
├── config.json                      # 配置文件
├── requirements.txt                 # 依赖包清单
├── strategies/                      # 策略模块
│   ├── base_strategy.py            # 策略基类
│   └── hedge_strategy.py           # 对冲策略实现
├── position_manager/                # 仓位管理模块
│   ├── hedge_stop_loss_manager.py   # 对冲策略止盈止损管理器
│   ├── stop_loss_manager.py        # 通用止盈止损管理器
│   └── position_monitor.py        # 独立监控入口
└── utils/                           # 工具模块
    ├── config_loader.py            # 配置加载
    ├── exchange_utils.py           # 交易所工具函数
    ├── logger_setup.py             # 日志配置
    ├── math_utils.py               # 数学计算工具
    └── notification.py             # 通知模块
```

## 安装

1. 克隆或下载项目到本地

2. 安装依赖包：
```bash
pip install -r requirements.txt
```

3. 配置 `config.json`：
   ```bash
   # 复制示例配置文件
   cp config.example.json config.json
   
   # 编辑 config.json，填入你的API密钥等信息
   ```
   
   配置文件模板：
   ```json
   {
       "binance": {
           "apiKey": "你的API密钥",
           "secret": "你的API密钥",
           "leverage": 1.0
       },
       "dingtalk_webhook": "钉钉Webhook地址（可选）",
       "enable_dingtalk_notification": false,
       "monitor_interval": 60,
       "leverage": 6,
       "max_total_profit_loss_usdt": -10.0,
       "min_total_profit_usdt": null,
       "stop_loss": {
           "stop_loss_pct": 1,
           ...
       },
       "tradingPairs": {
           "RIVER-USDT-SWAP": {
               "long_amount_usdt": 20,
               "short_amount_usdt": 20,
               "value_multiplier": 2.5,
               "ema": 240
           }
       }
   }
   ```
   
   ⚠️ **重要**：`config.json` 文件包含敏感信息，已被 `.gitignore` 忽略，不会被提交到Git仓库。

## 使用方法

### 运行主程序
```bash
python main_hedge.py
```

### 独立运行止盈止损监控
```bash
python position_manager/position_monitor.py
```

## 配置说明

### 主要配置项

- **binance**: 币安API配置
  - `apiKey`: API密钥
  - `secret`: API密钥
  - `leverage`: 杠杆倍数（已废弃，使用全局leverage）

- **leverage**: 全局杠杆倍数（默认6倍）

- **monitor_interval**: 挂单检查间隔（秒，默认60秒）

- **max_total_profit_loss_usdt**: 累计总收益的最大亏损阈值（负数），如果累计总收益 <= 此值，停止机器人

- **min_total_profit_usdt**: 累计总收益的目标盈利阈值（正数），如果累计总收益 >= 此值，停止机器人（可选，设为null则不启用）

- **stop_loss**: 止盈止损配置
  - `stop_loss_pct`: 基础止损百分比（默认1%）
  - 11档移动止盈配置（详见config.json）

- **tradingPairs**: 交易对配置
  - `long_amount_usdt`: 多单金额（USDT）
  - `short_amount_usdt`: 空单金额（USDT）
  - `value_multiplier`: 价值乘数
  - `ema`: EMA周期

## 策略说明

### 对冲策略逻辑

1. **开仓**: 同时下多单和空单限价单，降低滑点
2. **监控**: 实时监控多空双方的盈亏情况
3. **止损**: 当一方亏损超过配置的止损百分比（默认1%）时：
   - 亏损方立即止损平仓
   - 盈利方进入移动止盈模式
4. **移动止盈**: 11档移动止盈，根据盈利幅度动态调整止盈比例

### 移动止盈档位

- 第1档: 阈值0.7%，比例回撤20%
- 第2档: 阈值1.2%，固定回撤0.2%
- 第3档: 阈值1.3%，固定回撤0.3%
- 第4档: 阈值1.5%，固定回撤0.3%
- 第5档: 阈值2.0%，比例回撤30%
- 第6档: 阈值2.5%，比例回撤30%
- 第7档: 阈值3.0%，比例回撤25%
- 第8档: 阈值4.0%，比例回撤25%
- 第9档: 阈值5.0%，比例回撤20%
- 第10档: 阈值7.5%，比例回撤20%
- 第11档: 阈值10.0%，比例回撤20%

## 注意事项

⚠️ **重要提示**:

1. **API密钥安全**: 请妥善保管API密钥，不要提交到代码仓库
2. **测试环境**: 建议先在测试环境（testnet）中测试
3. **双向持仓**: 确保币安账户已开启双向持仓模式
4. **资金管理**: 合理设置 `max_total_profit_loss_usdt`
5. **杠杆风险**: 注意杠杆倍数设置，高杠杆可能带来较大风险
6. **网络稳定**: 确保网络连接稳定，避免订单执行失败

## 日志

日志文件保存在 `log/` 目录下，文件名格式为 `integrated_bot.log.YYYY-MM-DD`，支持日志轮转（保留7天）。

## 许可证

- 本项目仅供学习和研究使用，使用本代码进行交易产生的任何损失，作者不承担责任。
- 使用作者的链接注册，可获得作者开发的crypto因子分析框架，包含分钟级数据全量/增量下载、数据聚合；基于GPlearn因子挖掘、因子表达式引擎、因子结果分析。
- https://accounts.bmwweb.link/register?ref=118922725

## 更新日志

- 修复了 `exchange_utils.py` 中的重复异常处理问题
- 添加了项目完整性检查报告
- 添加了依赖管理文件

