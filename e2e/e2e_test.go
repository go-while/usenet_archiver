// Package e2e tests usenet_archiver end to end: an nntp-testkit fakeserver
// serves a group, the real CLI ("python3 -m usenet_archiver -c pull") fetches
// it, and go-lib-mbox2nntp, the parser mbox2nntp imports with, reads the mbox
// back. Every article must come back byte-identical and exactly once, and a
// pull that did not get every article must not be recorded in
// completed_newsgroups.log or exit 0.
//
// Run: cd e2e && go test ./...   (needs python3 and Go 1.27+)
// UA_SRC overrides the usenet_archiver checkout (default: the parent directory).
//
// Tests for bugs that are not fixed yet run under knownBug: while they fail
// they are reported as skipped with the finding number of the review; once
// they pass they fail, so that the mark is removed together with the fix.
package e2e

import (
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	mbox "github.com/go-while/go-lib-mbox2nntp"
	"github.com/go-while/nntp-testkit/fakeserver"
)

const group = "test.group"

// T is what the checks need from a test. *testing.T and *recorder implement it.
type T interface {
	Helper()
	Errorf(format string, args ...any)
	Fatalf(format string, args ...any)
	TempDir() string
}

type recorder struct {
	*testing.T
	mu   sync.Mutex
	errs []string
}

type fatal struct{}

func (r *recorder) Errorf(format string, args ...any) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.errs = append(r.errs, fmt.Sprintf(format, args...))
}

func (r *recorder) Fatalf(format string, args ...any) {
	r.Errorf(format, args...)
	panic(fatal{})
}

// knownBug runs fn, which is expected to fail because of the given finding
// of the review. A failure is reported as a skip; a pass fails the test.
func knownBug(t *testing.T, finding string, fn func(T)) {
	t.Helper()
	r := &recorder{T: t}
	func() {
		defer func() {
			if v := recover(); v != nil {
				if _, ok := v.(fatal); !ok {
					panic(v)
				}
			}
		}()
		fn(r)
	}()
	if len(r.errs) == 0 {
		t.Errorf("passes now: remove the knownBug(%q) mark", finding)
		return
	}
	msg := strings.Join(r.errs, "\n")
	if len(msg) > 600 {
		msg = msg[:600] + "…"
	}
	t.Skipf("known bug, review finding %s:\n%s", finding, msg)
}

func uaSrc(t T) string {
	if s := os.Getenv("UA_SRC"); s != "" {
		return s
	}
	s, err := filepath.Abs("..")
	if err != nil {
		t.Fatalf("%v", err)
	}
	return s
}

type pullResult struct {
	dir, mbox string
	exit      int
	output    string
	completed bool
}

// pull runs the CLI against addr in dir (a fresh temp dir when "").
func pull(t T, addr, dir string, extra ...string) pullResult {
	t.Helper()
	if dir == "" {
		dir = t.TempDir()
	}
	args := append([]string{"-m", "usenet_archiver", "-c", "pull", "-s", addr, "--no-ssl", "--no-netrc",
		"-g", group, "-C", "2", "-D", "4", "-t", "2"}, extra...)
	cmd := exec.Command("python3", args...)
	cmd.Dir = dir
	cmd.Env = append(os.Environ(), "PYTHONPATH="+uaSrc(t), "NNTPSERVER=", "NNTP_PASSWORD=")
	out, err := cmd.CombinedOutput()
	exit := 0
	if ee, ok := err.(*exec.ExitError); ok {
		exit = ee.ExitCode()
	} else if err != nil {
		t.Fatalf("running the CLI: %v", err)
	}
	// "group.mbox", or "group-YYYYMMDD-YYYYMMDD.mbox" for a date window
	r := pullResult{dir: dir, mbox: filepath.Join(dir, group+".mbox"), exit: exit, output: string(out)}
	if m, _ := filepath.Glob(filepath.Join(dir, group+"-*.mbox")); len(m) == 1 {
		r.mbox = m[0]
	}
	if b, err := os.ReadFile(filepath.Join(dir, "completed_newsgroups.log")); err == nil {
		r.completed = strings.Contains(string(b), group) // each test has its own dir
	}
	return r
}

type msg struct {
	head []string
	body []string
}

