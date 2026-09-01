#!/usr/bin/env python3
"""
kml_radius_search.py

Search a KML or KMZ file (airspaces, airports, helipads, etc.) for every
feature within a given radius of a location, and print two tables of
Name / Frequency / Base Alt / Ceiling -- split by whether the feature
falls within a drone pilot's altitude ceiling (default 400 ft AGL) or
entirely above it. KMZ files (zipped KML, the normal Google Earth export
format) are unzipped automatically -- no manual extraction needed.

USAGE
-----
    python kml_radius_search.py INPUT.kml --lat 34.0522 --lon -118.2437 --radius 25

    python kml_radius_search.py INPUT.kmz --lat 51.4700 --lon -0.4543 \
        --radius 40 --units km --max-altitude 400 --output results.csv

ARGUMENTS
---------
    input                 Path to the KML or KMZ file
    --lat                 Latitude of the search center (decimal degrees)
    --lon                 Longitude of the search center (decimal degrees)
    --radius              Search radius (in --units, default = nautical miles)
    --units {nm,km,mi}    Units for --radius and the printed distance column
    --max-altitude        Your ceiling in feet AGL (default 400) -- defines the
                           top of the cylinder used to split results
    --output PATH.csv     Optional: also write the results to a CSV file
    --show-distance       Add a Distance column to the printed tables
    --all-fields          Don't filter by radius; list everything (debugging)
    --include-waypoints   Include Waypoints-folder features (excluded by default)
    --include-routes      Include Routes-folder features (excluded by default)
    --include-navaids     Include Navaids-folder features (excluded by default)

CATEGORY FILTERING
--------------------
By default, features whose folder path (the KML Folder/Document nesting
they live under) contains "waypoint", "route", or "navaid" (case
insensitive) are left out of the results entirely -- these are navigation
aids rather than airspace or aerodrome/helipad info, and just add noise
for this use case. Pass --include-waypoints / --include-routes /
--include-navaids to bring any of them back.

OUTPUT SECTIONS
----------------
    Section 1: features that intersect the cylinder formed by the search
               radius and your altitude ceiling (0 ft to --max-altitude AGL)
               -- these are the ones that matter for your flight.
    Section 2: airspaces whose base is entirely above your ceiling --
               informational only, no action needed.
    Section 3: airspace polygons where no vertical limits could be parsed
               from the source data -- flagged so you can check manually
               rather than silently assuming they're safe.
    Points (airports, helipads, navaids, corridors) have no vertical extent
    of their own and are always placed in Section 1, since they're relevant
    at ground level regardless of altitude.

HOW MATCHING WORKS
-------------------
- Points (airports, helipads, navaids, etc.): included if the point itself
  falls within the radius of the search center.
- Lines / Polygons (airspace boundaries): included if the search center
  falls *inside* the polygon, OR if any vertex of the shape is within the
  radius. This means a huge airspace that merely overlaps the search
  circle will still show up, which is what you want for airspace lookups.

HOW FREQUENCY IS FOUND
-----------------------
KML sources vary widely, so this script looks in a few places, in order:
  1. <ExtendedData><Data name="..."> or <SimpleData name="..."> where the
     name contains "freq" (case-insensitive).
  2. The free-text <description> field, searching for patterns like
     "Freq: 118.500" or "121.500 MHz".
If nothing is found, the Frequency column is left blank.

HOW ALTITUDE IS FOUND
-----------------------
Looks for a vertical-limits line in the description, e.g.
"1500FT AGL - FL145" or "GND - 1500FT ALT" or "FL145 / FL195". Flight
levels (FLnnn) are treated as nnn x 100 ft. Source data is not always
explicit about AGL vs AMSL/ALT for the lower limit -- this takes the
number at face value and does not correct for terrain elevation, so
treat the split as a guide and confirm against the current AIP/NOTAMs.
"""

import argparse
import csv
import math
import re
import sys
import zipfile
from xml.etree import ElementTree as ET

