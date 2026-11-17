"""`tret/batches/registers/esef.py`: cleaning the ESEF filings index, ranking by
revenue from xBRL-JSON, the cache, and the register CSV it writes. No network:
a fake fetcher serves synthetic documents."""
from __future__ import annotations

import csv
import json

import pytest

from tret.batches.registers import esef
from tret.batches.registers.base import RegisterRow, write_register_csv
from tret.services.collections import parse_register

YEAR = 2024


def lei(n: int) -> str:
    return f"5493{n:016d}"


def entity(eid: str, identifier: str, name: str) -> dict:
    return {"type": "entity", "id": eid, "attributes": {"identifier": identifier, "name": name}}


def filing(fid: str, eid: str, *, country="FR", period_end="2024-12-31", errors=0,
           processed="2025-04-01 10:00:00.000000", json_url="auto", report_url="auto") -> dict:
    attrs = {
        "country": country,
        "period_end": period_end,
        "fxo_id": fid,
        "sha256": "sha" + fid,
        "error_count": errors,
        "processed": processed,
        "package_url": f"/{fid}/pkg.zip",
        "report_url": f"/{fid}/r.xhtml" if report_url == "auto" else report_url,
        "json_url": f"/{fid}/r.json" if json_url == "auto" else json_url,
    }
    return {
        "type": "filing",
        "id": fid,
        "attributes": attrs,
        "relationships": {"entity": {"data": {"type": "entity", "id": eid}}},
    }


def index(*filings: dict, entities: list[dict]) -> dict:
    return {"meta": {"count": len(filings)}, "data": list(filings), "included": entities}


def fact(concept: str, value, *, unit="iso4217:EUR", period="2024-01-01T00:00:00/2025-01-01T00:00:00", **extra) -> dict:
    dims = {"concept": concept, "entity": "scheme:x", "period": period, "unit": unit, **extra}
    return {"value": str(value), "dimensions": dims}


def xbrl(*facts: dict) -> dict:
    return {"documentInfo": {}, "facts": {f"f{i}": f for i, f in enumerate(facts)}}


class FakeFetcher:
    def __init__(self, docs: dict[str, object] | None = None, errors: dict[str, esef.FetchError] | None = None):
        self.docs = docs or {}
        self.errors = errors or {}
        self.calls: list[str] = []

    def get_bytes(self, url: str, *, max_bytes: int) -> bytes:
        self.calls.append(url)
        if url in self.errors:
            raise self.errors[url]
        return json.dumps(self.docs[url]).encode()


def url(fid: str) -> str:
    return f"https://filings.xbrl.org/{fid}/r.json"


# ── cleaning ─────────────────────────────────────────────────────────────────
def test_one_row_per_lei_prefers_fewest_errors_then_latest_processed():
    doc = index(
        filing("a1", "e1", errors=3),
        filing("a2", "e1", errors=0, processed="2025-03-01 00:00:00"),
        filing("a3", "e1", errors=0, processed="2025-05-01 00:00:00"),
        filing("b1", "e2"),
        entities=[entity("e1", lei(1), "Alpha SA"), entity("e2", lei(2), "Beta SA")],
    )
    res = esef.clean_index(doc, YEAR)
    assert {c.lei: c.fxo_id for c in res.candidates} == {lei(1): "a3", lei(2): "b1"}
    assert res.filings == 4
    assert res.dropped["older or worse filing of the same LEI"] == 2


def test_bad_period_end_ua_bad_lei_and_unnamed_entities_are_dropped():
    doc = index(
        filing("a", "e1", period_end="4172-12-31"),
        filing("b", "e1", period_end="2023-12-31"),
        filing("c", "e1", period_end="garbage"),
        filing("d", "e2", country="UA"),
        filing("e", "e3"),
        filing("f", "e4"),
        filing("g", "missing"),
        filing("h", "e5"),
        entities=[
            entity("e1", lei(1), "Alpha SA"),
            entity("e2", lei(2), "Kyiv"),
            entity("e3", "NOT-A-LEI", "Gamma"),
            entity("e4", lei(4), " \x00 "),
            entity("e5", lei(5).lower(), "Epsilon"),
        ],
    )
    res = esef.clean_index(doc, YEAR)
    assert [c.name for c in res.candidates] == ["Epsilon"]  # lowercase LEI is upper-cased
    assert res.candidates[0].lei == lei(5)
    assert res.dropped["period_end outside the year"] == 3
    assert res.dropped["country excluded"] == 1
    assert res.dropped["identifier is not a LEI"] == 1
    assert res.dropped["no company name"] == 1
    assert res.dropped["no entity"] == 1


