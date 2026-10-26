尚无记录。
2026-10-26 未登录 GET /api/board-updates 返回 401：点名测试 test_board_updates.BoardUpdatesTest.test_updates_require_login 通过。Verification 退出码 1。阻塞：同一失败连续两次，没有新的测试输出。其余 7 个验证测试都在 app.initialize() 失败，系统临时目录里的库不在合成测试根，报数据库不存在、禁止自动创建或导入种子。本条不能改这些旧测试的夹具，也不能放宽初始化去绕过。停止。
2026-10-26 未登录 GET /api/board-updates 返回 401：点名测试 test_board_updates.BoardUpdatesTest.test_updates_require_login 通过。Verification 退出码 0。
2026-10-26 50 个已登录等待在版本不变时停留且停留期间不再调用 app.connect()：点名测试 test_board_updates.BoardUpdatesTest.test_unchanged_wait_does_not_open_database 通过。Verification 退出码 0。
2026-10-26 保存账号备注后等待者只收到这个账号及其新版本，且发送调用了 board_updates.recipients：点名测试 test_board_updates.BoardUpdatesTest.test_save_delivers_only_changed_account 通过。Verification 退出码 0。
2026-10-26 只有办公室 A 授权的员工收不到只属于办公室 B 的账号，总管理员收得到：点名测试 test_board_updates.BoardUpdatesTest.test_other_office_is_omitted 通过。Verification 退出码 0。
2026-10-26 账号从办公室 A 改到 B 时 A 得到移除、B 得到这条账号，保存发布路径调用了 notify_office_move；没有办公室信息的备注保存仍只产生一条更新：点名测试 test_board_updates.BoardUpdatesTest.test_office_move_notifies_remove_and_add 通过。Verification 退出码 0。
2026-10-26 账号保存和登记已发的成功响应包含这条新资料；冲突仍返回 409，后一次不写入：点名测试 test_board_updates.BoardUpdatesTest.test_save_response_includes_changed_record 通过。Verification 退出码 0。
2026-10-26 恢复成功后等待者收到整份重载通知且通知里没有账号列表；当前用户仍保留，过期预览仍失败：点名测试 test_board_updates.BoardUpdatesTest.test_restore_tells_waiters_to_reload 通过。Verification 退出码 0。
2026-10-26 导入确认成功后等待者收到整份重载通知且通知里没有账号列表；预览不改原资料，无权限仍拒绝：点名测试 test_board_updates.BoardUpdatesTest.test_import_apply_tells_waiters_to_reload 通过。Verification 退出码 0。
2026-10-26 导出名额被占用时后来的导出在释放前不返回；名额空闲时导出结果与原来一致：点名测试 test_board_updates.BoardUpdatesTest.test_export_waits_while_heavy_slot_held 通过。Verification 退出码 0。
2026-10-26 同一个名额被占用时导入确认和养号截图上传在释放前都不返回，单条账号保存不被堵住：点名测试 test_board_updates.BoardUpdatesTest.test_import_and_screenshot_share_export_slot 通过。Verification 退出码 0。
2026-10-26 `/api/board-updates` 返回 404 时看板才每 5 秒调用 load()，接口可用时不注册这个定时器：点名测试 test_board_updates.BoardUpdatesTest.test_interval_reload_is_only_the_fallback 通过。Verification 退出码 0。
2026-10-26 编辑框保存成功和登记已发成功后不再调用 load()，改用响应里的这一条更新页面：点名测试 test_board_updates.BoardUpdatesTest.test_editor_and_daily_do_not_full_reload 通过。Verification 退出码 0。
2026-10-26 远程更新不修改 capturedVersions，也不调用 applyVersionDelta 把别人的版本写进打开的编辑框：点名测试 test_board_updates.BoardUpdatesTest.test_remote_update_leaves_editor_version 通过。Verification 退出码 0。
2026-10-26 采集未启用时工作台不再每 5 秒请求采集进度：点名测试 test_board_updates.BoardUpdatesTest.test_collection_poll_stops_while_disabled 通过。Verification 退出码 0。
COMPLETE
