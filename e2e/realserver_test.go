package e2e

// TestRealServer runs usenet_archiver against a real NNTP server (INN,
// Diablo, NewsCache, OpenUsenetServer, ...): it injects this run's articles
// (the tricky set of e2e_test.go plus a few plain ones) by POST and/or IHAVE,
// waits until the reader lists them, pulls the group with the real CLI and
// reads the mbox back with go-lib-mbox2nntp. It is skipped unless
// UA_REAL_ADDR is set. See REALSERVER.md for the variables and the command.
//
// Failures are of two kinds, and the report says which:
//   - archiver: a pull that does not exit 0 or is not recorded as completed,
//     an article of this run that the reader lists but the mbox does not hold
//     exactly once, or an mbox message that differs from what the server
//     serves for it (ARTICLE);
//   - server: an accepted article that the reader never lists, or one that the
//     server serves with a body or one of our header lines changed (header
//     fields servers maintain, such as Path and Xref, are only reported).
//     UA_REAL_STRICT=0 turns these into report lines.
//
// What the server refuses at injection (441, 437, 436, ...) is a finding
// about the server, reported, never a failure.

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"
)

type rsConfig struct {
	name              string
	addr              string
	group             string
	inject            []string // "post", "ihave"
	postAddr, ihvAddr string
	user, pass        string
	injUser, injPass  string
	tls, injTLS       bool
	ca                string
	wait              time.Duration
	report            string
	strict            bool
	skip              map[string]bool
	timeout           time.Duration
}

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func envBool(k string, def bool) bool {
	switch strings.ToLower(os.Getenv(k)) {
	case "1", "true", "yes", "on":
		return true
	case "0", "false", "no", "off":
		return false
	}
	return def
}

func rsConfigFromEnv(t *testing.T) rsConfig {
	c := rsConfig{
		name:    envOr("UA_REAL_NAME", ""),
		addr:    os.Getenv("UA_REAL_ADDR"),
		group:   envOr("UA_REAL_GROUP", "local.test"),
		user:    os.Getenv("UA_REAL_USER"),
		pass:    os.Getenv("UA_REAL_PASS"),
		tls:     envBool("UA_REAL_TLS", false),
		ca:      os.Getenv("UA_REAL_CA"),
		report:  os.Getenv("UA_REAL_REPORT"),
		strict:  envBool("UA_REAL_STRICT", true),
		skip:    map[string]bool{},
		timeout: 60 * time.Second,
	}
	for _, m := range strings.Split(strings.ToLower(envOr("UA_REAL_INJECT", "post")), ",") {
		switch m = strings.TrimSpace(m); m {
		case "post", "ihave":
			c.inject = append(c.inject, m)
		case "", "none":
		default:
			t.Fatalf("UA_REAL_INJECT=%q: want post, ihave, post,ihave or none", m)
		}
	}
	inj := envOr("UA_REAL_INJECT_ADDR", c.addr)
	c.postAddr = envOr("UA_REAL_POST_ADDR", inj)
	c.ihvAddr = envOr("UA_REAL_IHAVE_ADDR", inj)
	c.injUser = os.Getenv("UA_REAL_INJECT_USER")
	c.injPass = os.Getenv("UA_REAL_INJECT_PASS")
	c.injTLS = envBool("UA_REAL_INJECT_TLS", false)
	w, err := strconv.ParseFloat(envOr("UA_REAL_WAIT", "120"), 64)
	if err != nil || w < 0 {
		t.Fatalf("UA_REAL_WAIT=%q: want seconds", os.Getenv("UA_REAL_WAIT"))
	}
	c.wait = time.Duration(w * float64(time.Second))
	for _, s := range strings.Split(os.Getenv("UA_REAL_SKIP"), ",") {
		if s = strings.TrimSpace(s); s != "" {
			c.skip[s] = true
		}
	}
	return c
}

// tlsFor: the reader address uses UA_REAL_TLS, any other address
// UA_REAL_INJECT_TLS.
func (c rsConfig) tlsFor(addr string) bool {
	if addr == c.addr {
		return c.tls
	}
	return c.injTLS
}

