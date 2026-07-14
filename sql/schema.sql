-- ============================================================================
-- WISER Weather Database Schema
-- Connecticut Storm & Weather Analytics
-- ============================================================================

PRAGMA foreign_keys = OFF;

-- ============================================================================
-- Drop existing tables (reverse dependency order)
-- ============================================================================
DROP TABLE IF EXISTS WeatherData;
DROP TABLE IF EXISTS ExternalOutageObservation;
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
--   peakWSPD10: Peak wind speed at 10m height (m/s)
--   wgt5-18: Hours where wind speed exceeded threshold (m/s)
--   outage: Number of trouble spots / outage locations
-- ============================================================================
CREATE TABLE WeatherData (
    stormCode BIGINT NOT NULL,
    county VARCHAR(64) NOT NULL,
    sumAFWA_TOTPRECIP REAL NOT NULL,
    peakWSPD10 REAL NOT NULL,
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
    CHECK (wgt5 >= 0),
    CHECK (wgt9 >= 0),
    CHECK (wgt13 >= 0),
    CHECK (wgt18 >= 0),
    CHECK (outage >= 0)
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
CREATE INDEX idx_external_outage_source_time
    ON ExternalOutageObservation(sourceSystem, observedAt DESC);
CREATE INDEX idx_external_outage_county
    ON ExternalOutageObservation(county);