def test_country_filters_and_market_mapping():
    doc = index(
        filing("a", "e1", country="GB"),
        filing("b", "e2", country="FR"),
        filing("c", "e3", country="NL"),
        entities=[entity("e1", lei(1), "A"), entity("e2", lei(2), "B"), entity("e3", lei(3), "C")],
    )
    assert esef.market_for("GB") == "UK" and esef.market_for("DK") == "EU"
    only = esef.clean_index(doc, YEAR, countries=["fr", "nl"])
    assert sorted(c.country for c in only.candidates) == ["FR", "NL"]
    skipped = esef.clean_index(doc, YEAR, exclude_countries=["GB"])
    assert sorted(c.country for c in skipped.candidates) == ["FR", "NL"]


def test_urls_are_made_absolute_and_foreign_hosts_refused():
    assert esef.absolute_url("/x/r.json") == "https://filings.xbrl.org/x/r.json"
    assert esef.absolute_url("https://filings.xbrl.org/x") == "https://filings.xbrl.org/x"
    for bad in ("https://evil.example/x", "//evil.example/x", "x/y", "/a/../b", "", None, "/a b"):
        assert esef.absolute_url(bad) is None


def test_names_are_sanitised():
    assert esef.clean_name("  Foo\x00\x07  \t Bar​\nBaz ") == "Foo Bar Baz"


# ── size ─────────────────────────────────────────────────────────────────────
def test_revenue_ignores_dimensional_facts_and_takes_the_latest_period():
    doc = xbrl(
        fact("ifrs-full:Revenue", 900, period="2023-01-01T00:00:00/2024-01-01T00:00:00"),
        fact("ifrs-full:Revenue", 1000),
        fact("ifrs-full:Revenue", 5000, **{"ifrs-full:ProductsAndServicesAxis": "x:Segment"}),
        fact("ifrs-full:Revenue", 70, unit="xbrli:pure"),
        fact("ifrs-full:Assets", 99999, period="2024-12-31"),
    )
    m = esef.extract_metric(doc)
    assert (m["metric"], m["value"], m["currency"]) == ("revenue", 1000.0, "EUR")


def test_assets_proxy_fallback_and_nothing_usable():
    m = esef.extract_metric(xbrl(fact("ifrs-full:Assets", 2000, unit="iso4217:SEK", period="2024-12-31")))
    assert m["metric"] == "assets_proxy" and m["value"] == pytest.approx(100.0) and m["currency"] == "SEK"
    assert esef.extract_metric(xbrl(fact("ifrs-full:Equity", 1)))["metric"] is None
    assert esef.extract_metric({"nofacts": 1})["metric"] is None
    assert esef.extract_metric([1, 2])["metric"] is None


def test_fx_conversion_is_approximate_and_unknown_currencies_have_no_size():
    assert esef.to_eur(100, "EUR") == 100
    assert esef.to_eur(100, "gbp") == pytest.approx(100 * esef.EUR_PER_UNIT["GBP"])
    assert esef.to_eur(100, "XXX") is None
    for ccy in ("EUR", "GBP", "SEK", "DKK", "NOK", "PLN", "CHF", "USD", "CZK", "HUF", "RON", "ISK"):
        assert ccy in esef.EUR_PER_UNIT


def _three(tmp_path):
    doc = index(
        filing("a", "e1"),
        filing("b", "e2", country="SE"),
        filing("c", "e3", json_url=None),
        filing("d", "e4"),
        entities=[entity("e1", lei(1), "Small"), entity("e2", lei(2), "Big"),
                  entity("e3", lei(3), "NoJson"), entity("e4", lei(4), "Broken")],
    )
    docs = {
        url("a"): xbrl(fact("ifrs-full:Revenue", 1000)),
        url("b"): xbrl(fact("ifrs-full:Revenue", 1_000_000, unit="iso4217:SEK")),  # ~88,000 EUR
        url("d"): xbrl(fact("ifrs-full:Equity", 5)),
    }
    return doc, docs


