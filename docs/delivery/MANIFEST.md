# Delivery manifest

Delivered under Article 2.1, identified under Article 2.2 of the
agreement, which identifies a delivery by its date, version, commit or
tag, and SHA-256 hash.

| | |
|---|---|
| **Date** | 20 November 2026 |
| **Version** | 1.0.0 |
| **Tag** | `v1.0.0` |
| **Generated from commit** | `4133d3b28a091b048b5e586fa8eab6cb0d0c597d` |
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
| Work 1 — architecture map | `docs/delivery/pdf/01_architecture_map.pdf` | 211,428 | `905ccea3328f2e47338ff1639a99efe8781edf0e0f9d1448617be403787fc713` |
| Work 1 — source | `docs/delivery/01_architecture_map.md` | 25,297 | `a5c8231e89b1df5b631696ef8e89e86aab6b1cdabf6c2a91a760665e740a5dd0` |
| Work 3 — database and API reference | `docs/delivery/pdf/03_database_and_api_reference.pdf` | 922,214 | `01ef3bf174ceab77f6eff168cebef532f1cacb3aa5cda4ec71219d1be9b1b030` |
| Work 3 — source | `docs/delivery/03_database_and_api_reference.md` | 113,182 | `b0356a19557f0f3fe31b03aacda638ea1c318baa25f93693b1defc42877772d6` |
| Work 4 — technical documentation | `docs/delivery/pdf/04_technical_documentation.pdf` | 873,159 | `f1a04d2005f745e723f5ff3f49f4f4350e69da82bc3ed60b7f0fcfcee1de707b` |
| Work 4 — source | `docs/delivery/04_technical_documentation.md` | 122,883 | `b44acb0d7b8fd3ce862a9a9483a4bd401afb1a2203dd830c3b6c25993a90b73e` |
| Article 4.1 — dependency inventory | `docs/THIRD_PARTY_LICENSES.md` | 38,709 | `fc95e762ee6eae07bd8cce22beaaec6bd807df76503594eced04595961103c51` |

## Source code (Schedule A Work 2)

| Schedule A | Source commit | Bytes | SHA-256 |
|---|---|---:|---|
| Work 2 — source code, `git archive` of the source commit | `4133d3b28a091b048b5e586fa8eab6cb0d0c597d` | 9,216,000 | `369e93f3735478e6415de96e807b5b72452bd96e9fc34cf50057305b284e760f` |

The hash is of the commit named above — the one this manifest was
generated from, which is the tag's parent — not of the tag itself: the
tag's commit contains this file, and a file cannot hold the hash of
something that contains it. The tag's commit differs from the source
commit by exactly `docs/delivery/MANIFEST.md` and
`docs/delivery/SHA256SUMS`. The archive is the *uncompressed* tar,
because a `.tar.gz` also depends on the gzip implementation. To
recompute it from any clone:

```bash
git archive --format=tar --prefix=reimagina-v1.0.0/ 4133d3b28a091b048b5e586fa8eab6cb0d0c597d | sha256sum
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
