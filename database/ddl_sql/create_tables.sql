-- Storm Table: Contains information about the storm. 
-- eventDateTime derived from eventCode.
CREATE TABLE Storm (
    stormCode bigint NOT NULL,
    eventDateTime TIMESTAMP NOT NULL,
    eventLength int NOT NULL,--is always 48? maybe should be removed?
    PRIMARY KEY (stormCode),
    UNIQUE (eventDateTime)
    CHECK (eventLength > 0)
)

-- County Table: Contains information about the county.
CREATE TABLE County (
    county varchar(255) NOT NULL,
    ohLine int NOT NULL,
    PRIMARY KEY (county)
    CHECK (ohLine > 0)
)

-- WeatherData Table: Contains weather data for a specific storm and county.
CREATE TABLE WeatherData (
    stormCode bigint NOT NULL,
    county varchar(255) NOT NULL,
    sumAFWA_TOTPRECIP float NOT NULL,--precipitation during storm (mm)
    peakWSPD10 float NOT NULL,--wind speed peak hours (m/s)
    wgt5 int NOT NULL,--num of hours where  winds speed above 5 m/s
    wgt9 int NOT NULL,--num of hours where  winds speed above 9 m/s   
    wgt13 int NOT NULL,--num of hours where  winds speed above 13 m/s
    wgt18 int NOT NULL,--num of hours where  winds speed above 18 m/s   
    outage int NOT NULL,--trouble spots counted and sent crew to
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
)