-- ============================================================================
-- WISER Weather Database Schema
-- Connecticut Storm & Weather Analytics
-- ============================================================================

PRAGMA foreign_keys = ON;

-- ============================================================================
-- Drop existing tables (reverse dependency order)
-- ============================================================================
DROP TABLE IF EXISTS WeatherData;
DROP TABLE IF EXISTS County;
DROP TABLE IF EXISTS Storm;

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
-- County Table: Contains information about Connecticut counties
-- ohLine: Overhead line length in county (in specified units)
-- ============================================================================
CREATE TABLE County (
    county VARCHAR(64) PRIMARY KEY,
    ohLine BIGINT NOT NULL,
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
-- Indexes for common query patterns
-- ============================================================================
CREATE INDEX idx_weatherdata_outage ON WeatherData(outage DESC);
CREATE INDEX idx_weatherdata_county ON WeatherData(county);
CREATE INDEX idx_storm_datetime ON Storm(eventDateTime);
