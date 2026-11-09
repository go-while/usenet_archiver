package e2e

// Tests for server behaviour that nntp-testkit's fakeserver imitates (most of
// it new between v0.4.1 and v0.11.2): server farms whose frontends report
// different numbers, the XHDR fallback, overview data out of step with the
// articles, slow but alive servers, a provider's connection limit, groups of
// ten million numbers, overview ranges a provider truncates, COMPRESS.
//
// Not tested, because the CLI never sends the command: LISTGROUP
// (StoreOptions.ListgroupRange), NEWNEWS (NoNewnews). COMPRESS is only
// checked not to be sent.

import (
	"fmt"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/go-while/nntp-testkit/fakeserver"
)

// TestServerFarmGroupNumbers: the numbers GROUP reports differ per
// connection (StoreOptions.GroupNumbers), as behind a provider's load
// balancer, where each -C worker sends GROUP on its own connection.
func TestServerFarmGroupNumbers(t *testing.T) {
	// The frontends of the fetch connections lag: their High is 5 lower
	// than the main connection's, but they serve the articles. The workers
	// fetch what the overview listed, whatever their own GROUP said.
	t.Run("workers' frontends lag", func(t *testing.T) {
		t.Parallel()
		arts := numbered(40, "lagw")
		srv, _ := storeServer(t, arts, fakeserver.StoreOptions{
			GroupNumbers: func(conn int, g fakeserver.Group) fakeserver.Group {
				if conn != 1 { // 1 is the main connection, the first one
					g.High -= 5
					g.Count -= 5
				}
				return g
			},
		}, fakeserver.Faults{}, fakeserver.Options{})
		expectComplete(t, pull(t, srv.Addr(), "", "-C", "3"), arts)
	})

	// The main connection's frontend lags: the overview ends at its High,
	// so the newest articles wait for the next run, which must fetch them
	// and nothing twice.
	t.Run("main frontend lags, next run catches up", func(t *testing.T) {
		t.Parallel()
		arts := numbered(30, "lagm")
		var lag atomic.Bool
		lag.Store(true)
		srv, _ := storeServer(t, arts, fakeserver.StoreOptions{
			GroupNumbers: func(conn int, g fakeserver.Group) fakeserver.Group {
				if lag.Load() {
					g.High -= 10
					g.Count -= 10
				}
				return g
			},
		}, fakeserver.Faults{}, fakeserver.Options{})
		dir := t.TempDir()
		expectComplete(t, pull(t, srv.Addr(), dir), arts[:20])
		lag.Store(false)
		expectComplete(t, pull(t, srv.Addr(), dir), arts)
	})

	// "211 0 0 0" for a group without articles (RFC 3977 6.1.1 allows it):
	// complete, nothing written, no overview asked for.
	t.Run("211 0 0 0", func(t *testing.T) {
		t.Parallel()
		srv, _ := storeServer(t, nil, fakeserver.StoreOptions{
			GroupNumbers: func(conn int, g fakeserver.Group) fakeserver.Group {
				g.Low, g.High, g.Count = 0, 0, 0
				return g
			},
		}, fakeserver.Faults{}, fakeserver.Options{})
		expectComplete(t, pull(t, srv.Addr(), ""), nil)
		if n := srv.Count("OVER") + srv.Count("XOVER"); n != 0 {
			t.Errorf("%d OVER/XOVER for an empty group", n)
		}
	})

	// Store.HighestExisting: High is the highest number that still exists,
	// so it goes down when the newest articles are cancelled (and Low-1 when
	// the group is empty). A rerun after that completes and adds nothing; an
	// article that arrives later is fetched by the next run.
	t.Run("HighestExisting: High goes down", func(t *testing.T) {
		t.Parallel()
		arts := numbered(10, "hiex")
		st := fakeserver.NewStore()
		st.AddGroup(group, "")
		for _, a := range arts {
			st.AddArticle(group, a)
		}
		srv := fakeserver.New(t, fakeserver.Options{Store: st, StoreOpts: fakeserver.StoreOptions{GroupNumbers: st.HighestExisting}})
		dir := t.TempDir()
		expectComplete(t, pull(t, srv.Addr(), dir), arts)
		st.Cancel("<9@hiex>")
		st.Cancel("<10@hiex>")
		expectComplete(t, pull(t, srv.Addr(), dir), arts) // High 8: nothing new; the archive keeps 9 and 10
		later := art("<later@hiex>", "arrived after the cancels")
		st.AddArticle(group, later) // number 11
		expectComplete(t, pull(t, srv.Addr(), dir), append(arts, later))
		st.Expire(group, 12) // the group is empty: "211 0 12 11"
		expectComplete(t, pull(t, srv.Addr(), dir), append(arts, later))
	})
}

