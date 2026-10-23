# Extras

Shell scripts for reporting on a **local INN/tradspool** tree (`/var/spool/news/articles/`, or `SPOOL` from the environment). They require **GNU userland** tools on the server (notably **GNU find**; `archive.sh` also GNU date and rsync 3.1 or later).

Also includes **`gz2mbox.py`**, which converts trees of `.gz` Usenet article dumps into a single mboxrd file, with the archiver's own writer (`usenet_archiver/mboxout.py`); see "mbox format" in the main README.

- A `.gz` holding an rnews batch (`#! rnews N` lines) is split by its byte counts; any other `.gz` is one article.
  For dumps of concatenated articles without that framing, `--split-on-path` splits at every line starting
  `Path: ` (a heuristic: a body line starting `Path: ` splits its article).
- The output is appended to, never overwritten; an article already in it is not written again.
- Articles over 1 MiB are skipped and counted (mbox2nntp cannot import them). A `.gz` that is already an mbox,
  a malformed batch or a corrupt `.gz`, is reported, the run goes on with the next file, and the script exits 1.

Full descriptions, options (such as **`REPORT_DIR`**), and usage examples are in the **[project README](../README.md)** under “Extras (server-side, tradspool layout)”.
