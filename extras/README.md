# Extras

Shell scripts for reporting on a **local INN/tradspool** tree (`/var/spool/news/articles/`). They require **GNU userland** tools on the server (notably **GNU find**).

Also includes **`gz2mbox.py`**, which converts trees of `.gz` Usenet article dumps into a single mboxrd file, with the archiver's own writer (`usenet_archiver/mboxout.py`); see "mbox format" in the main README.

Full descriptions, options (such as **`REPORT_DIR`**), and usage examples are in the **[project README](../README.md)** under “Extras (server-side, tradspool layout)”.