EARTH_RADIUS_KM = 6371.0088

UNIT_FACTORS = {
    "km": 1.0,
    "nm": 1.0 / 1.852,   # km -> nautical miles
    "mi": 1.0 / 1.60934,  # km -> statute miles
}

FREQ_LINE_RE = re.compile(r"(\d{2,3}[.,]\d{1,3})\s*mhz", re.IGNORECASE)
GENERIC_LABEL_RE = re.compile(r"^freq(?:uency)?\.?$", re.IGNORECASE)
BARE_CODE_RE = re.compile(r"^[\d/]+$")

# Matches an altitude range like "1500FT AGL - FL145", "GND - 1500FT ALT",
# "FL145 / FL195", using either a hyphen, slash, or en/em dash as separator.
ALT_TOKEN = r"(?:GND|SFC|UNL|FL\d{2,3}|\d{2,5}\s?FT(?:\s*(?:AGL|ALT|AMSL))?)"
ALT_RANGE_RE = re.compile(rf"({ALT_TOKEN})\s*[-/\u2013\u2014]\s*({ALT_TOKEN})", re.IGNORECASE)


# --------------------------------------------------------------------------
# XML helpers (namespace-agnostic)
# --------------------------------------------------------------------------

def local(tag):
    """Strip the XML namespace off a tag, e.g. '{...}Placemark' -> 'Placemark'."""
    return tag.split("}")[-1] if "}" in tag else tag


def get_child(elem, tagname):
    for c in elem:
        if local(c.tag) == tagname:
            return c
    return None


def iter_placemarks(root):
    """Yield (placemark, folder_path) for every Placemark in the document.
    folder_path is the list of enclosing <Folder>/<Document> names, e.g.
    ["Airspaces", "05. ATZ's & CTR's", "Class C", "FAOR CTR"] -- used to
    filter out whole categories (Waypoints, Routes, Navaids, etc.)."""
    results = []

    def walk(elem, path):
        tag = local(elem.tag)
        if tag in ("Folder", "Document"):
            name_el = get_child(elem, "name")
            name = name_el.text.strip() if (name_el is not None and name_el.text) else None
            new_path = path + [name] if name else path
            for child in elem:
                walk(child, new_path)
        elif tag == "Placemark":
            results.append((elem, path))
        else:
            for child in elem:
                walk(child, path)

    walk(root, [])
    return results


# Folder-name keywords (case-insensitive substring match against any level
# of a placemark's folder path) used to identify categories that can be
# excluded with --exclude-waypoints / --exclude-routes / --exclude-navaids.
CATEGORY_KEYWORDS = {
    "waypoints": ["waypoint"],
    "routes": ["route"],
    "navaids": ["navaid"],
}


def folder_path_matches(folder_path, keywords):
    joined = " ".join(p for p in folder_path if p).lower()
    return any(kw in joined for kw in keywords)


def parse_coordinates(text):
    """Parse a KML <coordinates> text blob into a list of (lon, lat, alt) tuples."""
    pts = []
    for chunk in text.split():
        parts = chunk.split(",")
        if len(parts) >= 2:
            try:
                lon = float(parts[0])
                lat = float(parts[1])
                pts.append((lon, lat))
            except ValueError:
                continue
    return pts


# --------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------

