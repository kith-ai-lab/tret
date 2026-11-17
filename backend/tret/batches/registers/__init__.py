"""Register importers: build a disclosure register CSV from a public filings index.

`base.py` holds the shared row type and CSV writer; each source (`esef.py` today)
turns its index into rows. Nothing here touches the database or a server."""