// readMbox parses path with go-lib-mbox2nntp the way mbox2nntp does: an empty
// file holds no messages; with X-Mbox2nntp-Format: mboxrd one ">" is taken
// off quoted From_/rnews lines and the field is dropped; trailing empty body
// lines are dropped (mbox cannot keep them); like mbox2nntp's readBody,
// each body line loses its trailing CR/LF, so a CR inside a line is kept.
func readMbox(t T, path string) ([]msg, int) {
	t.Helper()
	f, err := os.Open(path)
	if os.IsNotExist(err) {
		return nil, 0
	}
	if err != nil {
		t.Fatalf("open mbox: %v", err)
	}
	defer f.Close()
	if fi, _ := f.Stat(); fi.Size() == 0 {
		return nil, 0
	}
	m, err := mbox.CreateMboxStream(f)
	if err != nil {
		t.Fatalf("go-lib-mbox2nntp rejects the mbox: %v", err)
	}
	var out []msg
	for {
		mm, err := m.ReadMessage()
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Errorf("go-lib-mbox2nntp: %v", err)
			continue
		}
		b, _ := io.ReadAll(mm.BodyReader())
		quoted := false
		var head []string
		for _, h := range mm.Headers() {
			if name, v, ok := strings.Cut(h, ":"); ok && strings.EqualFold(strings.TrimSpace(name), "X-Mbox2nntp-Format") {
				quoted = strings.EqualFold(strings.TrimSpace(v), "mboxrd")
				continue
			}
			head = append(head, h)
		}
		body := strings.Split(string(b), "\n")
		for i, l := range body {
			body[i] = strings.TrimRight(l, "\r")
		}
		body = trimEmpty(body)
		if quoted {
			for i, l := range body {
				rest := strings.TrimLeft(l, ">")
				if strings.HasPrefix(l, ">") && (strings.HasPrefix(rest, "From ") || strings.HasPrefix(rest, "#! rnews ")) {
					body[i] = l[1:]
				}
			}
		}
		out = append(out, msg{head, body})
	}
	nm, _ := m.NearMisses()
	return out, nm
}

// msgID returns the first Message-ID field, unfolded.
func msgID(head []string) string {
	for i, h := range head {
		if len(h) >= 11 && strings.EqualFold(h[:11], "message-id:") {
			v := strings.TrimSpace(h[11:])
			for _, c := range head[i+1:] {
				if c == "" || (c[0] != ' ' && c[0] != '\t') {
					break
				}
				v += strings.TrimSpace(c)
			}
			return v
		}
	}
	return ""
}

func art(id string, body ...string) fakeserver.Article {
	return fakeserver.Article{
		MessageID: id,
		Headers: []string{
			"Path: fake!not-for-mail",
			"From: Someone <someone@example.com>",
			"Newsgroups: " + group,
			"Subject: test " + id,
			"Date: Thu, 08 Dec 2016 12:55:04 +0000",
			"Message-ID: " + id,
		},
		Body: body,
	}
}

func trimEmpty(b []string) []string {
	for len(b) > 0 && b[len(b)-1] == "" {
		b = b[:len(b)-1]
	}
	return b
}

// expectAll checks that every article in want is in the mbox exactly once,
// byte-identical, and that the mbox holds nothing else.
func expectAll(t T, path string, want []fakeserver.Article) {
	t.Helper()
	got, nearMisses := readMbox(t, path)
	byID := map[string][]msg{}
	for _, m := range got {
		byID[msgID(m.head)] = append(byID[msgID(m.head)], m)
	}
	if len(got) != len(want) {
		t.Errorf("mbox holds %d messages, want %d (near-misses %d)", len(got), len(want), nearMisses)
	}
	wanted := map[string]bool{}
	for _, a := range want {
		wanted[a.MessageID] = true
		ms := byID[a.MessageID]
		switch {
		case len(ms) == 0:
			t.Errorf("%s: missing from the mbox", a.MessageID)
		case len(ms) > 1:
			t.Errorf("%s: %d copies in the mbox", a.MessageID, len(ms))
		default:
			if g, w := strings.Join(ms[0].head, "\n"), strings.Join(a.Headers, "\n"); g != w {
				t.Errorf("%s: header changed\n got %q\nwant %q", a.MessageID, g, w)
			}
			if g, w := strings.Join(ms[0].body, "\n"), strings.Join(trimEmpty(a.Body), "\n"); g != w {
				t.Errorf("%s: body changed\n got %q\nwant %q", a.MessageID, g, w)
			}
		}
	}
	for id := range byID {
		if !wanted[id] {
			t.Errorf("mbox holds a message that is not on the server: Message-ID %q", id)
		}
	}
}

func newServer(t *testing.T, arts []fakeserver.Article, faults fakeserver.Faults) (*fakeserver.Server, *fakeserver.Store) {
	st := fakeserver.NewStore()
	st.AddGroup(group, "")
	for _, a := range arts {
		st.AddArticle(group, a)
	}
	return fakeserver.New(t, fakeserver.Options{Store: st, Faults: faults}), st
}

