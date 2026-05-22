DROP TABLE IF EXISTS StormCounty;
DROP TABLE IF EXISTS County;
DROP TABLE IF EXISTS Storm;

CREATE TABLE Storm (
    stormCode BIGINT PRIMARY KEY,
    state VARCHAR(8) NOT NULL,
    eventDateTime TIMESTAMP NOT NULL,
    eventLength INTEGER NOT NULL
);

CREATE TABLE County (
    County VARCHAR(64) PRIMARY KEY,
    ohLine BIGINT NOT NULL
);

CREATE TABLE StormCounty (
    stormCode BIGINT NOT NULL,
    County VARCHAR(64) NOT NULL,
    sumAFWA_TOTPRECIP DOUBLE PRECISION NOT NULL,
    peakWSPD10 DOUBLE PRECISION NOT NULL,
    wgt5 INTEGER NOT NULL,
    wgt9 INTEGER NOT NULL,
    wgt13 INTEGER NOT NULL,
    wgt18 INTEGER NOT NULL,
    outage INTEGER NOT NULL,
    PRIMARY KEY (stormCode, County),
    CONSTRAINT fk_storm
        FOREIGN KEY (stormCode)
        REFERENCES Storm (stormCode),
    CONSTRAINT fk_county
        FOREIGN KEY (County)
        REFERENCES County (County)
);
