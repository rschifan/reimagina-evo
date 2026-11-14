"""Decode a Mapbox Vector Tile and say what is in it. No dependencies.

The tile functions in ``serving_geo`` return ``bytea``. Verifying what they
returned normally means loading it into a map and looking, which confirms
nothing precisely: a tile with the right geometry and the wrong attributes
draws identically, and an empty tile and a broken tile both draw nothing.

So this reads the protobuf wire format directly, from the vector tile spec,
rather than through a library. Two reasons, and the second is the real one:

- it keeps the licence inventory honest — no new third-party dependency enters
  the delivered tree just to inspect a tile
- a lenient decoder proves less. ``mapbox-vector-tile`` will happily accept
  things the spec does not require, so "it parsed" is weaker evidence than
  "every field the spec names was present and these were the values".

Usage::

    psql -At -c "select encode(serving_geo.get_tile(14,2937,6619,'{}'),'base64')" \\
      | tr -d '\\n' | base64 -d > tile.mvt
    python scripts/mvt_inspect.py tile.mvt
    python scripts/mvt_inspect.py --json tile.mvt      # machine-readable

Two tiles compare feature by feature on one property, which is how a
scenario's effect is read off the bytes a client would actually receive::

    python scripts/mvt_inspect.py --diff baseline.mvt scenario.mvt \\
        --key feature_id --value value_numeric
"""

from __future__ import annotations

import argparse
import json
import pathlib
import struct
import sys
from typing import Any, Iterator

# Vector tile spec, section 4.3.4: the GeomType enum.
GEOM_TYPES = {0: "UNKNOWN", 1: "POINT", 2: "LINESTRING", 3: "POLYGON"}


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    """Read one base-128 varint. Returns (value, next offset)."""
    result = shift = 0
    while True:
        byte = buf[i]
        i += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, i
        shift += 7