// tricky lists body lines a byte-exact archive must survive, with the review
// finding that breaks each one today ("" = works).
var tricky = []struct {
	a       fakeserver.Article
	finding string
}{
	{art("<plain@x>", "just text"), ""},
	{art("<from@x>", "From here on, a body line starting with From"), "1.6"},
	{art("<gtfrom@x>", ">From a quoted line", ">>From deeper"), ""},
	{art("<rnews@x>", "quoting an rnews batch:", "#! rnews 1234", "Path: inner", "Message-ID: <inner@x>", "", "inner body"), "1.6"},
	{art("<cr@x>", "col1\rcol2 on one line"), "1.12"},
	{art("<dots@x>", ".", "..", ".leading dot", "...three"), ""},
	{art("<8bit@x>", "caf\xe9 au lait, \xd0\xbf\xd1\x80\xd0\xb8\xd0\xb2\xd0\xb5\xd1\x82"), ""},
	{art("<long@x>", strings.Repeat("x", 9991)), ""},
}

// TestTrickyArticle pulls each tricky article between two plain ones.
func TestTrickyArticle(t *testing.T) {
	for _, c := range tricky {
		c := c
		t.Run(strings.Trim(c.a.MessageID, "<>"), func(t *testing.T) {
			t.Parallel()
			want := []fakeserver.Article{art("<before@x>", "before"), c.a, art("<after@x>", "after")}
			srv, _ := newServer(t, want, fakeserver.Faults{})
			check := func(t T) {
				r := pull(t, srv.Addr(), "")
				expectAll(t, r.mbox, want)
				if r.exit != 0 || !r.completed {
					t.Errorf("clean pull: exit %d completed %v\n%s", r.exit, r.completed, r.output)
				}
			}
			if c.finding != "" {
				knownBug(t, c.finding, check)
			} else {
				check(t)
			}
		})
	}
}

// TestCleanPull pulls the articles that work today in one group: the
// regression guard for dot-stuffing, 8-bit bytes and long lines.
func TestCleanPull(t *testing.T) {
	var want []fakeserver.Article
	for _, c := range tricky {
		if c.finding == "" {
			want = append(want, c.a)
		}
	}
	srv, _ := newServer(t, want, fakeserver.Faults{})
	r := pull(t, srv.Addr(), "")
	if r.exit != 0 || !r.completed {
		t.Errorf("exit %d completed %v\n%s", r.exit, r.completed, r.output)
	}
	expectAll(t, r.mbox, want)
}

// TestRerunAddsNothing: pulling the same group three times into one mbox must
// leave one copy of each article.
func TestRerunAddsNothing(t *testing.T) {
	cases := map[string]struct {
		a       fakeserver.Article
		finding string
	}{
		"plain":      {art("<plain@x>", "x"), ""},
		"latin-1 id": {art("<caf\xe9@x>", "Latin-1 Message-ID"), ""},
		"utf-8 id":   {art("<caf\xc3\xa9@x>", "UTF-8 Message-ID"), "2.1"},
		"folded id": {func() fakeserver.Article {
			a := art("<folded@x>", "folded")
			a.Headers[5] = "Message-ID:"
			a.Headers = append(a.Headers, " <folded@x>")
			return a
		}(), "2.1"},
		"no id": {fakeserver.Article{Headers: []string{"From: a@b.example", "Newsgroups: " + group, "Subject: no Message-ID", "Date: Thu, 08 Dec 2016 12:55:04 +0000"}, Body: []string{"no id"}}, "2.1"},
	}
	for name, c := range cases {
		c := c
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			// served raw: the Store would add a Message-ID to "no id"
			srv := fakeserver.New(t, fakeserver.Options{Handler: rawHandler([]fakeserver.Article{c.a}, nil)})
			check := func(t T) {
				dir := t.TempDir()
				for run := 0; run < 3; run++ {
					pull(t, srv.Addr(), dir)
				}
				got, _ := readMbox(t, filepath.Join(dir, group+".mbox"))
				if len(got) != 1 {
					t.Errorf("%d copies after 3 runs", len(got))
				}
			}
			if c.finding != "" {
				knownBug(t, c.finding, check)
			} else {
				check(t)
			}
		})
	}
}