def test_ranking_orders_by_eur_size_with_unknown_last_and_caches(tmp_path):
    doc, docs = _three(tmp_path)
    cands = esef.clean_index(doc, YEAR).candidates
    fetcher = FakeFetcher(docs)
    ranked, stats = esef.rank_candidates(cands, fetcher=fetcher, cache_dir=tmp_path / "cache")
    assert [r.candidate.name for r in ranked] == ["Big", "Small", "Broken", "NoJson"]
    assert ranked[0].size_eur == pytest.approx(88_000) and ranked[0].currency == "SEK"
    assert ranked[2].size_eur is None and ranked[3].size_eur is None
    assert (stats.fetched, stats.cached, stats.unreadable) == (3, 0, 2)
    assert len(fetcher.calls) == 3
    # Only the small metric is cached, never the report.
    for f in (tmp_path / "cache").iterdir():
        assert set(json.loads(f.read_text())) <= {"metric", "value", "currency", "period", "reason", "raw"}

    again = FakeFetcher({})  # a second fetch would raise KeyError
    ranked2, stats2 = esef.rank_candidates(cands, fetcher=again, cache_dir=tmp_path / "cache")
    assert again.calls == [] and stats2.fetched == 0 and stats2.cached == 3
    assert [r.candidate.name for r in ranked2] == ["Big", "Small", "Broken", "NoJson"]


def test_limit_fetch_resumes_on_the_next_run(tmp_path):
    doc, docs = _three(tmp_path)
    cands = esef.clean_index(doc, YEAR).candidates
    f1 = FakeFetcher(docs)
    _, s1 = esef.rank_candidates(cands, fetcher=f1, cache_dir=tmp_path / "c", limit_fetch=1)
    assert len(f1.calls) == 1 and s1.not_fetched == 2
    f2 = FakeFetcher(docs)
    _, s2 = esef.rank_candidates(cands, fetcher=f2, cache_dir=tmp_path / "c")
    assert len(f2.calls) == 2 and s2.cached == 1  # the first run's one fetch is reused


def test_transient_errors_are_not_cached_but_permanent_ones_are(tmp_path):
    doc, docs = _three(tmp_path)
    cands = esef.clean_index(doc, YEAR).candidates
    errors = {
        url("a"): esef.FetchError("HTTP 503 after 6 attempts"),
        url("b"): esef.FetchError("too big", permanent=True),
    }
    f = FakeFetcher(docs, errors)
    _, stats = esef.rank_candidates(cands, fetcher=f, cache_dir=tmp_path / "c")
    assert stats.not_fetched == 1
    f2 = FakeFetcher(docs, errors)
    esef.rank_candidates(cands, fetcher=f2, cache_dir=tmp_path / "c")
    assert f2.calls == [url("a")]  # only the transient failure is retried


# ── output ───────────────────────────────────────────────────────────────────
def test_build_register_round_trips_through_parse_register_and_writes_the_sidecar(tmp_path):
    doc, docs = _three(tmp_path)
    doc["data"].append(filing("g", "e6", country="GB", report_url=None))
    doc["included"].append(entity("e6", lei(6), 'Brit, "Quoted" plc'))
    idx = tmp_path / "idx.json"
    idx.write_text(json.dumps(doc))
    out = tmp_path / "out" / "reg.csv"
    docs[url("g")] = xbrl(fact("ifrs-full:Revenue", 10, unit="iso4217:GBP"))
    summary = esef.build_register(
        YEAR, out, index_file=idx, rank_by="revenue", top=2,
        cache_dir=tmp_path / "cache", fetcher=FakeFetcher(docs),
    )
    assert summary.candidates == 5 and summary.written == 2

    rows, _ = parse_register(None, out.read_text(encoding="utf-8"), default_fiscal_year=2025)
    assert [r["company"] for r in rows] == ["Big", "Small"]
    big = rows[0]
    assert big["market"] == "EU" and big["domicile_country"] == "SE"
    assert big["category"] == "other" and big["expected_form"] == "embedded"
    assert big["fiscal_year"] == 2024 and big["known_urls"] == [] and big["sector"] is None
    assert big["notes"] == f"ESEF filing jurisdiction SE; LEI {lei(2)}"
    assert big["identifiers"] == {"lei": lei(2), "source_filing_url": "https://filings.xbrl.org/b/r.xhtml"}

    side = list(csv.DictReader((tmp_path / "out" / "reg.csv.ranking.csv").open(encoding="utf-8")))
    assert len(side) == 5  # all candidates, not just the top 2
    assert list(side[0]) == list(esef.SIDECAR_COLUMNS)
    assert side[0]["company"] == "Big" and side[0]["size_metric"] == "revenue" and side[0]["currency"] == "SEK"
    assert side[-1]["size_eur"] == "" and side[-1]["size_metric"] == ""


