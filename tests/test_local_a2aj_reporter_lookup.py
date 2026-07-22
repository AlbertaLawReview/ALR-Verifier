import hashlib
import json

import duckdb

import a2aj_client
from local_a2aj import CorpusFile, LocalA2AJCorpus


def test_dotted_reporter_lookup_deduplicates_one_physical_corpus_row(tmp_path):
    corpus = LocalA2AJCorpus(tmp_path / "corpus")
    active = corpus.root / "cases"
    parquet = active / "SCC" / "train.parquet"
    parquet.parent.mkdir(parents=True)
    with duckdb.connect() as connection:
        connection.execute(
            "CREATE TABLE rows AS SELECT "
            "'SCC' dataset, '[1988] 2 SCR 833' citation_en, "
            "'[1988] 2 SCR 833' citation2_en, 'R. v. Bernard' name_en, "
            "'decision text' unofficial_text_en, "
            "'https://decisions.scc-csc.ca/scc-csc/scc-csc/en/item/388/index.do' url_en"
        )
        connection.table("rows").write_parquet(str(parquet))

    content = parquet.read_bytes()
    item = CorpusFile(
        "SCC/train.parquet", hashlib.sha256(content).hexdigest(), len(content)
    )
    (active / "manifest.json").write_text(
        json.dumps(
            {
                "revision": "test-revision",
                "files": [
                    {"path": item.path, "sha256": item.sha256, "size": item.size}
                ],
            }
        ),
        encoding="utf-8",
    )
    corpus._build_lookup_index(active, [item], "test-revision")

    rows = corpus.fetch("[1988] 2 S.C.R. 833", "cases")["json"]["results"]
    assert len(rows) == 1
    assert rows[0]["name_en"] == "R. v. Bernard"

    lookup = a2aj_client.A2AJClient(
        local_corpus=corpus,
        local_only=True,
        reporter_aliases_path="",
        min_seconds_between_requests=0,
    ).lookup("[1988] 2 S.C.R. 833", "cases")
    assert lookup.status == "found"
    assert lookup.document.url.endswith("/item/388/index.do")
