package main

import (
	"errors"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"

	"douyinboard/internal/collection"
)

type collectionTokenConfig struct {
	FilePath, EnvironmentToken, ListenHost, PublicOrigin string
	CloudBase, Legacy, Initialize                        bool
}

// resolveCollectionToken validates the explicit activation before opening a database.
// A private file enables the browser queue on the public service, but does not
// change the collection package's loopback-only worker authentication.
func resolveCollectionToken(config collectionTokenConfig) (string, error) {
	if config.FilePath != "" && config.EnvironmentToken != "" {
		return "", errors.New("作品采集令牌只能选择私有文件或本机环境中的一种来源")
	}
	if config.FilePath == "" && config.EnvironmentToken == "" {
		return "", nil
	}
	if config.Legacy || config.Initialize {
		return "", errors.New("作品采集不能与旧版代理或空库初始化同时启用")
	}
	if config.FilePath == "" {
		if config.CloudBase || !isLoopback(config.ListenHost) || config.PublicOrigin != "" || !collection.ValidToken(config.EnvironmentToken) {
			return "", errors.New("环境采集令牌需要 32 至 256 位可见 ASCII 字符，仅在本机独立监听时启用")
		}
		return config.EnvironmentToken, nil
	}
	if (!isLoopback(config.ListenHost) || config.CloudBase) && config.PublicOrigin == "" {
		return "", errors.New("正式作品采集必须明确配置原网站来源")
	}
	if config.PublicOrigin != "" {
		if _, err := parseOrigin(config.PublicOrigin); err != nil {
			return "", errors.New("作品采集的网站来源不正确")
		}
	}
	return readCollectionTokenFile(config.FilePath)
}

func readCollectionTokenFile(path string) (string, error) {
	invalid := errors.New("作品采集令牌文件必须是绝对路径的私有普通文件，并含 32 至 256 位可见 ASCII 字符")
	if !filepath.IsAbs(path) {
		return "", invalid
	}
	info, err := os.Lstat(path)
	if err != nil || !info.Mode().IsRegular() || info.Size() < 32 || info.Size() > 258 {
		return "", invalid
	}
	// Windows file modes do not represent ACLs. The Windows launcher protects the
	// local configuration separately; on Linux systemd credentials use mode 0400.
	if runtime.GOOS != "windows" && info.Mode().Perm()&0o077 != 0 {
		return "", invalid
	}
	file, err := os.Open(path)
	if err != nil {
		return "", invalid
	}
	defer file.Close()
	opened, err := file.Stat()
	if err != nil || !opened.Mode().IsRegular() || !os.SameFile(info, opened) {
		return "", invalid
	}
	if runtime.GOOS != "windows" && opened.Mode().Perm()&0o077 != 0 {
		return "", invalid
	}
	content, err := io.ReadAll(io.LimitReader(file, 259))
	if err != nil || len(content) > 258 {
		return "", invalid
	}
	token := string(content)
	if strings.HasSuffix(token, "\n") {
		token = strings.TrimSuffix(strings.TrimSuffix(token, "\n"), "\r")
	}
	if !collection.ValidToken(token) {
		return "", invalid
	}
	return token, nil
}
