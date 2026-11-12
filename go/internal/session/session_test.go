package session

import (
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestPasswordMatchesPythonFixture(t *testing.T) {
	const encoded = "pbkdf2_sha256$600000$0123456789abcdef0123456789abcdef$86e1852eac40931c7f272443d9191840cb4c1be93538869db5f2fab4c239770f"
	if !passwordMatches("synthetic-password-测试", encoded) {
		t.Fatal("Go rejected a valid Python PBKDF2 fixture")
	}
	for _, candidate := range []string{
		"synthetic-password-错误", strings.Repeat("测", 129), "",
	} {
		if passwordMatches(candidate, encoded) {
			t.Fatal("incorrect password accepted")
		}
	}
	if passwordMatches("synthetic-password-测试", strings.Replace(encoded, "$600000$", "$90000000$", 1)) {
		t.Fatal("unbounded work factor accepted")
	}
}

func TestOfficeScopeAndOperationPermissionsAreIndependent(t *testing.T) {
	owner := User{Role: "owner", Permissions: permissionsForRole("owner", "[]")}
	admin := User{Role: "admin", Permissions: permissionsForRole("admin", "[\"account_status\"]")}
	employee := User{Role: "employee", Permissions: permissionsForRole("employee", "[\"publish\"]")}
	if !owner.Global() || !owner.Has("owner") || !owner.Has("publish") {
		t.Fatal("owner lost inherited permissions")
	}
	if !admin.Global() || admin.Has("owner") || admin.Has("publish") || !admin.Has("account_status") {
		t.Fatal("global scope improperly widened operation permissions")
	}
	if employee.Global() || !employee.Has("publish") || employee.Has("account_status") {
		t.Fatal("employee role grants were altered")
	}
}

func TestSessionCookieValidation(t *testing.T) {
	for _, header := range []string{"", "board_session=a; board_session=b"} {
		request := httptest.NewRequest(http.MethodGet, "/", nil)
		request.Header.Set("Cookie", header)
		if _, err := (Store{}).Authenticate(request.Context(), request); !errors.Is(err, ErrUnauthorized) {
			t.Fatalf("cookie %q was not rejected before a query", header)
		}
	}
}

func TestLoginRequiresSameOriginJSON(t *testing.T) {
	handler := Handler{Origin: "https://board.example.test"}
	for _, test := range []struct {
		name, origin, contentType, body string
		want                            int
	}{
		{"cross-origin", "https://another.example.test", "application/json", "{}", http.StatusForbidden},
		{"non-JSON", "", "text/plain", "{}", http.StatusBadRequest},
		{"two-objects", "", "application/json", "{} {}", http.StatusBadRequest},
		{"null", "", "application/json", "null", http.StatusBadRequest},
		{"valid-but-not-configured", "https://board.example.test", "application/json; charset=UTF-8", "{}", http.StatusServiceUnavailable},
	} {
		t.Run(test.name, func(t *testing.T) {
			request := httptest.NewRequest(http.MethodPost, "/api/auth/login", strings.NewReader(test.body))
			request.Header.Set("Content-Type", test.contentType)
			request.Header.Set("Origin", test.origin)
			response := httptest.NewRecorder()
			handler.Login(response, request)
			if response.Code != test.want {
				t.Fatalf("status = %d, want %d", response.Code, test.want)
			}
			if response.Header().Get("Cache-Control") != "no-store" || response.Header().Get("Set-Cookie") != "" {
				t.Fatal("login error cached or session established")
			}
		})
	}
}

func TestCookieAttributes(t *testing.T) {
	handler := Handler{Origin: "https://board.example.test"}
	request := httptest.NewRequest(http.MethodPost, "/api/auth/login", nil)
	cookie := handler.cookie(request, "synthetic-token", sessionSeconds)
	if cookie.Name != "board_session" || cookie.Path != "/" || !cookie.Secure || !cookie.HttpOnly ||
		cookie.SameSite != http.SameSiteStrictMode || cookie.MaxAge != sessionSeconds {
		t.Fatalf("unexpected auth cookie settings: %#v", cookie)
	}
	if !strings.Contains(handler.cookie(request, "", -1).String(), "Max-Age=0") {
		t.Fatal("logout cookie did not expire")
	}
}
