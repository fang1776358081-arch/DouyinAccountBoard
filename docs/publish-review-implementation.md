# 今日发布核对：实现与上线条件

## 页面与运行

入口：日常工作 → 今日发布核对，页面 `/publish-review.html`。采用现有静态 HTML/CSS/JavaScript 与 Python 服务，不嵌入演示 iframe。

候选工作副本：`E:\管理系统\DouyinAccountBoard-publish-review`，已整合远端 `71032fb` 的已提交员工办卡代码；原主工作区办公室三的12个未提交路径未带入。

只读合成预览可运行：

```powershell
& 'C:\Users\Administrator\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -B tools/publish_review_preview.py --root .synthetic-tests/review-preview-new --bulk 2000
```

使用项目现有 Python 运行时；目录须为未使用的合成测试目录。工具只监听 `127.0.0.1`，使用明确的新建合成库，所有 POST 返回405；后台使用实际 `publish_review.overview()` 查询，不能把预览数字当成正式资料。最终浏览器预览为本副本 `.synthetic-tests/publish-review-browser-20261102-final`，端口记录在该目录 `preview.json`。

## 修改文件

| 范围 | 文件 |
| --- | --- |
| 统计、计划、采样与范围检查 | `publish_review.py` |
| 页面、筛选及目标交互 | `static/publish-review.html`、`publish-review.css`、`publish-review.js` |
| API、SQLite合成初始化、压缩路由、权限说明 | `app.py`、`auth.py` |
| 已有采集结果留存 | `collection.py`、`f2_worker.py` |
| 原导航增加入口 | `static/index.html`、`workspace.html`、`users.html` |
| 显式打包与测试入口 | `server_package.py`、`run_isolated_tests.py` |
| 显式追加迁移，已注册但正式库尚未执行 | `migrations/postgresql/0006_publish_review.sql` |
| 合成验证与只读预览 | `test_publish_review.py`、`test_publish_review_http.py`、`test_publish_review_ui.cjs`、`tools/publish_review_preview.py` |
| 说明 | `README.md`、本文 |

## 统计依据

- `collection.complete()` 保存 `accounts.work_count` 累计公开作品总数；`f2_worker.result_body()` 将工具的 `count` 交回该服务。没有证据表明这是当日发布数。
- 旧 `daily.published` 仅0/1人工登记，发布布局也没有按日数量目标；因此不把“已发”勾选转换为条数，不使用固定30或75目标。
- 本日净增为本业务日最近成功采样总数，减去固定昨日基线。若没有本日成功采样，差值为空；不会把昨日采样与昨日基线相减后显示零。负差值保留，不画完成进度。
- 第一次收到新业务日的采样尝试时，从已保存的上一个业务日成功样本中选择实际时间最后的一条并封存。以后刷新只读，后续今日采样或迟到的昨日样本不覆盖已封存基线。未保存的旧历史不会自动补造，封存时间和原采样时间分别保留。
- 所有样本必须提供带时区的实际采样时间并规范化为北京时间；上传时刻不替代采样时刻。现有工具没有更细的有效期配置，自动队列按 `last_attempted` 日期每天一次，因此本页仅把所选北京时间业务日的成功采样视为本日有效。历史日期不按现在的时间把历史样本错误地全部判过期。
- 新版 F2 交付前保留本次工具采样完成时刻，重试交付不重新生成时间。旧工作进程没有这个字段时继续旧最新值逻辑，但不生成伪历史样本。本轮没有开启采集或访问抖音。
- 历史累计差值可能受到删除或隐藏影响，不宣称准确的当天发布条数。稳定关联键是系统账号ID；两次采样的抖音号和当日账号标识不一致时，不能直接相减。

## 计划、状态与归属

每日计划按账号ID与日期唯一保存目标、执行状态、截止时间、电脑ID/名称、负责人文字、明确办公室、当时名称及抖音号。负责人来自现有电脑 `operator`，缺失时显示待补充，不用登录人员代填。

