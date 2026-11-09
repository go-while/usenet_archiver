package e2e

import (
	"sync"
	"testing"

	"github.com/go-while/nntp-testkit/fakeserver"
)

// TestLaggingFrontend: on a server farm a frontend behind the others answers
// 423 for an article the overview listed. The CLI asks once more on a new
// connection after the fetch, so the pull still gets every article; an
// article that is really gone is asked twice and counted missing.
func TestLaggingFrontend(t *testing.T) {
	arts := numbered(8, "lag")

	t.Run("423 once", func(t *testing.T) {
		t.Parallel()
		var mu sync.Mutex
		left := map[int]int{3: 1, 6: 1}
		asked := map[int]int{}
		srv := fakeserver.New(t, fakeserver.Options{Handler: rawHandlerWith(arts, func(n int) bool {
			mu.Lock()
			defer mu.Unlock()
			asked[n]++
			if left[n] > 0 {
				left[n]--
				return true
			}
			return false
		})})
		expectComplete(t, pull(t, srv.Addr(), "", "-C", "2", "-D", "3"), arts)
		mu.Lock()
		defer mu.Unlock()
		if asked[3] != 2 || asked[1] != 1 {
			t.Errorf("ARTICLE 3 asked %d times (want 2), ARTICLE 1 %d (want 1)", asked[3], asked[1])
		}
	})

	t.Run("423 every time", func(t *testing.T) {
		t.Parallel()
		var mu sync.Mutex
		asked := 0
		srv := fakeserver.New(t, fakeserver.Options{Handler: rawHandlerWith(arts, func(n int) bool {
			if n != 5 {
				return false
			}
			mu.Lock()
			defer mu.Unlock()
			asked++
			return true
		})})
		r := pull(t, srv.Addr(), "", "-C", "2", "-D", "3")
		want := append(append([]fakeserver.Article{}, arts[:4]...), arts[5:]...)
		expectAll(t, r.mbox, want)
		if r.exit != 0 || !r.completed {
			t.Errorf("exit %d completed %v\n%s", r.exit, r.completed, r.output)
		}
		mu.Lock()
		defer mu.Unlock()
		if asked != 2 {
			t.Errorf("ARTICLE 5 asked %d times, want 2", asked)
		}
	})
}
