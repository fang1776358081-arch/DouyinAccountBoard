package main

import (
	"context"
	"errors"
	"net"
	"net/url"
	"regexp"
	"strconv"
	"strings"

	"github.com/jackc/pgx/v5"
)

const cloudBaseDatabasePrefix = "DOUYIN_BOARD_DATABASE_"

var cloudBaseDNSLabel = regexp.MustCompile(`^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$`)

// parseCloudBaseConfig takes its settings from the existing service environment.
// It never falls back to the development database, a PostgreSQL service file,
// a password file, or a connection without TLS.
func parseCloudBaseConfig(getenv func(string) string) (*pgx.ConnConfig, error) {
	// pgx reads its own environment independently of the connection string.
	// Reject overrides before parsing, in particular PGSERVICE which would
	// cause pgx to open a service file even with explicit URL parameters.
	for _, key := range strings.Fields(`
		PGHOST PGHOSTADDR PGPORT PGDATABASE PGUSER PGPASSWORD PGPASSFILE
		PGAPPNAME PGCONNECT_TIMEOUT PGSSLMODE PGSSLKEY PGSSLCERT PGSSLCERTMODE
		PGSSLSNI PGSSLROOTCERT PGSSLPASSWORD PGSSLNEGOTIATION PGTARGETSESSIONATTRS
		PGSERVICE PGSERVICEFILE PGTZ PGOPTIONS PGMINPROTOCOLVERSION
		PGMAXPROTOCOLVERSION PGCHANNELBINDING PGREQUIREAUTH
		DATABASE_URL DOUYIN_BOARD_DATA_DIR DOUYIN_BOARD_TEST_ROOT DOUYIN_BOARD_OWNER_PASSWORD
	`) {
		if getenv(key) != "" {
			return nil, errors.New("CloudBase 模式不允许继承其他数据库或初始化配置")
		}
	}

	value := func(name string) string { return strings.TrimSpace(getenv(cloudBaseDatabasePrefix + name)) }
	host, database, user := value("HOST"), value("NAME"), value("USER")
	password := getenv(cloudBaseDatabasePrefix + "PASSWORD")
	if !validCloudBaseHost(host) {
		return nil, errors.New("CloudBase 数据库必须使用单个非回环主机")
	}
	if !validCloudBaseIdentity(database) || !validCloudBaseIdentity(user) {
		return nil, errors.New("CloudBase 数据库名称或用户配置不正确")
	}
	if password == "" || strings.ContainsRune(password, 0) ||
		strings.Contains(host, password) || strings.Contains(database, password) || strings.Contains(user, password) {
		return nil, errors.New("CloudBase 数据库密码配置不正确")
	}

	port := value("PORT")
	if port == "" {
		port = "5432"
	}
	for _, digit := range port {
		if digit < '0' || digit > '9' {
			return nil, errors.New("CloudBase 数据库端口配置不正确")
		}
	}
	portNumber, err := strconv.ParseUint(port, 10, 16)
	if err != nil || portNumber == 0 {
		return nil, errors.New("CloudBase 数据库端口配置不正确")
	}

	mode, rootCert := value("SSLMODE"), value("SSLROOTCERT")
	switch mode {
	case "require":
		if rootCert != "" {
			return nil, errors.New("CloudBase require 模式不接受显式 CA 文件")
		}
	case "verify-ca", "verify-full":
		if rootCert == "" {
			return nil, errors.New("CloudBase 证书校验需要 CA 文件")
		}
	default:
		return nil, errors.New("CloudBase 数据库必须使用 require、verify-ca 或 verify-full")
	}

	query := url.Values{
		"application_name": {"douyin-board-cloudbase"},
		"connect_timeout":  {"10"},
		"passfile":         {""},
		"search_path":      {"board_candidate,pg_catalog"},
		"sslcertmode":      {"disable"},
		"sslmode":          {mode},
	}
	if rootCert != "" {
		query.Set("sslrootcert", rootCert)
	}
	connection := url.URL{
		Scheme: "postgres",
		Host:   net.JoinHostPort(host, strconv.FormatUint(portNumber, 10)),
		User:   url.UserPassword(user, password),
		Path:   "/" + database,
		// PostgreSQL URI parsing percent-decodes values without treating
		// a plus sign as a space as an HTML form would.
		RawQuery: strings.ReplaceAll(query.Encode(), "+", "%20"),
	}
	config, err := pgx.ParseConfig(connection.String())
	if err != nil || config.TLSConfig == nil || len(config.Fallbacks) != 0 {
		// Parser errors may include the connection string or local file paths.
		return nil, errors.New("CloudBase PostgreSQL TLS 配置不正确")
	}
	lookup := config.LookupFunc
	config.LookupFunc = func(ctx context.Context, hostname string) ([]string, error) {
		addresses, err := lookup(ctx, hostname)
		if err != nil || len(addresses) == 0 {
			return nil, errors.New("无法解析 CloudBase 数据库主机")
		}
		for _, address := range addresses {
			if !validCloudBaseIP(net.ParseIP(address)) {
				return nil, errors.New("CloudBase 数据库主机解析到本机或无效地址")
			}
		}
		return addresses, nil
	}
	return config, nil
}

func validCloudBaseHost(host string) bool {
	if ip := net.ParseIP(host); ip != nil {
		return ip.To4() != nil && validCloudBaseIP(ip)
	}
	host = strings.TrimSuffix(strings.ToLower(host), ".")
	if len(host) > 253 || host == "localhost" || strings.HasSuffix(host, ".localhost") {
		return false
	}
	for _, label := range strings.Split(host, ".") {
		if !cloudBaseDNSLabel.MatchString(label) {
			return false
		}
	}
	return true
}

func validCloudBaseIP(ip net.IP) bool {
	return ip != nil && ip.IsGlobalUnicast() && !ip.IsLoopback()
}

func validCloudBaseIdentity(value string) bool {
	return value != "" && !strings.HasPrefix(strings.ToLower(value), "synthetic_") &&
		!strings.ContainsAny(value, "/@\\ \t\r\n\x00")
}
