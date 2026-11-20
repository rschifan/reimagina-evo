# Delivery manifest

Delivered under Article 2.1, identified under Article 2.2 of the
agreement, which identifies a delivery by its date, version, commit or
tag, and SHA-256 hash.

| | |
|---|---|
| **Date** | 20 November 2026 |
| **Version** | 1.0.0 |
| **Tag** | `v1.0.0` |
| **Generated from commit** | `dcb5bdb7c24cd53dd5bde254fd68e005188ce174` |
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
| Work 1 — architecture map | `docs/delivery/pdf/01_architecture_map.pdf` | 210,452 | `1ea21aa42e8e64cb5e42fc1e15cdafd96e8ab0ec562fd1c3201e91c88687b908` |
| Work 1 — source | `docs/delivery/01_architecture_map.md` | 25,435 | `e5d85d13d3e9356e88b1da21ebb942177bf48815b79836bd43f73acf16f48b30` |
| Work 3 — database and API reference | `docs/delivery/pdf/03_database_and_api_reference.pdf` | 922,121 | `dbd86c0b1bf420064a85b419902e7ff7700d937fae06d6504810d51290d0835d` |
| Work 3 — source | `docs/delivery/03_database_and_api_reference.md` | 113,320 | `e7c289fb9205ef9eb5b4fa2847ffa7d68dcd259d7727429ac18d22acba0b2092` |
| Work 4 — technical documentation | `docs/delivery/pdf/04_technical_documentation.pdf` | 883,674 | `5d87c2ba63e8832ca052344710f4af2c20b35e26859d1dc973272baa6103a8f3` |
| Work 4 — source | `docs/delivery/04_technical_documentation.md` | 126,108 | `202d52f4d2dffe22503a8b4539d254abf771ee1a3f35ee40715937787daacb6e` |
| Article 4.1 — dependency inventory | `docs/THIRD_PARTY_LICENSES.md` | 38,709 | `fc95e762ee6eae07bd8cce22beaaec6bd807df76503594eced04595961103c51` |

## Source code (Schedule A Work 2)

| Schedule A | Source commit | Bytes | SHA-256 |
|---|---|---:|---|
| Work 2 — source code, `git archive` of the source commit | `dcb5bdb7c24cd53dd5bde254fd68e005188ce174` | 9,236,480 | `fae771afbf5c31a7b63cfd088c1674b6563732cf5b8a9d5d88d31db6b5ef2d82` |

The hash is of the commit named above — the one this manifest was
generated from, which is the tag's parent — not of the tag itself: the
tag's commit contains this file, and a file cannot hold the hash of
something that contains it. The tag's commit differs from the source
commit by exactly `docs/delivery/MANIFEST.md` and
`docs/delivery/SHA256SUMS`. The archive is the *uncompressed* tar,
because a `.tar.gz` also depends on the gzip implementation. To
recompute it from any clone:

```bash
git archive --format=tar --prefix=reimagina-v1.0.0/ dcb5bdb7c24cd53dd5bde254fd68e005188ce174 | sha256sum
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