def get_geometries(placemark):
    """Return a list of (kind, [(lat, lon), ...]) for every simple geometry
    found anywhere inside this Placemark (handles MultiGeometry transparently
    since ElementTree's .iter() walks all descendants)."""
    geoms = []
    for el in placemark.iter():
        tag = local(el.tag)
        if tag == "Point":
            coords_el = get_child(el, "coordinates")
            if coords_el is not None and coords_el.text:
                pts = parse_coordinates(coords_el.text)
                if pts:
                    geoms.append(("Point", [(p[1], p[0]) for p in pts]))
        elif tag == "LineString":
            coords_el = get_child(el, "coordinates")
            if coords_el is not None and coords_el.text:
                pts = parse_coordinates(coords_el.text)
                if pts:
                    geoms.append(("LineString", [(p[1], p[0]) for p in pts]))
        elif tag == "Polygon":
            outer = None
            for boundary in el:
                if local(boundary.tag) == "outerBoundaryIs":
                    ring = get_child(boundary, "LinearRing")
                    if ring is not None:
                        coords_el = get_child(ring, "coordinates")
                        if coords_el is not None and coords_el.text:
                            pts = parse_coordinates(coords_el.text)
                            outer = [(p[1], p[0]) for p in pts]
            if outer:
                geoms.append(("Polygon", outer))
    return geoms


def haversine_km(lat1, lon1, lat2, lon2):
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def point_in_polygon(lat, lon, poly):
    """Simple ray-casting point-in-polygon test. poly is [(lat, lon), ...].
    Flat lat/lon plane approximation -- fine for airspace-sized polygons."""
    n = len(poly)
    if n < 3:
        return False
    inside = False
    x, y = lon, lat
    j = n - 1
    for i in range(n):
        xi, yi = poly[i][1], poly[i][0]
        xj, yj = poly[j][1], poly[j][0]
        if (yi > y) != (yj > y):
            x_intersect = (xj - xi) * (y - yi) / ((yj - yi) or 1e-15) + xi
            if x < x_intersect:
                inside = not inside
        j = i
    return inside


def min_distance_km(geoms, center_lat, center_lon):
    """Closest-approach distance in km from the search center to this
    feature. Returns 0.0 if the center is inside a polygon geometry."""
    min_dist = math.inf
    for kind, pts in geoms:
        for lat, lon in pts:
            d = haversine_km(center_lat, center_lon, lat, lon)
            if d < min_dist:
                min_dist = d
        if kind == "Polygon" and point_in_polygon(center_lat, center_lon, pts):
            return 0.0
    return min_dist


# --------------------------------------------------------------------------
# Attribute extraction
# --------------------------------------------------------------------------

def extract_name(placemark):
    """Get a human-readable name for this feature.

    Handles a common real-world quirk: some sources give the <name> element
    a bare reference code (e.g. "139/05/0008") and put the real name either
    in ExtendedData (a field ending in "name", e.g. "aerodrome_name") or as
    the first line of <description>. When that happens, this returns
    "CODE - Real Name" instead of just the unhelpful code.
    """
    name_el = get_child(placemark, "name")
    raw_name = name_el.text.strip() if (name_el is not None and name_el.text) else ""
    if not raw_name:
        raw_name = "(unnamed)"

    # Prefer a descriptive ExtendedData field like "aerodrome_name" if the
    # element name isn't already descriptive.
    extended_name = None
    for el in placemark.iter():
        if local(el.tag) == "Data":
            attr = el.get("name", "").lower()
            if attr.endswith("name") and attr != "name":
                val_el = get_child(el, "value")
                if val_el is not None and val_el.text and val_el.text.strip():
                    extended_name = val_el.text.strip()
                    break

    if extended_name and extended_name.lower() != raw_name.lower():
        return f"{raw_name} - {extended_name}"

    # Otherwise, if the name looks like a bare reference code, fall back to
    # the first line of the description (as long as it isn't itself just a
    # frequency line).
    if BARE_CODE_RE.match(raw_name):
        desc_el = get_child(placemark, "description")
        if desc_el is not None and desc_el.text:
            first_line = desc_el.text.strip().splitlines()[0].strip()
            if first_line and len(first_line) < 80 and not FREQ_LINE_RE.search(first_line):
                return f"{raw_name} - {first_line}"

    return raw_name


