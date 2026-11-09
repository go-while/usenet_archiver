# Running the e2e test against a real NNTP server

`TestRealServer` (`realserver_test.go`) injects about 16 articles into a test group, waits until
the reader lists them, pulls the group with the usenet_archiver CLI and reads the mbox back with
go-lib-mbox2nntp, the parser mbox2nntp imports with. Without `UA_REAL_ADDR` it is skipped.

Needs: Go (the version in `go.mod`), python3 3.9 or newer, a checkout of usenet_archiver (the test
runs `python3 -m usenet_archiver` from the parent directory, or from `UA_SRC`), network access to
the server, and a group the injector may post or feed to. Nothing else is installed.

## Commands

POST to the reader (INN nnrpd, NewsCache, a reader that takes posts):

```bash
cd usenet_archiver/e2e
UA_REAL_ADDR=127.0.0.1:119 UA_REAL_GROUP=local.test UA_REAL_INJECT=post \
  go test -count=1 -timeout 30m -run TestRealServer -v .
```

IHAVE to a feed port, reading from a separate reader port (Diablo: diablo feeds, dreaderd reads):

```bash
UA_REAL_ADDR=127.0.0.1:1119 UA_REAL_INJECT=ihave UA_REAL_INJECT_ADDR=127.0.0.1:1120 \
  go test -count=1 -timeout 30m -run TestRealServer -v .
```

Both in one run (INN: POST to nnrpd, IHAVE to innd; OpenUsenetServer: POST needs a login, IHAVE
and reading do not):

```bash
UA_REAL_ADDR=10.0.0.5:1119 UA_REAL_INJECT=post,ihave \
UA_REAL_POST_ADDR=10.0.0.5:1119 UA_REAL_IHAVE_ADDR=10.0.0.5:1119 \
UA_REAL_INJECT_USER=e2euser UA_REAL_INJECT_PASS=secret \
UA_REAL_REPORT=/tmp/ua-report.txt \
  go test -count=1 -timeout 30m -run TestRealServer -v .
```

## Variables

