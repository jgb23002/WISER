-- ============================================================================
-- WISER Weather Database Schema
-- Connecticut Storm & Weather Analytics
-- ============================================================================

PRAGMA foreign_keys = OFF;

-- ============================================================================
-- Drop existing tables (reverse dependency order)
-- ============================================================================
DROP TABLE IF EXISTS WeatherData;
DROP TABLE IF EXISTS HourlyWeatherObservation;
DROP TABLE IF EXISTS CurrentWeatherObservation;
DROP TABLE IF EXISTS ExternalOutageObservation;
DROP TABLE IF EXISTS StormMetricDefinition;
DROP TABLE IF EXISTS RegionCountyMapping;
DROP TABLE IF EXISTS StormCounty;
DROP TABLE IF EXISTS County;
DROP TABLE IF EXISTS Storm;

PRAGMA foreign_keys = ON;

-- ============================================================================
-- Storm Table: Contains information about weather events
-- stormCode: 10-digit code formatted as YYYYMMDDHH (e.g., 2005100800)
-- eventDateTime: Derived from stormCode timestamp
-- ============================================================================
CREATE TABLE Storm (
    stormCode BIGINT PRIMARY KEY,
    eventDateTime TIMESTAMP NOT NULL,
    eventLength INTEGER NOT NULL,
    UNIQUE (eventDateTime),
    CHECK (eventLength > 0)
);

-- ============================================================================
-- County Table: Contains information about counties
-- state:  Two-letter US state abbreviation (e.g. 'CT').
--         Defaults to 'CT' so existing data needs no migration.
--         Future multi-state expansion should promote (state, county)
--         to a composite primary key and update WeatherData's FK
--         accordingly.
-- ohLine: Overhead line length in county (in specified units)
-- ============================================================================
CREATE TABLE County (
    county VARCHAR(64) PRIMARY KEY,
    state  VARCHAR(2)  NOT NULL DEFAULT 'CT',
    ohLine BIGINT NOT NULL,
    CHECK (LENGTH(state) = 2),
    CHECK (ohLine > 0)
);

-- ============================================================================
-- WeatherData Table: Contains weather observations for storm + county pairs
-- Represents the normalized intersection: one record per (stormCode, county)
--
-- Fields:
--   sumAFWA_TOTPRECIP: Total precipitation during storm (mm)
--   peakWSPD10: Peak SUSTAINED wind speed at 10m height (m/s)
--   peakGUST10: Peak wind gust magnitude (m/s), NULL when source omits it
--   wgt5-18: Hours where the wind GUST exceeded 5/9/13/18 m/s
--   outage: Number of trouble spots / outage locations
--
-- wgt* and peakWSPD10 do not measure the same quantity, and the data proves
-- it: 211 rows have wgt13 > 0 while peakWSPD10 < 13, and all 11 rows with
-- wgt18 > 0 have peakWSPD10 < 18 (statewide MAX(peakWSPD10) is 13.11 m/s, so
-- no row could reach 18 m/s sustained). Middlesex in storm 2006090118 records
-- an hour above 18 m/s against a 7.51 m/s peak sustained wind.
--
-- Gusts exceeding sustained speed is the explanation consistent with all four
-- thresholds; the source AFWA field has not been confirmed, so treat "gust" as
-- the working interpretation rather than a verified provenance claim. What is
-- certain is that wgt* must NOT be read as hours above peakWSPD10, and that a
-- threshold-hour count is exposure, never a peak magnitude.
-- ============================================================================
CREATE TABLE WeatherData (
    stormCode BIGINT NOT NULL,
    county VARCHAR(64) NOT NULL,
    sumAFWA_TOTPRECIP REAL NOT NULL,
    peakWSPD10 REAL NOT NULL,
    peakGUST10 REAL,
    wgt5 INTEGER NOT NULL,
    wgt9 INTEGER NOT NULL,
    wgt13 INTEGER NOT NULL,
    wgt18 INTEGER NOT NULL,
    outage INTEGER NOT NULL,
    PRIMARY KEY (stormCode, county),
    FOREIGN KEY (stormCode) REFERENCES Storm(stormCode),
    FOREIGN KEY (county) REFERENCES County(county),
    CHECK (sumAFWA_TOTPRECIP >= 0),
    CHECK (peakWSPD10 > 0),
    CHECK (peakGUST10 IS NULL OR peakGUST10 >= 0),
    CHECK (wgt5 >= 0),
    CHECK (wgt9 >= 0),
    CHECK (wgt13 >= 0),
    CHECK (wgt18 >= 0),
    CHECK (outage >= 0)
);

-- ============================================================================
-- RegionCountyMapping Table: Default regional rollups for Connecticut
--
-- Sponsor-specific service territory analysis requires territory boundary or
-- county-to-territory mapping data. Until that is supplied, this table supports
-- transparent Connecticut planning regions such as northern/southern CT.
-- ============================================================================
CREATE TABLE RegionCountyMapping (
    region VARCHAR(64) NOT NULL,
    county VARCHAR(64) NOT NULL,
    state  VARCHAR(2)  NOT NULL DEFAULT 'CT',
    source TEXT NOT NULL DEFAULT 'wiser_default',
    PRIMARY KEY (region, county, state),
    FOREIGN KEY (county) REFERENCES County(county),
    CHECK (LENGTH(state) = 2)
);