def test_gb_maps_to_uk_and_falls_back_to_the_package_url(tmp_path):
    doc = index(
        filing("g", "e1", country="GB", report_url=None),
        entities=[entity("e1", lei(1), 'Brit, "Quoted" plc')],
    )
    idx = tmp_path / "i.json"
    idx.write_text(json.dumps(doc))
    out = tmp_path / "r.csv"
    esef.build_register(YEAR, out, index_file=idx)
    rows, _ = parse_register(None, out.read_text(encoding="utf-8"), default_fiscal_year=2025)
    assert rows[0]["company"] == 'Brit, "Quoted" plc' and rows[0]["market"] == "UK"
    assert rows[0]["identifiers"]["source_filing_url"] == "https://filings.xbrl.org/g/pkg.zip"


def test_write_register_csv_round_trips_every_column(tmp_path):
    row = RegisterRow(
        company="Acme Inc", market="US", domicile_country="US", sector="Tools", category="industrial",
        expected_form="standalone", fiscal_year=2024, known_urls="https://a.example/r.pdf|https://a.example/b.pdf",
        notes="line one", lei=lei(9), cik="0000001234", ticker="acme", source_filing_url="https://a.example/f",
    )
    path = tmp_path / "r.csv"
    assert write_register_csv([row], path) == 1
    rows, _ = parse_register(None, path.read_text(encoding="utf-8"), default_fiscal_year=2025)
    r = rows[0]
    assert r["company"] == "Acme Inc" and r["known_urls"] == ["https://a.example/r.pdf", "https://a.example/b.pdf"]
    assert r["identifiers"] == {
        "lei": lei(9), "cik": "1234", "ticker": "ACME", "source_filing_url": "https://a.example/f",
    }


def test_load_index_rejects_a_document_without_data(tmp_path):
    p = tmp_path / "x.json"
    p.write_text('{"nope": 1}')
    with pytest.raises(ValueError, match="no 'data'"):
        esef.load_index(YEAR, index_file=p)


