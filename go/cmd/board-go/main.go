package main

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"douyinboard/internal/account"
	"douyinboard/internal/accountwrite"
	"douyinboard/internal/admin"
	"douyinboard/internal/backuprestore"
	"douyinboard/internal/board"
	"douyinboard/internal/bootstrap"
	"douyinboard/internal/collection"
	"douyinboard/internal/daily"
	"douyinboard/internal/employeecards"
	"douyinboard/internal/exchange"
	"douyinboard/internal/gateway"
	"douyinboard/internal/inventory"
	"douyinboard/internal/publishing"
	"douyinboard/internal/publishreview"
	"douyinboard/internal/session"
	"douyinboard/internal/tablesync"
	"douyinboard/internal/updates"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/stdlib"
)

var boardBuild = "go-local"

func main() {
	if err := run(); err != nil {
		slog.Error("工作台启动或运行失败", "reason", err.Error())
		os.Exit(1)
	}
}

func run() error {
	listen := flag.String("listen", "127.0.0.1:8080", "Go server listen address")
	legacy := flag.String("legacy", "", "Optional legacy application origin on loopback")
	public := flag.String("origin", "", "Browser origin, required for non-loopback listeners and TLS termination")
	initDatabase := flag.Bool("init-db", false, "Initialize an empty local PostgreSQL database and exit")
	autoBackup := flag.Bool("auto-backup", false, "Enable scheduled PostgreSQL business snapshots")
	showVersion := flag.Bool("version", false, "Print the Go workbench build ID and exit")
	flag.Parse()
	if *showVersion {
		fmt.Println(boardBuild)
		return nil
	}

	var legacyOrigin *url.URL
	var err error
	if *legacy != "" {
		legacyOrigin, err = parseOrigin(*legacy)
		if err != nil || !isLoopback(legacyOrigin.Hostname()) {
			return errors.New("legacy 必须是本机 HTTP 或 HTTPS 来源")
		}
	}
	listenHost, _, err := net.SplitHostPort(*listen)
	if err != nil {
		return errors.New("监听地址不正确")
	}
	if *public != "" {
		if _, err := parseOrigin(*public); err != nil {
			return errors.New("浏览器来源不正确")
		}
	} else if !isLoopback(listenHost) {
		return errors.New("非本机监听需要明确配置浏览器来源")
	}

	lifecycle, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	var db *sql.DB
	if dsn := os.Getenv("BOARD_GO_PG_DSN"); dsn != "" {
		db, err = openDatabase(lifecycle, dsn)
		if err != nil {
			return err
		}
		defer db.Close()
	}
	if db == nil && legacyOrigin == nil {
		return errors.New("独立运行需要设置 BOARD_GO_PG_DSN 指向本机 PostgreSQL")
	}
	if *initDatabase {
		if db == nil || legacyOrigin != nil {
			return errors.New("空库初始化需要单独配置本机 PostgreSQL")
		}
		ctx, cancel := context.WithTimeout(lifecycle, time.Minute)
		defer cancel()
		if err := bootstrap.Initialize(ctx, db); err != nil {
			return err
		}
		slog.Info("工作台空库初始化完成")
		return nil
	}

	setupKey := os.Getenv("BOARD_GO_SETUP_KEY")
	if setupKey != "" && (!isLoopback(listenHost) || *public != "" || legacyOrigin != nil || len(setupKey) < 24 || len(setupKey) > 256) {
		return errors.New("首次设置密钥需要 24 至 256 位，仅在本机独立监听时启用")
	}
	collectionToken := os.Getenv("BOARD_GO_COLLECTION_TOKEN")
	if collectionToken != "" && (!isLoopback(listenHost) || *public != "" || legacyOrigin != nil || !collection.ValidToken(collectionToken)) {
		return errors.New("作品采集需要 32 至 256 位可见 ASCII 令牌，仅在本机独立监听时启用")
	}

	gatewayHandler := gateway.New(gateway.Config{LegacyOrigin: legacyOrigin, PublicOrigin: *public, Database: db})
	mux := http.NewServeMux()
	mux.Handle("/", gatewayHandler)
	if db != nil {
		var fallback http.Handler
		if legacyOrigin != nil {
			fallback = gatewayHandler
		}
		cleanup, err := mountPostgres(lifecycle, mux, db, *public, setupKey, collectionToken, *autoBackup, fallback)
		if err != nil {
			return err
		}
		defer cleanup()
	}
	server := &http.Server{
		Addr: *listen,
		Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("X-Board-Build", boardBuild)
			mux.ServeHTTP(w, r)
		}),
		ReadHeaderTimeout: 5 * time.Second,
		IdleTimeout:       90 * time.Second,
		MaxHeaderBytes:    32 << 10,
		BaseContext:       func(net.Listener) context.Context { return lifecycle },
	}
	listener, err := net.Listen("tcp", *listen)
	if err != nil {
		return fmt.Errorf("无法监听本机地址: %w", err)
	}
	slog.Info("Go 工作台已监听", "address", listener.Addr().String(), "postgres", db != nil)
	done := make(chan error, 1)
	go func() { done <- server.Serve(listener) }()
	select {
	case err := <-done:
		return err
	case <-lifecycle.Done():
		shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := server.Shutdown(shutdown); err != nil {
			_ = server.Close()
			return err
		}
		if err := <-done; !errors.Is(err, http.ErrServerClosed) {
			return err
		}
		return nil
	}
}