-- ============================================================================
-- StormMetricDefinition Table: Metric labels, units, and severity direction
--
-- New mock-data metrics such as humidity can be registered here without
-- hardcoding every presentation label into each query path.
-- ============================================================================
CREATE TABLE StormMetricDefinition (
    metricKey TEXT PRIMARY KEY,
    displayName TEXT NOT NULL,
    storedUnit TEXT NOT NULL,
    higherIsMoreSevere INTEGER NOT NULL DEFAULT 1,
    sourceColumn TEXT,
    CHECK (higherIsMoreSevere IN (0, 1))
);

-- ============================================================================
-- HourlyWeatherObservation Table: Future hourly 48-window weather metrics
--
-- This table normalizes wide hourly source columns into a long form:
-- one metric value per storm, county, hour offset, and metric. Keeping the
-- model long rather than 48 hardcoded columns lets future metrics such as
-- humidity and gust timestamps arrive without schema churn.
-- ============================================================================
CREATE TABLE HourlyWeatherObservation (
    stormCode BIGINT NOT NULL,
    county VARCHAR(64) NOT NULL,
    hourOffset INTEGER NOT NULL,
    metricKey TEXT NOT NULL,
    metricValue REAL NOT NULL,
    observedAt TIMESTAMP,
    sourceColumn TEXT,
    PRIMARY KEY (stormCode, county, hourOffset, metricKey),
    FOREIGN KEY (stormCode) REFERENCES Storm(stormCode),
    FOREIGN KEY (county) REFERENCES County(county),
    FOREIGN KEY (metricKey) REFERENCES StormMetricDefinition(metricKey),
    CHECK (hourOffset >= 0 AND hourOffset < 48)
);

-- ============================================================================
-- CurrentWeatherObservation Table: Staging table for current/live weather feeds
--
-- This table represents observed current-month weather/outage rows, separate
-- from the historical storm event table. "This month" sponsor queries prefer
-- this table when it has rows for the current calendar month; otherwise WISER
-- falls back to historical calendar-month comparison with a clear notice.
-- ============================================================================
CREATE TABLE CurrentWeatherObservation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sourceSystem TEXT NOT NULL,
    sourceRecordId TEXT NOT NULL,
    observedAt TIMESTAMP NOT NULL,
    county VARCHAR(64),
    state VARCHAR(2) NOT NULL DEFAULT 'CT',
    peakWSPD10 REAL,
    peakGUST10 REAL,
    sumAFWA_TOTPRECIP REAL,
    humidity REAL,
    outage INTEGER,
    rawPayload TEXT NOT NULL,
    UNIQUE (sourceSystem, sourceRecordId, observedAt, county),
    FOREIGN KEY (county) REFERENCES County(county),
    CHECK (LENGTH(state) = 2),
    CHECK (peakWSPD10 IS NULL OR peakWSPD10 >= 0),
    CHECK (peakGUST10 IS NULL OR peakGUST10 >= 0),
    CHECK (sumAFWA_TOTPRECIP IS NULL OR sumAFWA_TOTPRECIP >= 0),
    CHECK (humidity IS NULL OR (humidity >= 0 AND humidity <= 100)),
    CHECK (outage IS NULL OR outage >= 0)
);

-- ============================================================================
-- ExternalOutageObservation Table: Staging/provenance table for utility/source
-- outage feeds such as EagleEye and National Grid.
--
-- This intentionally does not write into WeatherData because live utility feeds
-- may represent incidents, customers, assets, feeders, or service territories
-- rather than one normalized historical (stormCode, county) observation.
-- ============================================================================
CREATE TABLE ExternalOutageObservation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sourceSystem TEXT NOT NULL,
    sourceRecordId TEXT NOT NULL,
    observedAt TIMESTAMP NOT NULL,
    county VARCHAR(64),
    serviceTerritory TEXT,
    outageCount INTEGER,
    customerCount INTEGER,
    restorationStatus TEXT,
    rawPayload TEXT NOT NULL,
    UNIQUE (sourceSystem, sourceRecordId, observedAt),
    CHECK (outageCount IS NULL OR outageCount >= 0),
    CHECK (customerCount IS NULL OR customerCount >= 0)
);

-- ============================================================================
-- Indexes for common query patterns
-- ============================================================================
CREATE INDEX idx_weatherdata_outage ON WeatherData(outage DESC);
CREATE INDEX idx_weatherdata_county ON WeatherData(county);
CREATE INDEX idx_storm_datetime ON Storm(eventDateTime);
CREATE INDEX idx_region_county_state
    ON RegionCountyMapping(state, region, county);
CREATE INDEX idx_hourly_weather_storm_county_hour
    ON HourlyWeatherObservation(stormCode, county, hourOffset);
CREATE INDEX idx_hourly_weather_metric
    ON HourlyWeatherObservation(metricKey, metricValue DESC);
CREATE INDEX idx_current_weather_time
    ON CurrentWeatherObservation(observedAt DESC);
CREATE INDEX idx_current_weather_county_time
    ON CurrentWeatherObservation(county, observedAt DESC);
CREATE INDEX idx_external_outage_source_time
    ON ExternalOutageObservation(sourceSystem, observedAt DESC);
CREATE INDEX idx_external_outage_county
    ON ExternalOutageObservation(county);