# ── the HTTP fetcher's retry rules, with a stub transport ────────────────────
def test_http_fetcher_backs_off_on_429_and_respects_the_size_cap(monkeypatch):
    import httpx

    seen = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["user-agent"] == "tret-register-importer"
        seen["n"] += 1
        if seen["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "7"})
        if request.url.path.endswith("big.json"):
            return httpx.Response(200, content=b"x" * 100)
        return httpx.Response(200, content=b"{}")

    sleeps: list[float] = []
    f = esef.HttpFetcher(min_interval=1.5, sleep=sleeps.append)
    f._client = httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": esef.USER_AGENT})
    assert f.get_bytes("https://filings.xbrl.org/a.json", max_bytes=1000) == b"{}"
    assert f.rate_limited == 1 and 7 in sleeps
    with pytest.raises(esef.FetchError) as e:
        f.get_bytes("https://filings.xbrl.org/big.json", max_bytes=10)
    assert e.value.permanent
    with pytest.raises(esef.FetchError):
        f.get_bytes("https://elsewhere.example/a.json", max_bytes=10)


# ── review fixes ─────────────────────────────────────────────────────────────
def test_full_year_revenue_beats_a_later_listed_quarter_with_the_same_end():
    q4 = "2024-10-01T00:00:00/2025-01-01T00:00:00"
    doc = xbrl(fact("ifrs-full:Revenue", 250, period=q4), fact("ifrs-full:Revenue", 1000))
    assert esef.extract_metric(doc)["value"] == 1000.0
    doc = xbrl(fact("ifrs-full:Revenue", 1000), fact("ifrs-full:Revenue", 250, period=q4))
    assert esef.extract_metric(doc)["value"] == 1000.0


def test_short_durations_are_ignored_only_when_a_full_year_exists():
    q4 = "2024-10-01T00:00:00/2025-01-01T00:00:00"
    h2 = "2024-07-01T00:00:00/2025-01-01T00:00:00"
    # A later-ending quarter does not beat an earlier full year.
    older_year = fact("ifrs-full:Revenue", 900, period="2023-01-01T00:00:00/2024-01-01T00:00:00")
    assert esef.extract_metric(xbrl(older_year, fact("ifrs-full:Revenue", 250, period=q4)))["value"] == 900.0
    # No full year at all: the longest available at the latest end.
    assert esef.extract_metric(xbrl(fact("ifrs-full:Revenue", 250, period=q4), fact("ifrs-full:Revenue", 500, period=h2)))["value"] == 500.0
    # Instants keep latest-end behaviour.
    m = esef.extract_metric(xbrl(
        fact("ifrs-full:Assets", 10, period="2023-12-31"), fact("ifrs-full:Assets", 20, period="2024-12-31"),
    ))
    assert m["period"] == "2024-12-31" and m["value"] == pytest.approx(1.0)


def test_non_finite_values_are_rejected():
    for bad in ("NaN", "inf", "-Infinity"):
        assert esef.extract_metric(xbrl(fact("ifrs-full:Revenue", bad)))["metric"] is None
    assert esef.extract_metric(xbrl(fact("ifrs-full:Revenue", "nan"), fact("ifrs-full:Revenue", 5)))["value"] == 5.0


def test_dedupe_empty_processed_loses_and_bad_error_count_is_worst():
    ents = [entity("e1", lei(1), "A")]
    # Same errors: an empty processed stamp must not beat a real one, in either order.
    for order in ((("a", ""), ("b", "2025-04-01")), (("b", "2025-04-01"), ("a", ""))):
        doc = index(*(filing(fid, "e1", processed=p) for fid, p in order), entities=ents)
        assert esef.clean_index(doc, YEAR).candidates[0].fxo_id == "b"
    # A missing or non-integer error_count is worst, not zero.
    for bad in (None, "many", 2.5, True):
        good = filing("good", "e1", errors=3)
        broken = filing("broken", "e1")
        broken["attributes"]["error_count"] = bad
        for pair in ((good, broken), (broken, good)):
            c = esef.clean_index(index(*pair, entities=ents), YEAR).candidates[0]
            assert c.fxo_id == ("good" if bad != 2.5 else "broken") or bad == 2.5
    missing = filing("m", "e1")
    del missing["attributes"]["error_count"]
    c = esef.clean_index(index(missing, filing("g", "e1", errors=9), entities=ents), YEAR).candidates[0]
    assert c.fxo_id == "g"


@pytest.mark.parametrize(
    "path",
    ["/a/b​.json", "/a/b .json", "/a/b .json", "/a/‮b.json", "/a/b\x7f.json", "/a/b c.json", "/a/b\x00"],
)
def test_absolute_url_rejects_invisible_and_control_characters(path):
    assert esef.absolute_url(path) is None
    assert esef.absolute_url(esef.BASE_URL + path) is None


def test_http_fetcher_turns_decoding_errors_into_transient_fetch_errors():
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-encoding": "gzip"}, content=b"this is not gzip")

    sleeps: list[float] = []
    f = esef.HttpFetcher(min_interval=0, max_retries=1, sleep=sleeps.append)
    f._client = httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": esef.USER_AGENT})
    with pytest.raises(esef.FetchError) as e:
        f.get_bytes("https://filings.xbrl.org/a.json", max_bytes=1000)
    assert not e.value.permanent and "DecodingError" in str(e.value)


def test_http_fetcher_403_is_transient_and_404_permanent():
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403 if "forbidden" in request.url.path else 404)

    f = esef.HttpFetcher(min_interval=0, max_retries=2, sleep=lambda s: None)
    f._client = httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": esef.USER_AGENT})
    with pytest.raises(esef.FetchError) as e:
        f.get_bytes("https://filings.xbrl.org/forbidden.json", max_bytes=10)
    assert not e.value.permanent and f.requests == 3  # retried with back-off
    with pytest.raises(esef.FetchError) as e:
        f.get_bytes("https://filings.xbrl.org/gone.json", max_bytes=10)
    assert e.value.permanent


