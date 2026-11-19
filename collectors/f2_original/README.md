# 原始 F2 采集核心

`douyin_counts.py`与`requirements.txt`按用户找回的原始ZIP逐字节保留，来源与SHA256见`ORIGIN.json`。本目录不含用户账号列表、历史数量、主页索引、浏览器会话或凭据。目录级`.gitattributes`防止Git自动转换原文件换行。

工作台worker固定使用此脚本并校验SHA256，不再使用旧`DOUYIN_BOARD_F2_SCRIPT`指向的安装扩展版。普通query仅接收已确认的MS4w主页，保持原包无界面取数；`login`仍须用户主动打开。没有抖音号搜索、可见query、交互观察或上游重抓分支。Python、专用profile、浏览器与output仍沿用现有运行配置。

可通过`DOUYIN_BOARD_F2_PROFILE_INDEX`配置私有JSON文件：

```json
{"version":1,"profiles":[{"account":"完整抖音号原字符串","url":"https://www.douyin.com/user/MS4wSyntheticConfirmed"}]}
```

索引仅存公共账号标识与已确认canonical URL，放在私有运行目录，不得提交仓库或放在网页资源目录。启动时最多读取8MiB、20000条并缓存；缺配置或空profiles正常。完整号不去空格、不转数字，前导0保留。同号同URL去重，多URL冲突不猜。显式合法任务主页优先；否则仅使用索引，缺失或冲突标为待补主页，不启动子进程、不增加连续采集失败数、不暂停队列。索引更新后需重启worker以加载；不需要数据库迁移。

真实取数仍需本人正常登录的专用profile；失败不能写成0。只有原脚本返回身份匹配的有效数量，且Go确认接收，才形成成功delivery回执。历史映射与旧数量都不能代替新查询结果。