// TestQuotedMessageIDDoesNotHideArticle: a body line quoting a Message-ID
// header must not make a later run skip the article with that ID.
func TestQuotedMessageIDDoesNotHideArticle(t *testing.T) {
	quoting := art("<quoting@x>", "> I saw this header:", "Message-ID: <quoted@x>", "in a reply")
	st := fakeserver.NewStore()
	st.AddGroup(group, "")
	st.AddArticle(group, quoting)
	srv := fakeserver.New(t, fakeserver.Options{Store: st})
	knownBug(t, "1.7", func(t T) {
		dir := t.TempDir()
		pull(t, srv.Addr(), dir)
		later := art("<quoted@x>", "the article the other one quoted")
		st.AddArticle(group, later)
		pull(t, srv.Addr(), dir)
		expectAll(t, filepath.Join(dir, group+".mbox"), []fakeserver.Article{quoting, later})
	})
}

// TestDateWindowAcrossGap: a hole in the article numbers must not cut off the
// articles on its far side when a date window is given.
func TestDateWindowAcrossGap(t *testing.T) {
	st := fakeserver.NewStore()
	st.AddGroup(group, "")
	n := 0
	for _, num := range append(seq(1, 1000), seq(1601, 2600)...) {
		a := art(fmt.Sprintf("<%d@gap>", num), "in the window")
		a.Number = int64(num)
		st.AddArticle(group, a)
		n++
	}
	srv := fakeserver.New(t, fakeserver.Options{Store: st})
	knownBug(t, "1.5", func(t T) {
		r := pull(t, srv.Addr(), "", "--start", "2016-12-08", "--end", "2016-12-08")
		got, _ := readMbox(t, r.mbox)
		if len(got) != n {
			t.Errorf("%d of %d articles in the window fetched (completed=%v)", len(got), n, r.completed)
		}
	})
}

func seq(a, b int) []int {
	var s []int
	for i := a; i <= b; i++ {
		s = append(s, i)
	}
	return s
}

// TestFaultsNotCompleted: none of these pulls got every article, so none may
// be recorded as completed or exit 0, and the mbox may hold only whole
// articles from the server.
func TestFaultsNotCompleted(t *testing.T) {
	arts := []fakeserver.Article{art("<1@x>", "one"), art("<2@x>", "two", "three", "four"), art("<3@x>", "five")}
	cases := map[string]struct {
		f       fakeserver.Faults
		finding string
	}{
		"ARTICLE 480":           {fakeserver.Faults{Override: map[string][]string{"ARTICLE": {"480 authentication required"}}}, "1.2, 4.2"},
		"ARTICLE 502":           {fakeserver.Faults{Override: map[string][]string{"ARTICLE": {"502 access denied"}}}, "1.2, 4.2"},
		"ARTICLE 503":           {fakeserver.Faults{Override: map[string][]string{"ARTICLE": {"503 temporary failure"}}}, "1.2, 4.2"},
		"OVER 503":              {fakeserver.Faults{Override: map[string][]string{"OVER": {"503 overview unavailable"}, "XOVER": {"503 overview unavailable"}}}, "1.3, 4.2"},
		"OVER stalls":           {fakeserver.Faults{Delay: map[string]time.Duration{"OVER": 5 * time.Second, "XOVER": 5 * time.Second}}, "1.4, 4.2"},
		"ARTICLE stalls":        {fakeserver.Faults{Delay: map[string]time.Duration{"ARTICLE": 5 * time.Second}}, "4.2"},
		"ARTICLE cut mid-reply": {fakeserver.Faults{DropInMultiline: map[string]int{"ARTICLE": 8}}, "4.2"},
	}
	for name, c := range cases {
		c := c
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			srv, _ := newServer(t, arts, c.f)
			knownBug(t, c.finding, func(t T) {
				r := pull(t, srv.Addr(), "")
				if r.completed {
					t.Errorf("recorded as completed")
				}
				if r.exit == 0 {
					t.Errorf("exit code 0")
				}
				onlyWholeArticles(t, r.mbox, arts)
			})
		})
	}
}

// onlyWholeArticles checks that every message in the mbox is one of arts, whole.
func onlyWholeArticles(t T, path string, arts []fakeserver.Article) {
	t.Helper()
	got, _ := readMbox(t, path)
	for _, m := range got {
		ok := false
		for _, a := range arts {
			ok = ok || (a.MessageID == msgID(m.head) && strings.Join(m.body, "\n") == strings.Join(a.Body, "\n"))
		}
		if !ok {
			t.Errorf("mbox holds a partial or foreign message: %q / %q", m.head, m.body)
		}
	}
}

