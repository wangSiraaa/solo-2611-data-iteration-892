# 案卷页序目录与引用数据仓（task 726）

独立的卷内页序目录 + 摘录引用仓，输出可被只读客户端直接消费的
`citation-product/1` JSONL。不解析 PDF 或图像；不依赖旧后端。

只提供输入与只读客户端时也能完整运行：`materials/726/` 下的
`pages.json`、`references.jsonl`、`operations.jsonl` 是全部初始数据。

## 快速开始

```bash
# 1) 建目录 → 建引用 → 应用操作 → 导出（与 materials 的真实输入一致）
python3 -m citation_system init --db work/cli_run/catalog.db \
    --pages materials/726/pages.json --force
python3 -m citation_system add-references --db work/cli_run/catalog.db \
    --references materials/726/references.jsonl
python3 -m citation_system apply-operations --db work/cli_run/catalog.db \
    --operations materials/726/operations.jsonl
python3 -m citation_system export --db work/cli_run/catalog.db \
    --usable exports/cli_run/usable.jsonl \
    --needs-attention exports/cli_run/needs_attention.jsonl

# 2) 只读客户端只读 JSONL，不打开数据库
python3 materials/726/citation_client.py \
    exports/cli_run/usable.jsonl exports/cli_run/needs_attention.jsonl
```

一次跑完全部展示场景（含保留的旧导出）：

```bash
python3 scripts/demo_726.py     # 写 exports/scenario/，每步一个目录，互不覆盖
python3 scripts/demo_rights_726.py  # 权属版 + 合刊版税结算展示，写 exports/rights/
python3 -m unittest discover -s tests -v
python3 scripts/benchmark_726.py  # 20 卷 × 200 页 × 500 引用
```

`exports/scenario/00_initial/` 与随题的 `example_usable.jsonl` **逐字节一致**；
客户端对它的输出为：

```
C_PIN usable V1/p2,V1/p3 -
C_RANGE usable V1/p2,V1/p3,V1/p4 -
```

## 目录（`citation_system/catalog.py`）

- **身份**：页的完整身份是 `(volume_id, page_id)`。`page_id` 只在卷内唯一；
  `V1/p1` 与 `V2/p1` 是不同页。`page_id` 一旦分配，插页/移位/停用都**不重发**，
  插入已存在的 `page_id` 会以 `duplicate_page_id` 整批拒绝。
- **物理次序**：每卷内 `seq` 连续 `0..n-1`，**同时**覆盖在用与停用页；
  停用只改状态，不删除、不改位置、不重排号。`UNIQUE(volume_id, seq)` 在
  任何单条 UPDATE 后都成立（右移从高到低，左移从低到高，先挪到临时槽）。
- **显示页码**：`label` 一律字符串，`"3"`、`"03"`、`"2a"` 各不相同，不做
  数值化或前缀匹配。
- **解析**：`resolve(volume, section, label)` 只在**在用**页里找，必须恰好
  一个：0 个 → `resolution_not_found`，多个 → `resolution_ambiguous`
  （附 `matching_page_ids`）。正文 `body/3` 与附录 `appendix/3` 因此不混淆。
- **查询**：当前前后页 `neighbours`（可按 `active_only` 跳过停用页，但返回
  的仍是真实页身份）；有序区间 `ordered_range`（含端，可选排除停用）。
- **操作**：`insert` / `insert_many` / `deactivate` / `activate` / `move` /
  `move_many` / `reorder`，每个请求是一个原子批次。`{"batch_id", "operations"}`
  把多个操作合成一个批次；**任一失败则整批回滚**，包括本来合法的兄弟操作。
- **同卷限制**：`move` 带 `target_volume_id` 且与源卷不同 → `cross_volume_move`；
  锚点页只存在于另一卷 → `cross_volume_anchor`。两种都在写之前拒绝。
- **最终次序**：`reorder` 必须列出该卷全部页（含停用页）且恰好一次，
  可同时改变次序与状态；位置不复用、不重复。
- **操作日志**：每个请求（含被拒绝的）都记录 `status`、`error_code`、
  `catalog_revision`、`expect`/`expectation_met`，可审计。
- **目录独立**：没有任何摘录时目录照常建、改、查。

## 引用仓（`citation_system/repository.py`）

- **创建时固定身份**：接受 `references.jsonl` 的两种写法：
  - 直接给身份：PINNED `page_ids` / RANGE `start_page_id`,`end_page_id`；
  - 给 `(section,label)`（PINNED 用 `items`，RANGE 用 `start_*`/`end_*`）：
    创建时解析一次，之后**只存实际页身份**，读取时不再重解文字。
  - 解析歧义/未找到时**什么都不写**。