// rsArticle is one article of this run.
type rsArticle struct {
	name, method, id, subject string
	head, body                []string

	result   string // the server's final reply at injection
	accepted bool
	num      int64 // in the group, once listed
	listed   bool
	served   *msg // what ARTICLE returns
}

func (a *rsArticle) key() string { return a.name + "." + a.method }

func (a *rsArticle) lines() []string {
	return append(append(append([]string{}, a.head...), ""), a.body...)
}

func newRunTag() string {
	b := make([]byte, 3)
	rand.Read(b)
	return "r" + time.Now().UTC().Format("20060102T150405") + "-" + hex.EncodeToString(b)
}

// rsArticles builds the run's articles for one injection method: every
// tricky article of e2e_test.go with this run's Message-ID, Subject and Date
// (servers refuse old ones), plus a few plain ones.
func rsArticles(tag, method, group string, now time.Time, skip map[string]bool) []*rsArticle {
	std := []string{"Path: ", "From: ", "Newsgroups: ", "Subject: ", "Date: ", "Message-ID: "}
	type src struct {
		name  string
		extra []string
		body  []string
	}
	var srcs []src
	for _, c := range tricky {
		var extra []string
	next:
		for _, h := range c.a.Headers {
			for _, p := range std {
				if strings.HasPrefix(h, p) {
					continue next
				}
			}
			extra = append(extra, h)
		}
		name := strings.Trim(strings.Split(c.a.MessageID, "@")[0], "<")
		if len(extra) < 2 {
			srcs = append(srcs, src{name, extra, c.a.Body})
			continue
		}
		// one article per extra header line ("fromhdr": "From : x" and a
		// "From nobody ..." line), so that a server refusing one of them
		// still takes the other
		for i, h := range extra {
			n := name
			if i > 0 {
				n = fmt.Sprintf("%s%d", name, i+1)
			}
			srcs = append(srcs, src{n, []string{h}, c.a.Body})
		}
	}
	srcs = append(srcs,
		src{"utf8", nil, []string{"Grüße, привет, ✓ (UTF-8 only)"}},
		src{"folded", []string{"X-UA-Folded: the first part of a folded header field,", "\tand its second line"}, []string{"a folded header"}},
		src{"spaces", nil, []string{"trailing spaces   ", "\ta tab at the start", "", "after an empty line", "   ", "end"}},
		src{"multi", []string{"References: <a.ua-e2e@usenet-archiver.invalid> <b.ua-e2e@usenet-archiver.invalid>"},
			[]string{"a reply", "", "> quoted", ">> deeper", "", "-- ", "signature"}},
	)
	var out []*rsArticle
	for _, s := range srcs {
		if skip[s.name] {
			continue
		}
		a := &rsArticle{name: s.name, method: method}
		a.id = fmt.Sprintf("<%s.%s.%s@usenet-archiver.invalid>", s.name, tag, method)
		a.subject = fmt.Sprintf("ua-e2e %s %s %s", tag, method, s.name)
		if method == "ihave" {
			a.head = append(a.head, "Path: ua-e2e!not-for-mail")
		}
		a.head = append(a.head,
			"From: usenet_archiver e2e <ua-e2e@usenet-archiver.invalid>",
			"Newsgroups: "+group,
			"Subject: "+a.subject,
			"Date: "+now.UTC().Format("Mon, 02 Jan 2006 15:04:05 -0700"))
		a.head = append(a.head, s.extra...)
		a.head = append(a.head, "Message-ID: "+a.id)
		a.body = s.body
		out = append(out, a)
	}
	return out
}

// rsReport collects the report; it is logged and written to UA_REAL_REPORT.
type rsReport struct {
	t      *testing.T
	strict bool
	lines  []string
	fails  []string
}

func (r *rsReport) add(format string, args ...any) {
	r.lines = append(r.lines, fmt.Sprintf(format, args...))
}

// archiver records a failure of usenet_archiver.
func (r *rsReport) archiver(format string, args ...any) {
	s := "archiver: " + fmt.Sprintf(format, args...)
	r.fails = append(r.fails, s)
	r.t.Error(s)
}

// server records a finding about the server that breaks archive fidelity:
// a failure unless UA_REAL_STRICT=0.
func (r *rsReport) server(format string, args ...any) {
	s := "server: " + fmt.Sprintf(format, args...)
	r.fails = append(r.fails, s)
	if r.strict {
		r.t.Error(s)
	}
}

