# Geography Assets

## `ct_counties_legacy.geojson`

This file contains the 8 legacy Connecticut county polygons used by WISER's
county-level heat maps.

Source: State of Connecticut, Office of Policy and Management, CT Counties
layer: https://geodata.ct.gov/datasets/ctmaps::ct-counties

Direct service used for this lightweight copy:
https://services3.arcgis.com/3FL1kr7L4LvwA2Kb/arcgis/rest/services/CT_Counties/FeatureServer/0

The layer documentation states that it represents the 8 counties that make up
Connecticut, derived from CTDOT municipality geometry dissolved into counties.
It also notes that the 9 planning regions have replaced counties for newer
statistical and administrative functions, so these 8 counties are legacy
geography. That matches the WISER dataset, which stores these county names:
Fairfield, Hartford, Litchfield, Middlesex, New Haven, New London, Tolland, and
Windham.

To keep the app responsive, the checked-in GeoJSON was exported from the
ArcGIS FeatureServer with coordinate precision and geometry generalization:

```text
where=1=1
outFields=County,CountyFIPS,StateFIPS,CountyFIPS_GEOID
returnGeometry=true
outSR=4326
f=geojson
geometryPrecision=4
maxAllowableOffset=0.002
```

Backup provenance: U.S. Census TIGER/Line county files are another authoritative
source for county boundaries. The 2020 TIGER/Line county shapefiles describe
boundaries and names as of January 1, 2020:
https://www.census.gov/geographies/mapping-files/time-series/geo/tiger-line-file.2020.html