- **PINNED**：固定成员集合，无顺序语义。后续插页/移位/重排都不改成员。
  成员停用后仍是成员，整体转入待处理并标明 `active:false`；**绝不静默删成员**。
- **RANGE**：固定同卷两个端页身份。查询时取**当前次序**两端之间（含端）的
  **在用页**，所以中间插页会真实进入结果、中间页停用会被排除。
  任一端停用或先后反转 → 待处理；`source` 与 `pages` 都保持原端点顺序，
  **不交换、不猜测**。
- **重开**：端点重新启用 / 移回原序后，同一引用无需重建即恢复可用。
- **反查**：`citations_referencing(volume, page)` 按实际身份返回 PINNED
  成员与当前 RANGE 覆盖（`member`/`start`/`end`/`interior`）。

## 导出（`citation_system/export.py`）

- 在**一个只读事务**里取完整已提交快照（revision、全部页、全部引用、成员、
  端点），之后只从该快照构造产品。因此一条引用的页列表**不可能拼自两个
  状态**，所有行的 `catalog_revision` 相同。
- `usable.jsonl`：完整可用引用；`reason` 为 `null`；RANGE 的 `pages` 按当前
  卷内次序。
- `needs_attention.jsonl`：端点失效/反转的 RANGE，或含停用固定页的 PINNED。
  保留 `citation_id`、`source`（原卷/原端点或原成员）、`pages`（完整固定集合
  或两个已知端页及状态）、稳定 `reason`，并附 `detail`。
  `reason` 取值：`pinned_inactive_member`、`range_endpoint_inactive`、
  `range_endpoints_reversed`。
- **每个引用恰好出现在一个文件**；PINNED 含停用页时整体进待处理文件，
  不导出其余页冒充完整引用。
- 写出前用 `validate_products` 校验合同；两文件先写临时文件再 `os.replace`，
  单个文件不会半写。若需两文件成对切换，请以**整个导出目录**为单位发布。

## 权属版与合刊版税结算（`citation_system/rights.py` / `settlement.py`）

在既有目录与引用仓之上，为**页身份**增加权属修订，为**刊次**增加版税结算。
全部使用本地合成合同数据，不联网。

### 权属版（`rights.py`）

- **权属版**是按名标识的一组声明；页份额与账户分配都**按生效日分版**。
  查询某页在某个**出版日**的权利时，取 `effective_date <= 出版日` 的
  **最新一版**；更晚的更正不会回填旧刊。旧刊重查因此拿到旧版。
- 页份额与每条账户分配都是非负有理数（内部 `Fraction`，绝不经过 `float`），
  每级必须恰好合为 `1`。声明可批量提交：**任一非法则整批回滚**。
  同一 `(版, 页, 生效日)` 重复声明会以 `rights_version_exists` 拒绝，
  除非显式 `replace`（批量更正用新的生效日或整版替换）。
- **复合权益**：页的直接账户可继续把自身份额按版/日期分给多个收款人或
  中间账户；各边按份额成比例，同一收款人从多条路径所得**合并**。
  没有分配行的账户即最终收款人。返回的每页权益同时给出逐层依据
  （页份额版、账户分配版、每条通道）与合并后的最终份额。
- **待核**：若请求的出版日没有任何页版本（`declaration_not_effective`）、
  某账户的分配只覆盖更晚的日期（`allocation_not_effective`）、存在
  **环**（`allocation_cycle`），或某级不归一，则该页解析失败，
  `try_resolve_page` 返回结构化 `problem`。

CLI：`rights-edition` / `rights-declare`（读取 JSONL，单事务批量）/
`rights-page`（查看一页最终权益与逐层依据）。

声明 JSONL 示例：

```json
{"kind":"page_shares","edition_id":"E","volume_id":"V1","page_id":"p2",
 "effective_date":"2020-01-01","shares":{"A":"1/2","B":"1/2"}}
{"kind":"account_allocations","edition_id":"E","account_id":"A",
 "effective_date":"2020-06-01","shares":{"B":"1"}}
```

### 合刊结算（`settlement.py`）

- **计价组 / 栏目**：栏目属于恰好一个计价组，每组单页金额为整数分。
- **同组同页只付一次，不同组同页各计一次**。刊内以
  `(计价组, volume_id, page_id)` 去重后逐个计费。
- 每个计价单元的分额 **乘以该页最终权属份额**，按收款人把整刊精确应得
  合计，先**向下取整**，余额分按**小数余数从大到小**、**同余按收款人号升序**
  逐分补足。