func clip(s string, n int) string {
	if len(s) > n {
		return fmt.Sprintf("%s…(%d bytes)", s[:n], len(s))
	}
	return s
}

func TestRealServer(t *testing.T) {
	if os.Getenv("UA_REAL_ADDR") == "" {
		t.Skip("set UA_REAL_ADDR=host:port (and see REALSERVER.md) to run against a real server")
	}
	cfg := rsConfigFromEnv(t)
	tag := newRunTag()
	now := time.Now()
	rep := &rsReport{t: t, strict: cfg.strict}
	defer func() {
		rep.add("")
		if len(rep.fails) == 0 {
			rep.add("RESULT: no failures")
		} else {
			rep.add("RESULT: %d finding(s) that break archive fidelity:", len(rep.fails))
			for _, f := range rep.fails {
				rep.add("  %s", f)
			}
		}
		text := strings.Join(rep.lines, "\n") + "\n"
		t.Log("\n" + text)
		if cfg.report != "" {
			if err := os.WriteFile(cfg.report, []byte(text), 0o644); err != nil {
				t.Errorf("writing UA_REAL_REPORT: %v", err)
			}
		}
	}()

	label := cfg.name
	if label == "" {
		label = cfg.addr
	}
	rep.add("usenet_archiver real-server run %s at %s", tag, now.UTC().Format(time.RFC3339))
	var how []string
	for _, m := range cfg.inject {
		a := cfg.postAddr
		if m == "ihave" {
			a = cfg.ihvAddr
		}
		how = append(how, m+" to "+a)
	}
	if len(how) == 0 {
		how = []string{"none"}
	}
	rep.add("server: %s; reader %s (tls=%v); group %s; inject: %s; wait %s",
		label, cfg.addr, cfg.tls, cfg.group, strings.Join(how, ", "), cfg.wait)

	// the reader: greeting, CAPABILITIES, GROUP
	rd, err := cfg.reader()
	if err != nil {
		rep.add("reader: %v", err)
		t.Fatalf("reader %s: %v", cfg.addr, err)
	}
	rep.add("greeting: %s", rd.greeting)
	caps, st := rd.capabilities()
	if caps != nil {
		rep.add("CAPABILITIES: %s", strings.Join(caps, " | "))
	} else {
		rep.add("CAPABILITIES: %s", st)
	}
	g0, err := rd.group(cfg.group)
	rd.close()
	if err != nil {
		rep.add("group: %v", err)
		t.Fatalf("%v", err)
	}
	rep.add("group before: %s", g0.status)

	// inject
	var arts []*rsArticle
	for _, m := range cfg.inject {
		as := rsArticles(tag, m, cfg.group, now, cfg.skip)
		addr := cfg.postAddr
		if m == "ihave" {
			addr = cfg.ihvAddr
		}
		cfg.injectAll(rep, m, addr, as)
		arts = append(arts, as...)
	}
	var accepted []*rsArticle
	for _, a := range arts {
		if a.accepted {
			accepted = append(accepted, a)
		}
	}
	if len(accepted) == 0 && len(cfg.inject) > 0 {
		t.Fatalf("the server accepted none of the %d articles", len(arts))
	}

	// wait until the reader lists them
	cfg.waitListed(rep, tag, arts)
	var listed []*rsArticle
	for _, a := range arts {
		switch {
		case a.listed && !a.accepted:
			rep.add("refused at injection but listed in the group: %s (%s)", a.key(), a.result)
		case !a.listed && a.accepted:
			rep.server("%s accepted (%s) but not listed in %s after %s", a.key(), a.result, cfg.group, cfg.wait)
		}
		if a.listed {
			listed = append(listed, a)
		}
	}

	// what the server serves, compared with what was injected
	cfg.fetchServed(rep, listed)
	rep.add("")
	rep.add("server changes (served vs injected):")
	serverDiffs(rep, listed)

	// pull with the CLI
	rep.add("")
	rep.add("pulls (usenet_archiver):")
	dirA := t.TempDir()
	start := now.UTC().Add(-24 * time.Hour).Format("2006-01-02")
	type pullCase struct {
		name, dir string
		args      []string
	}
	cases := []pullCase{
		{"-C 1", dirA, []string{"-C", "1"}},
		{"-C 3", t.TempDir(), []string{"-C", "3"}},
		{"-C 3 --start " + start, t.TempDir(), []string{"-C", "3", "--start", start}},
		{"rerun -C 1 (same mbox)", dirA, []string{"-C", "1"}},
	}
	var firstTotal int
	for i, pc := range cases {
		r := cfg.pull(t, pc.dir, pc.args...)
		got, _ := readMbox(t, r.mbox)
		mine := map[string][]msg{}
		for _, m := range got {
			if k := runKey(m.head, tag); k != "" {
				mine[k] = append(mine[k], m)
			}
		}
		ok := 0
		for _, a := range listed {
			ms := mine[a.key()]
			switch {
			case len(ms) == 0 && a.served == nil:
				rep.add("  pull %s: %s listed but not served (ARTICLE failed above), not in the mbox", pc.name, a.key())
			case len(ms) == 0:
				rep.archiver("pull %s: %s (number %d) missing from the mbox", pc.name, a.key(), a.num)
			case len(ms) > 1:
				rep.archiver("pull %s: %s %d copies in the mbox", pc.name, a.key(), len(ms))
			case a.served == nil:
				ok++ // the server would not serve it to us; nothing to compare with
			default:
				if d := diffMsg(*a.served, ms[0]); d != "" {
					rep.archiver("pull %s: %s differs from what the server serves: %s", pc.name, a.key(), d)
				} else {
					ok++
				}
			}
			delete(mine, a.key())
		}
		for k, ms := range mine {
			rep.add("  pull %s: %d message(s) of this run not listed by the reader: %s", pc.name, len(ms), k)
		}
		if r.exit != 0 || !r.completed {
			rep.archiver("pull %s: exit %d, completed %v; output:\n%s", pc.name, r.exit, r.completed, tail(r.output, 30))
		}
		rep.add("  %-28s exit %d, completed %v, %d messages in the mbox, this run's %d/%d once and identical to the server's",
			pc.name, r.exit, r.completed, len(got), ok, len(listed))
		if i == 0 {
			firstTotal = len(got)
		}
		if i == len(cases)-1 && len(got) != firstTotal {
			rep.archiver("rerun into the same mbox: %d messages, %d after the first run", len(got), firstTotal)
		}
	}
}

