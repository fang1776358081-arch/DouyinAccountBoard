package backuprestore

import (
	"bufio"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"time"

	"douyinboard/internal/accountwrite"
)

const (
	archiveName    = "douyin-board-postgresql-business"
	archiveVersion = 1
	archiveExt     = ".boardpg.gz"
	maxRecord      = 12 << 20
	maxArchive     = int64(8 << 30)
)

var archivePattern = regexp.MustCompile(`^(?:manual|auto|before-restore)-[0-9]{8}T[0-9]{6}-[a-f0-9]{32}\.boardpg\.gz$`)
var errBadArchive = errors.New("backup archive integrity check failed")

type archiveHeader struct {
	Format        string `json:"format"`
	Version       int    `json:"version"`
	SchemaVersion int    `json:"schema_version"`
	SchemaHash    string `json:"schema_hash"`
}

type archiveInfo struct {
	Hash   string
	Counts map[string]int64
}

type listItem struct {
	ID    string `json:"id"`
	Name  string `json:"name"`
	Bytes int64  `json:"bytes"`
	At    int64  `json:"at"`
}

func validArchiveID(id string) bool {
	if !archivePattern.MatchString(path.Base(id)) || strings.ContainsAny(id, "\\:") {
		return false
	}
	switch path.Dir(id) {
	case ".":
		return path.Base(id) == id && !strings.HasPrefix(id, "auto-")
	case "automatic":
		return path.Join("automatic", path.Base(id)) == id && strings.HasPrefix(path.Base(id), "auto-")
	default:
		return false
	}
}

func privateArchive(info os.FileInfo) bool {
	return info.Mode().IsRegular() && info.Size() > 0 && info.Size() <= maxArchive &&
		(runtime.GOOS == "windows" || info.Mode().Perm()&0o077 == 0)
}

func (s *Service) openArchive(id string) (*os.File, error) {
	if !validArchiveID(id) {
		return nil, errBadArchive
	}
	info, err := s.directory.Lstat(id)
	if err != nil || !privateArchive(info) {
		return nil, errBadArchive
	}
	file, err := s.directory.Open(id)
	if err != nil {
		return nil, errBadArchive
	}
	opened, err := file.Stat()
	if err != nil || !privateArchive(opened) || !os.SameFile(info, opened) {
		file.Close()
		return nil, errBadArchive
	}
	return file, nil
}

func (s *Service) writeArchive(ctx context.Context, tx *sql.Tx, definition catalog, folder, prefix string) (string, error) {
	suffix, err := accountwrite.NewGroup()
	if err != nil {
		return "", err
	}
	id := path.Join(folder, prefix+"-"+time.Now().UTC().Format("20060102T150405")+"-"+suffix+archiveExt)
	temporary := id + ".tmp"
	file, err := s.directory.OpenFile(temporary, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		return "", err
	}
	defer s.directory.Remove(temporary)
	defer file.Close()
	writer := gzip.NewWriter(file)
	if err := encodeArchive(ctx, tx, definition, writer); err != nil {
		return "", err
	}
	if err := writer.Close(); err != nil {
		return "", err
	}
	if err := file.Sync(); err != nil {
		return "", err
	}
	if err := file.Close(); err != nil {
		return "", err
	}
	verification, err := s.directory.Open(temporary)
	if err != nil {
		return "", err
	}
	_, verifyErr := decodeArchive(ctx, verification, definition, nil)
	closeErr := verification.Close()
	if err := errors.Join(verifyErr, closeErr); err != nil {
		return "", err
	}
	if err := s.directory.Rename(temporary, id); err != nil {
		return "", err
	}
	if runtime.GOOS != "windows" {
		parent, err := s.directory.Open(path.Dir(id))
		if err != nil {
			return "", err
		}
		err = errors.Join(parent.Sync(), parent.Close())
		if err != nil {
			return "", err
		}
	}
	return id, nil
}

