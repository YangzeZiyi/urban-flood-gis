package com.floodgis.controller;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 易涝区查询接口：GET /api/zones?minLevel=1
 *
 * <h3>这个接口在做什么</h3>
 * <p>把数据库里的 {@code flood_zone} 表读出来，转成前端地图能直接吃的 GeoJSON。</p>
 *
 * <h3>为什么要在后端转 GeoJSON，不直接把表丢给前端？</h3>
 * <ul>
 *   <li>数据库里的几何字段是 PostGIS 的二进制 WKB 格式，浏览器不认识</li>
 *   <li>前端拿到标准 GeoJSON 后，Cesium 一行 {@code GeoJsonDataSource.load()} 就能上图</li>
 *   <li>还能顺便做过滤（按风险等级、按范围），减少传输量</li>
 * </ul>
 *
 * <p>GeoJSON 的结构是固定的，背下来就行：</p>
 * <pre>
 * {
 *   "type": "FeatureCollection",
 *   "features": [
 *     { "type": "Feature",
 *       "geometry":   { "type": "Polygon", "coordinates": [[[114.3,30.5], ...]] },
 *       "properties": { "name": "武昌火车站下穿通道", "riskLevel": 4 } }
 *   ]
 * }
 * </pre>
 * <p>geometry 负责"画在哪"，properties 负责"是什么"。</p>
 */
@RestController
@RequestMapping("/api/zones")
public class  FloodZoneController {

    private final JdbcTemplate jdbc;
    /** ObjectMapper 是 Jackson 的核心类，负责 JSON 与 Java 对象互转。Spring 启动时已建好一个，直接注入 */
    private final ObjectMapper objectMapper;

    public FloodZoneController(JdbcTemplate jdbc, ObjectMapper objectMapper) {
        this.jdbc = jdbc;
        this.objectMapper = objectMapper;
    }

    /**
     * @param minLevel 最低风险等级（1=低 2=中 3=高 4=极高），不传默认 1，即全部返回。
     *                 {@code @RequestParam(defaultValue = "1")} 让参数可选，
     *                 访问 /api/zones 和 /api/zones?minLevel=3 都能用。
     */
    @GetMapping
    public Map<String, Object> listZones(@RequestParam(defaultValue = "1") int minLevel) {

        // 文本块（Java 15+ 的 """ 语法）比字符串拼接好读，缩进会被自动去掉。
        // 注意 SELECT 里写了 geometry::text —— 把 json 类型的字段转成字符串，
        // 这样 Java 侧拿到的是 String，再用 Jackson 解析成树，避免处理 PGobject 的麻烦。
        String sql = """
                SELECT id,
                       name,
                       risk_level,
                       max_depth,
                       area_m2,
                       description,
                       geometry::text AS geojson
                FROM v_flood_zone_geojson
                WHERE risk_level >= ?
                ORDER BY risk_level DESC, id
                """;

        // ? 是占位符，minLevel 会被安全地填进去。
        // 千万不要用字符串拼接 SQL——那是 SQL 注入漏洞的源头。
        List<Map<String, Object>> rows = jdbc.queryForList(sql, minLevel);

        List<Map<String, Object>> features = new ArrayList<>();

        for (Map<String, Object> row : rows) {
            // ---- 1. 组装 properties ----
            Map<String, Object> properties = new LinkedHashMap<>();
            properties.put("id", row.get("id"));
            properties.put("name", row.get("name"));
            properties.put("riskLevel", row.get("risk_level"));
            properties.put("maxDepth", row.get("max_depth"));
            properties.put("areaM2", row.get("area_m2"));
            properties.put("description", row.get("description"));

            // ---- 2. 解析 geometry ----
            // 数据库给的是字符串 '{"type":"Polygon","coordinates":[...]}'，
            // readTree 把它变成 JsonNode，整个 Feature 才能被 Jackson 原样序列化回去，
            // 而不会变成"字符串里套 JSON"的尴尬结构。
            JsonNode geometry;
            try {
                String geojsonText = (String) row.get("geojson");
                if (geojsonText == null) {
                    continue; // 几何为空的行直接跳过，地图上画不出来
                }
                geometry = objectMapper.readTree(geojsonText);
            } catch (Exception e) {
                // 单条数据坏了不该让整个接口 500，跳过它并在日志里能看到即可
                System.err.println("[zones] 解析几何失败，id=" + row.get("id") + " -> " + e.getMessage());
                continue;
            }

            // ---- 3. 拼成一个 Feature ----
            Map<String, Object> feature = new LinkedHashMap<>();
            feature.put("type", "Feature");
            feature.put("geometry", geometry);
            feature.put("properties", properties);
            features.add(feature);
        }

        // ---- 4. 包成 FeatureCollection ----
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("type", "FeatureCollection");
        result.put("features", features);
        return result;
    }
}