func mountPostgres(lifecycle context.Context, mux *http.ServeMux, db *sql.DB, origin, setupKey, collectionToken string, autoBackup bool, fallback http.Handler) (func(), error) {
	ctx, cancel := context.WithTimeout(lifecycle, 5*time.Second)
	defer cancel()
	var epoch int64
	var generation string
	err := db.QueryRowContext(ctx, `SELECT writer_epoch, restore_generation
		FROM board_candidate.runtime_control WHERE id=1`).Scan(&epoch, &generation)
	if err != nil || epoch < 1 || generation == "" {
		return nil, errors.New("无法读取现有工作台的数据库运行状态")
	}
	write := accountwrite.Service{DB: db, WriterEpoch: epoch, RestoreGeneration: generation, PublicOrigin: origin}
	directory, err := backupDirectory(ctx, db)
	if err != nil {
		return nil, err
	}
	backups, err := backuprestore.New(backuprestore.Config{Write: write, Directory: directory})
	if err != nil {
		return nil, fmt.Errorf("无法打开 PostgreSQL 业务备份目录: %w", err)
	}
	backupContext, stopBackups := context.WithCancel(lifecycle)
	backupDone := make(chan struct{})
	if autoBackup {
		go func() {
			defer close(backupDone)
			backups.RunAutomatic(backupContext)
		}()
	} else {
		close(backupDone)
	}
	cleanup := func() {
		stopBackups()
		<-backupDone
		_ = backups.Close()
	}
	auth := session.Handler{Store: session.Store{DB: db}, Origin: origin, WriterEpoch: epoch, RestoreGeneration: generation, SetupKey: setupKey}
	dailyFlags := daily.Service{DB: db, WriterEpoch: epoch, RestoreGeneration: generation, PublicOrigin: origin}
	accounts := account.Handler{DB: db}
	collector := collection.Service{Write: write, Token: collectionToken}
	boardRead := board.Handler{DB: db, BackupStatus: backups.Status, CollectionJobPrefix: collector.ActiveJobPrefix()}
	accountWrite := account.WriteHandler{Service: write}
	personnel := admin.Handler{DB: db}
	mux.HandleFunc("GET /api/auth/me", auth.Me)
	mux.HandleFunc("POST /api/auth/desktop-setup", auth.MintSetup)
	mux.HandleFunc("POST /api/auth/setup", auth.SetupOwner)
	mux.HandleFunc("POST /api/auth/login", auth.Login)
	mux.HandleFunc("POST /api/auth/logout", auth.Logout)
	mux.HandleFunc("POST /api/auth/password", auth.ChangePassword)
	mux.HandleFunc("POST /api/auth/username", auth.ChangeUsername)
	mux.HandleFunc("GET "+admin.UsersPath, personnel.Users)
	mux.HandleFunc("GET /api/auth/audit", personnel.Audit)
	mux.HandleFunc("GET /api/auth/entity-scopes", personnel.EntityScopes)
	mux.HandleFunc("POST "+admin.UsersPath, write.Handle(admin.UsersPath, "保存人员必须带请求标识", admin.AuthorizeOwner, admin.SaveUser))
	mux.HandleFunc("POST "+admin.OfficesPath, write.Handle(admin.OfficesPath, "新增办公室必须带请求标识", admin.AuthorizeOwner, admin.CreateOffice))
	mux.HandleFunc("POST "+admin.ScopePath, write.Handle(admin.ScopePath, "分配归属必须带请求标识", admin.AuthorizeOwner, admin.Reassign))
	mux.HandleFunc("GET /api/account/inline", accounts.Inline)
	mux.HandleFunc("GET /api/nurture/image", accounts.NurtureImage)
	mux.HandleFunc("GET /api/board", boardRead.Board)
	mux.HandleFunc("GET /api/ban-impact", boardRead.BanImpact)
	mux.HandleFunc("GET /api/record-versions", boardRead.RecordVersions)
	mux.HandleFunc("GET /api/workspace/history", boardRead.History)
	mux.HandleFunc("GET "+backuprestore.ListPath, backups.List)
	mux.HandleFunc("POST "+backuprestore.ManualPath, backups.Manual())
	mux.HandleFunc("POST "+backuprestore.PreviewPath, backups.Preview())
	mux.HandleFunc("POST "+backuprestore.ApplyPath, backups.Apply())
	employeeCards := employeecards.Service{Write: write}
	mux.HandleFunc("GET "+employeecards.Path, employeeCards.Get)
	mux.HandleFunc("POST "+employeecards.PreviewPath, employeeCards.Preview())
	mux.HandleFunc("POST "+employeecards.ApplyPath, employeeCards.Apply())
	mux.HandleFunc("GET /api/collection/status", boardRead.CollectionStatus)
	collector.Register(mux)
	review := publishreview.Handler{DB: db, WriterEpoch: epoch, RestoreGeneration: generation, PublicOrigin: origin}
	mux.HandleFunc("GET /api/publish-review", review.Overview)
	mux.HandleFunc("POST /api/publish-review/task", review.Write)
	mux.HandleFunc("POST /api/publish-review/sample", review.Write)
	tableRead := tablesync.Handler{DB: db}
	for _, path := range []string{"/api/table-sync", "/api/table-sync/account-sources", "/api/table-sync/batch", "/api/table-sync/request"} {
		mux.HandleFunc("GET "+path, tableRead.Read)
	}
	tableWrite := tablesync.Writer{DB: db, WriterEpoch: epoch, RestoreGeneration: generation, PublicOrigin: origin}
	for _, action := range []string{"pilot", "preview", "apply", "review", "rollback-preview", "rollback-apply"} {
		mux.HandleFunc("POST /api/table-sync/"+action, tableWrite.Write)
	}
	mux.HandleFunc("POST /api/account/patch", accountWrite.Patch())
	mux.HandleFunc("POST /api/account", accountWrite.Save(false))
	mux.HandleFunc("POST /api/account/create", accountWrite.Save(true))
	for _, path := range []string{"/api/account/status", "/api/account/card-readiness", "/api/account/phone-device", "/api/lifecycle-status", "/api/account/nurture"} {
		mux.HandleFunc("POST "+path, accountWrite.Field(path))
	}
	mux.HandleFunc("POST /api/bulk-status", accountWrite.BulkStatus())
	mux.HandleFunc("POST /api/batch-edit", accountWrite.Batch())
	for _, action := range account.NurtureActions() {
		mux.HandleFunc("POST "+account.NurturePathPrefix+action, accountWrite.Nurture(action))
	}
	mux.HandleFunc("GET /api/idempotency/result", write.RequestStatus(permitReceipt))
	mux.HandleFunc("GET /api/daily-flags", dailyFlags.Flags)
	mux.HandleFunc("POST /api/daily", dailyFlags.Save)
	mux.Handle("GET /api/board-updates", updates.Service{DB: db, Fallback: fallback})
	for _, path := range []string{inventory.CardPath, inventory.ImportCardsPath, inventory.DevicePath, inventory.CreateDevicePath} {
		mux.HandleFunc("POST "+path, inventory.Handler(write, path))
	}
	mux.HandleFunc("POST "+inventory.WorkflowPath, inventory.WorkflowHandler(write))
	mux.HandleFunc("POST "+publishing.LinkPath, write.Handle(publishing.LinkPath, "关联发布格子必须带请求标识", publishing.Authorize, publishing.Link))
	mux.HandleFunc("POST "+publishing.AssignmentPath, write.Handle(publishing.AssignmentPath, "调整实际发布位置必须带请求标识", publishing.Authorize, publishing.Assign))
	mux.HandleFunc("POST "+exchange.PreviewPath, write.Direct(12<<20, false, exchange.Permit("data_import"), exchange.Preview))
	mux.HandleFunc("POST "+exchange.ApplyPath, write.Handle(exchange.ApplyPath, "确认导入必须带请求标识", exchange.Permit("data_import"), exchange.Apply))
	mux.HandleFunc("POST "+exchange.ExportPath, write.Direct(100000, true, exchange.Permit("data_export"), exchange.Export))
	mux.HandleFunc("POST "+exchange.OfficePreviewPath, write.Direct(12<<20, false, exchange.AuthorizeOfficeThree, exchange.OfficePreview))
	mux.HandleFunc("POST "+exchange.OfficeApplyPath, write.Handle(exchange.OfficeApplyPath, "确认导入办公室三必须带请求标识", exchange.AuthorizeOfficeThree, exchange.OfficeApply))
	mux.HandleFunc("POST "+exchange.OfficeComputerScopePath, write.Handle(exchange.OfficeComputerScopePath, "确认电脑归属必须带请求标识", exchange.AuthorizeOfficeThree, exchange.OfficeThreeAssignComputers))
	mux.HandleFunc("GET /api/health", func(w http.ResponseWriter, _ *http.Request) {
		reply(w, http.StatusOK, map[string]any{"service": "douyin-account-board", "shared": true, "authentication_required": true, "board_guard": "response-kept"})
	})
	mux.HandleFunc("GET /api/ready", func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
		defer cancel()
		var mode string
		var currentEpoch int64
		var currentGeneration string
		err := db.QueryRowContext(ctx, "SELECT mode,writer_epoch,restore_generation FROM runtime_control WHERE id=1").
			Scan(&mode, &currentEpoch, &currentGeneration)
		if err != nil || mode != "open" || currentEpoch != epoch || currentGeneration != generation {
			reply(w, http.StatusServiceUnavailable, map[string]any{"ready": false, "code": "NOT_READY"})
			return
		}
		reply(w, http.StatusOK, map[string]any{"ready": true, "build": boardBuild})
	})
	collectionDisabled := func(w http.ResponseWriter, _ *http.Request) {
		reply(w, http.StatusServiceUnavailable, map[string]any{"error": "采集暂未启用；已有数量、时间、错误和任务记录保留", "code": "COLLECTION_DISABLED"})
	}
	mux.HandleFunc("/api/collection/", collectionDisabled)
	mux.HandleFunc("/api/maintenance/replace-collector", collectionDisabled)
	return cleanup, nil
}