def extract_frequencies(placemark):
    """Return a list of frequency strings found for this feature.

    Checks ExtendedData first, then scans the free-text description
    line-by-line for any "NNN.NN MHz" / "NNN,NN MHz" patterns (comma or
    period as the decimal separator, case-insensitive MHz). When a line
    carries a label (e.g. "FAKM APP: 119,4 MHz" or "Freq: 125.6 MHz
    (Tower)"), that label is kept alongside the frequency so multiple
    frequencies for one feature (APP/TWR/GND/etc.) stay distinguishable.
    """
    results = []

    # 1. ExtendedData: <Data name="..."><value>...</value></Data>
    #    or <SimpleData name="...">...</SimpleData>
    for el in placemark.iter():
        tag = local(el.tag)
        if tag == "Data":
            name_attr = el.get("name", "")
            if "freq" in name_attr.lower():
                val_el = get_child(el, "value")
                if val_el is not None and val_el.text and val_el.text.strip():
                    results.append(val_el.text.strip())
        elif tag == "SimpleData":
            name_attr = el.get("name", "")
            if "freq" in name_attr.lower() and el.text and el.text.strip():
                results.append(el.text.strip())
    if results:
        return results

    # 2. Fallback: scan the free-text description, line by line
    desc_el = get_child(placemark, "description")
    if desc_el is None or not desc_el.text:
        return results

    for line in desc_el.text.splitlines():
        last_end = 0
        for m in FREQ_LINE_RE.finditer(line):
            freq_val = m.group(1).replace(",", ".")
            # Only look at text since the previous match on this line, so
            # "Freq: 126.7 MHz (North) & 128.3 MHz (South)" doesn't bleed
            # the first frequency's label into the second one.
            prefix = line[last_end : m.start()].strip(" :()*\t-&")
            suffix_match = re.match(r"\s*\(([^)]+)\)", line[m.end():])
            note = suffix_match.group(1).strip() if suffix_match else ""
            # Advance past any "(note)" we just consumed too, so it doesn't
            # leak into the next frequency's prefix on the same line.
            last_end = m.end() + (suffix_match.end() if suffix_match else 0)

            label = ""
            if prefix and not GENERIC_LABEL_RE.match(prefix):
                label = prefix
            elif note:
                label = note

            entry = f"{freq_val} MHz ({label})" if label else f"{freq_val} MHz"
            if entry not in results:
                results.append(entry)

    return results


def alt_token_to_feet(token):
    """Convert a raw altitude token (e.g. 'GND', 'FL145', '1500FT AGL') to
    a numeric altitude in feet. Flight levels (FLnnn) are nnn x 100 ft.
    Returns None if the token can't be parsed."""
    t = token.strip().upper()
    if t in ("GND", "SFC"):
        return 0.0
    if t == "UNL":
        return math.inf
    m = re.match(r"FL(\d{2,3})$", t)
    if m:
        return float(m.group(1)) * 100
    m = re.match(r"(\d{2,5})\s?FT", t)
    if m:
        return float(m.group(1))
    return None


def get_altitude_range(placemark):
    """Look for a vertical-limits line in the description, e.g.
    "1500FT AGL - FL145" or "GND - 1500FT ALT". Returns
    (lower_ft, upper_ft, lower_label, upper_label) or None if no altitude
    range could be found/parsed.

    Caveat: source data is inconsistent about AGL vs AMSL/ALT for the
    lower limit. This treats the raw number at face value; it does not
    (and generally cannot, without per-location terrain data) correct for
    the AGL/AMSL difference.
    """
    desc_el = get_child(placemark, "description")
    if desc_el is None or not desc_el.text:
        return None
    m = ALT_RANGE_RE.search(desc_el.text)
    if not m:
        return None
    lower_label, upper_label = m.group(1).strip(), m.group(2).strip()
    lower_ft = alt_token_to_feet(lower_label)
    upper_ft = alt_token_to_feet(upper_label)
    if lower_ft is None or upper_ft is None:
        return None
    return (lower_ft, upper_ft, lower_label, upper_label)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def print_table(rows, headers):
    widths = [len(h) for h in headers]
    for row in rows:
        for i, val in enumerate(row):
            widths[i] = max(widths[i], len(str(val)))

    def fmt_row(vals):
        return "  ".join(str(v).ljust(widths[i]) for i, v in enumerate(vals))

    print(fmt_row(headers))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print(fmt_row(row))


