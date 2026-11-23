# 初始结构

`src/models/tables.py` 定义初始关系结构。API 启动时显式创建演示库的表；此版本只包含初始 schema。需要在已有部署升级表结构时，应单独引入版本化迁移。

## 更正补正（演示升级）

演示库在既有数据卷上升级时，`src/main.py` 的 `upgrade()` 会为 `business_evidence` 补上 `correction_batch` 列；新表 `correction_state`、`correction_orders`、`correction_lines` 由 `schema.create_all` 创建。正式部署应改用版本化迁移，并按以下顺序在事务中执行：

1. 新增更正状态行，`current_batch` 从 `0` 起。
2. 插入更正单与更正行时同时推进 `current_batch`，保证同一批次的行一次性可见。
3. 重算未签证据并把 `correction_batch` 固定为当前批次；已签证据保持不变。
