package e2e

// A small NNTP client for the real-server test (realserver_test.go): it
// injects articles (POST, IHAVE), polls the group and fetches what the server
// serves. It keeps bytes as they are: a line is what comes before "\r\n" (or
// a bare "\n"), so a CR inside a line stays in it.

import (
	"bufio"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"net"
	"os"
	"strconv"
	"strings"
	"time"
)

type nntpConn struct {
	c        net.Conn
	r        *bufio.Reader
	greeting string
	timeout  time.Duration
}

// dialNNTP connects to addr, over TLS when useTLS (verified against caFile
// when given, else the system roots), and reads the greeting.
func dialNNTP(addr string, useTLS bool, caFile string, timeout time.Duration) (*nntpConn, error) {
	d := &net.Dialer{Timeout: timeout}
	var c net.Conn
	var err error
	if useTLS {
		cfg := &tls.Config{MinVersion: tls.VersionTLS12}
		if host, _, e := net.SplitHostPort(addr); e == nil {
			cfg.ServerName = host
		}
		if caFile != "" {
			pem, e := os.ReadFile(caFile)
			if e != nil {
				return nil, e
			}
			cfg.RootCAs = x509.NewCertPool()
			if !cfg.RootCAs.AppendCertsFromPEM(pem) {
				return nil, fmt.Errorf("no certificate in %s", caFile)
			}
		}
		c, err = tls.DialWithDialer(d, "tcp", addr, cfg)
	} else {
		c, err = d.Dial("tcp", addr)
	}
	if err != nil {
		return nil, err
	}
	n := &nntpConn{c: c, r: bufio.NewReaderSize(c, 64<<10), timeout: timeout}
	n.greeting, err = n.line()
	if err != nil {
		c.Close()
		return nil, fmt.Errorf("reading the greeting: %w", err)
	}
	return n, nil
}

func (n *nntpConn) close() {
	if n == nil || n.c == nil {
		return
	}
	n.c.SetDeadline(time.Now().Add(2 * time.Second))
	fmt.Fprintf(n.c, "QUIT\r\n")
	n.line()
	n.c.Close()
}

// line reads one line without its line end.
func (n *nntpConn) line() (string, error) {
	n.c.SetReadDeadline(time.Now().Add(n.timeout))
	s, err := n.r.ReadString('\n')
	if err != nil {
		return s, err
	}
	s = strings.TrimSuffix(s, "\n")
	return strings.TrimSuffix(s, "\r"), nil
}

func code(status string) int {
	if len(status) < 3 {
		return 0
	}
	c, err := strconv.Atoi(status[:3])
	if err != nil {
		return 0
	}
	return c
}

// cmd sends one command line and reads the status line.
func (n *nntpConn) cmd(format string, args ...any) (int, string, error) {
	n.c.SetWriteDeadline(time.Now().Add(n.timeout))
	if _, err := fmt.Fprintf(n.c, format+"\r\n", args...); err != nil {
		return 0, "", err
	}
	s, err := n.line()
	if err != nil {
		return 0, s, err
	}
	return code(s), s, nil
}

// dotLines reads a multi-line block, un-stuffed, without the terminator.
func (n *nntpConn) dotLines() ([]string, error) {
	var out []string
	for {
		l, err := n.line()
		if err != nil {
			return out, err
		}
		if l == "." {
			return out, nil
		}
		if strings.HasPrefix(l, ".") {
			l = l[1:]
		}
		out = append(out, l)
	}
}

// sendArticle writes lines dot-stuffed, each with CRLF, and the terminator,
// and reads the status line.
func (n *nntpConn) sendArticle(lines []string) (int, string, error) {
	var b strings.Builder
	for _, l := range lines {
		if strings.HasPrefix(l, ".") {
			b.WriteByte('.')
		}
		b.WriteString(l)
		b.WriteString("\r\n")
	}
	b.WriteString(".\r\n")
	n.c.SetWriteDeadline(time.Now().Add(n.timeout))
	if _, err := n.c.Write([]byte(b.String())); err != nil {
		return 0, "", err
	}
	s, err := n.line()
	return code(s), s, err
}

