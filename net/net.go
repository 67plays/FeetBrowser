// Package net is the "from scratch" transport layer for FeetBrowser: raw
// sockets speaking HTTP/1.1, TLS for https, plus support for data:, file: and
// view-source: URLs.
//
// Nothing here uses net/http — connections are dialed directly and the
// request/response framing is written and parsed by hand. It is a port of
// feetbrowser/net.py and keeps that file's behavior, including its keep-alive
// connection pool, address cache, response cache and body-size caps.
package net

import (
	"bytes"
	"compress/flate"
	"compress/gzip"
	"compress/zlib"
	"crypto/tls"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode/utf8"
)

// DefaultHeaders are sent with every http(s) request.
var DefaultHeaders = [][2]string{
	{"User-Agent", "FeetBrowser/0.1.1 (https://github.com/JuiceyDew/FeetBrowser)"},
	{"Accept", "text/html,application/xhtml+xml,text/css,*/*;q=0.8"},
	{"Accept-Encoding", "gzip, deflate"},
}

const (
	// MaxRedirects bounds a redirect chain before it is treated as a loop.
	MaxRedirects = 10
	// CacheMaxSize is the number of entries the in-process cache holds.
	CacheMaxSize = 1000

	// maxBodyBytes is a hard cap on any single response (headers + body) read
	// from the network or from a local file, and on the decompressed size of a
	// gzip/deflate payload. Keeps a hostile peer from exhausting memory with an
	// unbounded stream or a decompression bomb.
	maxBodyBytes = 64 * 1024 * 1024

	connectTimeout = 20 * time.Second
	readTimeout    = 30 * time.Second
)

// knownSchemes lets the parser distinguish a bare host from a scheme-less
// string like "example.com:8080".
var knownSchemes = map[string]bool{"http": true, "https": true, "file": true, "data": true}

var (
	// ErrTooManyRedirects is returned when a redirect chain exceeds MaxRedirects.
	ErrTooManyRedirects = errors.New("too many redirects")
	// ErrBodyTooLarge is returned when a response exceeds maxBodyBytes.
	ErrBodyTooLarge = errors.New("HTTP response body too large")
	// ErrHeadersTooLarge is returned when the header block exceeds maxBodyBytes.
	ErrHeadersTooLarge = errors.New("HTTP response headers too large")
)

// Response is the result of a fetch. Text is the decoded body; Bytes is the
// undecoded body and is only populated for byte requests (images).
type Response struct {
	Headers     map[string]string // lower-cased keys
	Text        string
	Bytes       []byte
	ContentType string
}

// URL is a parsed URL plus the logic to fetch it.
type URL struct {
	Raw         string
	Scheme      string
	Host        string
	Port        int
	Path        string
	Fragment    string
	DataPayload string
	ViewSource  bool
}

// Parse parses a URL string. Anything without a known scheme is treated as a
// bare host/path (with optional port) and assumed to be https.
func Parse(raw string) (*URL, error) {
	u := &URL{Raw: raw}
	s := raw

	if strings.HasPrefix(s, "view-source:") {
		u.ViewSource = true
		s = strings.TrimPrefix(s, "view-source:")
		u.Raw = s
	}

	if !strings.Contains(s, "://") {
		head := strings.ToLower(strings.SplitN(s, ":", 2)[0])
		if !knownSchemes[head] {
			s = "https://" + s
		}
	}

	scheme, rest, ok := strings.Cut(s, ":")
	if !ok {
		return nil, fmt.Errorf("unsupported URL scheme: %q", s)
	}
	u.Scheme = strings.ToLower(scheme)

	switch u.Scheme {
	case "http", "https":
		if err := u.parseHTTP(rest); err != nil {
			return nil, err
		}
	case "file":
		// file:///path, file://host/path (host ignored) or file:/path
		if strings.HasPrefix(rest, "//") {
			remainder := rest[2:]
			if slash := strings.Index(remainder, "/"); slash != -1 {
				u.Path = remainder[slash:]
			} else {
				u.Path = "/"
			}
		} else if rest != "" {
			u.Path = rest
		} else {
			u.Path = "/"
		}
	case "data":
		u.DataPayload = rest
	default:
		// Unknown scheme: parse it anyway so extensions can intercept it
		// through the toes handle hook. Fetching it still fails loudly.
		if strings.HasPrefix(rest, "//") {
			if len(rest) > 2 {
				if err := u.parseHTTP(rest); err != nil {
					return nil, err
				}
			} else {
				// Empty host (e.g. toehub://): keep host empty so an extension
				// scheme can still route on it.
				u.Path = "/"
			}
		} else {
			u.Path = rest
			if u.Path == "" {
				u.Path = "/"
			}
		}
	}
	return u, nil
}

