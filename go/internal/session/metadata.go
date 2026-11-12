package session

import (
	"database/sql"
	"encoding/json"
	"errors"
	"net/http"
	"slices"
	"time"
)

var roles = map[string]string{
	"owner": "总管理员", "admin": "管理员", "employee": "员工", "viewer": "只读人员",
}

var permissionOrder = []string{
	"nurture_manage", "history_view", "data_import", "data_export", "publishing_manage",
	"publish", "account_status", "account_progress", "account_create", "account_manage",
	"card_tasks", "card_create", "card_manage", "card_import", "device_create",
	"device_manage", "collection_manage",
}

var permissionNames = map[string]string{
	"nurture_manage":    "管理养号进度及截图",
	"history_view":      "查看业务操作历史",
	"data_import":       "预览及确认资料导入",
	"data_export":       "导出资料与模板",
	"publishing_manage": "管理发布格子关联及每日目标",
	"publish":           "记录当天发布",
	"account_status":    "修改账号状态",
	"account_progress":  "修改注册进度、稳定及改密记录",
	"account_create":    "新增抖音账号 / 小号",
	"account_manage":    "修改账号基础资料",
	"card_tasks":        "处理手机卡状态、接码和归还",
	"card_create":       "新增手机卡",
	"card_manage":       "修改手机卡基础资料",
	"card_import":       "从已有账号补录手机卡",
	"device_create":     "新增手机设备",
	"device_manage":     "修改手机设备及账号关联",
	"collection_manage": "操作作品采集及采集设置",
}

var permissionDetails = map[string]string{
	"nurture_manage":    "加入待检查、记录核对情况/负责人/备注、暂停养号及管理截图；开始、继续和结束养号还需修改账号状态权限。",
	"history_view":      "查看业务资料修改人、时间及字段前后值；不展示登录密码。",
	"data_import":       "上传Excel/CSV并预览差异，确认所选行；实际写入仍需要对应新增、修改及字段权限。",
	"data_export":       "导出筛选资料及导入模板，不导出人员密码和登录会话。",
	"publishing_manage": "关联待匹配发布格子，调整或取消手动关联；设置当日或未来任务目标、任务电脑及截止时间，需填写依据；不能直接改历史目标，不修改账号基础资料、原表文字和历史发布记录。",
	"publish":           "勾选或取消当天是否发布，不代表自动操作抖音发布。",
	"account_status":    "修改正常、封禁、养号7天等状态，支持批量修改。",
	"account_progress":  "修改小号注册进度、账号稳定和改密记录。",
	"account_create":    "录入新的抖音账号资料；不包含修改已有账号。初始状态/手机关联还需对应权限。",
	"account_manage":    "修改已有账号昵称、抖音号、手机号、注册日期、大小号关系、备注和资料电脑；状态、进度及手机关联另行授权。",
	"card_tasks":        "记录卡状态、待注销、接码事项、归还状态及日期；不改卡主和号码。",
	"card_create":       "新增一张手机卡并填写初始资料；不包含修改已有卡。",
	"card_manage":       "修改已有卡的号码、卡主、归属、位置、联系人和备注；卡状态与待办另行授权。",
	"card_import":       "根据工作台已有账号手机号补录缺少的卡，不包含任意文件导入。",
	"device_create":     "新增手机设备库存记录；不包含更改已有设备或移动账号。",
	"device_manage":     "修改设备编号、状态、负责人和位置，关联、移入或移出账号登录手机。",
	"collection_manage": "提交作品数量采集任务、更改自动采集设置，以及在本机桌面程序打开采集浏览器。",
}

var permissionGroups = [][2]any{
	{"养号管理", []string{"nurture_manage"}},
	{"发布与账号", []string{"publish", "publishing_manage", "account_status", "account_progress", "account_create", "account_manage"}},
	{"手机卡", []string{"card_tasks", "card_create", "card_manage", "card_import"}},
	{"手机设备", []string{"device_create", "device_manage"}},
	{"作品采集", []string{"collection_manage"}},
	{"数据管理", []string{"history_view", "data_import", "data_export"}},
}

var defaults = map[string][]string{
	"admin":    permissionOrder,
	"employee": {"publish", "account_status", "account_progress", "card_tasks"},
	"viewer":   {},
}

// PublicPermissions decodes stored permission names for a personnel record.
// The owner retains every defined permission, as in the session response.
func PublicPermissions(role, stored string) []string { return permissionsForRole(role, stored) }

func DefaultPermissions(role string) ([]string, bool) {
	permissions, ok := defaults[role]
	return slices.Clone(permissions), ok
}

func ValidPermission(key string) bool {
	_, found := permissionNames[key]
	return found
}

type Handler struct {
	Store             Store
	Origin            string
	WriterEpoch       int64
	RestoreGeneration string
	SetupKey          string
}

func (h Handler) Me(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.Header().Set("Allow", http.MethodGet)
		replyError(w, http.StatusMethodNotAllowed, "请求方法不正确")
		return
	}
	ctx := r.Context()
	tx, err := h.Store.DB.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelRepeatableRead, ReadOnly: true})
	if err != nil {
		replyError(w, http.StatusServiceUnavailable, "登录服务暂时不可用")
		return
	}
	defer tx.Rollback()

	user, err := AuthenticateWith(ctx, tx, r)
	var current *User
	if err == nil {
		current = &user
	} else if !errors.Is(err, ErrUnauthorized) {
		replyError(w, http.StatusServiceUnavailable, "登录服务暂时不可用")
		return
	}
	var initialized, canSetup bool
	err = tx.QueryRowContext(ctx, "SELECT EXISTS(SELECT 1 FROM auth_users WHERE role = 'owner')").Scan(&initialized)
	if err == nil && !initialized && localSetupRequest(r) {
		setup := requestCookie(r, "board_setup")
		if setup != "" {
			err = tx.QueryRowContext(ctx, `
SELECT EXISTS(SELECT 1 FROM auth_setup WHERE token_hash = $1 AND expires_at > $2)
`, digest(setup), time.Now().Unix()).Scan(&canSetup)
		}
	}
	if err != nil || tx.Commit() != nil {
		replyError(w, http.StatusServiceUnavailable, "登录服务暂时不可用")
		return
	}
	replyJSON(w, http.StatusOK, map[string]any{
		"user": current, "initialized": initialized, "can_setup": canSetup, "permissions": permissionNames,
		"roles": roles, "defaults": defaults, "permission_groups": permissionGroups, "permission_details": permissionDetails,
		"setup_key_required": !initialized && !canSetup && h.SetupKey != "" && localSetupRequest(r),
	})
}

func replyError(w http.ResponseWriter, status int, message string) {
	replyJSON(w, status, map[string]string{"error": message})
}

func replyJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