// auth logs in with AUTHINFO USER/PASS (PASS only after 381).
func (n *nntpConn) auth(user, pass string) error {
	c, s, err := n.cmd("AUTHINFO USER %s", user)
	if err != nil {
		return err
	}
	if c == 381 {
		c, s, err = n.cmd("AUTHINFO PASS %s", pass)
		if err != nil {
			return err
		}
	}
	if c != 281 {
		return fmt.Errorf("AUTHINFO: %s", s)
	}
	return nil
}

// capabilities returns the CAPABILITIES lines, or nil and the status line.
func (n *nntpConn) capabilities() ([]string, string) {
	c, s, err := n.cmd("CAPABILITIES")
	if err != nil {
		return nil, "error: " + err.Error()
	}
	if c != 101 {
		return nil, s
	}
	l, _ := n.dotLines()
	return l, s
}

type groupInfo struct {
	count, low, high int64
	status           string
}

func (n *nntpConn) group(name string) (groupInfo, error) {
	c, s, err := n.cmd("GROUP %s", name)
	if err != nil {
		return groupInfo{status: s}, err
	}
	if c != 211 {
		return groupInfo{status: s}, fmt.Errorf("GROUP %s: %s", name, s)
	}
	g := groupInfo{status: s}
	f := strings.Fields(s)
	if len(f) >= 4 {
		g.count, _ = strconv.ParseInt(f[1], 10, 64)
		g.low, _ = strconv.ParseInt(f[2], 10, 64)
		g.high, _ = strconv.ParseInt(f[3], 10, 64)
	}
	return g, nil
}

// ovRow is one overview record: number, Subject, Message-ID.
type ovRow struct {
	num          int64
	subject, mid string
}

// overview lists low-high with OVER, XOVER, or else XHDR Message-ID and
// XHDR Subject; it returns the rows and the command that worked.
func (n *nntpConn) overview(low, high int64) ([]ovRow, string, error) {
	if high < low {
		return nil, "none (empty range)", nil
	}
	for _, verb := range []string{"OVER", "XOVER"} {
		c, s, err := n.cmd("%s %d-%d", verb, low, high)
		if err != nil {
			return nil, verb, err
		}
		if c == 423 || c == 420 {
			return nil, verb, nil
		}
		if c != 224 {
			continue
		}
		lines, err := n.dotLines()
		if err != nil {
			return nil, verb, err
		}
		var rows []ovRow
		for _, l := range lines {
			f := strings.Split(l, "\t")
			if len(f) < 5 {
				continue
			}
			num, _ := strconv.ParseInt(f[0], 10, 64)
			rows = append(rows, ovRow{num: num, subject: f[1], mid: strings.TrimSpace(f[4])})
		}
		_ = s
		return rows, verb, nil
	}
	hdr := func(field string) (map[int64]string, error) {
		c, s, err := n.cmd("XHDR %s %d-%d", field, low, high)
		if err != nil {
			return nil, err
		}
		if c != 221 {
			return nil, fmt.Errorf("XHDR %s: %s", field, s)
		}
		lines, err := n.dotLines()
		m := map[int64]string{}
		for _, l := range lines {
			num, v, _ := strings.Cut(l, " ")
			k, _ := strconv.ParseInt(num, 10, 64)
			m[k] = strings.TrimSpace(v)
		}
		return m, err
	}
	mids, err := hdr("Message-ID")
	if err != nil {
		return nil, "XHDR", err
	}
	subs, _ := hdr("Subject")
	var rows []ovRow
	for k, v := range mids {
		rows = append(rows, ovRow{num: k, mid: v, subject: subs[k]})
	}
	return rows, "XHDR", nil
}

// article fetches ARTICLE spec and splits it into head and body lines.
func (n *nntpConn) article(spec string) (head, body []string, status string, err error) {
	c, s, err := n.cmd("ARTICLE %s", spec)
	if err != nil {
		return nil, nil, s, err
	}
	if c != 220 {
		return nil, nil, s, fmt.Errorf("ARTICLE %s: %s", spec, s)
	}
	lines, err := n.dotLines()
	if err != nil {
		return nil, nil, s, err
	}
	for i, l := range lines {
		if l == "" {
			return lines[:i], lines[i+1:], s, nil
		}
	}
	return lines, nil, s, nil
}