def _fields(buf: bytes, start: int = 0, end: int | None = None) -> Iterator[tuple]:
    """Yield ``(field_number, wire_type, payload)`` for one protobuf message.

    Length-delimited payloads come back as ``bytes``; varints as ``int``;
    fixed-width as the raw 4 or 8 bytes. An unknown field is yielded like any
    other rather than skipped, so a tile carrying extensions is still read
    instead of silently truncated.
    """
    i = start
    end = len(buf) if end is None else end
    while i < end:
        key, i = _varint(buf, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _varint(buf, i)
            yield field, wire, value
        elif wire == 2:
            length, i = _varint(buf, i)
            yield field, wire, buf[i : i + length]
            i += length
        elif wire == 5:
            yield field, wire, buf[i : i + 4]
            i += 4
        elif wire == 1:
            yield field, wire, buf[i : i + 8]
            i += 8
        else:
            raise ValueError(f"unsupported protobuf wire type {wire} at byte {i}")


def _value(buf: bytes) -> Any:
    """One ``Tile.Value``: exactly one of its seven typed fields is set."""
    for field, _wire, payload in _fields(buf):
        if field == 1:
            return payload.decode("utf-8")
        if field == 2:
            return struct.unpack("<f", payload)[0]
        if field == 3:
            return struct.unpack("<d", payload)[0]
        if field in (4, 5):
            return payload
        if field == 6:  # sint64, zigzag-encoded
            return (payload >> 1) ^ -(payload & 1)
        if field == 7:
            return bool(payload)
    return None


def decode(raw: bytes) -> list[dict[str, Any]]:
    """Decode a whole tile into a list of layer dicts."""
    layers: list[dict[str, Any]] = []
    for field, _wire, payload in _fields(raw):
        if field != 3:  # Tile.layers
            continue
        name: str | None = None
        version: int | None = None
        extent = 4096  # spec default
        keys: list[str] = []
        values: list[Any] = []
        raw_features: list[bytes] = []
        for lf, _lw, lp in _fields(payload):
            if lf == 1:
                name = lp.decode("utf-8")
            elif lf == 2:
                raw_features.append(lp)
            elif lf == 3:
                keys.append(lp.decode("utf-8"))
            elif lf == 4:
                values.append(_value(lp))
            elif lf == 5:
                extent = lp
            elif lf == 15:
                version = lp

        features = []
        for rf in raw_features:
            fid: int | None = None
            tags: list[int] = []
            geom_type = 0
            geom_ints = 0
            for ff, _fw, fp in _fields(rf):
                if ff == 1:
                    fid = fp
                elif ff == 2:
                    j = 0
                    while j < len(fp):
                        tag, j = _varint(fp, j)
                        tags.append(tag)
                elif ff == 3:
                    geom_type = fp
                elif ff == 4:
                    j = 0
                    while j < len(fp):
                        _, j = _varint(fp, j)
                        geom_ints += 1
            props = {
                keys[tags[k]]: values[tags[k + 1]] for k in range(0, len(tags) - 1, 2)
            }
            features.append(
                {
                    "id": fid,
                    "geometry_type": GEOM_TYPES.get(geom_type, str(geom_type)),
                    "properties": props,
                    "geometry_commands": geom_ints,
                }
            )
        layers.append(
            {
                "name": name,
                "spec_version": version,
                "extent": extent,
                "feature_count": len(features),
                "property_keys": sorted(keys),
                "features": features,
            }
        )
    return layers


def summarise(layers: list[dict[str, Any]]) -> list[str]:
    out = []
    for layer in layers:
        out.append(
            f"layer {layer['name']!r}  spec v{layer['spec_version']}  "
            f"extent {layer['extent']}  {layer['feature_count']} features"
        )
        grouped: dict[tuple, int] = {}
        for feature in layer["features"]:
            props = feature["properties"]
            key = (
                feature["geometry_type"],
                props.get("type_slug"),
                props.get("role"),
            )
            grouped[key] = grouped.get(key, 0) + 1
        for geom, slug, role in sorted(grouped, key=str):
            parts = [f"{grouped[(geom, slug, role)]:>5}  {geom:<10}"]
            if slug is not None:
                parts.append(f"type_slug={slug}")
            if role is not None:
                parts.append(f"role={role}")
            out.append("  " + " ".join(parts))
        out.append(f"  properties: {', '.join(layer['property_keys'])}")
    if not layers:
        out.append("empty tile: no layers (0 bytes, or every feature clipped away)")
    return out


def diff(
    left: list[dict[str, Any]],
    right: list[dict[str, Any]],
    *,
    key: str,
    value: str,
) -> list[str]:
    """Compare two tiles feature by feature on one property."""

    def index(layers):
        return {
            f["properties"].get(key): f["properties"].get(value)
            for layer in layers
            for f in layer["features"]
        }

    a, b = index(left), index(right)
    out = [f"{key:<40}{'left':>14}{'right':>14}{'delta':>10}", ""]
    changed = 0
    for k in sorted(a, key=str):
        av, bv = a.get(k), b.get(k)
        if bv is None:
            delta = "(absent)"
        elif av == bv:
            delta = "="
        else:
            changed += 1
            delta = (
                f"{bv - av:+g}"
                if isinstance(av, (int, float)) and isinstance(bv, (int, float))
                else "changed"
            )
        out.append(f"{str(k)[:38]:<40}{str(av):>14}{str(bv):>14}{delta:>10}")
    out.append("")
    out.append(f"{changed} of {len(a)} features differ between the two tiles")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tile", help="Path to a .mvt file.")
    parser.add_argument("second", nargs="?", help="Second tile, with --diff.")
    parser.add_argument("--json", action="store_true", help="Emit the full decode.")
    parser.add_argument(
        "--diff", action="store_true", help="Compare two tiles property by property."
    )
    parser.add_argument("--key", default="feature_id", help="Join key for --diff.")
    parser.add_argument("--value", default="value_numeric", help="Compared property.")
    args = parser.parse_args(argv)

    left = decode(pathlib.Path(args.tile).read_bytes())
    if args.diff:
        if not args.second:
            print("--diff needs two tiles", file=sys.stderr)
            return 2
        right = decode(pathlib.Path(args.second).read_bytes())
        print("\n".join(diff(left, right, key=args.key, value=args.value)))
        return 0
    if args.json:
        print(json.dumps(left, indent=2, default=str))
        return 0
    print("\n".join(summarise(left)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