// TestFaultNoPartialArticle: whatever the exit code, a cut or stalled reply
// must never leave part of an article in the mbox. This holds today.
func TestFaultNoPartialArticle(t *testing.T) {
	arts := []fakeserver.Article{art("<1@x>", "one"), art("<2@x>", "two", "three", "four"), art("<3@x>", "five")}
	for name, f := range map[string]fakeserver.Faults{
		"cut":   {DropInMultiline: map[string]int{"ARTICLE": 8}},
		"close": {CloseAfter: 4}, // after CAPABILITIES, MODE, GROUP and the first ARTICLE
	} {
		f := f
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			srv, _ := newServer(t, arts, f)
			r := pull(t, srv.Addr(), "")
			if r.completed {
				t.Errorf("recorded as completed")
			}
			onlyWholeArticles(t, r.mbox, arts)
		})
	}
}

// rawHandler returns a small reader server for one group that serves arts
// as numbers 1..len(arts) with their headers exactly as given (the Store adds
// a Message-ID when one is missing), and answers ARTICLE n with gone[n] when
// set: an article that OVER lists but that expired before ARTICLE, which the
// Store cannot do either.
func rawHandler(arts []fakeserver.Article, gone map[int]string) func(*fakeserver.Conn) {
	return func(c *fakeserver.Conn) {
		for {
			line, err := c.ReadLine()
			if err != nil {
				return
			}
			f := strings.Fields(line)
			if len(f) == 0 {
				continue
			}
			switch strings.ToUpper(f[0]) {
			case "CAPABILITIES":
				c.WriteLines("101 caps", "VERSION 2", "READER", "OVER", ".")
			case "MODE":
				c.WriteLines("200 reader")
			case "GROUP":
				c.WriteLines(fmt.Sprintf("211 %d 1 %d %s", len(arts), len(arts), group))
			case "OVER", "XOVER":
				lines := []string{"224 overview"}
				for i, a := range arts {
					lines = append(lines, fmt.Sprintf("%d\ts\tf\tThu, 08 Dec 2016 12:55:04 +0000\t%s\t\t10\t1", i+1, msgID(a.Headers)))
				}
				c.WriteLines(append(lines, ".")...)
			case "ARTICLE":
				var n int
				fmt.Sscan(f[len(f)-1], &n)
				if st, ok := gone[n]; ok {
					c.WriteLines(st)
					continue
				}
				if n < 1 || n > len(arts) {
					c.WriteLines("423 no article with that number")
					continue
				}
				a := arts[n-1]
				lines := []string{fmt.Sprintf("220 %d %s", n, msgID(a.Headers))}
				lines = append(lines, a.Headers...)
				lines = append(lines, "")
				for _, l := range a.Body {
					if strings.HasPrefix(l, ".") {
						l = "." + l
					}
					lines = append(lines, l)
				}
				c.WriteLines(append(lines, ".")...)
			case "QUIT":
				c.WriteLines("205 bye")
				return
			default:
				c.WriteLines("500 unknown")
			}
		}
	}
}

// TestMissingArticlesAreNotFailures: 430/423 mid-pipeline are counted as
// missing, the other responses stay paired with their requests, and the pull
// completes.
func TestMissingArticlesAreNotFailures(t *testing.T) {
	arts := []fakeserver.Article{art("<1@m>", "one"), art("<2@m>", "two"), art("<3@m>", "three"), art("<4@m>", "four"), art("<5@m>", "five")}
	gone := map[int]string{2: "430 no such article", 4: "423 no article with that number"}
	srv := fakeserver.New(t, fakeserver.Options{Handler: rawHandler(arts, gone)})
	r := pull(t, srv.Addr(), "", "-C", "1", "-D", "5")
	expectAll(t, r.mbox, []fakeserver.Article{arts[0], arts[2], arts[4]})
	if r.exit != 0 || !r.completed {
		t.Errorf("exit %d completed %v\n%s", r.exit, r.completed, r.output)
	}
}

// TestAuth: AUTHINFO with the right password pulls the group; a wrong one
// must neither exit 0 nor be recorded as completed.
func TestAuth(t *testing.T) {
	st := fakeserver.NewStore()
	st.AddGroup(group, "")
	want := []fakeserver.Article{art("<auth@x>", "behind auth")}
	st.AddArticle(group, want[0])
	srv := fakeserver.New(t, fakeserver.Options{Store: st, StoreOpts: fakeserver.StoreOptions{User: "u", Pass: "p"}})
	expectAll(t, pull(t, srv.Addr(), "", "-u", "u", "--password", "p").mbox, want)
	bad := pull(t, srv.Addr(), "", "-u", "u", "--password", "wrong")
	if bad.exit == 0 || bad.completed {
		t.Errorf("wrong password: exit %d completed %v", bad.exit, bad.completed)
	}
}