func tail(s string, n int) string {
	l := strings.Split(strings.TrimRight(s, "\n"), "\n")
	if len(l) > n {
		l = l[len(l)-n:]
	}
	return strings.Join(l, "\n")
}

// runKey returns "name.method" for a message of this run (by Message-ID,
// else by Subject), or "".
func runKey(head []string, tag string) string {
	id := msgID(head)
	if i := strings.Index(id, "."+tag+"."); i > 0 && strings.HasPrefix(id, "<") {
		name := id[1:i]
		rest := id[i+len(tag)+2:]
		if m, _, ok := strings.Cut(rest, "@"); ok {
			return name + "." + m
		}
	}
	for _, h := range head {
		if v, ok := strings.CutPrefix(h, "Subject: ua-e2e "+tag+" "); ok {
			if m, name, ok := strings.Cut(v, " "); ok {
				return name + "." + m
			}
		}
	}
	return ""
}

func (c rsConfig) reader() (*nntpConn, error) {
	n, err := dialNNTP(c.addr, c.tls, c.ca, c.timeout)
	if err != nil {
		return nil, err
	}
	if code(n.greeting) != 200 && code(n.greeting) != 201 {
		n.c.Close()
		return nil, fmt.Errorf("greeting %q", n.greeting)
	}
	n.cmd("MODE READER")
	if c.user != "" {
		if err := n.auth(c.user, c.pass); err != nil {
			n.close()
			return nil, err
		}
	}
	return n, nil
}