func (u *URL) parseHTTP(rest string) error {
	// rest looks like //host[:port]/path?query#frag
	rest = strings.TrimPrefix(rest, "//")
	if i := strings.Index(rest, "#"); i != -1 {
		u.Fragment = rest[i+1:]
		rest = rest[:i]
	}
	authority := rest
	if i := strings.Index(rest, "/"); i != -1 {
		authority, u.Path = rest[:i], rest[i:]
	} else {
		u.Path = "/"
	}
	if i := strings.LastIndex(authority, "@"); i != -1 {
		authority = authority[i+1:] // drop userinfo
	}

	var port string
	switch {
	case strings.HasPrefix(authority, "["): // IPv6 literal: [::1]:8080
		end := strings.Index(authority, "]")
		if end == -1 {
			return fmt.Errorf("malformed IPv6 address in URL: %q", authority)
		}
		u.Host = authority[1:end]
		if tail := authority[end+1:]; strings.HasPrefix(tail, ":") {
			port = tail[1:]
		}
	case strings.Contains(authority, ":"):
		i := strings.LastIndex(authority, ":")
		u.Host, port = authority[:i], authority[i+1:]
	default:
		u.Host = authority
	}

	if u.Host == "" {
		return fmt.Errorf("missing host in URL: %q", authority)
	}
	u.Host = strings.ToLower(u.Host)
	if port == "" {
		if u.Scheme == "https" {
			u.Port = 443
		} else {
			u.Port = 80
		}
	} else {
		n, err := strconv.Atoi(port)
		if err != nil {
			return fmt.Errorf("invalid port in URL: %q", port)
		}
		u.Port = n
	}
	if u.Port <= 0 || u.Port > 65535 {
		return fmt.Errorf("port out of range in URL: %d", u.Port)
	}
	return nil
}

// Resolve resolves a possibly-relative URL against u.
func (u *URL) Resolve(ref string) (*URL, error) {
	if strings.Contains(ref, "://") || strings.HasPrefix(ref, "data:") ||
		strings.HasPrefix(ref, "view-source:") {
		return Parse(ref)
	}
	if strings.HasPrefix(ref, "//") {
		return Parse(u.Scheme + ":" + ref)
	}
	if strings.HasPrefix(ref, "#") {
		clone, err := Parse(u.String())
		if err != nil {
			return nil, err
		}
		clone.Fragment = ref[1:]
		return clone, nil
	}
	if !strings.HasPrefix(ref, "/") {
		// Relative to the current directory.
		dir := u.Path
		if i := strings.LastIndex(dir, "/"); i != -1 {
			dir = dir[:i]
		} else {
			dir = ""
		}
		ref = dir + "/" + ref
	}
	// Normalize ../ and ./
	var parts []string
	for _, seg := range strings.Split(ref, "/") {
		switch {
		case seg == ".." && len(parts) > 0:
			parts = parts[:len(parts)-1]
		case seg == "" || seg == "." || seg == "..":
		default:
			parts = append(parts, seg)
		}
	}
	next, err := Parse(fmt.Sprintf("%s://%s/%s", u.Scheme, u.Netloc(), strings.Join(parts, "/")))
	if err != nil {
		return nil, err
	}
	next.ViewSource = u.ViewSource
	return next, nil
}

// Netloc returns host[:port], bracketing IPv6 literals and omitting the
// default port for the scheme.
func (u *URL) Netloc() string {
	host := u.Host
	if strings.Contains(host, ":") {
		host = "[" + host + "]"
	}
	if u.Port == 0 || u.Port == 80 || u.Port == 443 {
		return host
	}
	return host + ":" + strconv.Itoa(u.Port)
}

