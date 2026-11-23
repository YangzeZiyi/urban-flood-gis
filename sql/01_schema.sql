-- ============================================================
-- 城市内涝风险可视化平台 · 数据库结构
-- 执行前提：已创建数据库 floodgis 并启用 postgis 扩展
--   CREATE DATABASE floodgis;
--   \c floodgis
--   CREATE EXTENSION postgis;
-- 执行方式：
--   psql -U postgres -d floodgis -f sql/01_schema.sql
-- ============================================================

-- 统一使用 WGS84 经纬度（EPSG:4326）
-- 前端 Cesium 原生支持 4326，无需再转换

-- ------------------------------------------------------------
-- 1. 易涝风险区
-- ------------------------------------------------------------
DROP TABLE IF EXISTS flood_zone CASCADE;

CREATE TABLE flood_zone (
    id          BIGSERIAL PRIMARY KEY,
    name        VARCHAR(100),                -- 区域名称
    risk_level  SMALLINT NOT NULL,           -- 风险等级 1低 2中 3高 4极高
    max_depth   NUMERIC(6,2),                -- 最大积水深度（米）
    area_m2     NUMERIC(14,2),               -- 面积（平方米）
    rain_mm     NUMERIC(7,2),                -- 触发该风险的降雨量（毫米）
    description TEXT,                        -- 描述/建议
    geom        GEOMETRY(Polygon, 4326),     -- 空间几何
    created_at  TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_flood_zone_geom ON flood_zone USING GIST (geom);
CREATE INDEX idx_flood_zone_level ON flood_zone (risk_level);

COMMENT ON TABLE flood_zone IS '易涝风险区，前端按 risk_level 分级着色';

-- ------------------------------------------------------------
-- 2. 降雨站点
-- ------------------------------------------------------------
DROP TABLE IF EXISTS rain_station CASCADE;

CREATE TABLE rain_station (
    id        BIGSERIAL PRIMARY KEY,
    code      VARCHAR(50) UNIQUE,            -- 站点编号
    name      VARCHAR(100),                  -- 站点名称
    district  VARCHAR(50),                   -- 所属行政区
    geom      GEOMETRY(Point, 4326)
);

CREATE INDEX idx_rain_station_geom ON rain_station USING GIST (geom);

-- ------------------------------------------------------------
-- 3. 降雨记录
-- ------------------------------------------------------------
DROP TABLE IF EXISTS rain_record CASCADE;

CREATE TABLE rain_record (
    id          BIGSERIAL PRIMARY KEY,
    station_id  BIGINT REFERENCES rain_station(id) ON DELETE CASCADE,
    obs_time    TIMESTAMP NOT NULL,          -- 观测时刻
    rainfall_mm NUMERIC(7,2),                -- 该时段降雨量（毫米）
    data_source VARCHAR(50)                  -- 数据来源标识
);

CREATE INDEX idx_rain_record_time ON rain_record (obs_time);
CREATE INDEX idx_rain_record_station ON rain_record (station_id);

-- ------------------------------------------------------------
-- 4. 行政区（底图辅助层）
-- ------------------------------------------------------------
DROP TABLE IF EXISTS admin_boundary CASCADE;

CREATE TABLE admin_boundary (
    id       BIGSERIAL PRIMARY KEY,
    name     VARCHAR(100),
    level    VARCHAR(20),                    -- city / district
    geom     GEOMETRY(MultiPolygon, 4326)
);

CREATE INDEX idx_admin_geom ON admin_boundary USING GIST (geom);

-- ------------------------------------------------------------
-- 5. 风险快照（时间轴回放用）
-- 每个时刻一组风险区状态，前端拖时间轴时按 time_bucket 查询
-- ------------------------------------------------------------
DROP TABLE IF EXISTS risk_snapshot CASCADE;

CREATE TABLE risk_snapshot (
    id         BIGSERIAL PRIMARY KEY,
    time_bucket TIMESTAMP NOT NULL,          -- 时间片（如整点）
    zone_id    BIGINT REFERENCES flood_zone(id) ON DELETE CASCADE,
    risk_level SMALLINT,
    water_depth NUMERIC(6,2)
);

CREATE INDEX idx_snapshot_time ON risk_snapshot (time_bucket);

COMMENT ON TABLE risk_snapshot IS '时间轴回放：每个 time_bucket 对应一组风险状态';