// injectAll offers each article by method ("post" or "ihave") to addr and
// records the reply. A 436 (try later) is retried once; a lost connection is
// reconnected.
func (c rsConfig) injectAll(rep *rsReport, method, addr string, arts []*rsArticle) {
	var n *nntpConn
	defer func() { n.close() }()
	connect := func() error {
		var err error
		n, err = dialNNTP(addr, c.tlsFor(addr), c.ca, c.timeout)
		if err != nil {
			return err
		}
		if method == "post" {
			n.cmd("MODE READER") // innd hands a reader to nnrpd
		}
		// UA_REAL_INJECT_USER, else UA_REAL_USER on the reader's address
		user, pass := c.injUser, c.injPass
		if user == "" && addr == c.addr {
			user, pass = c.user, c.pass
		}
		if user != "" {
			return n.auth(user, pass)
		}
		return nil
	}
	offer := func(a *rsArticle) (int, string, error) {
		var cd int
		var s string
		var err error
		if method == "post" {
			cd, s, err = n.cmd("POST")
			if err != nil || cd != 340 {
				return cd, s, err
			}
		} else {
			cd, s, err = n.cmd("IHAVE %s", a.id)
			if err != nil || cd != 335 {
				return cd, s, err
			}
		}
		return n.sendArticle(a.lines())
	}
	rep.add("")
	if err := connect(); err != nil {
		rep.add("inject %s @ %s: %v", method, addr, err)
		for _, a := range arts {
			a.result = "not offered: " + err.Error()
		}
		rep.server("inject %s @ %s: %v", method, addr, err)
		return
	}
	rep.add("inject %s @ %s: greeting %s", method, addr, n.greeting)
	want := 240
	if method == "ihave" {
		want = 235
	}
	var refused []string
	acc := 0
	for _, a := range arts {
		for try := 0; ; try++ {
			cd, s, err := offer(a)
			if err != nil {
				a.result = fmt.Sprintf("connection lost (%v) after %q", err, s)
				n.c.Close()
				if e := connect(); e != nil {
					rep.add("  reconnect: %v", e)
					n = nil
				}
				break
			}
			a.result = s
			if cd == 436 && try == 0 {
				time.Sleep(3 * time.Second)
				continue
			}
			a.accepted = cd == want
			break
		}
		if a.accepted {
			acc++
		} else {
			refused = append(refused, fmt.Sprintf("%s: %s", a.name, a.result))
		}
		if n == nil {
			break
		}
	}
	for _, a := range arts {
		if a.result == "" {
			a.result = "not offered: no connection"
			refused = append(refused, fmt.Sprintf("%s: %s", a.name, a.result))
		}
	}
	rep.add("  accepted %d of %d", acc, len(arts))
	for _, r := range refused {
		rep.add("  refused %s", r)
	}
}

// waitListed polls the group (a fresh connection each time: a cache such as
// NewsCache may show new articles only after its group timeout) until every
// accepted article is listed or the wait is over.
func (c rsConfig) waitListed(rep *rsReport, tag string, arts []*rsArticle) {
	byKey := map[string]*rsArticle{}
	want := 0
	for _, a := range arts {
		byKey[a.key()] = a
		if a.accepted {
			want++
		}
	}
	start := time.Now()
	var how string
	rounds := 0
	for {
		rounds++
		n, err := c.reader()
		if err == nil {
			var g groupInfo
			if g, err = n.group(c.group); err == nil {
				var rows []ovRow
				rows, how, err = n.overview(g.low, g.high)
				for _, r := range rows {
					k := runKey([]string{"Message-ID: " + r.mid, "Subject: " + r.subject}, tag)
					if a := byKey[k]; a != nil && !a.listed {
						a.listed, a.num = true, r.num
					}
				}
			}
			n.close()
		}
		got := 0
		for _, a := range arts {
			if a.listed && a.accepted {
				got++
			}
		}
		if got == want || time.Since(start) > c.wait {
			rep.add("")
			rep.add("listed by the reader (%s) after %s, %d poll(s): %d of %d accepted", how,
				time.Since(start).Round(time.Second), rounds, got, want)
			if err != nil {
				rep.add("  last poll: %v", err)
			}
			return
		}
		time.Sleep(3 * time.Second)
	}
}

