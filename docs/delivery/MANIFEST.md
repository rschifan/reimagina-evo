# Delivery manifest

Delivered under Article 2.1, identified under Article 2.2 of the
agreement, which identifies a delivery by its date, version, commit or
tag, and SHA-256 hash.

| | |
|---|---|
| **Date** | 20 November 2026 |
| **Version** | 1.0.0 |
| **Tag** | `v1.0.0` |
| **Generated from commit** | `d13e612d4317c4e955e4487e4f44d058aa837da4` |
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
| Work 1 — architecture map | `docs/delivery/pdf/01_architecture_map.pdf` | 222,974 | `9c26305c41a10661e4a743b95fd2e7aac3019d7b1b7d4d8e5f101d403783e8ba` |
| Work 1 — source | `docs/delivery/01_architecture_map.md` | 28,050 | `ed4e1fa02f3ca06365ec4b5b213fd93102773f095be477f113e0eeaa93ace8a6` |
| Work 3 — database and API reference | `docs/delivery/pdf/03_database_and_api_reference.pdf` | 924,361 | `7a180dff2dfe86c7626fb53ae3fb5fefa36adbff351e48f0369158147119d171` |
| Work 3 — source | `docs/delivery/03_database_and_api_reference.md` | 113,858 | `73f9a1b12125aeadfb31633620d47ac991f93a47ab31b94bbd204253d99bd027` |
| Work 4 — technical documentation | `docs/delivery/pdf/04_technical_documentation.pdf` | 399,968 | `822e1f0d8c6ae22630a3656a9dc5d734dc04afb3a9ff50f223d01984f0e8890c` |
| Work 4 — source | `docs/delivery/04_technical_documentation.md` | 58,679 | `f620fd6db5514baf33533c8c32f759bac0a8119b74e8424d7d5ad3cd97194dec` |
| Article 4.1 — dependency inventory | `docs/THIRD_PARTY_LICENSES.md` | 38,709 | `fc95e762ee6eae07bd8cce22beaaec6bd807df76503594eced04595961103c51` |

## Source code (Schedule A Work 2)

| Schedule A | Source commit | Bytes | SHA-256 |
|---|---|---:|---|
| Work 2 — source code, `git archive` of the source commit | `d13e612d4317c4e955e4487e4f44d058aa837da4` | 9,154,560 | `75ec7356e099ee1c995d4a4a81e58d8f939ee589704237b1e10368b72024f408` |

The hash is of the commit named above — the one this manifest was
generated from, which is the tag's parent — not of the tag itself: the
tag's commit contains this file, and a file cannot hold the hash of
something that contains it. The tag's commit differs from the source
commit by exactly `docs/delivery/MANIFEST.md` and
`docs/delivery/SHA256SUMS`. The archive is the *uncompressed* tar,
because a `.tar.gz` also depends on the gzip implementation. To
recompute it from any clone:

```bash
git archive --format=tar --prefix=reimagina-v1.0.0/ d13e612d4317c4e955e4487e4f44d058aa837da4 | sha256sum
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
