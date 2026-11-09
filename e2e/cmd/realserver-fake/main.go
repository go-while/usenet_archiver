// realserver-fake: a stand-in news server for trying TestRealServer
// (e2e/realserver_test.go) without a real one. It runs until SIGINT or SIGTERM.
//
// -addr is a reader: an nntp-testkit Store serving -group, which takes POST
// and lists a posted article at once. -feed, when set, is a second port that
// takes IHAVE and POST and files the articles into the same Store after
// -delay, as a feeder in front of a reader (Diablo, innd) or a cache with a
// group timeout (NewsCache) would; on the way it can refuse what some servers
// refuse (-reject-cr, -reject-8bit), lose what some lose
// (-strip-trailing-space), prepend its name to Path (IHAVE), add an
// Injection-Info field (POST) and rebuild the head (-mangle: folded fields
// joined, X- fields moved to the end), so that the test's report has something
// to show. Once both listen it prints "listening ADDR [FEED]".
package main

import (
	"flag"
	"fmt"
	"log"
	"net/mail"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"
	"unicode/utf8"

	"github.com/go-while/nntp-testkit/fakeserver"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:1119", "reader address (POST lists at once)")
	feed := flag.String("feed", "", "feed address for IHAVE and POST (empty: none)")
	group := flag.String("group", "local.test", "the group")
	user := flag.String("user", "", "reader AUTHINFO user (empty: no login)")
	pass := flag.String("pass", "", "reader AUTHINFO password")
	delay := flag.Duration("delay", 0, "feed: list an article this long after it was accepted")
	rejectCR := flag.Bool("reject-cr", false, "feed: refuse a CR inside a line (441/437)")
	reject8 := flag.Bool("reject-8bit", false, "feed: refuse a body or head that is not UTF-8 (POST 441, IHAVE 436)")
	host := flag.String("path-host", "fakefeed", "feed: name prepended to Path (IHAVE) and in Injection-Info (POST)")
	mangle := flag.Bool("mangle", false, "feed: join folded header fields and move X- fields to the end")
	strip := flag.Bool("strip-trailing-space", false, "feed: strip trailing white space from body lines (a fidelity loss the test must report)")
	flag.Parse()

	st := fakeserver.NewStore()
	st.AddGroup(*group, "")
	rd, err := fakeserver.Start(fakeserver.Options{Addr: *addr, Store: st, Deadline: time.Hour,
		StoreOpts: fakeserver.StoreOptions{User: *user, Pass: *pass}})
	if err != nil {
		log.Fatal(err)
	}
	listening := rd.Addr()

	var fd *fakeserver.Server
	if *feed != "" {
		f := &feeder{st: st, group: *group, delay: *delay, rejectCR: *rejectCR, reject8: *reject8,
			host: *host, mangle: *mangle, strip: *strip, seen: map[string]bool{}}
		fd, err = fakeserver.Start(fakeserver.Options{Addr: *feed, Deadline: time.Hour,
			Greeting: "200 realserver-fake feed ready", Handler: fakeserver.LineHandler(f.line)})
		if err != nil {
			log.Fatal(err)
		}
		listening += " " + fd.Addr()
	}
	fmt.Println("listening", listening)

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	<-sig
	if fd != nil {
		fd.Close()
	}
	rd.Close()
}

type feeder struct {
	st                *fakeserver.Store
	group, host       string
	delay             time.Duration
	rejectCR, reject8 bool
	mangle, strip     bool
	mu                sync.Mutex
	seen              map[string]bool
}