// fetchServed gets each listed article by number, as the CLI does.
func (c rsConfig) fetchServed(rep *rsReport, arts []*rsArticle) {
	n, err := c.reader()
	if err != nil {
		rep.add("fetching the served articles: %v", err)
		return
	}
	defer func() { n.close() }()
	if _, err := n.group(c.group); err != nil {
		rep.add("fetching the served articles: %v", err)
		return
	}
	for _, a := range arts {
		h, b, s, err := n.article(strconv.FormatInt(a.num, 10))
		if err != nil {
			rep.add("ARTICLE %d (%s): %v", a.num, a.key(), err)
			if s == "" { // connection lost
				n.c.Close()
				if n, err = c.reader(); err != nil {
					return
				}
				n.group(c.group)
			}
			continue
		}
		for i := range b {
			b[i] = strings.TrimRight(b[i], "\r")
		}
		a.served = &msg{head: h, body: trimEmpty(b)}
	}
}

// diffMsg describes how the mbox message got differs from the served one, or "".
func diffMsg(served, got msg) string {
	if d := diffLines(served.head, got.head); d != "" {
		return "header: " + d
	}
	if d := diffLines(served.body, got.body); d != "" {
		return "body: " + d
	}
	return ""
}

func diffLines(want, got []string) string {
	for i := 0; i < len(want) || i < len(got); i++ {
		switch {
		case i >= len(got):
			return fmt.Sprintf("line %d missing: %q", i+1, clip(want[i], 120))
		case i >= len(want):
			return fmt.Sprintf("extra line %d: %q", i+1, clip(got[i], 120))
		case want[i] != got[i]:
			return fmt.Sprintf("line %d: want %q, got %q", i+1, clip(want[i], 120), clip(got[i], 120))
		}
	}
	return ""
}

// field is one header field: its name and its physical lines.
type field struct {
	name  string // lower case, trimmed; "" for a line without a colon
	lines []string
}

func (f field) raw() string { return strings.Join(f.lines, "\n") }

// unfolded joins the lines and collapses runs of white space.
func (f field) unfolded() string {
	return strings.Join(strings.Fields(strings.Join(f.lines, " ")), " ")
}

func fields(head []string) []field {
	var out []field
	for _, l := range head {
		if len(out) > 0 && l != "" && (l[0] == ' ' || l[0] == '\t') {
			out[len(out)-1].lines = append(out[len(out)-1].lines, l)
			continue
		}
		// "From : x" (obsolete syntax) is a From field; "From nobody Thu
		// Dec  8 12:55:04 2016" is no field at all
		name, _, ok := strings.Cut(l, ":")
		name = strings.TrimRight(name, " \t")
		if !ok || strings.ContainsAny(name, " \t") {
			name = ""
		}
		out = append(out, field{name: strings.ToLower(name), lines: []string{l}})
	}
	return out
}

// maintained are header fields an NNTP server sets or rewrites by design.
func maintained(name string) bool {
	switch name {
	case "path", "xref", "lines", "x-trace", "x-complaints-to", "sender", "nntp-posting-date", "nntp-posting-host":
		return true
	}
	return strings.HasPrefix(name, "injection-") || strings.HasPrefix(name, "nntp-posting-") || strings.HasPrefix(name, "x-nntp-posting-")
}