def test_streamed_body_without_content_length_is_capped():
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        def body():
            for _ in range(10):
                yield b"x" * 10

        return httpx.Response(200, content=body())  # a generator: chunked, no content-length

    f = esef.HttpFetcher(min_interval=0, sleep=lambda s: None)
    f._client = httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": esef.USER_AGENT})
    with pytest.raises(esef.FetchError) as e:
        f.get_bytes("https://filings.xbrl.org/big.json", max_bytes=50)
    assert e.value.permanent and str(e.value) == esef.OVER_SIZE_REASON


def test_recursion_error_while_parsing_is_unreadable_not_a_crash(tmp_path):
    class Deep:
        def get_bytes(self, url, *, max_bytes):
            return b"[" * 200000 + b"]" * 200000

    doc, _ = _three(tmp_path)
    cands = esef.clean_index(doc, YEAR).candidates[:1]
    ranked, stats = esef.rank_candidates(cands, fetcher=Deep(), cache_dir=tmp_path / "c")
    assert stats.unreadable == 1 and ranked[0].size_eur is None


def test_five_consecutive_transient_failures_stop_fetching_and_write_outputs(tmp_path):
    ents = [entity(f"e{i}", lei(i), f"Co{i}") for i in range(1, 9)]
    doc = index(*(filing(f"f{i}", f"e{i}") for i in range(1, 9)), entities=ents)
    errors = {url(f"f{i}"): esef.FetchError("HTTP 403 after 6 attempts", status=403) for i in range(1, 9)}
    f = FakeFetcher({}, errors)
    out = tmp_path / "reg.csv"
    summary = esef.build_register(
        YEAR, out, rank_by="revenue", index_file=_write_index(tmp_path, doc), cache_dir=tmp_path / "c", fetcher=f,
    )
    assert len(f.calls) == esef.MAX_CONSECUTIVE_TRANSIENT
    assert summary.stats.aborted and summary.stats.not_fetched == 8
    assert out.exists() and summary.sidecar.exists()
    assert not (tmp_path / "c").exists() or not list((tmp_path / "c").iterdir())  # nothing cached


def test_a_success_resets_the_transient_streak(tmp_path):
    ents = [entity(f"e{i}", lei(i), f"Co{i}") for i in range(1, 9)]
    doc = index(*(filing(f"f{i}", f"e{i}") for i in range(1, 9)), entities=ents)
    cands = esef.clean_index(doc, YEAR).candidates
    errors = {url(f"f{i}"): esef.FetchError("HTTP 503", status=503) for i in (1, 2, 3, 4, 6, 7, 8)}
    f = FakeFetcher({url("f5"): xbrl(fact("ifrs-full:Revenue", 1))}, errors)
    _, stats = esef.rank_candidates(cands, fetcher=f, cache_dir=tmp_path / "c")
    assert not stats.aborted and len(f.calls) == 8


def test_over_size_reason_reaches_the_sidecar(tmp_path):
    doc, docs = _three(tmp_path)
    errors = {url("a"): esef.FetchError(esef.OVER_SIZE_REASON, permanent=True)}
    out = tmp_path / "reg.csv"
    summary = esef.build_register(
        YEAR, out, rank_by="revenue", index_file=_write_index(tmp_path, doc), cache_dir=tmp_path / "c",
        fetcher=FakeFetcher(docs, errors),
    )
    rows = list(csv.DictReader(open(summary.sidecar, encoding="utf-8")))
    assert esef.SIDECAR_COLUMNS[-1] == "reason"
    by_name = {r["company"]: r for r in rows}
    assert by_name["Small"]["reason"] == "over size cap"
    assert by_name["Big"]["reason"] == ""


def _write_index(tmp_path, doc):
    p = tmp_path / "index.json"
    p.write_text(json.dumps(doc))
    return p