// adopt makes u look like other. Used to follow HTTP redirects in place so a
// URL reports the location it actually fetched content from. Callers resolve
// relative URLs against the page URL and must not keep the pre-redirect host,
// or every relative resource (images, stylesheets, scripts) is fetched from the
// wrong server.
func (u *URL) adopt(other *URL) {
	u.Scheme = other.Scheme
	u.Host = other.Host
	u.Port = other.Port
	u.Path = other.Path
	u.Fragment = other.Fragment
	u.ViewSource = other.ViewSource
	u.Raw = other.Raw
	if other.DataPayload != "" {
		u.DataPayload = other.DataPayload
	}
}

func (u *URL) String() string {
	prefix := ""
	if u.ViewSource {
		prefix = "view-source:"
	}
	switch u.Scheme {
	case "http", "https":
		frag := ""
		if u.Fragment != "" {
			frag = "#" + u.Fragment
		}
		return fmt.Sprintf("%s%s://%s%s%s", prefix, u.Scheme, u.Netloc(), u.Path, frag)
	case "file":
		return prefix + "file://" + u.Path
	case "data":
		return prefix + "data:" + u.DataPayload
	}
	return u.Raw
}

// -- Fetching ---------------------------------------------------------------

// Options controls a fetch. The zero value is a plain cached GET.
type Options struct {
	// Payload, when non-nil, makes the request a POST with this
	// application/x-www-form-urlencoded body.
	Payload *string
	// Raw skips text decoding and returns the body as bytes (used by image
	// fetches). It is threaded through redirects so an image served from a
	// redirect location still comes back undecoded.
	Raw bool
	// Refresh bypasses the response cache so callers (e.g. the reload button)
	// always get a fresh copy.
	Refresh bool

	redirectsLeft int
}

// Get fetches the URL and decodes the body as text.
func (u *URL) Get() (*Response, error) { return u.Request(Options{}) }

// Post sends payload as an application/x-www-form-urlencoded body.
func (u *URL) Post(payload string) (*Response, error) {
	return u.Request(Options{Payload: &payload})
}

// GetBytes fetches the URL without decoding the body, for binary data such as
// images.
func (u *URL) GetBytes() (*Response, error) { return u.Request(Options{Raw: true}) }

// Reload fetches the URL bypassing the response cache.
func (u *URL) Reload() (*Response, error) { return u.Request(Options{Refresh: true}) }

// Request fetches the URL under opts.
func (u *URL) Request(opts Options) (*Response, error) {
	if opts.redirectsLeft == 0 {
		opts.redirectsLeft = MaxRedirects
	}
	switch u.Scheme {
	case "file":
		return u.requestFile(opts.Raw)
	case "data":
		return u.requestData(opts.Raw)
	}
	return u.requestHTTP(opts)
}

// RequestImpersonated is the counterpart of net.py's request_impersonated().
//
// The Python version shells out to curl_cffi, which is built against BoringSSL
// and reproduces Chrome's TLS + HTTP/2 + header fingerprints, so sites like
// Google serve the real application instead of an "enable JavaScript" stub. Go
// has no stdlib equivalent — crypto/tls emits a Go-shaped ClientHello that such
// sites fingerprint the same way our raw stack is fingerprinted, and matching
// Chrome needs a third-party library (uTLS).
//
// Rather than pretend, this falls back to a normal Request, exactly as the
// Python version does when curl_cffi is not installed.
func (u *URL) RequestImpersonated() (*Response, error) { return u.Get() }

func (u *URL) requestFile(raw bool) (*Response, error) {
	// Unlike Python's open(), os.Open succeeds on a directory and only fails on
	// read, so the directory case is checked up front.
	if info, err := os.Stat(u.Path); err == nil && info.IsDir() {
		entries, err := os.ReadDir(u.Path)
		if err != nil {
			return textResponse(fmt.Sprintf("<h1>Cannot open file</h1><p>%s</p>", err),
				"text/html", raw), nil
		}
		names := make([]string, 0, len(entries))
		for _, e := range entries {
			names = append(names, e.Name())
		}
		sort.Strings(names)
		var links strings.Builder
		for _, name := range names {
			full := filepath.Join(u.Path, name)
			fmt.Fprintf(&links, `<li><a href="file://%s">%s</a></li>`,
				(&url.URL{Path: full}).EscapedPath(), name)
		}
		return textResponse(fmt.Sprintf("<h1>Index of %s</h1><ul>%s</ul>", u.Path, links.String()),
			"text/html", raw), nil
	}

	f, err := os.Open(u.Path)
	if err != nil {
		return textResponse(fmt.Sprintf("<h1>Cannot open file</h1><p>%s</p>", err),
			"text/html", raw), nil
	}
	defer f.Close()

	data, err := io.ReadAll(io.LimitReader(f, maxBodyBytes+1))
	if err != nil {
		return textResponse(fmt.Sprintf("<h1>Cannot open file</h1><p>%s</p>", err),
			"text/html", raw), nil
	}
	if len(data) > maxBodyBytes {
		return textResponse(fmt.Sprintf("<h1>File too large to open</h1><p>%s</p>", u.Path),
			"text/html", raw), nil
	}

	ctype := "text/plain"
	switch strings.ToLower(filepath.Ext(u.Path)) {
	case ".html", ".htm":
		ctype = "text/html"
	}
	resp := &Response{Headers: map[string]string{}, ContentType: ctype}
	if raw {
		resp.Bytes = data
	} else {
		resp.Text = toValidUTF8(data)
	}
	return resp, nil
}

