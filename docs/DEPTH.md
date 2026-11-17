# 深度登记 扫描期版质检台

- 规模：中型
- 核心规则：期号、版次、页码构成业务键，图像相似只生成候选，页眉证据决定混期。
- 使用者与工作流：质检员导入扫描清单，核对期版与页序，裁决重扫和混期，归档证据。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 清单采信 | 导入扫描清单并定位坏行 | POST /api/scans/import | `app/modules/m1_intake/parser.py` `app/modules/m1_intake/integrity.py` | `tests/test_m1_intake.py` |
| M2 期版契约 | 规范期号、版次和页码 | POST /api/issues/normalize | `app/modules/m2_issue/rules.py` `app/modules/m2_issue/catalog.py` | `tests/test_m2_issue.py` |
| M3 页码序列 | 查缺页、重页与越界页 | POST /api/pages/check | `app/modules/m3_pages/sequence.py` `app/modules/m3_pages/reconcile.py` | `tests/test_m3_pages.py` |
| M4 重扫判定 | 比较扫描候选并保留清晰版本 | POST /api/rescans/evaluate | `app/modules/m4_rescans/similarity.py` `app/modules/m4_rescans/decision.py` | `tests/test_m4_rescans.py` |
| M5 混期归因 | 用页眉和页码证据裁决混期 | POST /api/mixed/evaluate | `app/modules/m5_mixed/evidence.py` `app/modules/m5_mixed/conflicts.py` | `tests/test_m5_mixed.py` |
| M6 批次对账 | 检查四账并导出证据 | POST /api/reconcile | `app/modules/m6_batches/compare.py` `app/modules/m6_batches/exporter.py` | `tests/test_m6_batches.py` |
| M7 目录合并 | 登记清单、合并同版目录、撤回误合并 | POST /api/catalogs/register, /merge, /retract | `app/modules/m7_catalogs/service.py` `app/modules/m7_catalogs/schema.py` | `tests/test_m7_catalogs.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现会怎样 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-02 | 首列带 BOM | 期号字段取不到 | 读取表头时移除 BOM | `app/modules/m1_intake/parser.py` | `tests/test_m1_intake.py::test_strip_bom_header` |
| TRAP-05 | 行列数不齐 | 错位行进入页序 | 隔离并保留物理行号 | `app/modules/m1_intake/parser.py` | `tests/test_m1_intake.py::test_column_count_goes_quarantine` |
| TRAP-07 | 页眉为空 | 空串被当作一致 | 返回未知并转人工复核 | `app/modules/m5_mixed/evidence.py` | `tests/test_m5_mixed.py::test_missing_header_is_unknown` |
| TRAP-08 | 前导零页码 | 转整数后无法复核原编号 | 规范为三位字符串 | `app/modules/m2_issue/rules.py` | `tests/test_m2_issue.py::test_page_zero_pads_without_numeric_roundtrip` |
| TRAP-09 | 大摘要或相似哈希 | 数字往返或只凭相似度误判 | 摘要保形，相似仅生成候选 | `app/modules/m5_mixed/conflicts.py` | `tests/test_m5_mixed.py::test_hash_candidate_without_header_needs_review` |
| TRAP-14 | 同版多扫描值冲突 | 后写覆盖清晰版本 | 版本、清晰度、时间稳定择优 | `app/modules/m3_pages/sequence.py` | `tests/test_m3_pages.py::test_duplicate_page_detected` |
| TRAP-16 | 清单尾行截断 | 半行被当成扫描件 | 完整尾行检查并隔离 | `app/modules/m1_intake/integrity.py` | `tests/test_m1_intake.py::test_truncated_tail_detected` |
| TRAP-19 | 候选顺序改变 | 先到者决定保留项 | 显式排序键保持顺序无关 | `app/modules/m3_pages/sequence.py` | `tests/test_m3_pages.py::test_selection_order_independent` |
| TRAP-22 | 同内容多别名 | 合并顺序改变代表扫描 | 清晰度后以时间和文件名择一 | `app/modules/m4_rescans/decision.py` | `tests/test_m4_rescans.py::test_selection_is_deterministic_on_tie` |
| TRAP-26 | 陈旧重扫晚提交 | 旧版本覆盖新版 | 版本优先于输入顺序 | `app/modules/m3_pages/sequence.py` | `tests/test_m3_pages.py::test_higher_version_wins_not_later_input` |
| TRAP-27 | 同期报纸登记成两个目录 | 改业务键导致旧验收凭据断引用；保留双目录重复报缺页 | 当前归属与原始身份分离，合并只改归属并记录映射依据 | `app/modules/m7_catalogs/service.py` | `tests/test_m7_catalogs.py::test_merge_keeps_original_identity_for_old_credentials` |
| TRAP-28 | 合并时同页不同扫描相撞 | 按清晰度直接丢件 | 挂起为冲突待人工裁决，双方都保留 | `app/modules/m7_catalogs/service.py` | `tests/test_m7_catalogs.py::test_same_page_different_scan_is_held_not_dropped_by_clarity` |
| TRAP-29 | 合并中途中断后重试 | 重复插入扫描件 | 迁移账本逐件提交，从中断处续做，不新增扫描 | `app/modules/m7_catalogs/service.py` | `tests/test_m7_catalogs.py::test_interrupted_merge_resumes_without_duplicate_scans` |
| TRAP-30 | 合并、撤回与新清单并发 | 陈旧操作按旧视图覆盖新状态 | 目录版本校验，过期操作 409 拒绝 | `app/modules/m7_catalogs/schema.py` | `tests/test_m7_catalogs.py::test_stale_merge_version_is_rejected` |
| TRAP-31 | 撤回误合并带走后来归入的扫描 | 整目录回滚或删键 | 只回放本次合并的迁移账本，后来的扫描留在原处 | `app/modules/m7_catalogs/service.py` | `tests/test_m7_catalogs.py::test_retract_restores_only_this_merge_and_keeps_later_scans` |

## 对账规则

- 应有页数 = 唯一采纳页 + 缺页数。
- 扫描件数 = 唯一采纳 + 被替代重扫 + 混期 + 隔离。
- 同一输入与规则版本重跑，运行标识和结果行数不变。

## 目录合并规则（M7）

- 扫描件始终保存原始身份（`original_catalog_id`/`original_key`/`original_business_key`）；合并只改当前归属（`catalog_id`）。
- 验收凭据按原始身份核验：`POST /api/catalogs/verify` 不因合并而失效，并返回迁移合并与映射依据。
- 同页同摘要为重复件（`DUPLICATE`），不计页；同页不同扫描为冲突（`CONFLICT`）并挂起，不按清晰度丢件，须人工裁决。
- 合并逐件提交迁移账本；中断后重试从首个无账本记录的扫描继续，不新增扫描。已完成合并重试为幂等重放。
- 合并、撤回、登记、裁决均受目录版本约束；携带过期版本号返回 409 `STALE_CATALOG`。合并进行中拒绝该目录的新登记。
- 撤回只还原本次合并账本内的扫描；后来归入的扫描、后续合并的目录、已裁决的冲突不会被带走。
- 目录统计给出扫描总数、状态分布、页序缺口（`missing_pages`）、重复页与覆盖率。

## 已知不覆盖

- 不读取图像二进制，感知哈希与 OCR 页眉由上游扫描软件提供。