| Variable | Meaning |
|---|---|
| `UA_REAL_ADDR` | reader `host:port` (required); the CLI pulls from it |
| `UA_REAL_GROUP` | the group (default `local.test`); it may hold other articles |
| `UA_REAL_INJECT` | `post`, `ihave`, `post,ihave` or `none` (default `post`) |
| `UA_REAL_INJECT_ADDR` | where to inject (default `UA_REAL_ADDR`); `UA_REAL_POST_ADDR` and `UA_REAL_IHAVE_ADDR` set one method's address |
| `UA_REAL_USER`, `UA_REAL_PASS` | AUTHINFO for reading (CLI and test); also used for POST to the reader address |
| `UA_REAL_INJECT_USER`, `UA_REAL_INJECT_PASS` | AUTHINFO for injecting |
| `UA_REAL_TLS=1` | the reader speaks TLS from the first byte (the CLI's default); `UA_REAL_CA=file.pem` trusts a test CA; `UA_REAL_INJECT_TLS=1` for an injection address other than the reader's |
| `UA_REAL_WAIT` | seconds to wait until the reader lists the accepted articles (default 120); more than the group timeout of a cache (NewsCache) or the feed delay of a reader behind a feeder (Diablo) |
| `UA_REAL_REPORT` | also write the report to this file |
| `UA_REAL_NAME` | a label for the report, e.g. `INN 2.7.2` |
| `UA_REAL_SKIP` | article names not to inject, comma-separated (e.g. `long512k`) |
| `UA_REAL_STRICT=0` | report server-side changes to bodies and our header lines instead of failing on them |

## What it does and checks

Every run has a fresh tag (`r20261109T150058-137c23`) in each Message-ID
(`<name.TAG.method@usenet-archiver.invalid>`) and Subject (`ua-e2e TAG method name`), and a current
Date, so runs never collide and can be repeated on the same server. The articles are the e2e
"tricky" set (From_ at every `>` depth, `#! rnews`, a CR inside a line, dot lines, 8-bit, a 9991-byte
and a 512 KiB line, header lines that start like an mbox separator) plus UTF-8, a folded header,
trailing white space and a reply. IHAVE articles carry `Path: ua-e2e!not-for-mail`, POSTed ones no
Path.

1. Injection: each reply is recorded. A refusal (441, 437, 435, 436 after one retry) is a finding
   about the server, listed in the report, never a failure.
2. The test polls `GROUP` and `OVER`/`XOVER` (else `XHDR`) on fresh connections until every accepted
   article is listed or `UA_REAL_WAIT` is over. An accepted article that is never listed fails.
3. It fetches each listed article by number (`ARTICLE n`) and compares it with what was injected:
   added fields, changed Path, Xref, Lines, Injection-\*, NNTP-Posting-\* and the like, unfolded
   fields and a changed field order are reported; a changed body or a lost or changed field of ours
   fails (`server:` lines; `UA_REAL_STRICT=0` makes them report lines).
4. It pulls the whole group with `-C 1`, with `-C 3`, with `-C 3 --start <yesterday>` (a date
   window), and again with `-C 1` into the first mbox. Each pull must exit 0 and record the job in
   `completed_newsgroups.log`; each listed article of this run must be in the mbox exactly once and
   identical, head and body, to what the server serves; the rerun must add nothing. These are the
   `archiver:` failures.

Comparisons ignore what the transport legitimately changes: dot-stuffing is undone, line ends are
CRLF on the wire and LF in the mbox, and trailing empty body lines are dropped (mbox cannot keep
them).

## Without a real server

`cmd/realserver-fake` is a stand-in: an nntp-testkit Store reader that takes POST, and an optional
feed port for IHAVE and POST that lists articles after a delay and can refuse or change some:

```bash
go run ./cmd/realserver-fake -addr 127.0.0.1:21119 -feed 127.0.0.1:21120 -delay 20s -reject-cr -reject-8bit &
UA_REAL_ADDR=127.0.0.1:21119 UA_REAL_INJECT=post,ihave UA_REAL_IHAVE_ADDR=127.0.0.1:21120 UA_REAL_WAIT=60 \
  go test -count=1 -run TestRealServer -v .
```

## Results

### 2026-11-09

usenet_archiver had no findings on any server: every pull exited 0, was recorded, and held each
listed article once, identical to what the server serves. What the servers did with the article set:

| Server | Injected | Refused | Changed (served vs injected) |
|---|---|---|---|
| INN 2.8.0 prerelease (main 4c5218607), nnrpd + innd | POST, IHAVE | colon-less `From ` header lines (441 / 437) | adds Path, Injection-Info, Organization, Xref; IHAVE prepends to Path. A bare CR is accepted and served unchanged (deliberate since 1999, logged only) |
| Diablo (main 5f3a3cc), dreaderd + feeder | POST, IHAVE | nothing | adds Xref; POST adds Path, Injection-Info; IHAVE prepends to Path. A bare CR is served unchanged (`rejectartswithbarecr` is off by default) |
| NewsCache 1.3.23 (ba7ef87) in front of OUS 0.8.0 | POST | bare CR (441, by design) | adds X-NNTP-Posting-Host; the rest is OUS's. A Latin-1 article got 240, stays in NewsCache's outgoing spool because OUS answers 403 |
| OpenUsenetServer 0.8.0 | POST, IHAVE | Latin-1 body (POST 403, IHAVE 436; OUS O-13) | **cuts a body line at a bare CR** (splits it into two lines); **rewrites a colon-less header line** as `name: value`; reorders and unfolds header fields (O-05); adds Path, Injection-*, Organization. Reported to its maintainers |
| go-pugleaf 0.6.5 (de62f1a), local scratch build | POST, IHAVE | colon-less `From ` header lines | sends a stored bare CR as a space, as RFC 3977 3.1.1 requires (the stored article keeps it); adds Path, Injection-Info, Xref |

A bare CR in a line is not allowed in an NNTP data block (RFC 3977 3.1.1) nor in an article
(RFC 5322 2.3), but old archives contain them; servers differ, so an archive pulled from one keeps
whatever that server serves.