func encodeArchive(ctx context.Context, tx *sql.Tx, definition catalog, dest io.Writer) error {
	header := archiveHeader{archiveName, archiveVersion, definition.version, definition.hash}
	if err := json.NewEncoder(dest).Encode(header); err != nil {
		return err
	}
	for _, spec := range definition.tables {
		if _, err := fmt.Fprintf(dest, "T %s\n", spec.name); err != nil {
			return err
		}
		count, err := eachRow(ctx, tx, spec.tableSpec, func(row []byte) error {
			if len(row) > maxRecord-3 {
				return errBadArchive
			}
			if _, err := dest.Write([]byte("R ")); err != nil {
				return err
			}
			if _, err := dest.Write(row); err != nil {
				return err
			}
			_, err := dest.Write([]byte{'\n'})
			return err
		})
		if err != nil {
			return err
		}
		if _, err := fmt.Fprintf(dest, "E %d\n", count); err != nil {
			return err
		}
	}
	_, err := io.WriteString(dest, "Z\n")
	return err
}

func (s *Service) readArchive(ctx context.Context, id string, definition catalog, receive func(table, []byte) error) (archiveInfo, error) {
	file, err := s.openArchive(id)
	if err != nil {
		return archiveInfo{}, err
	}
	defer file.Close()
	return decodeArchive(ctx, file, definition, receive)
}

func decodeArchive(ctx context.Context, src io.Reader, definition catalog, receive func(table, []byte) error) (archiveInfo, error) {
	digest := sha256.New()
	compressed := &io.LimitedReader{R: io.TeeReader(src, digest), N: maxArchive + 1}
	reader, err := gzip.NewReader(compressed)
	if err != nil {
		return archiveInfo{}, errBadArchive
	}
	defer reader.Close()
	plain := &io.LimitedReader{R: reader, N: maxArchive + 1}
	scanner := bufio.NewScanner(plain)
	scanner.Buffer(make([]byte, 64<<10), maxRecord)
	var header archiveHeader
	if !scanner.Scan() || json.Unmarshal(scanner.Bytes(), &header) != nil ||
		header != (archiveHeader{archiveName, archiveVersion, definition.version, definition.hash}) {
		return archiveInfo{}, errBadArchive
	}
	counts := make(map[string]int64, len(definition.tables))
	for _, spec := range definition.tables {
		if !scanner.Scan() || scanner.Text() != "T "+spec.name {
			return archiveInfo{}, errBadArchive
		}
		var count int64
		for scanner.Scan() {
			if err := ctx.Err(); err != nil {
				return archiveInfo{}, err
			}
			line := scanner.Bytes()
			if len(line) < 3 {
				return archiveInfo{}, errBadArchive
			}
			if string(line[:2]) == "E " {
				if text := string(line[2:]); text != strconv.FormatInt(count, 10) {
					return archiveInfo{}, errBadArchive
				}
				counts[spec.name] = count
				break
			}
			if string(line[:2]) != "R " || !validRow(line[2:], spec.columns) {
				return archiveInfo{}, errBadArchive
			}
			if receive != nil {
				if err := receive(spec, line[2:]); err != nil {
					return archiveInfo{}, err
				}
			}
			count++
		}
		if _, found := counts[spec.name]; !found {
			return archiveInfo{}, errBadArchive
		}
	}
	if !scanner.Scan() || scanner.Text() != "Z" || scanner.Scan() || scanner.Err() != nil || plain.N == 0 || compressed.N == 0 {
		return archiveInfo{}, errBadArchive
	}
	return archiveInfo{Hash: sum(digest), Counts: counts}, nil
}

func validRow(raw []byte, columns []string) bool {
	if len(raw) == 0 || raw[0] != '{' {
		return false
	}
	var fields map[string]json.RawMessage
	if json.Unmarshal(raw, &fields) != nil || len(fields) != len(columns) {
		return false
	}
	for _, name := range columns {
		if _, exists := fields[name]; !exists {
			return false
		}
	}
	return true
}
