-- Inside Airbnb Los Angeles -- DSCI 551 Fall 2026
-- Baseline physical design: primary keys, foreign keys (InnoDB auto-indexes
-- FK columns that lack a usable leading index), uniqueness constraints, and
-- the reverse junction index from the proposal. Experimental indexes (P1-P4)
-- are added and dropped by experiments/run_experiments.py, never here.

DROP DATABASE IF EXISTS airbnb_la;
CREATE DATABASE airbnb_la CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE airbnb_la;

CREATE TABLE neighbourhood (
    neighbourhood_id  SMALLINT UNSIGNED NOT NULL AUTO_INCREMENT,
    name              VARCHAR(100)      NOT NULL,
    group_name        VARCHAR(60)       NULL,
    PRIMARY KEY (neighbourhood_id),
    UNIQUE KEY uq_neighbourhood_name (name)
) ENGINE=InnoDB;

CREATE TABLE host (
    host_id               BIGINT UNSIGNED   NOT NULL,
    host_since            DATE              NULL,
    is_superhost          BOOLEAN           NULL,      -- NULL when source is blank
    source_listings_count SMALLINT UNSIGNED NULL,      -- source value, kept only for comparison
    PRIMARY KEY (host_id)
) ENGINE=InnoDB;

CREATE TABLE listing (
    listing_id        BIGINT UNSIGNED   NOT NULL,
    host_id           BIGINT UNSIGNED   NOT NULL,
    neighbourhood_id  SMALLINT UNSIGNED NOT NULL,
    name              VARCHAR(255)      NULL,
    room_type         ENUM('Entire home/apt','Private room','Shared room','Hotel room') NOT NULL,
    latitude          DECIMAL(8,6)      NULL,
    longitude         DECIMAL(9,6)      NULL,
    accommodates      TINYINT UNSIGNED  NULL,
    bedrooms          TINYINT UNSIGNED  NULL,
    price             DECIMAL(8,2)      NULL,
    review_score      DECIMAL(3,2)      NULL,
    license           VARCHAR(100)      NULL,
    PRIMARY KEY (listing_id),
    CONSTRAINT fk_listing_host  FOREIGN KEY (host_id)          REFERENCES host (host_id),
    CONSTRAINT fk_listing_nbhd  FOREIGN KEY (neighbourhood_id) REFERENCES neighbourhood (neighbourhood_id),
    CONSTRAINT ck_listing_score CHECK (review_score IS NULL OR review_score BETWEEN 0 AND 5),
    CONSTRAINT ck_listing_price CHECK (price IS NULL OR price >= 0)
) ENGINE=InnoDB;

-- Clustered on (listing_id, cal_date): one listing's dates are physically
-- adjacent; a date-only predicate has no useful leading key column.
CREATE TABLE calendar (
    listing_id      BIGINT UNSIGNED   NOT NULL,
    cal_date        DATE              NOT NULL,
    available       BOOLEAN           NOT NULL,
    price           DECIMAL(8,2)      NULL,
    minimum_nights  SMALLINT UNSIGNED NULL,
    PRIMARY KEY (listing_id, cal_date),
    CONSTRAINT fk_calendar_listing FOREIGN KEY (listing_id)
        REFERENCES listing (listing_id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE review (
    review_id    BIGINT UNSIGNED NOT NULL,
    listing_id   BIGINT UNSIGNED NOT NULL,
    review_date  DATE            NOT NULL,
    reviewer_id  BIGINT UNSIGNED NULL,      -- reviewer names are dropped during cleaning
    comments     TEXT            NULL,
    PRIMARY KEY (review_id),
    CONSTRAINT fk_review_listing FOREIGN KEY (listing_id)
        REFERENCES listing (listing_id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE amenity (
    amenity_id    SMALLINT UNSIGNED NOT NULL AUTO_INCREMENT,
    amenity_name  VARCHAR(150)      NOT NULL,
    PRIMARY KEY (amenity_id),
    UNIQUE KEY uq_amenity_name (amenity_name)
) ENGINE=InnoDB;

-- PK (listing_id, amenity_id) serves "amenities of a listing";
-- idx_amenity_listing (amenity_id, listing_id) serves "listings with an amenity"
-- and also backs the amenity_id foreign key.
CREATE TABLE listing_amenity (
    listing_id  BIGINT UNSIGNED   NOT NULL,
    amenity_id  SMALLINT UNSIGNED NOT NULL,
    PRIMARY KEY (listing_id, amenity_id),
    KEY idx_amenity_listing (amenity_id, listing_id),
    CONSTRAINT fk_la_listing FOREIGN KEY (listing_id)
        REFERENCES listing (listing_id) ON DELETE CASCADE,
    CONSTRAINT fk_la_amenity FOREIGN KEY (amenity_id)
        REFERENCES amenity (amenity_id)
) ENGINE=InnoDB;
