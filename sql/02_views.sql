-- ============================================================
-- 常用查询与视图
-- 执行：psql -U postgres -d floodgis -f sql/02_views.sql
-- ============================================================

-- ------------------------------------------------------------
-- 视图：易涝区（直接输出 GeoJSON，供后端接口调用）
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW v_flood_zone_geojson AS
SELECT
    id,
    name,
    risk_level,
    max_depth,
    area_m2,
    description,
    ST_AsGeoJSON(geom)::json AS geometry
FROM flood_zone;

-- ------------------------------------------------------------
-- 视图：各风险等级统计
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW v_risk_summary AS
SELECT
    risk_level,
    CASE risk_level
        WHEN 1 THEN '低'
        WHEN 2 THEN '中'
        WHEN 3 THEN '高'
        WHEN 4 THEN '极高'
        ELSE '未知'
    END AS level_name,
    COUNT(*)          AS zone_count,
    SUM(area_m2)      AS total_area_m2,
    MAX(max_depth)    AS max_depth
FROM flood_zone
GROUP BY risk_level
ORDER BY risk_level;

-- ------------------------------------------------------------
-- 查询示例：某个点落在哪些易涝区里
-- ------------------------------------------------------------
-- SELECT z.name, z.risk_level
-- FROM flood_zone z
-- WHERE ST_Contains(z.geom, ST_SetSRID(ST_Point(114.305, 30.593), 4326));

-- ------------------------------------------------------------
-- 查询示例：某范围内的易涝区（bbox 查询，前端地图移动时调用）
-- ------------------------------------------------------------
-- SELECT id, name, risk_level, ST_AsGeoJSON(geom)
-- FROM flood_zone
-- WHERE geom && ST_MakeEnvelope(114.2, 30.5, 114.4, 30.7, 4326);

-- ------------------------------------------------------------
-- 查询示例：距离某点最近的 5 个高风险区
-- ------------------------------------------------------------
-- SELECT name, risk_level,
--        ST_Distance(geom::geography, ST_Point(114.305, 30.593)::geography) AS dist_m
-- FROM flood_zone
-- WHERE risk_level >= 3
-- ORDER BY dist_m
-- LIMIT 5;

-- ------------------------------------------------------------
-- 查询示例：时间轴某一时刻的风险分布
-- ------------------------------------------------------------
-- SELECT z.name, z.risk_level, s.water_depth
-- FROM risk_snapshot s
-- JOIN flood_zone z ON z.id = s.zone_id
-- WHERE s.time_bucket = '2024-07-15 08:00:00';