def load_kml_root(path):
    """Load a .kml or .kmz file and return the parsed XML root element.

    .kmz files are just zip archives containing one or more .kml files
    (conventionally named doc.kml). This detects the zip signature rather
    than trusting the file extension, unzips in memory, and parses the
    first .kml entry found inside.
    """
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            kml_names = [n for n in zf.namelist() if n.lower().endswith(".kml")]
            if not kml_names:
                sys.exit(f"Error: '{path}' is a .kmz/zip file but contains no .kml file.")
            # Prefer a file literally named doc.kml if present (the usual
            # convention); otherwise just take the first .kml found.
            kml_name = next(
                (n for n in kml_names if n.lower().endswith("doc.kml")), kml_names[0]
            )
            with zf.open(kml_name) as f:
                data = f.read()
        try:
            return ET.fromstring(data)
        except ET.ParseError as e:
            sys.exit(f"Error: could not parse '{kml_name}' inside '{path}' as XML/KML: {e}")

    try:
        return ET.parse(path).getroot()
    except ET.ParseError as e:
        sys.exit(f"Error: could not parse '{path}' as XML/KML: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="Find airspaces/airports/helipads in a KML file within a radius of a "
        "point, split by whether they fall within your drone's altitude ceiling."
    )
    parser.add_argument("input", help="Path to the KML or KMZ file")
    parser.add_argument("--lat", type=float, required=True, help="Center latitude")
    parser.add_argument("--lon", type=float, required=True, help="Center longitude")
    parser.add_argument("--radius", type=float, required=True, help="Search radius")
    parser.add_argument(
        "--units", choices=["nm", "km", "mi"], default="nm",
        help="Units for --radius and the distance column (default: nm)",
    )
    parser.add_argument(
        "--max-altitude", type=float, default=400.0,
        help="Your maximum operating altitude in feet AGL -- defines the top of the "
        "cylinder used to split results into sections (default: 400, the standard "
        "drone limit)",
    )
    parser.add_argument("--output", help="Optional path to write results as CSV")
    parser.add_argument(
        "--show-distance", action="store_true",
        help="Add a Distance column to the printed tables",
    )
    parser.add_argument(
        "--all-fields", action="store_true",
        help="Ignore the radius and list every feature (useful for debugging)",
    )
    parser.add_argument(
        "--include-waypoints", action="store_true",
        help="Include features from folders whose path contains 'waypoint' "
        "(excluded by default)",
    )
    parser.add_argument(
        "--include-routes", action="store_true",
        help="Include features from folders whose path contains 'route' "
        "(excluded by default)",
    )
    parser.add_argument(
        "--include-navaids", action="store_true",
        help="Include features from folders whose path contains 'navaid' "
        "(excluded by default)",
    )
    args = parser.parse_args()

    try:
        root = load_kml_root(args.input)
    except FileNotFoundError:
        sys.exit(f"Error: file not found: '{args.input}'")

    placemarks = iter_placemarks(root)
    if not placemarks:
        sys.exit("No <Placemark> elements found in this KML file.")

    include_flags = {
        "waypoints": args.include_waypoints,
        "routes": args.include_routes,
        "navaids": args.include_navaids,
    }
    excluded_count = 0

    factor = UNIT_FACTORS[args.units]
    radius_km = args.radius / factor
    ceiling = args.max_altitude

    # section: 1 = intersects the drone's cylinder (must check), 2 = entirely
    # above it (informational), 3 = vertical limits unknown (verify manually)
    results = []
    for pm, folder_path in placemarks:
        geoms = get_geometries(pm)
        if not geoms:
            continue  # no usable geometry (e.g. a folder-level placemark)

        dist_km = min_distance_km(geoms, args.lat, args.lon)
        if not args.all_fields and dist_km > radius_km:
            continue

        skip = False
        for category, keywords in CATEGORY_KEYWORDS.items():
            if not include_flags[category] and folder_path_matches(folder_path, keywords):
                skip = True
                break
        if skip:
            excluded_count += 1
            continue


        name = extract_name(pm)
        freq = "; ".join(extract_frequencies(pm))
        dist_display = round(dist_km * factor, 1)

        alt_info = get_altitude_range(pm)
        has_polygon = any(kind == "Polygon" for kind, _ in geoms)

        if alt_info:
            lower_ft, upper_ft, lower_label, upper_label = alt_info
            section = 1 if lower_ft <= ceiling else 2
        elif has_polygon:
            # A real airspace volume, but we couldn't parse its vertical
            # limits from the description -- don't guess, flag for a
            # manual check instead.
            lower_label, upper_label = "", ""
            section = 3
        else:
            # Points/lines (airports, helipads, navaids, corridors) have no
            # vertical extent of their own -- treat as ground-level/always
            # relevant to a low-altitude drone flight.
            lower_label, upper_label = "GND", ""
            section = 1

        results.append((section, dist_display, name, freq, lower_label, upper_label))

    if not results:
        if excluded_count:
            print(
                f"No features found within the given radius "
                f"({excluded_count} waypoint/route/navaid feature(s) in range were excluded)."
            )
        else:
            print("No features found within the given radius.")
        return

    results.sort(key=lambda r: (r[0], r[1]))

    def build_rows(section_results):
        rows = []
        for _, dist, name, freq, lower, upper in section_results:
            row = [name, freq, lower, upper]
            if args.show_distance:
                row.append(f"{dist} {args.units}")
            rows.append(row)
        return rows

    headers = ["Name", "Frequency", "Base Alt", "Ceiling"]
    if args.show_distance:
        headers.append("Distance")

    sec1 = [r for r in results if r[0] == 1]
    sec2 = [r for r in results if r[0] == 2]
    sec3 = [r for r in results if r[0] == 3]

    print(
        f"Drone ceiling: {ceiling:.0f} ft AGL   |   Search radius: {args.radius} {args.units} "
        f"around ({args.lat}, {args.lon})"
    )
    print(
        "Note: source altitude labels aren't always explicit about AGL vs AMSL/ALT -- "
        "treat Base Alt/Ceiling as a guide and confirm against the current AIP/NOTAMs "
        "before flying.\n"
    )

    print(f"SECTION 1 -- Within your {ceiling:.0f} ft AGL cylinder (check before flying)")
    if sec1:
        print_table(build_rows(sec1), headers)
    else:
        print("(none)")

    print(f"\nSECTION 2 -- Entirely above {ceiling:.0f} ft AGL (informational only)")
    if sec2:
        print_table(build_rows(sec2), headers)
    else:
        print("(none)")

    if sec3:
        print("\nSECTION 3 -- Vertical limits not stated in the data (verify manually)")
        print_table(build_rows(sec3), headers)

    summary = (
        f"\n{len(results)} feature(s) found within {args.radius} {args.units} "
        f"({len(sec1)} in Section 1, {len(sec2)} in Section 2, {len(sec3)} in Section 3)."
    )
    if excluded_count:
        summary += f" {excluded_count} waypoint/route/navaid feature(s) in range were excluded."
    print(summary)

    if args.output:
        with open(args.output, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            csv_headers = ["Section", "Name", "Frequency", "Base Alt", "Ceiling"]
            if args.show_distance:
                csv_headers.append("Distance")
            writer.writerow(csv_headers)
            for section, dist, name, freq, lower, upper in results:
                row = [section, name, freq, lower, upper]
                if args.show_distance:
                    row.append(f"{dist} {args.units}")
                writer.writerow(row)
        print(f"Results written to {args.output}")


if __name__ == "__main__":
    main()