func (u *URL) requestData(raw bool) (*Response, error) {
	// data:[<mediatype>][;base64],<data>
	meta, data, _ := strings.Cut(u.DataPayload, ",")
	ctype := strings.SplitN(meta, ";", 2)[0]
	if ctype == "" {
		ctype = "text/plain"
	}
	var decoded []byte
	if strings.HasSuffix(meta, ";base64") {
		b, err := base64.StdEncoding.DecodeString(data)
		if err != nil {
			return nil, fmt.Errorf("invalid base64 in data: URL: %w", err)
		}
		decoded = b
	} else {
		unescaped, err := url.QueryUnescape(data)
		if err != nil {
			unescaped = data // best effort, matching the tolerant Python behavior
		}
		decoded = []byte(unescaped)
	}
	resp := &Response{Headers: map[string]string{}, ContentType: ctype}
	if raw {
		resp.Bytes = decoded
	} else {
		resp.Text = toValidUTF8(decoded)
	}
	return resp, nil
}

func (u *URL) newConnection() (net.Conn, error) {
	conn, err := dial(u.Host, u.Port)
	if err != nil {
		return nil, err
	}
	if u.Scheme == "https" {
		// ServerName stays the hostname even when dial used a cached IP.
		tlsConn := tls.Client(conn, &tls.Config{ServerName: u.Host})
		if err := tlsConn.SetDeadline(time.Now().Add(connectTimeout)); err != nil {
			conn.Close()
			return nil, err
		}
		if err := tlsConn.Handshake(); err != nil {
			conn.Close()
			return nil, err
		}
		return tlsConn, nil
	}
	return conn, nil
}

