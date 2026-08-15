package net

import (
	"bytes"
	"compress/gzip"
	"errors"
	"fmt"
	"net"
	"os"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func mustParse(t *testing.T, raw string) *URL {
	t.Helper()
	u, err := Parse(raw)
	if err != nil {
		t.Fatalf("Parse(%q): %v", raw, err)
	}
	return u
}

func TestParse(t *testing.T) {
	cases := []struct {
		raw    string
		scheme string
		host   string
		port   int
		path   string
	}{
		{"https://example.com/a/b/page.html", "https", "example.com", 443, "/a/b/page.html"},
		{"http://h.com:8080/p", "http", "h.com", 8080, "/p"},
		{"example.com", "https", "example.com", 443, "/"},
		{"example.com:8080", "https", "example.com", 8080, "/"},
		{"localhost:8000/path", "https", "localhost", 8000, "/path"},
		{"HTTP://EXAMPLE.COM", "http", "example.com", 80, "/"},
		{"https://user:pw@example.com/x", "https", "example.com", 443, "/x"},
		{"https://[::1]:8080/x", "https", "::1", 8080, "/x"},
	}
	for _, c := range cases {
		u := mustParse(t, c.raw)
		if u.Scheme != c.scheme || u.Host != c.host || u.Port != c.port || u.Path != c.path {
			t.Errorf("Parse(%q) = %s/%s:%d%s, want %s/%s:%d%s",
				c.raw, u.Scheme, u.Host, u.Port, u.Path, c.scheme, c.host, c.port, c.path)
		}
	}

	if f := mustParse(t, "file:///etc/hosts"); f.Path != "/etc/hosts" {
		t.Errorf("file path = %q", f.Path)
	}
	if v := mustParse(t, "view-source:https://example.com"); !v.ViewSource || v.Host != "example.com" {
		t.Errorf("view-source parse = %+v", v)
	}
	if frag := mustParse(t, "https://x.com/p#sec"); frag.Fragment != "sec" || frag.Path != "/p" {
		t.Errorf("fragment parse = %+v", frag)
	}
}

func TestParseErrors(t *testing.T) {
	for _, bad := range []string{"https://:8080/x", "https://example.com:notaport/", "https://[::1/x"} {
		if u, err := Parse(bad); err == nil {
			t.Errorf("Parse(%q) = %+v, want error", bad, u)
		}
	}
}

// An unknown scheme parses rather than failing, so extensions can intercept it
// through the toes handle hook.
func TestParseUnknownScheme(t *testing.T) {
	u := mustParse(t, "toehub://")
	if u.Scheme != "toehub" || u.Host != "" || u.Path != "/" {
		t.Errorf("empty-host extension scheme = %+v", u)
	}
	u = mustParse(t, "toehub://store/list")
	if u.Scheme != "toehub" || u.Host != "store" || u.Path != "/list" {
		t.Errorf("extension scheme with host = %+v", u)
	}
	// Without "//" there is nothing to distinguish "toes:panel" from a bare
	// host:port, so it is treated as one, same as the Python parser.
	if _, err := Parse("toes:panel"); err == nil {
		t.Error(`Parse("toes:panel") should fail as a bad port, matching net.py`)
	}
}

func TestString(t *testing.T) {
	cases := [][2]string{
		{"https://example.com/a", "https://example.com/a"},
		{"http://[::1]/y", "http://[::1]/y"},
		{"https://[::1]:8080/x", "https://[::1]:8080/x"},
		{"view-source:https://example.com", "view-source:https://example.com/"},
		{"https://x.com/p#sec", "https://x.com/p#sec"},
	}
	for _, c := range cases {
		if got := mustParse(t, c[0]).String(); got != c[1] {
			t.Errorf("Parse(%q).String() = %q, want %q", c[0], got, c[1])
		}
	}
}

func TestResolve(t *testing.T) {
	base := mustParse(t, "https://example.com/a/b/page.html")
	cases := [][2]string{
		{"c.html", "https://example.com/a/b/c.html"},
		{"../c.html", "https://example.com/a/c.html"},
		{"/c.html", "https://example.com/c.html"},
		{"//other.com/x", "https://other.com/x"},
		{"https://other.com/x", "https://other.com/x"},
		{"#frag", "https://example.com/a/b/page.html#frag"},
	}
	for _, c := range cases {
		got, err := base.Resolve(c[0])
		if err != nil {
			t.Fatalf("Resolve(%q): %v", c[0], err)
		}
		if got.String() != c[1] {
			t.Errorf("Resolve(%q) = %q, want %q", c[0], got, c[1])
		}
	}
}

func TestRequestData(t *testing.T) {
	resp, err := mustParse(t, "data:text/html,<b>hi</b>").Get()
	if err != nil || resp.Text != "<b>hi</b>" || resp.ContentType != "text/html" {
		t.Fatalf("data: html = %+v, %v", resp, err)
	}
	resp, err = mustParse(t, "data:text/plain;base64,aGVsbG8=").Get()
	if err != nil || resp.Text != "hello" {
		t.Fatalf("data: base64 = %+v, %v", resp, err)
	}
}

func TestRequestFile(t *testing.T) {
	path := t.TempDir() + "/page.html"
	if err := writeFile(path, "<p>hi</p>"); err != nil {
		t.Fatal(err)
	}
	resp, err := mustParse(t, "file://"+path).Get()
	if err != nil || resp.Text != "<p>hi</p>" || resp.ContentType != "text/html" {
		t.Fatalf("file get = %+v, %v", resp, err)
	}
	// A directory renders an index page instead of failing.
	resp, err = mustParse(t, "file://"+t.TempDir()).Get()
	if err != nil || !strings.Contains(resp.Text, "Index of") {
		t.Fatalf("dir index = %+v, %v", resp, err)
	}
}

func TestDechunk(t *testing.T) {
	body := []byte("4\r\nWiki\r\n5\r\npedia\r\n0\r\n\r\n")
	if got := string(dechunk(body)); got != "Wikipedia" {
		t.Errorf("dechunk = %q", got)
	}
}

func TestCharsetOf(t *testing.T) {
	cases := [][2]string{
		{"text/html; charset=\"utf-8\"", "utf-8"},
		{"text/html;charset=ISO-8859-1", "iso-8859-1"},
		{"text/html", "utf-8"},
	}
	for _, c := range cases {
		if got := charsetOf(map[string]string{"content-type": c[0]}); got != c[1] {
			t.Errorf("charsetOf(%q) = %q, want %q", c[0], got, c[1])
		}
	}
}

// reset clears the global caches so tests don't leak state into each other.
func reset(t *testing.T) {
	t.Helper()
	ClearCache()
	CloseIdleConnections()
	t.Cleanup(CloseIdleConnections)
}

// serve runs a raw keep-alive HTTP server and returns its host:port. respond is
// called once per request; conns is incremented once per accepted connection so
// tests can assert on connection reuse.
func serve(t *testing.T, conns *int32, respond func(req string) []byte) string {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ln.Close() })
	go func() {
		for {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			if conns != nil {
				atomic.AddInt32(conns, 1)
			}
			go func() {
				defer conn.Close()
				buf := make([]byte, 8192)
				for {
					conn.SetReadDeadline(time.Now().Add(2 * time.Second))
					n, err := conn.Read(buf)
					if n > 0 {
						if _, werr := conn.Write(respond(string(buf[:n]))); werr != nil {
							return
						}
					}
					if err != nil {
						return
					}
				}
			}()
		}
	}()
	return ln.Addr().String()
}

