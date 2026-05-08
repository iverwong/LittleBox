-- M6-patch 一次性清理：清除 e2e-test 残留账户及关联
-- 仅在确认开发库 littlebox 受 e2e 污染时执行
-- 参考: M6-patch · 测试隔离纪律加固

-- ======================== Phase 1: SELECT only (always safe to run) ========================

-- 1. 计数 + 抽样（必须先执行确认）
SELECT COUNT(*) AS dirty_users FROM users
WHERE admin_note = 'e2e-test parent' OR phone ~ '^[a-z]{4}$';

SELECT id, phone, admin_note, role, created_at FROM users
WHERE admin_note = 'e2e-test parent' OR phone ~ '^[a-z]{4}$'
ORDER BY created_at LIMIT 5;

-- 2. 额外防御性核实：查出所有「无成员 family」中 non-e2e 部分
-- （e2e 特征是所有关联 member 的 user 命中 admin_note/phone 条件）
-- 见 m6_patch_cleanup.md §安全闸门
SELECT f.id, f.created_at FROM families f
WHERE f.id NOT IN (SELECT DISTINCT family_id FROM family_members)

EXCEPT

SELECT f.id, f.created_at FROM families f
JOIN family_members fm ON fm.family_id = f.id
JOIN users u ON u.id = fm.user_id
WHERE u.admin_note = 'e2e-test parent' OR u.phone ~ '^[a-z]{4}$';

-- 3. Redis bind:* 残留计数
-- （命令在 shell 执行，此处仅作提醒占位）

-- ======================== Phase 2: DELETE (manually uncomment after sampling) ========================
-- ⚠️ 仅在 Phase 1 抽样 100% 命中 e2e 特征后才可取消下方注释
-- ⚠️ 必须全程在 BEGIN; ... COMMIT; 事务内执行
-- ⚠️ 删除顺序：family_members → users → families（外键级联）
-- ⚠️ 清理后需将 DELETE 重新注释回去保留脚本作仓库纪录

-- BEGIN;

-- DELETE FROM family_members WHERE user_id IN (
--   SELECT id FROM users
--   WHERE admin_note = 'e2e-test parent' OR phone ~ '^[a-z]{4}$'
-- );

-- DELETE FROM users
-- WHERE admin_note = 'e2e-test parent' OR phone ~ '^[a-z]{4}$';

-- DELETE FROM families WHERE id NOT IN (
--   SELECT DISTINCT family_id FROM family_members
-- );

-- COMMIT;

-- ======================== Cleanup verification (always safe to run) ========================

-- SELECT COUNT(*) FROM users
-- WHERE admin_note = 'e2e-test parent' OR phone ~ '^[a-z]{4}$';