func (u *URL) requestHTTP(opts Options) (*Response, error) {
	// Two documents that differ only by fragment are the same resource.
	cacheKey := strings.SplitN(u.String(), "#", 2)[0]
	if !opts.Refresh && opts.Payload == nil {
		if hit, ok := cacheGet(cacheKey); ok {
			return hit, nil
		}
	}

	method := "GET"
	var bodyBytes []byte
	headers := append([][2]string(nil), DefaultHeaders...)
	headers = append(headers,
		[2]string{"Host", u.Netloc()}, // brackets IPv6, includes the port
		[2]string{"Connection", "keep-alive"})
	if opts.Refresh {
		// Ask intermediaries to revalidate too.
		headers = append(headers, [2]string{"Cache-Control", "no-cache"})
	}
	if opts.Payload != nil {
		method = "POST"
		bodyBytes = []byte(*opts.Payload)
		headers = append(headers,
			[2]string{"Content-Type", "application/x-www-form-urlencoded"},
			[2]string{"Content-Length", strconv.Itoa(len(bodyBytes))})
	}

	var req bytes.Buffer
	fmt.Fprintf(&req, "%s %s HTTP/1.1\r\n", method, u.Path)
	for _, h := range headers {
		fmt.Fprintf(&req, "%s: %s\r\n", h[0], h[1])
	}
	req.WriteString("\r\n")
	req.Write(bodyBytes)
	wire := req.Bytes()

	attempt := func(conn net.Conn) (int, map[string]string, []byte, bool, error) {
		// Bound the exchange so a dead or hostile peer can't hang the UI.
		if err := conn.SetDeadline(time.Now().Add(readTimeout)); err != nil {
			return 0, nil, nil, false, err
		}
		if _, err := conn.Write(wire); err != nil {
			return 0, nil, nil, false, err
		}
		return readResponse(conn)
	}

	origin := originKey{u.Scheme, u.Host, u.Port}
	conn := poolTake(origin)
	pooled := conn != nil
	var err error
	if conn == nil {
		if conn, err = u.newConnection(); err != nil {
			return nil, err
		}
	}

	status, respHeaders, body, reusable, err := attempt(conn)
	if err != nil {
		conn.Close()
		if !pooled {
			return nil, err
		}
		// The parked connection went stale (the peer closed it, e.g. an
		// HTTP/1.0 server or a keep-alive timeout): retry once on a fresh
		// connection rather than failing the request.
		if conn, err = u.newConnection(); err != nil {
			return nil, err
		}
		if status, respHeaders, body, reusable, err = attempt(conn); err != nil {
			conn.Close()
			return nil, err
		}
	}
	if reusable {
		poolPark(origin, conn)
	} else {
		// Body was read to EOF (no framing), so the connection cannot be
		// reused — it is already closed by the peer.
		conn.Close()
	}

	// Redirects.
	if isRedirect(status) {
		if location, ok := respHeaders["location"]; ok {
			if opts.redirectsLeft <= 1 {
				return nil, ErrTooManyRedirects
			}
			next, err := u.Resolve(location)
			if err != nil {
				return nil, err
			}
			followOpts := opts
			followOpts.redirectsLeft = opts.redirectsLeft - 1
			if status != 307 && status != 308 {
				followOpts.Payload = nil
			}
			switch next.Scheme {
			case "http", "https":
				// Follow in place so u reflects the URL we actually got content
				// from (see adopt); callers resolve relative URLs (img/style/
				// script src) against the page URL, so a bare `google.com` that
				// redirects to `www.google.com` must report the final host or
				// its image URLs resolve to a host that serves HTML instead of
				// the image.
				u.adopt(next)
				return u.requestHTTP(followOpts)
			case "file":
				// Never let a remote server redirect into the local filesystem:
				// that would be an arbitrary local-file read with no user
				// interaction.
				return nil, fmt.Errorf("blocked redirect to local file: %s", next)
			}
			return next.Request(followOpts)
		}
	}

	// Decode transfer-encoding and content-encoding.
	body = decodeBody(body, respHeaders)
	contentType := "text/html"
	if ct, ok := respHeaders["content-type"]; ok {
		contentType = strings.TrimSpace(strings.SplitN(ct, ";", 2)[0])
	}

	if opts.Raw {
		return &Response{Headers: respHeaders, Bytes: body, ContentType: contentType}, nil
	}

	result := &Response{
		Headers:     respHeaders,
		Text:        decodeText(body, charsetOf(respHeaders)),
		ContentType: contentType,
	}

	// Cache if allowed.
	cc := respHeaders["cache-control"]
	if opts.Payload == nil && status == 200 && !strings.Contains(cc, "no-store") {
		if maxAge, ok := maxAgeOf(cc); ok {
			cachePut(cacheKey, time.Now().Add(maxAge), result)
		}
	}
	return result, nil
}

func isRedirect(status int) bool {
	switch status {
	case 301, 302, 303, 307, 308:
		return true
	}
	return false
}