func backupDirectory(ctx context.Context, db *sql.DB) (string, error) {
	if directory := os.Getenv("BOARD_GO_BACKUP_DIR"); directory != "" {
		return directory, nil
	}
	config, err := os.UserConfigDir()
	if err != nil {
		return "", errors.New("请设置 BOARD_GO_BACKUP_DIR 指定业务备份目录")
	}
	var database, host string
	var port int
	if err := db.QueryRowContext(ctx, `SELECT current_database(),host(inet_server_addr()),inet_server_port()`).Scan(&database, &host, &port); err != nil {
		return "", errors.New("无法确定数据库的业务备份目录")
	}
	identity := sha256.Sum256([]byte(fmt.Sprintf("%s:%d/%s", host, port, database)))
	return filepath.Join(config, "DouyinAccountBoard", "Backups", fmt.Sprintf("%x", identity[:16])), nil
}

func permitReceipt(ctx context.Context, tx *sql.Tx, actor session.User, action string, result map[string]any) error {
	if action == publishing.LinkPath || action == publishing.AssignmentPath {
		return publishing.PermitReceipt(ctx, tx, actor, result)
	}
	if action == collection.EnqueuePath {
		return collection.PermitReceipt(ctx, tx, actor, result)
	}
	var permissions []string
	switch action {
	case "/api/daily":
		permissions = []string{"publish"}
	case inventory.CardPath:
		permissions = []string{"card_create", "card_manage", "card_tasks"}
	case inventory.ImportCardsPath:
		permissions = []string{"card_create"}
	case inventory.DevicePath, inventory.WorkflowPath:
		permissions = []string{"device_manage"}
	case inventory.CreateDevicePath:
		permissions = []string{"device_create"}
	case exchange.ApplyPath:
		permissions = []string{"data_import"}
	case exchange.OfficeApplyPath, exchange.OfficeComputerScopePath, admin.UsersPath, admin.OfficesPath, admin.ScopePath:
		permissions = []string{"owner"}
	default:
		return account.PermitReceipt(ctx, tx, actor, action, result)
	}
	for _, permission := range permissions {
		if actor.Has(permission) {
			if record, ok := result["daily"].(map[string]any); ok {
				id, _ := record["id"].(string)
				if _, _, err := account.ReadScoped(ctx, tx, actor, id); errors.Is(err, sql.ErrNoRows) {
					return accountwrite.Forbidden("无权访问此资料")
				} else if err != nil {
					return err
				}
			}
			return nil
		}
	}
	return accountwrite.Forbidden("当前账号没有此操作权限")
}

