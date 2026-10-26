# 看板按条更新 TODO

- [x] 未登录请求 `GET /api/board-updates` 返回 401，响应里没有账号、电脑或养号。已登录的 `/api/board` 仍可返回 200。点名测试：`test_board_updates.BoardUpdatesTest.test_updates_require_login`。允许修改：`test_board_updates.py`、`app.py`、`board_updates.py`。
- [x] 50 个已登录等待在版本不变时停留，停留期间不再调用 `app.connect()`，响应里没有账号列表。点名测试：`test_board_updates.BoardUpdatesTest.test_unchanged_wait_does_not_open_database`。允许修改：`test_board_updates.py`、`app.py`、`board_updates.py`。
- [x] 一个等待者在另一请求成功保存账号备注后，只收到这个账号及其新版本，不收到整份电脑、手机卡或养号；这次发送调用了 `board_updates.recipients`。点名测试：`test_board_updates.BoardUpdatesTest.test_save_delivers_only_changed_account`。允许修改：`test_board_updates.py`、`app.py`、`board_updates.py`。
- [x] 只有办公室 A 授权的员工收不到只属于办公室 B 的账号；总管理员收得到。点名测试：`test_board_updates.BoardUpdatesTest.test_other_office_is_omitted`。允许修改：`test_board_updates.py`、`board_updates.py`。
- [x] 账号从办公室 A 改到 B 时，A 得到移除，B 得到这条账号；保存发布路径会调用这个事件函数。没有办公室信息的备注保存仍只产生一条更新。点名测试：`test_board_updates.BoardUpdatesTest.test_office_move_notifies_remove_and_add`。允许修改：`test_board_updates.py`、`app.py`、`board_updates.py`。
- [x] 账号保存和登记已发的成功响应包含这条新资料，供页面直接更新。冲突时仍返回 409，且后一次不写入。点名测试：`test_board_updates.BoardUpdatesTest.test_save_response_includes_changed_record`。允许修改：`test_board_updates.py`、`app.py`、`account_service.py`。
- [x] 恢复成功后，等待者收到整份重载通知，通知里没有账号列表。当前用户仍保留，过期预览仍失败。点名测试：`test_board_updates.BoardUpdatesTest.test_restore_tells_waiters_to_reload`。允许修改：`test_board_updates.py`、`app.py`、`board_updates.py`、`workspace_service.py`。
- [x] 导入确认成功后，等待者收到整份重载通知，通知里没有账号列表。导入原有的预览和权限拒绝保持不变。点名测试：`test_board_updates.BoardUpdatesTest.test_import_apply_tells_waiters_to_reload`。允许修改：`test_board_updates.py`、`app.py`、`board_updates.py`、`workspace_service.py`。
- [x] 导出名额被占用时，后来的导出请求在名额释放前不返回；名额空闲时导出结果与原来一致。点名测试：`test_board_updates.BoardUpdatesTest.test_export_waits_while_heavy_slot_held`。允许修改：`test_board_updates.py`、`heavy_work.py`、`workspace_service.py`、`data_exchange.py`。
- [x] 同一个名额被占用时，导入确认和养号截图上传在释放前都不返回。单条账号保存不被这个名额堵住。点名测试：`test_board_updates.BoardUpdatesTest.test_import_and_screenshot_share_export_slot`。允许修改：`test_board_updates.py`、`heavy_work.py`、`workspace_service.py`、`nurture.py`、`app.py`。
- [x] `/api/board-updates` 返回 404 时，看板才每 5 秒调用 `load()`；接口可用时不注册这个定时器。点名测试：`test_board_updates.BoardUpdatesTest.test_interval_reload_is_only_the_fallback`。允许修改：`test_board_updates.py`、`static/app.js`、`static/board-updates.js`。
- [x] 编辑框保存成功和登记已发成功后不再调用 `load()`，改用响应里的这一条更新页面。点名测试：`test_board_updates.BoardUpdatesTest.test_editor_and_daily_do_not_full_reload`。允许修改：`test_board_updates.py`、`static/app.js`、`static/board-updates.js`。
- [x] 远程更新不修改 `capturedVersions`，也不调用 `applyVersionDelta` 把别人的版本写进打开的编辑框。点名测试：`test_board_updates.BoardUpdatesTest.test_remote_update_leaves_editor_version`。允许修改：`test_board_updates.py`、`static/app.js`、`static/board-updates.js`。
- [x] 采集未启用时，工作台不再每 5 秒请求采集进度。点名测试：`test_board_updates.BoardUpdatesTest.test_collection_poll_stops_while_disabled`。允许修改：`test_board_updates.py`、`static/collection-panel.js`。