// readResponse reads a full HTTP response, honoring Content-Length / chunked
// framing so a keep-alive peer that does not close the socket doesn't make us
// wait for EOF (which could stall for the socket timeout).
//
// The returned reusable flag is true only when the body was read to a clean
// frame boundary, so the caller may park the socket for another request. A
// read-to-EOF body (no framing hints) means the peer closed the connection, so
// it cannot be reused.
func readResponse(r io.Reader) (int, map[string]string, []byte, bool, error) {
	var buf []byte
	scratch := make([]byte, 65536)

	// readMore appends one read's worth of bytes, stopping at the body cap.
	// It reports how many bytes were added; 0 means "no more data".
	readMore := func(n int) int {
		if len(buf) >= maxBodyBytes {
			return 0
		}
		if n > maxBodyBytes-len(buf) {
			n = maxBodyBytes - len(buf)
		}
		if n > len(scratch) {
			n = len(scratch)
		}
		if n <= 0 {
			return 0
		}
		got, err := r.Read(scratch[:n])
		if got > 0 {
			buf = append(buf, scratch[:got]...)
		}
		if err != nil {
			return 0
		}
		return got
	}

	// Read the header block (terminated by an empty line).
	for !bytes.Contains(buf, []byte("\r\n\r\n")) {
		if readMore(len(scratch)) == 0 {
			break
		}
	}
	if !bytes.Contains(buf, []byte("\r\n\r\n")) {
		return 0, nil, nil, false, ErrHeadersTooLarge
	}
	head, body, _ := bytes.Cut(buf, []byte("\r\n\r\n"))
	headers := parseHeaders(head)

	reusable := false
	switch {
	case strings.EqualFold(headers["transfer-encoding"], "chunked"):
		// Read until the terminating zero-length chunk.
		for !bytes.Contains(buf, []byte("\r\n0\r\n\r\n")) {
			if readMore(len(scratch)) == 0 {
				break
			}
		}
		reusable = bytes.Contains(buf, []byte("\r\n0\r\n\r\n"))
	case headers["content-length"] != "":
		remaining, err := strconv.Atoi(headers["content-length"])
		if err != nil {
			remaining = -1
		} else {
			if remaining > maxBodyBytes {
				return 0, nil, nil, false, ErrBodyTooLarge
			}
			remaining -= len(body)
		}
		for remaining > 0 {
			n := readMore(remaining)
			if n == 0 {
				break
			}
			remaining -= n
		}
		reusable = remaining <= 0
	default:
		// No framing hints: read until EOF (the deadline guards stalls).
		for readMore(len(scratch)) > 0 {
		}
	}

	head, body, _ = bytes.Cut(buf, []byte("\r\n\r\n"))
	// Status line is: HTTP/x.y <code> <explanation>
	statusLine := string(bytes.SplitN(head, []byte("\r\n"), 2)[0])
	parts := strings.SplitN(statusLine, " ", 3)
	if len(parts) < 2 {
		return 0, nil, nil, false, fmt.Errorf("malformed status line: %q", statusLine)
	}
	status, err := strconv.Atoi(parts[1])
	if err != nil {
		return 0, nil, nil, false, fmt.Errorf("malformed status line: %q", statusLine)
	}
	return status, headers, body, reusable, nil
}

func parseHeaders(head []byte) map[string]string {
	headers := map[string]string{}
	lines := strings.Split(string(head), "\r\n")
	for _, line := range lines[1:] { // skip the status line
		if line == "" {
			continue
		}
		k, v, _ := strings.Cut(line, ":")
		headers[strings.ToLower(strings.TrimSpace(k))] = strings.TrimSpace(v)
	}
	return headers
}

func decodeBody(body []byte, headers map[string]string) []byte {
	if strings.EqualFold(headers["transfer-encoding"], "chunked") {
		body = dechunk(body)
	}
	switch strings.ToLower(headers["content-encoding"]) {
	case "gzip":
		zr, err := gzip.NewReader(bytes.NewReader(body))
		if err != nil {
			return body
		}
		if out, ok := readBounded(zr); ok {
			body = out
		}
	case "deflate":
		// zlib-wrapped is the spec; bare deflate is what some servers send.
		if zr, err := zlib.NewReader(bytes.NewReader(body)); err == nil {
			if out, ok := readBounded(zr); ok {
				return out
			}
		}
		if out, ok := readBounded(flate.NewReader(bytes.NewReader(body))); ok {
			body = out
		}
	}
	return body
}

// readBounded decompresses r, refusing output beyond the body cap so a
// decompression bomb cannot allocate unbounded memory. It reports false when
// the output would exceed the cap or the stream is malformed, in which case the
// caller keeps the undecoded body.
func readBounded(r io.Reader) ([]byte, bool) {
	out, err := io.ReadAll(io.LimitReader(r, maxBodyBytes+1))
	if err != nil || len(out) > maxBodyBytes {
		return nil, false
	}
	return out, true
}

func dechunk(body []byte) []byte {
	var out []byte
	for len(body) > 0 {
		sizeLine, rest, ok := bytes.Cut(body, []byte("\r\n"))
		if !ok {
			break
		}
		body = rest
		field := bytes.TrimSpace(bytes.SplitN(sizeLine, []byte(";"), 2)[0])
		size, err := strconv.ParseInt(string(field), 16, 64)
		if err != nil {
			break
		}
		if size == 0 {
			break
		}
		if int64(len(body)) < size+2 {
			break // truncated chunk
		}
		out = append(out, body[:size]...)
		body = body[size+2:] // skip trailing CRLF
	}
	return out
}

