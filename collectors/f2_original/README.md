# 原始 F2 采集核心

`douyin_counts.py`与`requirements.txt`按用户找回的原始ZIP逐字节保留，来源与SHA256见`ORIGIN.json`。本目录不含用户账号列表、历史数量、主页索引、浏览器会话或凭据。目录级`.gitattributes`防止Git自动转换原文件换行。

工作台在已有确定主页时使用此脚本并校验 SHA256。取数在后台执行，登录窗口由操作人员主动打开。Python、专用浏览器目录、浏览器与输出目录继续使用现有运行配置。

可通过`DOUYIN_BOARD_F2_PROFILE_INDEX`配置私有JSON文件：

```json
{"version":1,"profiles":[{"account":"完整抖音号原字符串","url":"https://www.douyin.com/user/MS4wSyntheticConfirmed"}]}
```

索引仅存公共账号标识与已确认主页，放在私有运行目录。启动时最多读取 8 MiB、20000 条记录，匹配保留抖音号的原字符串和前导零。合法的任务主页优先，同号对应多个历史主页时标为待核对。更新索引后重启采集器即可加载。

管理员可将采集器的 `DOUYIN_BOARD_F2_HANDLE_SEARCH_SCRIPT` 设为已安装且支持完整抖音号的 `douyin-f2-counts` 脚本绝对路径。这样，只有完整抖音号且索引无记录的任务可以调用该技能一次。技能须支持 `query` 并校验搜索结果和主页返回的身份。安全验证会暂停按号搜索，确认过主页的任务继续取数。专用会话登录失效时队列暂停。留空使用主页模式。

真实取数使用本人正常登录的专用浏览器。采集器再次核对完整抖音号、合法主页和作品数，收到服务器确认后记录成功回执。

`prepare_collection_profiles.py` 将已核验主页报告和旧私有索引合并到单独的输出文件。参数包括 `--accounts`、`--verified-report`、`--base-index` 和 `--output`，也可通过 `--checkpoint` 带上续采清单里已核验的主页。仅用完整抖音号匹配，重复或冲突链接另行统计。将输出文件配置为采集器的 `DOUYIN_BOARD_F2_PROFILE_INDEX` 后重启采集器即可加载。

### 本机按抖音号批量取数

`collect_counts_batch.py` 接收完整抖音号列表。使用已安装 F2 的 Python 运行，指定 `--input`、`--output` 和 `--profile`。可用 `--profile-index` 指向现有已核验主页索引，用 `--search-skill` 指向本机支持按号搜索的 F2 脚本。

同一个输出目录对应一份账号清单。每次运行更新 `checkpoint.json` 与 `作品采集结果.csv`，下一次使用同一份清单和目录时从未成功的账号继续。`--seed-report` 可以导入本批次此前已实测的报告，采样时间按原记录保留。`--prepare-only` 只整理已有记录。这一入口只在本机出结果，不向工作台写入数据。