// TestXhdrFallback: a server with neither OVER nor XOVER is listed with
// XHDR DATE (the Store serves XHDR since nntp-testkit v0.8.0). Every article
// is fetched, also with a date window, and an XHDR line with the number alone
// (a server that trims the empty value; StoreOptions.HdrLines) still counts
// as an article.
func TestXhdrFallback(t *testing.T) {
	arts := numbered(25, "xhdr")
	trim := func(conn int, g, field string, lines []string) []string {
		out := append([]string{}, lines...)
		for i, l := range out {
			if strings.HasPrefix(l, "7 ") {
				out[i] = "7"
			}
		}
		return out
	}
	for name, extra := range map[string][]string{
		"whole group": nil,
		"date window": {"--start", "2016-12-08", "--end", "2016-12-08"},
	} {
		extra := extra
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			srv, _ := storeServer(t, arts, fakeserver.StoreOptions{NoOver: true, NoXover: true, HdrLines: trim},
				fakeserver.Faults{}, fakeserver.Options{})
			expectComplete(t, pull(t, srv.Addr(), "", extra...), arts)
			if srv.Count("XHDR") == 0 {
				t.Errorf("XHDR never used: %v", srv.Counts())
			}
		})
	}
}

// TestOverviewOutOfStep: the overview's Message-ID differs from the
// article's own (StoreOptions.OverviewFields: an overview database out of
// step with the spool), or is empty. The mbox must hold each article once,
// under its own Message-ID, also after two more runs.
func TestOverviewOutOfStep(t *testing.T) {
	arts := numbered(12, "ovstep")
	for name, f := range map[string]func(string) string{
		"other Message-ID": func(n string) string { return "<overview-" + n + "@elsewhere>" },
		"no Message-ID":    func(string) string { return "" },
	} {
		f := f
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			srv, _ := storeServer(t, arts, fakeserver.StoreOptions{
				OverviewFields: func(conn int, g string, fields []string) []string {
					fields[4] = f(fields[0])
					return fields
				},
			}, fakeserver.Faults{}, fakeserver.Options{})
			dir := t.TempDir()
			for run := 1; run <= 3; run++ {
				r := pull(t, srv.Addr(), dir)
				if r.exit != 0 || !r.completed {
					t.Errorf("run %d: exit %d completed %v\n%s", run, r.exit, r.completed, r.output)
				}
				expectAll(t, r.mbox, arts)
			}
		})
	}
}

