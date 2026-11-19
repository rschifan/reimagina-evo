# Delivery manifest

Delivered under Article 2.1, identified under Article 2.2 of the
agreement, which identifies a delivery by its date, version, commit or
tag, and SHA-256 hash.

| | |
|---|---|
| **Date** | 20 November 2026 |
| **Version** | 1.0.0 |
| **Tag** | `v1.0.0` |
| **Generated from commit** | `fc74d6b76ac6b1a29db2b395be29157769188ca3` |
| **Check out** | `git checkout v1.0.0` |

⚠ The tag is the authoritative identifier, and the **Check out** row
names what to check out for the procedures below. The commit above is
the one this file was generated *from*, which is necessarily the commit
before the one that adds it — a file cannot record the hash of the
commit that contains it. Resolve a tag to its commit with
`git rev-list -n 1 <tag>`.

## Artefacts

| Schedule A | File | Bytes | SHA-256 |
|---|---|---:|---|
| Work 1 — architecture map | `docs/delivery/pdf/01_architecture_map.pdf` | 222,976 | `cd23e234110e3f0855fee961c7dcb5ca4ab9c0aae09dac3f1b3eb52b9e1fe1ea` |
| Work 1 — source | `docs/delivery/01_architecture_map.md` | 28,050 | `f1a8c736c1272ed545242e78a5010f572555fbf5d82b4a6420f53bdcafcf7146` |
| Work 3 — database and API reference | `docs/delivery/pdf/03_database_and_api_reference.pdf` | 918,705 | `4cfcae83d5a690d425cd8697bf5a3c13e9ac58fa793843e99e08bc8d1858dd25` |
| Work 3 — source | `docs/delivery/03_database_and_api_reference.md` | 112,793 | `878bf1a0028b03612cfa3ba999cd1a205e9722b7fea4056757002e28fbfeb2cb` |
| Work 4 — technical documentation | `docs/delivery/pdf/04_technical_documentation.pdf` | 393,775 | `19d5ad2a455c662c3bcfeed3f9d5dbd7bc1efe2a34e128c37647dad7e97740e8` |
| Work 4 — source | `docs/delivery/04_technical_documentation.md` | 57,465 | `0f252d8f1035e5627bad538204777578aa3b6a7dbd5999f252e1fd7d4d869fca` |
| Article 4.1 — dependency inventory | `docs/THIRD_PARTY_LICENSES.md` | 38,709 | `fc95e762ee6eae07bd8cce22beaaec6bd807df76503594eced04595961103c51` |

## Source code (Schedule A Work 2)

| Schedule A | Source commit | Bytes | SHA-256 |
|---|---|---:|---|
| Work 2 — source code, `git archive` of the source commit | `fc74d6b76ac6b1a29db2b395be29157769188ca3` | 9,052,160 | `be77d82305f764871d7beace6ae6efde4262212e366694c8f9e6311a5e6c5f08` |

The hash is of the commit named above — the one this manifest was
generated from, which is the tag's parent — not of the tag itself: the
tag's commit contains this file, and a file cannot hold the hash of
something that contains it. The tag's commit differs from the source
commit by exactly `docs/delivery/MANIFEST.md` and
`docs/delivery/SHA256SUMS`. The archive is the *uncompressed* tar,
because a `.tar.gz` also depends on the gzip implementation. To
recompute it from any clone:

```bash
git archive --format=tar --prefix=reimagina-v1.0.0/ fc74d6b76ac6b1a29db2b395be29157769188ca3 | sha256sum
```

The release also carries `reimagina-v1.0.0.tar.gz`, the same
archive compressed with `gzip -n`; its own hash is in the release asset
`SHA256SUMS-assets`, which lists every file of the release.

## Verifying this

The four PDFs are **not in the repository**: they are generated, and
`.gitignore` excludes `docs/delivery/pdf/`. They travel as assets of
the GitHub release of tag `v1.0.0`, together with `MANIFEST.md`,
`SHA256SUMS` and the source archive. A bare checkout therefore reports
four `FAILED open or read` lines from `sha256sum -c`, which is expected,
not a corrupt delivery. Use whichever of the two procedures below
matches what you have.

### If you have the delivered PDFs

Download them from the release, put them in a checkout of the delivery
(the **Check out** row above) at the paths the table above gives
(`docs/delivery/pdf/`), then:

```bash
sha256sum -c docs/delivery/SHA256SUMS
```

Every line must read `OK`. `SHA256SUMS` carries the same hashes in the
format that command expects, so nothing has to be compared by eye.

### If you have only the source

Rebuild them. The PDFs are byte-reproducible: rendering them from the
same sources produces the same bytes, because the only non-determinism
in the pipeline — the two timestamps the PDF engine embeds — is pinned
to the date each document declares.

```bash
python scripts/render_delivery_pdfs.py
sha256sum -c docs/delivery/SHA256SUMS
```

All nine lines then read `OK`. This is the stronger of the two checks:
it needs no trust in the delivered files at all, only in the sources
the checkout holds. The hashes were produced with pandoc 3.6.3,
Node 24, and Playwright 1.64.1 with its Chromium; the render script's
docstring says how to provide them (`npm i --no-save playwright@1.64.1
&& npx playwright install chromium` in `apps/web`, then
`NODE_PATH=apps/web/node_modules`).

### What a hash does and does not establish

A SHA-256 proves these bytes are the bytes that were delivered — it
detects corruption in transit and accidental substitution. It is not a
signature and does not establish authorship, because that would need a
key the recipient has independent reason to trust, and no such key
exists in this engagement.

⚠ Neither this file nor `SHA256SUMS` can cover itself. The artefacts
are covered by `SHA256SUMS`; both files are covered by the delivery
commit the **Check out** row names.

---

Generated by `scripts/delivery_manifest.py`. Do not edit by hand — a
contract test regenerates it and fails if the result differs.