- 没有当日计划：数据待核验，请确认是否安排任务；不声称已有任务未完成。未设计划且账号处于停用、封禁、短期封禁或养号状态时单列暂停，不计入未完成。
- 已安排但目标或电脑未确认：数据待核验；允许保留缺项，不猜电脑关系。
- 明确暂停或无任务：单列显示，不计入待完成。
- 本日最新尝试失败、采样过期、缺昨日基线、身份变化或数量减少：数据待核验，分别列原因。既有旧采集失败发生在已保存成功样本之后时也不能报告达标。
- 数据齐全且净增达到目标：净增达标；否则待完成并显示差额。零净增显示“暂无净增”。进度最多100%，实际超额值不裁剪。
- 截止时间未知不猜；有截止时间时区分进行中与截止后未达标。
- 同日目标调整必须填依据、提交当前 revision。历史日期拒绝直接改目标，包括总管理员；不以今天的目标替换昨天。
- 历史只读当日已保存计划，任务电脑、负责人、名称和办公室使用其快照；不存在历史计划时不拿今天登记电脑或发布格补出历史安排。

顶部汇总及电脑卡片使用当前办公室的全部授权资料；账号列表再叠加电脑和状态，页面注明这两个范围。主列表每页50个，电脑卡片每组最多渲染24台。暂停/无任务另列，因此三个主汇总相加再加两项排除数等于核对总量。

## 接口与权限

- `GET /api/publish-review?date=YYYY-MM-DD&office=...&computer=...&state=pending|review&page=1`：独立读取，不拉 `/api/board`，没有卡、设备流程、养号图片或完整历史版本。
- 返回授权用户及原CSRF、汇总、电脑摘要、最多50条明细。成功 GET 复用原 gzip 协商、长度计算与 `Vary`；保持 `Cache-Control: no-store`，不做公共或跨人员缓存。
- 办公室受现有授权约束；历史还要求当日办公室在当前授权范围内。已经迁到无权办公室的账号也不会通过旧计划泄漏。管理员/员工角色及已有授权未修改。
- `POST /api/publish-review/task` 复用 `publishing_manage`；`POST /api/publish-review/sample` 复用 `collection_manage`。写操作重新读取人员权限及账号/电脑归属，保留CSRF检查。只读人员无写入口。
- 两种写入均要求UUID请求标识，绑定本人、恢复代次和完整内容；相同请求重试回原结果，换内容409。目标还检查 revision，避免覆盖他人改动。新目标写入原审计体系，依据和新旧目标留存。

外部统计工具的采样结果接入合同（必须走现有合法登录/授权通道，不在导出文件里保存凭据）：

```json
{
  "account_id": "系统已有的稳定账号ID",
  "matched_douyin_id": "与当前真实抖音号完全相同的原始字符串",
  "sampled_at": "实际采样时间，包含时区",
  "total": 123,
  "failed": false,
  "request_id": "为该次采样生成并在重试中保持不变的UUID"
}
```

以上是字段示意，不是真实账号或任务数量。成功结果须核对完整抖音号；失败结果可用 `failed=true,total=null`，仍要求实际时间，保留此前成功数量。此接口仅接收已有统计结果，不会触发采集，也不会解除旧采集禁用规则。

现有业务只有最新数量时，页面会保留其参考值并提示缺采样历史；不会自动将旧值当昨日收口。正常计算至少需要相邻业务日可信样本及当日计划。外部工具未送入新合同前，不能承诺已有账号都会立即出现有效净增。

## 数据结构与发布边界

新增四张表：不可变采样 `publish_review_samples`、固定基线 `publish_review_baselines`、按日计划 `publish_review_tasks`、幂等回执 `publish_review_requests`。索引覆盖账号/时间、账号/日期/结果、日期/办公室/电脑。SQLite合成初始化已接入；既有SQLite备份会按原机制包括新表。

PostgreSQL迁移 `migrations/postgresql/0006_publish_review.sql` 已注册到 `pg_schema.FILES`，排在远端已发布的0005之后。0001～0005文件及校验值不变。运行时接受完整且校验匹配的v4/v5/v6；未安装0006时新接口明确503，原站继续运行。每个模块核对自身迁移和新表，不能把“最新迁移不是5”误当成员工办卡不可用。启动过程不执行DDL。

