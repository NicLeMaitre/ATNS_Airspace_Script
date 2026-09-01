Developed to ease the SACAA paperwork requirements for flying RPAS for commercial gain. 

Search a KML or KMZ file (airspaces, airports, helipads, etc.) for every
feature within a given radius of a location, and print a table of
Name / Frequency. KMZ files (zipped KML, the normal Google Earth export
format) are unzipped automatically -- no manual extraction needed.

USAGE
-----
    python kml_radius_search.py INPUT.kml --lat 34.0522 --lon -118.2437 --radius 25

    python kml_radius_search.py INPUT.kmz --lat 51.4700 --lon -0.4543 \
        --radius 40 --units km --output results.csv

ARGUMENTS
---------
    input                 Path to the KML or KMZ file
    --lat                 Latitude of the search center (decimal degrees)
    --lon                 Longitude of the search center (decimal degrees)
    --radius              Search radius (in --units, default = nautical miles)
    --units {nm,km,mi}    Units for --radius and the printed distance column
    --output PATH.csv     Optional: also write the results to a CSV file
    --show-distance       Add a Distance column to the printed table
    --all-fields          Don't filter by radius; list everything (debugging)

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
"""
