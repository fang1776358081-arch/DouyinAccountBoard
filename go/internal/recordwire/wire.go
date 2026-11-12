// Package recordwire encodes optimistic-lock tokens shared with the Python workbench.
package recordwire

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
)

// Encode matches Python's compact sorted JSON for the supported records.
// Go escapes U+2028 and U+2029 even when HTML escaping is disabled. The
// Python encoder leaves them in UTF-8 and preserves literal escape sequences.
func Encode(value any) ([]byte, error) {
	var output bytes.Buffer
	encoder := json.NewEncoder(&output)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(value); err != nil {
		return nil, err
	}
	raw := bytes.TrimSuffix(output.Bytes(), []byte{'\n'})
	encoded := make([]byte, 0, len(raw))
	for i := 0; i < len(raw); {
		if raw[i] == '\\' && i+1 < len(raw) {
			if i+5 < len(raw) {
				switch string(raw[i : i+6]) {
				case "\\u2028":
					encoded = append(encoded, "\u2028"...)
					i += 6
					continue
				case "\\u2029":
					encoded = append(encoded, "\u2029"...)
					i += 6
					continue
				}
			}
			encoded = append(encoded, raw[i:i+2]...)
			i += 2
			continue
		}
		encoded = append(encoded, raw[i])
		i++
	}
	return encoded, nil
}

func Version(value any) (string, error) {
	raw, err := Encode(value)
	if err != nil {
		return "", err
	}
	digest := sha256.Sum256(raw)
	return hex.EncodeToString(digest[:]), nil
}