func reply(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func parseOrigin(value string) (*url.URL, error) {
	origin, err := url.Parse(value)
	if err != nil || origin.Hostname() == "" || origin.User != nil || origin.RawQuery != "" ||
		origin.ForceQuery || origin.Fragment != "" || origin.Path != "" || (origin.Scheme != "http" && origin.Scheme != "https") {
		return nil, errors.New("invalid origin")
	}
	return origin, nil
}

func isLoopback(host string) bool {
	ip := net.ParseIP(strings.Trim(host, "[]"))
	return ip != nil && ip.IsLoopback()
}

func openDatabase(ctx context.Context, dsn string) (*sql.DB, error) {
	config, err := pgx.ParseConfig(dsn)
	if err != nil {
		return nil, errors.New("PostgreSQL 配置不正确")
	}
	if !isLoopback(config.Host) {
		return nil, errors.New("PostgreSQL 地址必须是本机回环地址")
	}
	for _, alternate := range config.Fallbacks {
		if !isLoopback(alternate.Host) {
			return nil, errors.New("PostgreSQL 备用地址必须是本机回环地址")
		}
	}
	config.RuntimeParams["search_path"] = "board_candidate,pg_catalog"
	config.ConnectTimeout = 4 * time.Second
	db := stdlib.OpenDB(*config)
	db.SetMaxOpenConns(12)
	db.SetMaxIdleConns(4)
	db.SetConnMaxIdleTime(2 * time.Minute)

	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, errors.New("无法连接 PostgreSQL")
	}
	return db, nil
}