// TestSlowServer: README: --response-timeout is the most one reply may take
// in all, however slowly it arrives, and -t the most one read may wait. A
// server that is slow but alive (Faults.Drip: one byte at a time;
// Faults.LineDelay: one line at a time) and finishes each reply within
// --response-timeout must get the pull completed; a reply that takes longer
// is a failure: exit not 0, the job not recorded, only whole articles.
func TestSlowServer(t *testing.T) {
	arts := numbered(10, "slow")
	ok := map[string]fakeserver.Faults{
		// about 250 bytes per article: 0.25 s each
		"ARTICLE drip": {Drip: map[string]time.Duration{"ARTICLE": time.Millisecond}},
		"GROUP and CAPABILITIES drip": {Drip: map[string]time.Duration{
			"GROUP": 5 * time.Millisecond, "CAPABILITIES": 5 * time.Millisecond}},
		// 10 lines, 1 s in all
		"OVER line by line": {LineDelay: map[string]time.Duration{"OVER": 100 * time.Millisecond, "XOVER": 100 * time.Millisecond}},
	}
	for name, f := range ok {
		f := f
		t.Run(name+", within the deadline", func(t *testing.T) {
			t.Parallel()
			srv, _ := newServer(t, arts, f)
			expectComplete(t, pull(t, srv.Addr(), "", "--response-timeout", "10"), arts)
		})
	}

	// one article of about 4.5 KB, one byte a millisecond: longer than the
	// 2 s --response-timeout, though no single read waits for -t
	t.Run("ARTICLE drip, over the deadline", func(t *testing.T) {
		t.Parallel()
		var body []string
		for i := 0; i < 60; i++ {
			body = append(body, strings.Repeat("d", 70))
		}
		slow := []fakeserver.Article{art("<1@drip>", body...)}
		srv, _ := newServer(t, slow, fakeserver.Faults{Drip: map[string]time.Duration{"ARTICLE": time.Millisecond}})
		r := pullWithin(t, 60*time.Second, srv.Addr(), "", "--response-timeout", "2")
		expectFailed(t, r)
		onlyWholeArticles(t, r.mbox, slow)
	})

	// 300 overview lines 20 ms apart, 6 s per OVER: over the 3 s
	// --response-timeout, so the overview fails and with it the job; in
	// chunks of 50 lines (1 s each) the same server gets the pull completed
	t.Run("large OVER line by line", func(t *testing.T) {
		t.Parallel()
		many := numbered(300, "slowover")
		srv, _ := newServer(t, many, fakeserver.Faults{LineDelay: map[string]time.Duration{
			"OVER": 20 * time.Millisecond, "XOVER": 20 * time.Millisecond}})
		r := pullWithin(t, 80*time.Second, srv.Addr(), "", "--response-timeout", "3")
		expectFailed(t, r)
		if got, _ := readMbox(t, r.mbox); len(got) != 0 {
			t.Errorf("%d articles written although the overview failed", len(got))
		}
		expectComplete(t, pullWithin(t, 80*time.Second, srv.Addr(), "", "--response-timeout", "3", "--overview-chunk", "50"), many)
	})
}

// expectFailed: the pull ended by itself, not with exit 0, and is not recorded.
func expectFailed(t T, r pullResult) {
	t.Helper()
	if r.hung {
		t.Errorf("killed at the time limit\n%s", r.output)
	} else if r.exit == 0 {
		t.Errorf("exit code 0\n%s", r.output)
	}
	if r.completed {
		t.Errorf("recorded as completed")
	}
}

// TestConnectionPeak: README: "At most -C connections per pull", also across
// groups, where the main connection is opened again for the next group
// (Server.MaxOpen, new in nntp-testkit v0.11.0).
func TestConnectionPeak(t *testing.T) {
	const other = "other.group"
	st := fakeserver.NewStore()
	st.AddGroup(group, "")
	st.AddGroup(other, "")
	arts := numbered(120, "peak")
	for _, a := range arts {
		st.AddArticle(group, a)
		b := art(strings.Replace(a.MessageID, "@peak", "@peak2", 1), "in the other group")
		st.AddArticle(other, b)
	}
	srv := fakeserver.New(t, fakeserver.Options{Store: st,
		Faults: fakeserver.Faults{Delay: map[string]time.Duration{"ARTICLE": 2 * time.Millisecond}}})
	r := pull(t, srv.Addr(), "", "-C", "3", "-g", other)
	expectComplete(t, r, arts)
	if got, _ := readMbox(t, r.dir+"/"+other+".mbox"); len(got) != len(arts) {
		t.Errorf("%s: %d articles, want %d", other, len(got), len(arts))
	}
	if n := srv.MaxOpen(); n > 3 {
		t.Errorf("%d connections open at once with -C 3", n)
	} else if n < 2 {
		t.Errorf("at most %d connection open at once: the fetch did not run in parallel", n)
	}
}

