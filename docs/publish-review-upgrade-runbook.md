# 今日发布核对：一次性 v5 → v6 升级

仅供已有腾讯云服务器 `/opt/douyin-board`、其既有服务环境和 root 管理终端使用。它是手动运行的维护工具，不会在网站启动或代码同步时自动建表，不接受连接串、密码或数据库地址参数。

## 运行前

确认本次发布的40位Git提交与服务器一致、工作区干净，运行中的服务为 `douyin-board.service`。已核对的现场为 PostgreSQL/pg_dump/pg_restore 16.15、结构v5、历史校验一致、mode=open。代码同步后后端标记为 `publish-review-20261103-upgrade-r1`；这个标记只说明代码已更新，不表示数据结构已升级。

使用已核对的固定提交号，不以自动读取任意HEAD替代人工核对：

```bash
/opt/douyin-board/.venv/bin/python -B /opt/douyin-board/tools/upgrade_publish_review.py --expect-commit <本次已核对的40位提交号> --execute
```

去掉 `--execute` 只核对版本和状态，不建表。命令仍需有效的 `--expect-commit`。Git按目录所属Linux用户读取，不修改safe.directory或全局Git配置。

## 执行内容与成功结果

1. 核对提交、源码字节、固定0006哈希、完整v5/v6账本；v5须没有任何部分v6表或索引。v6已存在且可用时，只记录审计回执，不改数据或重启。
2. 获取独占进程锁和既有迁移advisory锁，记录同步timer原状态；暂停timer，等待同步service的MainPID/ControlPID归零，不强杀正在运行的Git同步。原timer关闭时保持关闭。
3. 以原control行的mode/epoch/generation条件进入maintenance，等待已有写事务结束。期间暂时不能保存业务资料。
4. 在服务器root私有目录 `/root/douyin-board-v6-*` 生成0600的完整业务schema自定义格式备份，检查退出码、全部旧表/序列与数据条目、流式SHA256及完整解码。不上传备份，不记录业务明文或凭据。生产备份完整解码不等于生产恢复演练。
5. 对比备份前后的全部旧业务行指纹、ACL、schema权限和序列；再次检查代码、timer与control身份未变。仅调用现有受控 `pg_schema.apply_migrations()` 从合法v5追加0006，不改迁移清单，不安装更多版本。
6. 核对四张新表、三个索引、空的新业务记录、v6账本及旧数据/授权/序列不变；恢复原open，保留epoch/generation，重启原网站并核对health/ready及实际构建标记，最后恢复原timer状态。不开启采集，不创建演示数据，不补造历史。

成功输出 `"status":"complete"`、`"schema_version":6` 和备份/回执路径。随后由有权用户刷新 `/publish-review.html` 验收。旧系统没有昨日历史时，待核验是正确结果。

## 停止与回退

`STOP` 时先保存命令输出。原始control行、timer原状态、备份校验和执行阶段在私有 `receipt.json`；未解决的任务标记在 `/root/douyin-board-publish-review-v6.pending`。不要删除标记后盲目重跑。

旧版工具曾在新连接默认 `search_path=public` 时，于结构检查报 `UndefinedTable`。修复版在任何模块查询前明确选择业务schema，并补测了真实的默认入口。若此次失败留下的私有目录完全为空、没有执行回执或备份，且现场仍是完整合法v5/open、没有任何部分v6关系，才可在原固定提交的执行命令末尾加 `--resolve-preflight`。工具会在独占锁内核验并将旧标记归档到原私有目录，保留证据后再开始新的备份升级；不删除标记、不修改数据库来消除报错。只要已有回执/文件、处于维护、存在部分v6或已提交v6，就会拒绝归档，需按原回执人工复核。

- 仅当证明账本仍是原v5、旧数据/权限/序列完全一致、没有部分v6关系且control身份未变时，工具会恢复原open与timer，记录 `recovered-v5`。
- 已提交v6后，后置验证、重启或timer恢复失败时不会恢复整库、DROP表或伪装回到v5；保留回执与剩余维护状态，按实际版本人工排查。核对 `mode` 后再操作，不能假定任何失败都已自动恢复open。
- 成功后回退界面仍须保留v6兼容模块和全部原迁移哈希，详见 `publish-review-implementation.md`。不撤销其他窗口的新提交。

## 验证边界

可通过独立、受限的真实本机PostgreSQL演练复现：

```powershell
python -B probe_postgresql.py --root .synthetic-tests/review-upgrade-new --bin <已核对的本机PostgreSQL二进制目录> --publish-review-upgrade-checks
```

该运行器只准许新建标记回环库及固定pg_dump/pg_restore，不改变原应用测试的禁止子进程规则。覆盖备份失败、DDL事务回滚、外部数据漂移、成功v5→v6、完整备份恢复到第二个隔离库、重复v6，以及同步进程和timer状态。Systemd动作在Windows测试中模拟；Linux私有文件权限及实际服务重启只能在现场核验，不能把本机演练当成生产成功。
