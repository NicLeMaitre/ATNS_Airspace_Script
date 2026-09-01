Developed to ease the SACAA paperwork requirements for flying RPAS for commercial gain. 

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