func TestRequestHTTP(t *testing.T) {
	reset(t)
	var gz bytes.Buffer
	zw := gzip.NewWriter(&gz)
	zw.Write([]byte("<h1>ok</h1>"))
	zw.Close()

	addr := serve(t, nil, func(req string) []byte {
		if !strings.HasPrefix(req, "GET /page HTTP/1.1\r\n") {
			return []byte("HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
		}
		head := fmt.Sprintf("HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"+
			"Content-Encoding: gzip\r\nContent-Length: %d\r\n\r\n", gz.Len())
		return append([]byte(head), gz.Bytes()...)
	})

	resp, err := mustParse(t, "http://"+addr+"/page").Get()
	if err != nil {
		t.Fatal(err)
	}
	if resp.Text != "<h1>ok</h1>" || resp.ContentType != "text/html" {
		t.Fatalf("got %+v", resp)
	}
}

func TestRequestChunkedAndPost(t *testing.T) {
	reset(t)
	addr := serve(t, nil, func(req string) []byte {
		if !strings.HasPrefix(req, "POST ") || !strings.HasSuffix(req, "a=1") {
			return []byte("HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
		}
		return []byte("HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" +
			"3\r\nyes\r\n0\r\n\r\n")
	})
	resp, err := mustParse(t, "http://"+addr+"/form").Post("a=1")
	if err != nil {
		t.Fatal(err)
	}
	if resp.Text != "yes" {
		t.Fatalf("post body = %q", resp.Text)
	}
}

// A fully-framed response leaves the connection parked, so a second request to
// the same origin reuses it instead of opening a new one.
func TestKeepAliveReuse(t *testing.T) {
	reset(t)
	var conns int32
	addr := serve(t, &conns, func(string) []byte {
		return []byte("HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi")
	})
	for i := 0; i < 3; i++ {
		u := mustParse(t, fmt.Sprintf("http://%s/p%d", addr, i))
		if resp, err := u.Get(); err != nil || resp.Text != "hi" {
			t.Fatalf("get %d: %+v %v", i, resp, err)
		}
	}
	if got := atomic.LoadInt32(&conns); got != 1 {
		t.Errorf("server accepted %d connections, want 1 (keep-alive reuse)", got)
	}
}

// A parked connection whose peer has gone away is retried once on a fresh one.
func TestStalePooledConnectionRetried(t *testing.T) {
	reset(t)
	var conns int32
	addr := serve(t, &conns, func(string) []byte {
		return []byte("HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi")
	})
	u := mustParse(t, "http://"+addr+"/p")
	if _, err := u.Get(); err != nil {
		t.Fatal(err)
	}
	// Kill the parked socket behind the pool's back, as a keep-alive timeout
	// on the server would.
	poolMu.Lock()
	for _, list := range pool {
		for _, pc := range list {
			pc.conn.Close()
		}
	}
	poolMu.Unlock()

	resp, err := mustParse(t, "http://"+addr+"/q").Get()
	if err != nil || resp.Text != "hi" {
		t.Fatalf("retry after stale conn: %+v %v", resp, err)
	}
	if got := atomic.LoadInt32(&conns); got != 2 {
		t.Errorf("server accepted %d connections, want 2", got)
	}
}

func TestRedirect(t *testing.T) {
	reset(t)
	target := serve(t, nil, func(string) []byte {
		return []byte("HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\ndone")
	})
	from := serve(t, nil, func(string) []byte {
		return []byte("HTTP/1.1 302 Found\r\nLocation: http://" + target + "/end\r\nContent-Length: 0\r\n\r\n")
	})
	u := mustParse(t, "http://"+from+"/start")
	resp, err := u.Get()
	if err != nil {
		t.Fatal(err)
	}
	if resp.Text != "done" {
		t.Fatalf("redirect body = %q", resp.Text)
	}
	// The URL follows in place, so callers resolve relative resources against
	// the host that actually served the content.
	if want := "http://" + target + "/end"; u.String() != want {
		t.Errorf("post-redirect URL = %q, want %q", u.String(), want)
	}
}

// A remote server must not be able to redirect us into the local filesystem.
func TestRedirectToFileBlocked(t *testing.T) {
	reset(t)
	addr := serve(t, nil, func(string) []byte {
		return []byte("HTTP/1.1 302 Found\r\nLocation: file:///etc/passwd\r\nContent-Length: 0\r\n\r\n")
	})
	if _, err := mustParse(t, "http://"+addr+"/start").Get(); err == nil ||
		!strings.Contains(err.Error(), "local file") {
		t.Fatalf("err = %v, want a blocked-redirect error", err)
	}
}

func TestRedirectLoop(t *testing.T) {
	reset(t)
	var addr string
	addr = serve(t, nil, func(string) []byte {
		return []byte("HTTP/1.1 302 Found\r\nLocation: http://" + addr + "/loop\r\nContent-Length: 0\r\n\r\n")
	})
	if _, err := mustParse(t, "http://"+addr+"/loop").Get(); !errors.Is(err, ErrTooManyRedirects) {
		t.Fatalf("err = %v, want ErrTooManyRedirects", err)
	}
}

func TestCacheRoundTrip(t *testing.T) {
	reset(t)
	var hits int32
	addr := serve(t, nil, func(string) []byte {
		atomic.AddInt32(&hits, 1)
		return []byte("HTTP/1.1 200 OK\r\nCache-Control: max-age=60\r\nContent-Length: 2\r\n\r\nhi")
	})
	u := mustParse(t, "http://"+addr+"/cached")
	for i := 0; i < 2; i++ {
		if resp, err := u.Get(); err != nil || resp.Text != "hi" {
			t.Fatalf("get %d: %+v %v", i, resp, err)
		}
	}
	if got := atomic.LoadInt32(&hits); got != 1 {
		t.Errorf("server hit %d times, want 1 (cached)", got)
	}

	// Reload bypasses the cache and asks intermediaries to revalidate.
	var sawNoCache bool
	addr2 := serve(t, nil, func(req string) []byte {
		sawNoCache = strings.Contains(req, "Cache-Control: no-cache")
		return []byte("HTTP/1.1 200 OK\r\nCache-Control: max-age=60\r\nContent-Length: 2\r\n\r\nyo")
	})
	u2 := mustParse(t, "http://"+addr2+"/cached")
	if _, err := u2.Get(); err != nil {
		t.Fatal(err)
	}
	if _, err := u2.Reload(); err != nil {
		t.Fatal(err)
	}
	if !sawNoCache {
		t.Error("Reload did not send Cache-Control: no-cache")
	}
}

func TestNoStoreNotCached(t *testing.T) {
	reset(t)
	var hits int32
	addr := serve(t, nil, func(string) []byte {
		atomic.AddInt32(&hits, 1)
		return []byte("HTTP/1.1 200 OK\r\nCache-Control: no-store, max-age=60\r\nContent-Length: 2\r\n\r\nhi")
	})
	u := mustParse(t, "http://"+addr+"/nostore")
	for i := 0; i < 2; i++ {
		if _, err := u.Get(); err != nil {
			t.Fatal(err)
		}
	}
	if got := atomic.LoadInt32(&hits); got != 2 {
		t.Errorf("server hit %d times, want 2 (no-store)", got)
	}
}

func TestOversizedBodyRejected(t *testing.T) {
	reset(t)
	addr := serve(t, nil, func(string) []byte {
		return []byte("HTTP/1.1 200 OK\r\nContent-Length: 999999999999\r\n\r\nhi")
	})
	if _, err := mustParse(t, "http://"+addr+"/big").Get(); !errors.Is(err, ErrBodyTooLarge) {
		t.Fatalf("err = %v, want ErrBodyTooLarge", err)
	}
}

func writeFile(path, content string) error {
	return os.WriteFile(path, []byte(content), 0o644)
}
