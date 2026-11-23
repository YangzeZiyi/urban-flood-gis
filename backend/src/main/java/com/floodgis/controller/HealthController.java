package com.floodgis.controller;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 健康检查接口：GET /api/health
 *
 * <h3>这个接口有什么用？</h3>
 * <p>它是第一周最重要的"三合一探针"，一次请求就能回答三个问题：</p>
 * <ol>
 *   <li>后端服务起来了吗？    —— 有返回就说明 Spring Boot + Tomcat 正常</li>
 *   <li>数据库连上了吗？      —— 看 database 字段是 UP 还是 DOWN</li>
 *   <li>PostGIS 装好没有？    —— 看 postgis 字段里的版本号</li>
 * </ol>
 * <p>以后写前端、写 Docker、做上线监控，都是先打这个接口确认链路通畅。</p>
 *
 * <h3>三个注解分别在干什么</h3>
 * <ul>
 *   <li>{@code @RestController} = {@code @Controller} + {@code @ResponseBody}。
 *       意思是"这个类的每个方法都直接返回数据（自动转 JSON），不要去找页面"。</li>
 *   <li>{@code @RequestMapping("/api/health")} —— 给整个类定一个 URL 前缀。</li>
 *   <li>{@code @GetMapping} —— 这个方法是 GET 请求的处理器。</li>
 * </ul>
 *
 * <h3>依赖注入是怎么回事</h3>
 * <p>类里没有 {@code new JdbcTemplate(...)}。构造方法需要它，Spring 启动时发现容器里有
 * 一个现成的 JdbcTemplate Bean，就自动塞进来。这叫"构造器注入"，是官方推荐写法：
 * 字段可以声明成 final（不可变），也方便写单元测试。</p>
 */
@RestController
@RequestMapping("/api/health")
public class HealthController {

    /** JdbcTemplate 是 Spring 提供的"手写 SQL + 自动关连接"的工具类 */
    private final JdbcTemplate jdbc;

    /** 构造器注入：Spring 看到这个构造方法，会自动传一个 JdbcTemplate 进来 */
    public HealthController(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    /**
     * 返回结构示例：
     * <pre>
     * {
     *   "service": "floodgis-backend",
     *   "status": "UP",
     *   "time": "2026-11-20T02:30:11",
     *   "database": "UP",
     *   "postgis": "3.7.0 ...",
     *   "zoneCount": 0
     * }
     * </pre>
     */
    @GetMapping
    public Map<String, Object> health() {

        // LinkedHashMap 会保持插入顺序，JSON 输出好看一点（HashMap 顺序是乱的）
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("service", "floodgis-backend");
        result.put("status", "UP");
        result.put("time", LocalDateTime.now()
                .format(DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss")));

        // 数据库检查一定要 try-catch。
        // 如果这里不捕获，数据库一挂整个接口直接 500，前端看到的就是一片红，
        // 反而不知道是"服务挂了"还是"库挂了"。
        try {
            // postgis_version() 是 PostGIS 装好后自带的函数，返回类似 "3.7 USE_GEOS=1 ..."
            String postgisVersion = jdbc.queryForObject("SELECT postgis_version()", String.class);
            result.put("database", "UP");
            result.put("postgis", postgisVersion);

            // 顺便数一下易涝区有几条，用来确认表建好且能查
            Integer count = jdbc.queryForObject("SELECT count(*) FROM flood_zone", Integer.class);
            result.put("zoneCount", count);

        } catch (Exception e) {
            result.put("database", "DOWN");
            // 只把异常类型和消息带出去，不打印完整堆栈给前端（避免泄露内部结构）
            result.put("error", e.getClass().getSimpleName() + ": " + e.getMessage());
        }

        return result;
    }
}
