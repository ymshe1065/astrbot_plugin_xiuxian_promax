# 物品价格调整执行包

> 本目录是可执行价格表，尚未写入游戏配置。
> 生成依据：《[物品价格调整报告.md](../物品价格调整报告.md)》。

## 文件

| 文件 | 内容 |
| --- | --- |
| `materials.csv` | 174 条材料的当前价与新价 |
| `packages.csv` | 16 个材料包的新价格与期望收益 |
| `recipes.csv` | 42 条丹方的新材料、灵石、成功率、丹药价、卷轴价 |
| `pills.csv` | 全部丹药的当前价与新价（含未调整项） |
| `equipment_exceptions.csv` | 首批异常装备的人工重定价 |
| `summary.json` | 数据统计 |

## 价格锚点

### 材料

```text
材料价 = 类别基准 × 品阶倍率
```

类别基准：

```text
灵植 60 / 矿材 72 / 妖兽 90 / 灵液 120 / 特殊保留原价
```

品阶倍率：

```text
凡1 / 黄2.5 / 玄6 / 灵12 / 地25 / 天50 / 皇100 / 圣200
/ 帝500 / 道1500 / 仙4000 / 混元10000
```

### 材料包

```text
包价 = 期望材料价值 ÷ 0.8
```

目标期望/售价：

```text
0.75～0.90
```

### 丹药与丹方

```text
期望成本 = (材料成本 + 灵石成本) ÷ 成功率
丹药价 = max(品阶最低价, 期望成本 × 1.25)
丹方卷轴价 = 丹药价 × 10%
```

当前执行表使用较低品阶底价，避免高阶底价再次制造暴利。

### 配方

- 固定使用 `category + tier + count`；
- 10 条旧「灵草」配方已映射到对应灵植品阶；
- 灵石成本按材料成本的 10% 计算；
- `pill_recipes.json` 作为唯一有效丹方来源。

## 执行顺序

1. 应用 `materials.csv` 到 `material_catalog.json`；
2. 修改 `ConfigManager._inject_material_packages`，按 `packages.csv` 生成包价；
3. 按 `recipes.csv` 重写 `pill_recipes.json`；
4. 按 `pills.csv` 同步 `pills.json`、`exp_pills.json`、`utility_pills.json`、`items.json`；
5. 应用 `equipment_exceptions.csv`；
6. 运行价格校验；
7. 更新 README 和测试；
8. 部署时不备份。

## 注意

`pills.csv` 中 `action=hold` 的条目本轮不改价，只列出当前价格，避免一次性改动没有配方依据的丹药。

`recipes.csv` 中高品阶丹药价格已经按期望成本压缩；如果后续希望保留“超长期目标”溢价，应单独设定品阶底价，而不是使用百倍随机价。