// serverDiffs compares what the server serves with what was injected.
func serverDiffs(rep *rsReport, arts []*rsArticle) {
	// changes to maintained fields and added fields, counted per method
	type tally struct {
		n       int
		example string
	}
	tallies := map[string]*tally{}
	perMethod := map[string]int{}
	count := func(kind, method, example string) {
		k := kind + " (" + method + ")"
		if tallies[k] == nil {
			tallies[k] = &tally{example: example}
		}
		tallies[k].n++
	}
	quiet := true
	for _, a := range arts {
		if a.served == nil {
			continue
		}
		perMethod[a.method]++
		sent, got := fields(a.head), fields(a.served.head)
		used := make([]bool, len(got))
		pos := make([]int, len(sent))
		state := make([]string, len(sent))
		match := func(pass int) {
			for i, s := range sent {
				if state[i] != "" {
					continue
				}
				for j, g := range got {
					if used[j] {
						continue
					}
					var ok bool
					switch pass {
					case 0:
						ok = s.raw() == g.raw()
					case 1:
						ok = s.name == g.name && s.unfolded() == g.unfolded()
					case 2:
						ok = s.name != "" && s.name == g.name
					}
					if ok {
						used[j], pos[i] = true, j
						state[i] = []string{"same", "unfolded", "changed"}[pass]
						break
					}
				}
			}
		}
		match(0)
		match(1)
		match(2)
		var notes []string
		last, reordered := -1, false
		for i, s := range sent {
			switch state[i] {
			case "":
				msg := fmt.Sprintf("%s: our header line %q is gone", a.key(), clip(s.raw(), 100))
				if maintained(s.name) {
					count("removed "+s.name, a.method, clip(s.raw(), 100))
				} else {
					rep.server("%s", msg)
				}
				continue
			case "unfolded":
				notes = append(notes, fmt.Sprintf("refolded/unfolded %q -> %q", clip(s.raw(), 80), clip(got[pos[i]].raw(), 80)))
			case "changed":
				m := fmt.Sprintf("%q -> %q", clip(s.raw(), 100), clip(got[pos[i]].raw(), 100))
				if maintained(s.name) {
					count("changed "+s.name, a.method, m)
				} else {
					rep.server("%s: our header line changed: %s", a.key(), m)
				}
			}
			if pos[i] < last {
				reordered = true
			}
			last = pos[i]
		}
		if reordered {
			notes = append(notes, "header order changed")
		}
		for j, g := range got {
			if !used[j] {
				n := g.name
				if n == "" {
					n = clip(g.raw(), 40)
				}
				count("added "+n, a.method, clip(g.raw(), 100))
			}
		}
		sb := trimEmpty(append([]string{}, a.body...))
		for i := range sb {
			sb[i] = strings.TrimRight(sb[i], "\r")
		}
		if d := diffLines(sb, a.served.body); d != "" {
			rep.server("%s: body served differs from the injected one: %s", a.key(), d)
		}
		if len(notes) > 0 {
			quiet = false
			rep.add("  %s: %s", a.key(), strings.Join(notes, "; "))
		}
	}
	keys := make([]string, 0, len(tallies))
	for k := range tallies {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		quiet = false
		method := k[strings.LastIndex(k, "(")+1 : len(k)-1]
		rep.add("  %s: %d of %d articles, e.g. %s", k, tallies[k].n, perMethod[method], tallies[k].example)
	}
	if quiet {
		rep.add("  none (apart from the failures listed at the end)")
	}
}

// pull runs the CLI against the reader for cfg.group in dir.
func (c rsConfig) pull(t *testing.T, dir string, extra ...string) pullResult {
	t.Helper()
	args := []string{"-m", "usenet_archiver", "-c", "pull", "-s", c.addr, "--no-netrc",
		"-g", c.group, "-D", "8", "-t", "60"}
	if !c.tls {
		args = append(args, "--no-ssl")
	}
	if c.user != "" {
		pf := filepath.Join(t.TempDir(), "pass")
		if err := os.WriteFile(pf, []byte(c.pass), 0o600); err != nil {
			t.Fatalf("%v", err)
		}
		args = append(args, "-u", c.user, "--password-file", pf)
	}
	args = append(args, extra...)
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Minute)
	defer cancel()
	cmd := exec.CommandContext(ctx, "python3", args...)
	cmd.Dir = dir
	cmd.Env = append(os.Environ(), "PYTHONPATH="+uaSrc(t), "NNTPSERVER=", "NNTP_PASSWORD=")
	if c.ca != "" {
		cmd.Env = append(cmd.Env, "SSL_CERT_FILE="+c.ca)
	}
	out, err := cmd.CombinedOutput()
	r := pullResult{dir: dir, mbox: filepath.Join(dir, c.group+".mbox"), output: string(out), hung: ctx.Err() != nil}
	if ee, ok := err.(*exec.ExitError); ok {
		r.exit = ee.ExitCode()
	} else if err != nil {
		t.Fatalf("running the CLI: %v", err)
	}
	if m, _ := filepath.Glob(filepath.Join(dir, c.group+"-*.mbox")); len(m) == 1 {
		r.mbox = m[0]
	}
	if b, err := os.ReadFile(filepath.Join(dir, "completed_newsgroups.log")); err == nil {
		for _, l := range strings.Split(string(b), "\n") {
			if _, g, ok := strings.Cut(l, "\t"); ok && g == c.group {
				r.completed = true
			}
		}
	}
	return r
}