- **待领余额与门槛**：每位收款人有由**批次（lot）**组成的待领余额，
  每个 lot 标记产生它的刊次（或 `opening`）。`余额 + 本刊分摊 >= 门槛`
  就汇出**全数**并消耗旧 lot，否则整笔留到下刊（新分摊作为新 lot）。
- **原子关账**：一次 `settle` 在 `BEGIN IMMEDIATE` 中完成，且
  1. 只要任一登记摘录当前不可解析（失效成员/端点、反转等）或任一页权属
     待核，**整刊拒绝**（`settlement_blocked`），绝不只按可用部分结账；
  2. 计算本刊整数分摊；
  3. 结算并登记汇出，消耗旧余额；
  4. 写入**封存凭据**（`royalty-voucher/1`），其规范化 JSON 的 SHA-256
     可离线重算校验（`voucher --verify`）。
  同一刊次重试返回同一凭据、**不再扣一次**旧余额；并发关账在写事务上串行，
  只有第一次真正改动账本。后续刊次接续。
- 凭据自带解释材料：登记（栏目/摘录/登记时目录修订）、计价单元、
  每页权益与逐层依据、精确/取整/余数/补足、每人 prior lots（含来源刊次）、
  实付与留存。因此历史凭证可独立解释每一分从哪几页、哪几期余额产生。

CLI：`pricing-group` / `column` / `issue` / `register` / `settle` /
`voucher` / `balance` / `payee-terms`。

### 工作示例（任务给定）

同卷 `p`、`q` 均印页码 3；`p` 对甲乙各半，`q` 全归乙。栏目一引固定 `p`，
栏目二引含 `p,q` 的范围，起初同组，单页 3 分：

- 同组：`p,q` 各计一次 = 6 分；甲乙精确 1.5 / 4.5，甲号靠前 → 实付 2 / 4。
- 栏目二换到另一同价组：合计 9 分；甲乙应付 3 / 6。
- 保持原合同，仅把甲账户份额全数分给乙：最终归属 0 / 6。
- 用原权属、甲乙门槛均 5、期初零：首刊两人都暂存（甲 2、乙 4）；
  下刊重印，甲累至 4 仍暂存，乙累至 8 应汇 8。

`scripts/demo_rights_726.py` 逐项跑通以上场景，并额外演示多路径复合权益、
范围中间插页、以及并发关账只付一次，凭据写入 `exports/rights/`。

## 依赖索引（`citation_system/dependency.py`，可选）

- 目的：加速"某实际页被哪些引用覆盖"。
- 表 `page_citation_dependency(volume_id, page_id, citation_id, role)`。
- **从不用于导出**，只用于查询，所以索引陈旧不会污染产品。
- 带 `catalog_revision` 戳；任何目录变更（含同卷移位）都会使戳不匹配，
  下次查询时惰性整表重建。PINNED 行与次序无关，RANGE 行随次序变化；
  为简单起见整表重建。
- 成本（实测，见 `scripts/benchmark_726.py`）：20 卷 × 200 页 × 500 引用时
  重建约 35 ms、约 1.7 万行；单次反查中位数：直查 ~0.25 ms，
  走索引 ~0.18 ms。规模更小时直查已足够，索引只是可选加速。
  同卷移位本身 ~0.6 ms，重导出 ~18 ms。

## 规模与性能

`scripts/benchmark_726.py` 在目标规模（20 卷、每卷 200 页、500 引用）测得：

| 操作 | 耗时 |
| --- | --- |
| 载入目录 | ~8 ms |
| 创建 500 条引用 | ~31 ms（~0.06 ms/条） |
| 一次性导出 | ~19 ms |
| 同卷移位 | ~0.6 ms |
| 移位后重导出 | ~18 ms |
| 依赖索引重建 | ~35 ms |

## 布局

```
citation_system/   实现源码（catalog / repository / export / dependency / rights / settlement / cli）
tests/             单元与集成测试（含随机不变量、目标规模、权属与结算）
scripts/demo_726.py        端到端展示，逐步保留旧导出
scripts/demo_rights_726.py 权属版与合刊版税结算展示
scripts/benchmark_726.py   目标规模基准
exports/scenario/  演示产生的各步骤导出（旧目录保留）
exports/rights/    结算演示的封存凭据与索引
exports/cli_run/   按 materials 原始输入走完整 CLI 的导出
work/              运行的 SQLite 数据库
```

## 不做什么

不 OCR、不做自然语言定位、不做版式编辑；不解析 PDF 图像；不读取图片内容。
