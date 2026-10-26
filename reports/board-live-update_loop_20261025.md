# 看板按条更新：执行提示

每次只执行本文件。先读 `reports/board-live-update_TODO.md` 和 `reports/board-live-update_LOOP_LOG.md` 的末尾。计划在 `reports/board-live-update_plan_20261025.md`，只读当前这一条对应的那一节。不要读其他进度文件。

## Verification

Verify: `python -B -m unittest test_board_updates.BoardUpdatesTest test_board.BoardTest.test_persistence_history_move_and_validation test_board.BoardTest.test_concurrent_identity_edits test_batch_edit.BatchEditTest.test_stale_token_rejects_entire_group test_workspace.WorkspaceTest.test_restore_preserves_current_users_and_requires_fresh_preview test_workspace.WorkspaceTest.test_import_preview_preserves_and_explicit_apply test_workspace.WorkspaceTest.test_import_rejects_stale_rows_duplicates_and_permission test_workspace.WorkspaceTest.test_export_csv_and_xlsx_roundtrip`

没有上面这一行反引号命令，就停止，不要开始修代码。执行时原样运行，不要改写命令，不要改这一行。

## 本轮

1. 取 TODO 里最上面一条未勾选的事项，只做这一条。
2. 先在代码里搜索，确认这件事还没做。不要假设它不存在。
3. 先写这一条点名的测试，不改生产代码。运行这个测试，确认它因为行为还没实现而失败。失败若是导入错误、夹具写错或拼写错误，先修测试，不能开始实现。
4. 再改这一条允许的生产代码，让该测试通过。实现时不要改测试。TODO 明确点名要改写的旧断言除外。
5. 再跑一遍该测试，确认通过。然后原样运行 Verification 的命令。失败就读输出，只修这一条的原因，再跑。命令没通过，不能勾选，不能做下一条。
6. 两条都通过后，把这一条改成已勾选。在日志末尾追加一行：日期、条目、点名测试的结果、Verify 的退出码。
7. 本轮停止。还有未勾选条目时，不要开始下一条。

同一条的同一种失败连续出现两次，并且没有新的测试输出，就在日志写明阻塞原因，然后停止。不要换一种做法绕过。

## 完成

TODO 全部勾选，并且 Verification 命令退出码为 0 时，在日志写 `COMPLETE`，然后停止。

## 规则

- 一次只做一条。验证没通过就留在这一条。
- 不要把测试改松、跳过或删掉来换通过。待办点名要改写的旧断言必须保留待办里写明的检查。
- 在项目根目录 `F:\AI\FZ\DouyinAccountBoard` 执行。测试库只用临时目录，禁止使用 `data/board.db`、客户种子、真实备份、浏览器登录态和云数据库。
- 禁止开启采集，禁止启动浏览器或 F2 采集器。禁止部署、购买云资源、上传客户数据库、修改腾讯云。
- 禁止把《第一版上线实施计划》或《当前服务器多人同时使用计划》的验收标成完成。禁止修改 `desktop/resources`。禁止把分发版桌面壳改成打开服务器网址。
- 禁止打印密码、密码哈希、令牌、Cookie 和数据库连接串。
- 禁止运行整包测试、`pg_http_checks.py`、`pg_scope_read_checks.py` 和 `.synthetic-tests` 探针。
- 禁止 git 提交和推送。不要回退工作区里与本条无关的已有修改。
- 不改本文件的 Verification 行，不改计划文件。