func charsetOf(headers map[string]string) string {
	ctype := headers["content-type"]
	const marker = "charset="
	i := strings.Index(strings.ToLower(ctype), marker)
	if i == -1 {
		return "utf-8"
	}
	value := ctype[i+len(marker):]
	value = strings.SplitN(value, ";", 2)[0]
	value = strings.Trim(strings.TrimSpace(value), `"'`) // servers send charset="utf-8" too
	if value == "" {
		return "utf-8"
	}
	return strings.ToLower(value)
}

// decodeText converts a body to a Go (UTF-8) string. Only the encodings a
// from-scratch browser realistically meets are handled; anything else falls
// back to UTF-8 with replacement characters.
func decodeText(body []byte, charset string) string {
	switch charset {
	case "iso-8859-1", "latin-1", "latin1", "iso8859-1", "windows-1252", "cp1252":
		// windows-1252 differs from latin-1 only in the C1 range; treating it
		// as latin-1 keeps the decoder dependency-free and never fails.
		var sb strings.Builder
		sb.Grow(len(body))
		for _, b := range body {
			sb.WriteRune(rune(b))
		}
		return sb.String()
	default:
		return toValidUTF8(body)
	}
}

func toValidUTF8(body []byte) string {
	if utf8.Valid(body) {
		return string(body)
	}
	return strings.ToValidUTF8(string(body), "\uFFFD")
}

func textResponse(body, ctype string, raw bool) *Response {
	resp := &Response{Headers: map[string]string{}, ContentType: ctype}
	if raw {
		resp.Bytes = []byte(body)
	} else {
		resp.Text = body
	}
	return resp
}

// -- Address cache ----------------------------------------------------------

// Bounded cache of resolved host addresses. Image-heavy pages fetch dozens of
// resources from the same host; without this, every request pays a fresh DNS
// lookup (a blocking, sometimes multi-second round-trip). A failed connect
// drops the entry and falls back to the full resolver. A mutex guards access
// because image fetches run on background goroutines, and a TTL keeps rotated
// DNS from pinning the browser to a stale endpoint forever.
const (
	dnsCacheMax = 512
	dnsTTL      = 300 * time.Second
	poolTTL     = 30 * time.Second

	poolMaxPerOrigin = 4
	poolMaxTotal     = 64
)

type hostPort struct {
	host string
	port int
}

type dnsEntry struct {
	at   time.Time
	addr string // resolved "ip:port", ready to dial
}

var (
	dnsMu    sync.Mutex
	dnsCache = map[hostPort]dnsEntry{}
)

// dial opens a TCP connection, reusing a cached address when one is available
// and falling back to a full resolve (which retries across every address the
// host reports) otherwise.
func dial(host string, port int) (net.Conn, error) {
	key := hostPort{host, port}
	now := time.Now()

	dnsMu.Lock()
	entry, ok := dnsCache[key]
	if ok && now.Sub(entry.at) > dnsTTL {
		delete(dnsCache, key)
		ok = false
	}
	dnsMu.Unlock()

	if ok {
		conn, err := net.DialTimeout("tcp", entry.addr, connectTimeout)
		if err == nil {
			return conn, nil
		}
		// Cached address unreachable (rotated DNS / load balancer): drop it and
		// let the full resolver try every address the host reports.
		dnsMu.Lock()
		if cur, still := dnsCache[key]; still && cur == entry {
			delete(dnsCache, key)
		}
		dnsMu.Unlock()
	}

	conn, err := net.DialTimeout("tcp", net.JoinHostPort(host, strconv.Itoa(port)), connectTimeout)
	if err != nil {
		return nil, err
	}
	// Cache the address the fallback actually connected to, so a repeat request
	// skips the failed attempt.
	if addr := conn.RemoteAddr(); addr != nil {
		dnsMu.Lock()
		if len(dnsCache) < dnsCacheMax {
			dnsCache[key] = dnsEntry{at: time.Now(), addr: addr.String()}
		}
		dnsMu.Unlock()
	}
	return conn, nil
}

// -- Connection pool --------------------------------------------------------

