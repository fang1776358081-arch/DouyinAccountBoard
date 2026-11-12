package bootstrap

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"strconv"
	"strings"
	"testing"
	"time"

	_ "github.com/jackc/pgx/v5/stdlib"
)

func TestValidateExistingSyntheticPostgres(t *testing.T) {
	value := os.Getenv("BOARD_GO_SCHEMA_TEST_PORT")
	if value == "" {
		t.Skip("set BOARD_GO_SCHEMA_TEST_PORT for the loopback synthetic PostgreSQL fixture")
	}
	port, err := strconv.ParseUint(value, 10, 16)
	if err != nil || port == 0 {
		t.Fatal("invalid synthetic PostgreSQL port")
	}
	database := os.Getenv("BOARD_GO_SCHEMA_TEST_DATABASE")
	if !strings.HasPrefix(database, "synthetic_") ||
		strings.IndexFunc(database, func(r rune) bool { return !(r == '_' || r >= 'a' && r <= 'z' || r >= '0' && r <= '9') }) >= 0 {
		t.Fatal("set BOARD_GO_SCHEMA_TEST_DATABASE to an isolated synthetic database with a canonical ledger")
	}
	dsn := fmt.Sprintf("postgres://synthetic_owner@127.0.0.1:%d/%s?sslmode=disable&default_transaction_read_only=on", port, database)
	db, err := sql.Open("pgx", dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := ValidateExisting(ctx, db); err != nil {
		t.Fatal(err)
	}
}
