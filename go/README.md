# 抖音账号工作台 Go 版

这是 Go 切换候选。仓库现有的自动同步只会重启当前生产服务。正式切换前需要核对生产 PostgreSQL 连接与迁移记录，准备可验证的备份、Linux 构建和服务回退，并使用原网站地址完成验收。试点库仅用于测试。

Go 程序内置原网页，直接读写 PostgreSQL。默认启动方式只运行 Go 和 PostgreSQL，无需 Python 或 Node.js 服务。浏览器收到 WebSocket 更新提示后，按登录会话、办公室授权和记录版本读取增量。断线时使用 HTTP 同步。

项目基于原版提交 `2da2e81` 的数据库结构和网页。迁移已有工作台时，启动 Go 服务并连接原 PostgreSQL 库即可沿用已有的账号、办公室、资料和操作记录。程序启动时读取当前写入代次。

员工办卡和今日发布核对分别使用已安装的 0005 和 0006 结构。空库初始化包含这两项，已有库按原迁移记录开放对应功能。

## 本机启动

在本目录使用 Go 1.26 或更新版本。先准备独立的本机 PostgreSQL 测试库，在当前终端的 `BOARD_GO_PG_DSN` 环境变量中设置连接配置。程序只接受回环地址。

```powershell
go build -mod=readonly -o .local/board-go.exe ./cmd/board-go
& ./.local/board-go.exe -listen 127.0.0.1:8080
```

打开 [本机工作台](http://127.0.0.1:8080/)，用本机测试库中已初始化的用户登录。[健康检查](http://127.0.0.1:8080/api/go/health) 的 `standalone` 字段应为 `true`。

## 首次安装

先在本机 PostgreSQL 创建空数据库，将 `BOARD_GO_PG_DSN` 指向它。用下列命令应用原版 0001 至 0007 结构。该命令会拒绝已有工作台结构或资料的数据库。

```powershell
& ./.local/board-go.exe -init-db
$env:BOARD_GO_SETUP_KEY = Read-Host -MaskInput '输入临时首次设置密钥'
& ./.local/board-go.exe -listen 127.0.0.1:8080
```

临时密钥请用 24 至 256 位随机字符。打开本机登录页，输入同一密钥并创建总管理员。授权有效期为 30 分钟，创建成功后即失效。以后按正常方式启动和登录。

## 业务入口

- 看板、账号总览、实际发布、历史、账号编辑及批量修改。原有权限、记录版本检查和请求回执在 Go 内处理。
- 手机卡和手机资料的新增、修改、登录手机关系、从账号补录手机卡。实物状态、SIM 卡移放、换机、重新编号、故障处理及账号恢复走原有权限和记录版本规则。
- 发布格关联、取消、移动和撤销位置，继续校验来源批次、版本和办公室。
- 通用 CSV、Excel 导入和导出。先预览并确认选择的行，旧记录的空白列保留原值。办公室三旧版双表新增、归属确认和仅补名，以及已安装 0007 结构的新表格同步预览、确认、回执与回退，都由 Go 处理。旧版未确认的预览需重新生成。
- 登录、改密码和登录名、人员和办公室授权、操作历史和养号流程，包括截图。
- 员工办卡的实名卡台账、运营商和规则、核验与申请、卡关联和释放。
- 今日发布核对的目标、电脑、截止时间和作品样本差值。原发布勾选记录独立保存。
- 作品采集入口在「日常工作」，也可从账号详情或按筛选结果提交。采集默认关闭，队列历史仍可查看。

## 本机手动作品采集

仅独立 Go 服务、回环监听且无公网代理配置时，才可用 `BOARD_GO_COLLECTION_TOKEN` 启用手动采集。令牌使用 32 至 256 位随机可见 ASCII 字符，放在进程环境中。原项目的 `f2_worker.py` 是访问抖音的可选进程。沿用其独立浏览器和 F2 环境，设置 `DOUYIN_BOARD_COLLECTION_ENABLED=1`，`DOUYIN_BOARD_WORKER_TOKEN` 使用同一令牌，`DOUYIN_BOARD_WORKER_ORIGIN` 指向 `http://127.0.0.1:<端口>`。

采集器在线后，如队列显示暂停，由有全局采集权限的人员在「作品采集 → 队列设置」继续队列。再从账号详情或筛选结果提交，队列会显示待处理、成功、失败或需验证。已有成功数量在后续失败时保留。测试库可以用模拟采集器核对队列与回填，连接真实抖音需要可用的独立采集会话。

## 备份和恢复

总管理员可以在「备份与恢复」创建 PostgreSQL 业务快照、检查内容并预览恢复。备份前后均校验文件，恢复前保存当前业务快照。恢复业务资料和归属时，当前登录人员、授权、审计与操作历史继续保留。恢复成功后重启 Go 服务，服务会接入新的写入代次。

备份以 `.boardpg.gz` 保存。可以通过 `BOARD_GO_BACKUP_DIR` 指定私有目录，默认按本机数据库实例分开保存在当前用户配置目录。启动时加 `-auto-backup` 可开启自动快照，每四小时备份一次，自动快照保留 30 天。原 SQLite `.db` 和 PostgreSQL 运维 `.dump` 继续由各自原有工具处理。

## 验证

```powershell
go test -mod=readonly ./...
go vet -mod=readonly ./...
```

真实 PostgreSQL 的测试需要一份独立合成库。初始化条件见 [基础夹具](internal/daily/testdata/fixture.sql)。设置测试包所需的合成库端口后，使用 `go test -p 1 -count=1 -mod=readonly ./...` 串行执行共用夹具的测试。

完整网页验收需要一份全新的测试库，可用 [夹具创建脚本](testdata/New-StandaloneFixture.ps1) 从已初始化的基础合成库创建。先在当前终端设置测试用户密码 `SYNTHETIC_FIXTURE_PASSWORD`，传入本机 `psql`、端口、合成模板库名以及以 `synthetic_go_standalone_` 开头的新库名。将 `BOARD_GO_PG_DSN` 指向刚创建的合成库，安装 Playwright 并提供 Chrome，运行以下命令。

```powershell
$env:TEST_OFFICE_THREE = '1'
$env:TEST_TABLE_SYNC = '1'
node ./testdata/standalone-smoke.cjs
```

脚本启动新的 Go 进程，验证会话、账号协作、手机与卡、发布格和导入，然后关闭进程。回退测试入口时，可重新使用原工作区的 Python 程序。

空库初始化和首次管理员浏览器验证使用 `testdata/bootstrap-smoke.cjs`，目标必须是名称以 `synthetic_go_bootstrap_` 开头的全新本机空库。

全流程验证使用 `testdata/complete-migration-smoke.cjs`。将 `BOARD_GO_PG_DSN` 指向名称以 `synthetic_go_bootstrap_complete_` 开头的全新本机空库，然后运行 `node ./testdata/complete-migration-smoke.cjs`。脚本在项目的 `.local` 目录创建独立备份目录，验证员工办卡、发布核对、备份恢复、服务重启和 WebSocket。