// Bounded pool of idle keep-alive connections, keyed by origin. HTTP/1.1 lets
// one connection serve several requests to the same origin, which skips the
// fresh TCP + TLS handshake each resource used to pay — the most expensive part
// of a fetch. Sockets are parked after a fully-framed response and reclaimed by
// the next request to that origin; a parked socket whose peer already closed it
// is detected and retried once on a fresh connection.
type originKey struct {
	scheme string
	host   string
	port   int
}

type pooledConn struct {
	conn   net.Conn
	parked time.Time
}

var (
	poolMu sync.Mutex
	pool   = map[originKey][]pooledConn{}
)

// poolTake pops the most recently parked idle connection for key, or nil.
// Stale (TTL-expired) connections are closed rather than reused.
func poolTake(key originKey) net.Conn {
	poolMu.Lock()
	defer poolMu.Unlock()
	list := pool[key]
	now := time.Now()
	for len(list) > 0 {
		last := list[len(list)-1]
		list = list[:len(list)-1]
		if now.Sub(last.parked) > poolTTL {
			last.conn.Close()
			continue
		}
		if len(list) == 0 {
			delete(pool, key)
		} else {
			pool[key] = list
		}
		return last.conn
	}
	delete(pool, key)
	return nil
}

// poolPark returns a healthy connection to the idle pool, evicting the oldest
// connection for the same origin when that origin is full, and refusing new
// origins once the pool is full.
func poolPark(key originKey, conn net.Conn) {
	poolMu.Lock()
	defer poolMu.Unlock()
	if _, known := pool[key]; !known && len(pool) >= poolMaxTotal {
		conn.Close()
		return
	}
	list := pool[key]
	if len(list) >= poolMaxPerOrigin {
		list[0].conn.Close()
		list = list[1:]
	}
	// A parked connection has no outstanding deadline; the next user sets one.
	_ = conn.SetDeadline(time.Time{})
	pool[key] = append(list, pooledConn{conn: conn, parked: time.Now()})
}

// CloseIdleConnections closes every parked connection and empties the pool.
func CloseIdleConnections() {
	poolMu.Lock()
	defer poolMu.Unlock()
	for key, list := range pool {
		for _, pc := range list {
			pc.conn.Close()
		}
		delete(pool, key)
	}
}

// -- Response cache ---------------------------------------------------------

// A tiny in-process cache keyed by URL string. Honors a very small subset of
// Cache-Control (max-age). Good enough to avoid re-fetching stylesheets.
type cacheEntry struct {
	expires time.Time
	resp    *Response
}

var (
	cacheMu sync.Mutex
	cache   = map[string]cacheEntry{}
)

func cacheGet(key string) (*Response, bool) {
	cacheMu.Lock()
	defer cacheMu.Unlock()
	entry, ok := cache[key]
	if !ok {
		return nil, false
	}
	if time.Now().After(entry.expires) {
		delete(cache, key)
		return nil, false
	}
	return entry.resp, true
}

func cachePut(key string, expires time.Time, resp *Response) {
	cacheMu.Lock()
	defer cacheMu.Unlock()
	if len(cache) >= CacheMaxSize {
		// Evict expired entries first, then the oldest live one.
		now := time.Now()
		for k, e := range cache {
			if now.After(e.expires) {
				delete(cache, k)
			}
		}
		if len(cache) >= CacheMaxSize {
			oldestKey, first := "", true
			for k, e := range cache {
				if first || e.expires.Before(cache[oldestKey].expires) {
					oldestKey, first = k, false
				}
			}
			if !first {
				delete(cache, oldestKey)
			}
		}
	}
	cache[key] = cacheEntry{expires: expires, resp: resp}
}

// ClearCache empties the in-process response cache.
func ClearCache() {
	cacheMu.Lock()
	defer cacheMu.Unlock()
	cache = map[string]cacheEntry{}
}

func maxAgeOf(cacheControl string) (time.Duration, bool) {
	for _, part := range strings.Split(cacheControl, ",") {
		part = strings.TrimSpace(part)
		if rest, ok := strings.CutPrefix(part, "max-age="); ok {
			secs, err := strconv.Atoi(strings.TrimSpace(rest))
			if err != nil {
				return 0, false
			}
			return time.Duration(secs) * time.Second, true
		}
	}
	return 0, false
}
