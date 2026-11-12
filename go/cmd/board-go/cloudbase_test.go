package main

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"math/big"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func cloudBaseTestEnvironment() map[string]string {
	return map[string]string{
		cloudBaseDatabasePrefix + "HOST":     "postgres.example.test",
		cloudBaseDatabasePrefix + "NAME":     "board?prod",
		cloudBaseDatabasePrefix + "USER":     "board:runtime",
		cloudBaseDatabasePrefix + "PASSWORD": "fixture password :@/#?%",
		cloudBaseDatabasePrefix + "SSLMODE":  "require",
	}
}

func TestParseCloudBaseConfigRequiresTLS(t *testing.T) {
	environment := cloudBaseTestEnvironment()
	config, err := parseCloudBaseConfig(func(key string) string { return environment[key] })
	if err != nil {
		t.Fatal(err)
	}
	if config.Host != environment[cloudBaseDatabasePrefix+"HOST"] ||
		config.Port != 5432 || config.Database != environment[cloudBaseDatabasePrefix+"NAME"] ||
		config.User != environment[cloudBaseDatabasePrefix+"USER"] ||
		config.Password != environment[cloudBaseDatabasePrefix+"PASSWORD"] {
		t.Fatal("CloudBase settings did not survive URL encoding")
	}
	if config.TLSConfig == nil || len(config.Fallbacks) != 0 ||
		config.RuntimeParams["search_path"] != "board_candidate,pg_catalog" ||
		config.ConnectTimeout != 10*time.Second {
		t.Fatal("CloudBase TLS, timeout, or schema defaults are incorrect")
	}
}

func TestParseCloudBaseConfigRejectsUntrustedSettings(t *testing.T) {
	cases := []struct {
		key   string
		value string
	}{
		{cloudBaseDatabasePrefix + "HOST", "127.0.0.2"},
		{cloudBaseDatabasePrefix + "HOST", "0.0.0.0"},
		{cloudBaseDatabasePrefix + "HOST", "LOCALHOST."},
		{cloudBaseDatabasePrefix + "HOST", "x.localhost"},
		{cloudBaseDatabasePrefix + "HOST", "postgres.example.test,other.example.test"},
		{cloudBaseDatabasePrefix + "HOST", "postgres.example.test?sslmode=disable"},
		{cloudBaseDatabasePrefix + "HOST", "/tmp"},
		{cloudBaseDatabasePrefix + "NAME", "synthetic_probe"},
		{cloudBaseDatabasePrefix + "USER", "synthetic_owner"},
		{cloudBaseDatabasePrefix + "PASSWORD", ""},
		{cloudBaseDatabasePrefix + "PASSWORD", "example"},
		{cloudBaseDatabasePrefix + "PORT", "65536"},
		{cloudBaseDatabasePrefix + "PORT", "5432,5433"},
		{cloudBaseDatabasePrefix + "PORT", "0"},
		{cloudBaseDatabasePrefix + "SSLMODE", "disable"},
		{cloudBaseDatabasePrefix + "SSLMODE", "prefer"},
		{cloudBaseDatabasePrefix + "SSLMODE", "verify-full"},
		{cloudBaseDatabasePrefix + "SSLROOTCERT", "fixture-root-path"},
		{"PGSERVICE", "fixture-service"},
		{"PGPASSFILE", "fixture-passfile"},
		{"PGOPTIONS", "-c default_transaction_read_only=on"},
		{"PGSSLMODE", "disable"},
		{"DOUYIN_BOARD_TEST_ROOT", "fixture-test-root"},
		{"DOUYIN_BOARD_OWNER_PASSWORD", "fixture-owner-password"},
	}
	for _, tc := range cases {
		t.Run(tc.key+"="+tc.value, func(t *testing.T) {
			environment := cloudBaseTestEnvironment()
			environment[tc.key] = tc.value
			if config, err := parseCloudBaseConfig(func(key string) string { return environment[key] }); config != nil || err == nil {
				t.Fatal("untrusted setting was accepted")
			}
		})
	}
}

func TestParseCloudBaseConfigRetainsCertificateVerification(t *testing.T) {
	const server = "postgres.example.test"
	now := time.Now()
	template := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "CloudBase fixture"},
		DNSNames:              []string{server},
		NotBefore:             now.Add(-time.Minute),
		NotAfter:              now.Add(time.Hour),
		IsCA:                  true,
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature,
		BasicConstraintsValid: true,
	}
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	der, err := x509.CreateCertificate(rand.Reader, template, template, public, private)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "fixture root.pem")
	if err := os.WriteFile(path, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), 0600); err != nil {
		t.Fatal(err)
	}
	for _, mode := range []string{"verify-ca", "verify-full"} {
		t.Run(mode, func(t *testing.T) {
			environment := cloudBaseTestEnvironment()
			environment[cloudBaseDatabasePrefix+"SSLMODE"] = mode
			environment[cloudBaseDatabasePrefix+"SSLROOTCERT"] = path
			config, err := parseCloudBaseConfig(func(key string) string { return environment[key] })
			if err != nil {
				t.Fatal(err)
			}
			tlsConfig := config.TLSConfig
			if len(config.Fallbacks) != 0 || tlsConfig.RootCAs == nil {
				t.Fatal("expected a single TLS connection with the explicit CA")
			}
			if mode == "verify-ca" {
				if tlsConfig.VerifyPeerCertificate == nil || tlsConfig.VerifyPeerCertificate([][]byte{der}, nil) != nil ||
					tlsConfig.VerifyPeerCertificate([][]byte{[]byte("bad certificate")}, nil) == nil {
					t.Fatal("expected certificate chain verification")
				}
			} else if tlsConfig.InsecureSkipVerify || tlsConfig.ServerName != server {
				t.Fatal("expected certificate and hostname verification")
			}
		})
	}

	environment := cloudBaseTestEnvironment()
	environment[cloudBaseDatabasePrefix+"SSLMODE"] = "verify-full"
	missing := filepath.Join(t.TempDir(), "private fixture path")
	environment[cloudBaseDatabasePrefix+"SSLROOTCERT"] = missing
	_, err = parseCloudBaseConfig(func(key string) string { return environment[key] })
	if err == nil || strings.Contains(err.Error(), missing) ||
		strings.Contains(err.Error(), environment[cloudBaseDatabasePrefix+"PASSWORD"]) {
		t.Fatal("unexpected or sensitive certificate parsing result")
	}
}
