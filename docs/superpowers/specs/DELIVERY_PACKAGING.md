# Spec: delivery packaging — Article 2.2

Make the handover meet the contract's own definition of a delivery: an
electronic repository identified by **date, version, commit or tag, and
SHA-256 hash**.

**Status:** proposed. Implementation follows approval of this document.

**Scope:** `scripts/render_delivery_pdfs.py`, one new script, one new
contract test, the four delivery documents' front matter, and an annotated
git tag. No migration, no schema change, no change to `serving_geo`,
`explore` or any read path — so the `AGENTS.md` maintenance checklist reduces
to step 5 (add tests) and step 6 (this document).

**Honest scale, stated up front:** this is roughly 150 lines of script and a
tag. It adds no capability to the platform and no reviewer will enjoy reading
it. What it buys is narrow and non-optional: Article 2.2 names the SHA-256
hash as part of how a delivery is *identified*, so without this the handover
is arguably non-conforming regardless of how good the four Works are. It is
the cheapest unshipped item that can invalidate everything else.

**The test for whether this is worth doing properly:** *if the recipient
cannot independently verify every hash with one standard command, the
manifest is decoration.* A list of hashes typed into a document by the person
being paid proves nothing. So a machine-checkable `SHA256SUMS` that
`sha256sum -c` accepts is part of this change, not a nice-to-have, and the
human-readable manifest is generated from the same pass rather than written
beside it.

---

## What Article 2.2 asks for, and what we have

| Required | Status before this change |
|---|---|
| Date | Declared in each document's front matter — but all four say **8 November** while carrying 9 November revision rows |
| Version | Per-document revision tables exist; nothing names the delivery as a whole |
| Commit or tag | The commit exists; there is no tag |
| SHA-256 hash | **Absent.** Nothing in the repository hashes anything |

Three of the four are partially there and wrong in small ways. The fourth
does not exist.

---

## The measurement that shaped this

The obvious manifest pins the bytes of four PDFs and says "trust these". The
question worth asking first is whether the recipient could instead *rebuild*
them and compare — a far stronger property, because it needs no trust at all.

Measured, not assumed. Rendering the same Markdown twice and diffing:

```
differing bytes: 4 of 215,094
```

The four bytes are the two timestamps Chromium embeds:

```
offset 104  CreationDate  D:20261109105413+00'00'   (payload 23 bytes)
offset 144  ModDate       D:20261109105413+00'00'   (payload 23 bytes)
```

Everything else — every glyph, every inlined SVG, the xref table — is
byte-identical across builds. So the documents are already reproducible
except for a clock reading, and the gap closes with a fixed-length
substitution that leaves every byte offset and the xref valid.

**That changes the deliverable.** The manifest can promise rebuild-and-check
rather than trust-these-bytes, and it costs about thirty lines because there
was only ever one source of non-determinism.

⚠ The substitution must be **equal length**. The PDF xref table stores
absolute byte offsets; a 22- or 24-byte replacement for a 23-byte field
invalidates every offset after it and produces a file that some readers
repair silently and others reject. The timestamp format is fixed-width, so
this is free — but it is the one thing that must not be "simplified" later.

---

## Design

### 1. Deterministic PDF timestamps

The replacement value comes from the document's own declared `date:`, not
from a build-time constant and not from `SOURCE_DATE_EPOCH`:

- the PDF's metadata then **agrees with its own cover page**, which is what a
  reviewer checking provenance would compare it against
- it is a property of the source, so two people building the same commit on
  different days get the same bytes without coordinating an environment
  variable
- `SOURCE_DATE_EPOCH` would also work and is the broader convention, but it
  makes reproducibility conditional on the builder remembering to set it,
  which is exactly the failure this is meant to remove

Rejected: stripping the `Info` dictionary entirely. It changes the file
length, so the xref needs rebuilding — more machinery, and it would discard
`Creator`/`Producer`, which are genuine provenance.

### 2. The manifest

Two artefacts from one pass, because they serve different readers:

- `docs/delivery/SHA256SUMS` — plain, `sha256sum -c` accepts it verbatim.
  This is the one the recipient runs.
- `docs/delivery/MANIFEST.md` — the same hashes with the date, the version,
  the commit, the tag, and which Schedule A Work each artefact satisfies.
  This is the one that goes in the delivery email.

Both are generated. Neither is hand-edited, and a contract test asserts they
are current, the same way `docs/THIRD_PARTY_LICENSES.md` already is.

**What is hashed.** The four PDFs, their Markdown sources, and the licence
inventory Article 4.1 requires. Not the source tree: that is what the tag and
commit identify, and a tree hash duplicated in a file inside the tree is a
circularity with no reader.

⚠ The manifest cannot contain its own hash. `SHA256SUMS` covers the
artefacts; the manifest and `SHA256SUMS` are themselves covered by the
commit the tag points at. That is the standard resolution and it is worth
stating in the document rather than leaving a reviewer to notice the gap.

### 3. The tag

Annotated, not lightweight — a lightweight tag carries no date, no author and
no message, and Article 2.2 asks the tag to identify the delivery. The
message names the Works and the commit.

---

## What this does not do

- It does not make the *source tree* reproducible. The tag identifies it;
  reproducible builds of the Python environment are `uv.lock`'s job.
- It does not sign anything. A SHA-256 proves integrity against corruption
  and accidental substitution, not authorship. Signing would need a key the
  recipient has a reason to trust, and no such key exists in this engagement.
  Saying so is better than implying more than the hash provides.
- It does not change the delivery date. The documents will say 9 November
  because that is when this version was produced; Article 2.1's deadline is
  20 November and is unaffected.

---

## Acceptance

1. `python scripts/render_delivery_pdfs.py` twice in a row produces
   byte-identical PDFs.
2. `sha256sum -c docs/delivery/SHA256SUMS` passes from a clean checkout of
   the tag after one render.
3. `python scripts/delivery_manifest.py --check` fails when any artefact
   changes, and a contract test runs it.
4. The four documents declare one consistent date, and the PDFs' embedded
   metadata matches it.
