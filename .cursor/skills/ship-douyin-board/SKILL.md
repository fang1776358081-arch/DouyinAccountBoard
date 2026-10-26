---
name: ship-douyin-board
description: Ships Douyin Account Board changes on the two release paths. Use when the user asks to push to GitHub, update the server, publish or package the desktop shell, release an installer, or roll an update out to employees.
---

# 发布抖音账号工作台

先看改动落在哪一类，只做对应的路径。两条路径都要用户明确要求发布后才执行。

- 网页、接口、服务器 Python、`static/`：只走「推送网站」。
- `desktop-src/` 里的程序、图标、安装配置：走「发布桌面壳」。这条路径包含推送源码。
- 只改了网页时，不发桌面壳 Release。

## 推送网站

员工的桌面壳打开的是 http://42.192.45.194 ，所以网站更新后，他们刷新或重新打开页面就能看到。已经安装的桌面壳程序不会因为这次推送而更换。

1. 确认没有把 `data/board.db`、`logs/`、`desktop/`、`.env`、密码、令牌、连接串放进提交。LICENSE 保持不入库。
2. 提交并推送到 `fang1776358081-arch/DouyinAccountBoard` 的 `main`。不要改 git config，不要打印令牌，不要强推。本机默认凭据可能是另一个 GitHub 账号；用 `gh auth token --user fang1776358081-arch` 做一次性 askpass，并执行 `git -c credential.helper= push https://fang1776358081-arch@github.com/fang1776358081-arch/DouyinAccountBoard.git HEAD:main`。
3. 用 `git ls-remote` 核对远端 `main` 已是这次提交。
4. 服务器上的定时任务约每 2 分钟执行 `/usr/local/sbin/douyin-board-sync`：拉取远端；本地有未提交改动，或已经等于 `origin/main`，就跳过；否则快进合并并重启 `douyin-board.service`。服务器访问 GitHub 失败时会跳过，旧版本继续运行。
5. 等到一次同步周期后，直接请求 http://42.192.45.194 上这次改动的页面或接口，确认新代码已经在运行。看不到新代码时，说明服务器还没同步成功，不能告诉用户已经更新。

## 发布桌面壳

只在桌面壳程序本身有变化时做。网站改动不会进入这一步。

1. 先完成「推送网站」，让 `desktop-src` 的源码在 `main` 上。
2. 把 `desktop-src/package.json` 的版本提高一位。已安装的壳只认高于当前版本的 `vX.Y.Z`。
3. 在 `desktop-src` 执行 `npm run dist`。
4. 核对产物：
   - `DouyinAccountBoard-Setup-<版本>.exe`
   - 同名 `.exe.blockmap`
   - `latest.yml`，其中的版本和安装包文件名与上面一致
   - `DouyinAccountBoard-Portable-<版本>.zip`
   - 压缩包内的 `resources/app/main.cjs` 仍打开 http://42.192.45.194
5. 发布 GitHub Release，标签为 `v<版本>`，目标为 `main`。同一条 Release 必须带上安装包、blockmap、`latest.yml` 和便携版压缩包。不要上传 `builder-debug.yml`。
6. 这条 Release 必须是仓库当前最新的一条。之后不要再发一条不带 `latest.yml` 的更新 Release，否则已安装的壳会找不到更新。
7. 用 `gh release view` 确认四个文件都在。不要在维护这台电脑上擅自运行安装包。

已安装 0.3.0 及以上版本的员工会在启动后检查更新，下载完成后询问是否重启。0.3.0 及以上的便携版会下载新的压缩包，确认重启后换上。标签不是 `vX.Y.Z` 的旧包没有这个能力，需要先手工换成带更新功能的版本。
