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
 * 行政边界查询接口：GET /api/admin?level=district
 *
 * <h3>它和 FloodZoneController 几乎一模一样，为什么还要单独写一个？</h3>
 * <p>因为两者职责不同：一个是"易涝区"，一个是"行政边界"。
 * 如果硬塞进一个 Controller，将来权限、缓存策略、返回字段都会互相牵扯。
 * 后端里"一个资源一个 Controller"是常规做法，重复几行代码换来的是可维护性。</p>
 *
 * <h3>这个接口的两个新东西</h3>
 * <ul>
 *   <li><b>可选的 level 过滤</b>：不传返回全部（13 个区 + 1 个市），
 *       传 {@code district} 只返回区县，传 {@code city} 只返回市级轮廓。</li>
 *   <li><b>bbox 参数先不开放</b>：边界数据才 80KB，一次全给前端更简单。
 *       等以后数据量大了（比如几万个易涝点）再加空间过滤。</li>
 * </ul>
 */
@RestController
@RequestMapping("/api/admin")
public class AdminBoundaryController {

    private final JdbcTemplate jdbc;
    private final ObjectMapper objectMapper;

    public AdminBoundaryController(JdbcTemplate jdbc, ObjectMapper objectMapper) {
        this.jdbc = jdbc;
        this.objectMapper = objectMapper;
    }

    /**
     * @param level 可选过滤条件：city=市级 / district=区县级；不传则全部返回。
     *              用 String 而不是枚举，是为了以后加"街道级"时不用改签名。
     */
    @GetMapping
    public Map<String, Object> listBoundaries(
            @RequestParam(required = false) String level) {

        // 动态拼 WHERE 的正确做法：
        //   条件用 ? 占位，参数放进 List，绝不把用户输入拼进 SQL 字符串。
        // 如果直接写成 "... WHERE level = '" + level + "'"，就是 SQL 注入漏洞。
        StringBuilder sql = new StringBuilder("""
                SELECT id,
                       name,
                       level,
                       geometry::text AS geojson
                FROM v_admin_boundary_geojson
                """);

        List<Object> params = new ArrayList<>();
        if (level != null && !level.isBlank()) {
            sql.append(" WHERE level = ?");
            params.add(level);
        }
        // 市级排前面，区县按 id 排；这样前端画图时市级轮廓先垫底
        sql.append(" ORDER BY CASE level WHEN 'city' THEN 0 ELSE 1 END, id");

        List<Map<String, Object>> rows = jdbc.queryForList(sql.toString(), params.toArray());

        List<Map<String, Object>> features = new ArrayList<>();

        for (Map<String, Object> row : rows) {
            // ---- properties：这里只有 name 和 level 两个字段，
            //      因为边界数据本身没有更多属性。以后要加人口、面积可以在这里补 ----
            Map<String, Object> properties = new LinkedHashMap<>();
            properties.put("id", row.get("id"));
            properties.put("name", row.get("name"));
            properties.put("level", row.get("level"));

            // ---- geometry：字符串 -> JsonNode，避免"字符串里套 JSON" ----
            JsonNode geometry;
            try {
                String geojsonText = (String) row.get("geojson");
                if (geojsonText == null) {
                    continue;
                }
                geometry = objectMapper.readTree(geojsonText);
            } catch (Exception e) {
                System.err.println("[admin] 解析几何失败，id=" + row.get("id")
                        + " -> " + e.getMessage());
                continue;
            }

            Map<String, Object> feature = new LinkedHashMap<>();
            feature.put("type", "Feature");
            feature.put("geometry", geometry);
            feature.put("properties", properties);
            features.add(feature);
        }

        Map<String, Object> result = new LinkedHashMap<>();
        result.put("type", "FeatureCollection");
        result.put("features", features);
        return result;
    }
}