本轮未连接正式数据库或开启采集。代码发布与结构启用须分别验收；没有真实历史采样和按日目标可代填。正式库升级后还需要外部工具开始按合同传入实际采样，以及有权人员设置真实日期目标；首日缺昨日基线应如实显示待核验。

### 正式结构升级与回退

沿用 `docs/employee-card-migration-runbook.md` 的受控流程：仅在服务器已有合法认证会话中操作，核对工作目录/提交/原迁移哈希/维护状态/恢复代次，维护并排空写入，生成服务器私有0600完整备份，检查退出码、SHA256、目录及完整解码，校验恢复条件后调用既有 `pg_schema.apply_migrations(connection)`。禁止运行空库初始化或 `cloudbase_prepare.py`，不重置密码、权限、writer_epoch或restore_generation。

- 原库v4：该有序函数会依次安装已发布0005及本次0006。0005只新增员工办卡配置，不创建真实或演示员工；不能为绕过它而跳号。原库v5：只追加0006。
- 核对完整v6账本/哈希、四张新表和索引；没有自动样本、目标或其他演示数据。原账号/卡/设备/登录用户/办公室授权及控制代次应与升级前一致。
- 恢复原open并按原流程重启，确认health/ready和后端 `publish-review-20261102-r1`，匿名新接口401，有权登录读取成功。未具备认证通道前不声称正式迁移已执行。
- 迁移失败由同一事务回滚，先核对账本和旧业务再恢复原open。成功后回退功能应保留新增数据及兼容层：`pg_schema.py`、`pg_runtime.py`、`cloudbase_runtime.py`、`employee_cards.py`、`publish_review.py`以及0005/0006迁移文件，撤回入口/路由/采样钩子并运行原站回归。不能直接启动只认识v4/v5的老运行时，不DROP表或恢复整库。

本机验证副本 `E:\PublishReviewValidation-20261102` 只复制源码到英文路径，解决Windows便携PostgreSQL对中文data_directory返回本地编码的问题；没复制任何业务库。生产CAM拒绝和电脑操作工具策略不通过其他工具绕过。

## 验证边界

- `run_isolated_tests.py` 的 `test_publish_review`、`test_publish_review_http` 覆盖达标、超额、部分完成、真实零、缺基线、负值、失败、过期、新账号、不同目标、冻结历史、办公室/人员权限、幂等及并发冲突。
- 整合71032fb后重跑新功能/HTTP、员工办卡/HTTP/结构、核心、打包和编码协商，共84项通过；此前核心流程、原采集禁用、JSON压缩协商及现有账号/卡/UI同步回归通过。Node `test_publish_review_ui.cjs` 运行实际新脚本，包含HTTP来源的UUID兼容、过时响应竞争和不确定结果的同请求重试。
- 2002账号SQLite合成场景读取7条SQL，首屏只返回50条；不得将该耗时当作真实PostgreSQL或公网指标。首屏明细约57KB数量级，电脑仅轻量摘要；没有重新加载5MB整份看板。
- Chrome只读合成预览实际2010账号，核对第一页/第二页均50条、电脑与状态组合、空态和重置。预览不能证明正式数据已经有昨日基线、实际工具已送样本或功能已部署。
- 2026-11-02真实本机PostgreSQL16.15、全新回环合成集群验证通过：v4/v5兼容、v6故障原子回滚及重试、旧数据/授权/控制不变、负值/基线/范围/历史/幂等、缺表和恢复代次拒绝。2002账号首屏50条、48163字节，单次读取22.00ms；只是本机合成SQL，不是公网或生产结论。
- 原压缩HTTP回归入口依赖未在本副本提供的旧 `.synthetic-tests/publish-firstscreen-before-20261029-r1/SOURCE-MANIFEST.json`，第一次整合测试在该夹具准备处报错；没有创建/修改旧基线。新页实际HTTP gzip/no-store及编码协商单元测试已通过。
- 同一最终源码另跑员工办卡真实本机PG回归通过；两个PG演练来源哈希保持一致，临时集群均已停止。正式业务登录后和正式迁移验收尚未执行。