// TestHugeSparseGroup: a generated group (Store.AddGenerated) of ten million
// numbers with an article only every 250,000th number. The CLI must list it
// in overview chunks, not number by number, ask ARTICLE only for the
// articles there are, and get each once. An --overview-chunk wider than the
// server takes (503) is halved until the server takes it.
func TestHugeSparseGroup(t *testing.T) {
	const high = 10_000_000
	byNum := map[int64]fakeserver.Article{}
	var want []fakeserver.Article
	for n := int64(250_000); n <= high; n += 250_000 {
		a := art(fmt.Sprintf("<%d@huge>", n), fmt.Sprintf("article %d of %d", n, high))
		a.Number = n
		byNum[n] = a
		want = append(want, a)
	}
	gen := fakeserver.Generator{Low: 1, High: high, Article: func(g string, n int64) (fakeserver.Article, bool) {
		a, ok := byNum[n]
		return a, ok
	}}
	server := func(t *testing.T) *fakeserver.Server {
		st := fakeserver.NewStore()
		st.AddGenerated(group, "", gen)
		return fakeserver.New(t, fakeserver.Options{Store: st})
	}

	t.Run("default chunk", func(t *testing.T) {
		t.Parallel()
		srv := server(t)
		start := time.Now()
		expectComplete(t, pullWithin(t, 120*time.Second, srv.Addr(), ""), want)
		t.Logf("pulled in %v: %v", time.Since(start).Round(time.Millisecond), srv.Counts())
		chunks := high / 10000
		if n := srv.Count("OVER") + srv.Count("XOVER"); n < chunks || n > 2*chunks {
			t.Errorf("%d OVER/XOVER commands for %d chunks of 10000", n, chunks)
		}
		if n := srv.Count("ARTICLE"); n != len(want) {
			t.Errorf("%d ARTICLE commands for %d articles", n, len(want))
		}
	})

	t.Run("chunk over the server's limit", func(t *testing.T) {
		// the server answers 503 to the wide range: the CLI halves it
		t.Parallel()
		srv := server(t)
		r := pullWithin(t, 120*time.Second, srv.Addr(), "", "--overview-chunk", "200000")
		expectComplete(t, r, want)
		if n := srv.Count("OVER") + srv.Count("XOVER"); n > 200 {
			t.Errorf("%d OVER/XOVER for 10,000,000 numbers", n)
		}
	})
}

// TestTruncatedOverview: a provider that answers an OVER range wider than
// it allows with only the first part of it (StoreOptions.MaxOverRange
// without OverRangeTooLarge). The rest of the range must still be listed
// and fetched; a provider that refuses the wide range instead (501 or 503)
// is asked for smaller ranges.
func TestTruncatedOverview(t *testing.T) {
	arts := numbered(1000, "trunc")
	t.Run("first 100 of each range", func(t *testing.T) {
		t.Parallel()
		srv, _ := storeServer(t, arts, fakeserver.StoreOptions{MaxOverRange: 100}, fakeserver.Faults{}, fakeserver.Options{})
		expectComplete(t, pull(t, srv.Addr(), ""), arts)
		if n := srv.Count("OVER") + srv.Count("XOVER"); n < 10 {
			t.Errorf("%d OVER/XOVER for 1000 articles 100 at a time", n)
		}
	})
	for _, reply := range []string{"501 range too large", "503 range too large"} {
		reply := reply
		t.Run(reply[:3]+" for a wide range", func(t *testing.T) {
			// the range is halved until the server takes it, and the group
			// is still listed by OVER (with Message-IDs), not by XHDR
			t.Parallel()
			srv, _ := storeServer(t, arts, fakeserver.StoreOptions{MaxOverRange: 100, OverRangeTooLarge: []string{reply}},
				fakeserver.Faults{}, fakeserver.Options{})
			expectComplete(t, pull(t, srv.Addr(), ""), arts)
			if n := srv.Count("XHDR") + srv.Count("HDR"); n != 0 {
				t.Errorf("%d XHDR/HDR: fell back instead of asking a smaller range", n)
			}
		})
	}
}

// TestNoCompress: the CLI does not use COMPRESS DEFLATE (RFC 8054). Offered
// it, also broken (CompressOutputOnly), it must not send it and pull as
// without it.
func TestNoCompress(t *testing.T) {
	arts := numbered(8, "nocomp")
	for name, so := range map[string]fakeserver.StoreOptions{
		"offered":     {Compress: true},
		"output only": {CompressOutputOnly: true},
	} {
		so := so
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			srv, _ := storeServer(t, arts, so, fakeserver.Faults{}, fakeserver.Options{})
			expectComplete(t, pull(t, srv.Addr(), ""), arts)
			if n := srv.Count("COMPRESS"); n != 0 {
				t.Errorf("COMPRESS sent %d times", n)
			}
		})
	}
}