func (f *feeder) line(c *fakeserver.Conn, line string) ([]string, bool) {
	w := strings.Fields(line)
	if len(w) == 0 {
		return []string{"500 empty"}, false
	}
	switch strings.ToUpper(w[0]) {
	case "CAPABILITIES":
		return []string{"101 caps", "VERSION 2", "IHAVE", "POST", "IMPLEMENTATION realserver-fake feed", "."}, false
	case "MODE":
		if len(w) > 1 && strings.EqualFold(w[1], "READER") {
			return []string{"200 posting allowed"}, false
		}
		return []string{"501 Unknown MODE variant"}, false
	case "QUIT":
		return []string{"205 bye"}, true
	case "IHAVE":
		if len(w) != 2 {
			return []string{"501 IHAVE <id>"}, false
		}
		f.mu.Lock()
		have := f.seen[w[1]]
		f.mu.Unlock()
		if have {
			return []string{"435 duplicate"}, false
		}
		c.WriteLines("335 send it")
		wire, err := c.ReadDotBlock()
		if err != nil {
			return nil, true
		}
		return []string{f.file(wire, true)}, false
	case "POST":
		c.WriteLines("340 send article")
		wire, err := c.ReadDotBlock()
		if err != nil {
			return nil, true
		}
		return []string{f.file(wire, false)}, false
	}
	return []string{"500 unknown command"}, false
}

// file checks and files one article (raw wire lines) and returns the reply.
func (f *feeder) file(wire []string, ihave bool) string {
	refuse, okc := "441", "240 article posted"
	if ihave {
		refuse, okc = "437", "235 article transferred"
	}
	var a fakeserver.Article
	inBody := false
	for _, l := range wire {
		l = strings.TrimPrefix(l, ".") // un-stuff ("..x" -> ".x")
		if f.rejectCR && strings.Contains(l, "\r") {
			return refuse + " bare CR in article"
		}
		if f.reject8 && !utf8.ValidString(l) {
			if ihave {
				return "436 cannot store non-UTF-8 article" // as OpenUsenetServer
			}
			return refuse + " not UTF-8"
		}
		switch {
		case inBody:
			if f.strip {
				l = strings.TrimRight(l, " \t")
			}
			a.Body = append(a.Body, l)
		case l == "":
			inBody = true
		default:
			a.Headers = append(a.Headers, l)
		}
	}
	id := strings.TrimSpace(a.Header("Message-ID"))
	if !strings.HasPrefix(id, "<") || !strings.HasSuffix(id, ">") || a.Header("Newsgroups") == "" {
		return refuse + " bad Message-ID or no Newsgroups"
	}
	f.mu.Lock()
	if f.seen[id] {
		f.mu.Unlock()
		return refuse + " duplicate"
	}
	f.seen[id] = true
	f.mu.Unlock()
	a.MessageID = id
	if t, err := mail.ParseDate(a.Header("Date")); err == nil {
		a.Date = t
	}
	for i, h := range a.Headers {
		if ihave && strings.HasPrefix(strings.ToLower(h), "path:") {
			a.Headers[i] = "Path: " + f.host + "!" + strings.TrimSpace(h[5:])
		}
	}
	if !ihave {
		a.Headers = append([]string{"Path: " + f.host + "!not-for-mail"}, a.Headers...)
		a.Headers = append(a.Headers, "Injection-Info: "+f.host+"; posting-host=\"127.0.0.1\"")
	}
	if f.mangle {
		a.Headers = mangle(a.Headers)
	}
	add := func() { f.st.AddArticle(f.group, a) }
	if f.delay > 0 {
		time.AfterFunc(f.delay, add)
	} else {
		add()
	}
	return okc
}

// mangle joins folded fields with a space and moves X- fields to the end, as
// a server that rebuilds the head does.
func mangle(h []string) []string {
	var joined []string
	for _, l := range h {
		if len(joined) > 0 && l != "" && (l[0] == ' ' || l[0] == '\t') {
			joined[len(joined)-1] += " " + strings.TrimLeft(l, " \t")
			continue
		}
		joined = append(joined, l)
	}
	var out, x []string
	for _, l := range joined {
		if strings.HasPrefix(strings.ToLower(l), "x-") {
			x = append(x, l)
		} else {
			out = append(out, l)
		}
	}
	return append(out, x...)
}
