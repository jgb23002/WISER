# WISER Starter Repo

![WISER Image](WISER%20Image.png)

This repo contains your professor-provided WISER material plus an initial starter implementation for:

1. Task Routing Agent
2. Data Retrieval and Processing Agent
3. Storm Weather Analytics Agent

## Current Branch

Initial development branch: `jiyabh_initial_setup`

## Project Structure

```text
.
├── scripts/
│   └── bootstrap_wiser.py
├── sql/
│   └── schema.sql
├── src/
│   └── wiser/
│       ├── agents/
│       │   ├── data_retrieval_processing_agent.py
│       │   ├── storm_weather_analytics_agent.py
│       │   └── task_routing_agent.py
│       └── data_pipeline.py
├── testdata.csv
└── README.md
```

## Quick Start

1. Create and activate a virtual environment (optional but recommended).
2. Install dependencies (currently none external, command kept for future updates):

```bash
pip install -r requirements.txt
```

3. Run bootstrap script:

```bash
python scripts/bootstrap_wiser.py --input testdata.csv --output-dir data/normalized
```

This generates normalized files:

- `data/normalized/storm.csv`
- `data/normalized/county.csv`
- `data/normalized/storm_county.csv`

## Database Design

Based on the normalization from your professor’s notes/image, schema is split into:

1. `Storm(stormCode, state, eventDateTime, eventLength)`
2. `County(County, ohLine)`
3. `StormCounty(stormCode, County, sumAFWA_TOTPRECIP, peakWSPD10, wgt5, wgt9, wgt13, wgt18, outage)`

SQL DDL is in `sql/schema.sql`.

![WISER Database Info](WISER%20Database%20Info.png)

## Data Notes

Source schema columns:
`eventCode, stormCode, eventLength, County, ohLine, sumAFWA_TOTPRECIP, peakWSPD10, wgt5, wgt9, wgt13, wgt18, outage`

- `outage`: trouble spots where crews were sent
- `ohLine`: overhead line length per county
- `wgtX`: hours where wind speed exceeded threshold
- `peakWSPD10`: peak wind speed
- `sumAFWA_TOTPRECIP`: storm precipitation (mm)

## Reference

[wisermaci.engr.uconn.edu](http://wisermaci.engr.uconn.edu/)
