# M6-patch 数据清理：e2e-test 残留

## 用途

清理 `test_e2e_auth.py` 通过 `subprocess.Popen` 直写开发库 `littlebox` 累积的脏数据：
parent 账户、关联 family、family_members、Redis bind:* key。

## 脚本

- `m6_patch_cleanup.sql` — 两段式结构：Phase 1 SELECT（永远可安全执行）/ Phase 2 DELETE（须手动放开注释）

## 前置假设与风险声明

### 假设

1. **phone 条件不会误伤**：业务真实 phone 是 11 位数字，正则 `^[a-z]{4}$` 严格匹配 4 位纯小写字母，理论上不会碰撞
2. **family 孤儿清理不会误伤**：`DELETE FROM families WHERE id NOT IN (...)` 删除所有无成员 family。业务约定 parent 注册同事务创建 family + family_member(owner)，开发库不应存在无成员 family。若存在，可能是 e2e 残留（e2e 的 DELETE 顺序有误导致 member 先被删），也可能另有来源

## users(id) 反向 FK 穷举（Step 2 模板 C 后补）

通过远端拉取 `backend/app/models/{accounts,chat,parent,audit}.py` 穷举：

- **ON DELETE CASCADE × 8**：`child_profiles.child_user_id` / `child_profiles.created_by` /
  `auth_tokens.user_id` / `device_tokens.user_id` / `family_members.user_id` /
  `sessions.child_user_id` / `daily_reports.child_user_id` / `notifications.child_user_id`
- **ON DELETE NO ACTION × 2（必须显式先删）**：
  - `data_deletion_requests.requested_by` — model 注释「保留 FK：parent 不会被删」
  - `notifications.parent_user_id` — 无显式 `ondelete`，PG 默认 NO ACTION
- 间接 CASCADE：`messages` / `audit_records` / `rolling_summaries` 经 `sessions` 级联

DELETE 顺序由此确定为 **A → B → C → D**（见 SQL Phase 2）。

### 硬性闸门（不可违反）

1. **抽样发现非 e2e 特征样本立即停止**：Phase 1 SELECT 的输出中若有任一行的 `admin_note` 不含 `e2e-test parent` 且 phone 不匹配 `^[a-z]{4}$`，立即停止并上报
2. **孤儿 family 额外核实**：Phase 1 的「额外防御性核实」SELECT 查出非 e2e 关联的孤儿 family → 立即停止上报
3. **DELETE 必须在事务内，严格按 A→B→C→D 顺序**：`BEGIN; ... COMMIT;` 包裹
4. **清理后必须验证**：两次 COUNT 都应为 0

## Step 2 执行命令

### 1. 计数 + 抽样

```bash
docker compose exec -T db psql -U postgres -d littlebox \
  -f backend/scripts/cleanup/m6_patch_cleanup.sql
```

### 2. 抽样核实通过后放开 DELETE 注释，重新跑（事务包裹）

```bash
docker compose exec -T db psql -U postgres -d littlebox \
  -f backend/scripts/cleanup/m6_patch_cleanup.sql
```

### 3. 验证残留 = 0

```bash
docker compose exec -T db psql -U postgres -d littlebox -c \
  "SELECT COUNT(*) FROM users WHERE admin_note = 'e2e-test parent' OR phone ~ '^[a-z]{4}$';"
```

### 4. Redis bind:* 残留计数 + 清理

```bash
docker compose exec -T redis redis-cli --scan --pattern 'bind:*' | wc -l
docker compose exec -T redis sh -c \
  "redis-cli --scan --pattern 'bind:*' | xargs -r redis-cli del"
docker compose exec -T redis redis-cli --scan --pattern 'bind:*' | wc -l   # 应为 0
```

## Step 2 完成后

- 将 SQL 中 DELETE 块重新注释回去，保留脚本作仓库纪录
- 在本计划页 §10 追加清理前后行数对比
