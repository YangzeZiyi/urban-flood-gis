package com.floodgis.controller;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.floodgis.raster.FloodGrid;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 点击查询：GET /api/flood-index?lon=114.29&lat=30.59
 *
 * <h3>它解决什么问题</h3>
 * <p>前端在三维地球上点一下，拿到的是屏幕坐标；转成经纬度之后送过来，
 * 这里把经纬度换算成栅格的行列号，读出那一个格子的指数、高程、坡度、汇流，
 * 再把等级判定好一起返回。前端只管显示，不碰数据。</p>
 *
 * <h3>为什么读文件而不是查数据库</h3>
 * <p>277 万个格子的栅格，进 PostGIS 要上百万行几何、几个 G 的空间；
 * 而按坐标取一格，本质就是"算一个偏移量、读 16 个字节"。
 * 数据库在这里只增加复杂度，不带来任何好处。选型要看数据形状，不看哪个"高级"。</p>
 *
 * <h3>这个接口的三种返回</h3>
 * <ul>
 *   <li>{@code ok}      —— 正常取到值</li>
 *   <li>{@code no_data} —— 在范围内，但这一格是水体或市域外，指数为 NaN</li>
 *   <li>{@code outside} —— 经纬度落在 DEM 分幅矩形之外（比如点到隔壁城市）</li>
 * </ul>
 * <p>三种都返回 HTTP 200。"点到了没数据的地方"不是服务器错误，
 * 用 4xx/5xx 会让前端把正常交互当成故障处理。</p>
 */
@RestController
@RequestMapping("/api/flood-index")
public class FloodIndexController {

    /** 头文件候选位置：从项目根启动 / 从 backend 目录启动（IDEA 默认） */
    private static final String[] CANDIDATES = {
            "../data/processed/dem/wuhan_flood_index_grid.json",
            "data/processed/dem/wuhan_flood_index_grid.json",
    };

    private final ObjectMapper mapper = new ObjectMapper();

    /** 懒加载：第一次请求才读文件，文件缺失也不影响应用启动 */
    private volatile FloodGrid grid;
    private volatile String loadError;

    @GetMapping
    public ResponseEntity<Map<String, Object>> query(
            @RequestParam double lon,
            @RequestParam double lat) {

        FloodGrid g = grid();
        if (g == null) {
            // 503：服务暂时不可用，原因写清楚，别让前端猜
            return ResponseEntity.status(HttpStatus.SERVICE_UNAVAILABLE).body(Map.of(
                    "status", "unavailable",
                    "message", loadError == null ? "网格未加载" : loadError));
        }

        FloodGrid.Cell cell;
        try {
            cell = g.read(lon, lat);
        } catch (Exception e) {
            return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR).body(Map.of(
                    "status", "error",
                    "message", "读取网格失败：" + e.getMessage()));
        }

        if (cell == null || !cell.evaluated()) {
            Map<String, Object> body = new LinkedHashMap<>();
            body.put("status", "outside");
            body.put("lon", round(lon, 6));
            body.put("lat", round(lat, 6));
            body.put("message", "这一点不在武汉市域内，或落在水体上，不参与易涝评价");
            // 行列号仍然给出来，方便排查"到底是越界还是水体"
            if (cell != null) {
                body.put("row", cell.row());
                body.put("col", cell.col());
            }
            return ResponseEntity.ok(body);
        }

        FloodGrid.Level lv = g.levelOf(cell.index());

        Map<String, Object> body = new LinkedHashMap<>();
        body.put("status", "ok");
        body.put("lon", round(lon, 6));
        body.put("lat", round(lat, 6));
        body.put("row", cell.row());
        body.put("col", cell.col());
        body.put("index", round(cell.index(), 3));
        body.put("level", lv.name());
        body.put("color", lv.color());
        body.put("elev", round(cell.elev(), 1));
        body.put("slope", round(cell.slope(), 2));
        body.put("acc", Math.round(cell.acc()));
        return ResponseEntity.ok(body);
    }

    /**
     * 框选统计：GET /api/flood-index/stats?west=..&south=..&east=..&north=..
     *
     * <h3>和单点查询的区别</h3>
     * <p>单点是"读 16 个字节"，框选是"读一片、边读边累加"。后者才是 GIS 里
     * 最常写的代码——凡是"这块区域平均多少、各类占多少"，本质都是这个循环。</p>
     *
     * <h3>为什么不做成"多边形统计"</h3>
     * <p>那是下一步（按行政区、按流域）。这一版只做矩形，因为矩形的行列范围
     * 是连续的，一次读一大块就够；多边形要先栅格化，是另一套代码。
     * 先把"矩形能算对"跑通，再谈形状。</p>
     */
    @GetMapping("/stats")
    public ResponseEntity<Map<String, Object>> stats(
            @RequestParam double west,
            @RequestParam double south,
            @RequestParam double east,
            @RequestParam double north) {

        FloodGrid g = grid();
        if (g == null) {
            return ResponseEntity.status(HttpStatus.SERVICE_UNAVAILABLE).body(Map.of(
                    "status", "unavailable",
                    "message", loadError == null ? "网格未加载" : loadError));
        }
        if (!(west < east) || !(south < north)) {
            return ResponseEntity.badRequest().body(Map.of(
                    "status", "error",
                    "message", "经纬度顺序不对：需要 west < east 且 south < north"));
        }

        FloodGrid.Summary s;
        try {
            s = g.summarize(west, south, east, north);
        } catch (Exception e) {
            return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR).body(Map.of(
                    "status", "error",
                    "message", "统计失败：" + e.getMessage()));
        }

        if (s == null) {
            return ResponseEntity.ok(Map.of(
                    "status", "empty",
                    "message", "框选范围与网格没有交集（拖到武汉市域外了）"));
        }

        Map<String, Object> body = new LinkedHashMap<>();
        body.put("status", "ok");
        body.put("bbox", Map.of(
                "west", round(s.west(), 6),
                "south", round(s.south(), 6),
                "east", round(s.east(), 6),
                "north", round(s.north(), 6)));
        body.put("cells", s.cells());
        body.put("evaluated", s.evaluated());
        body.put("no_data", s.noData());
        body.put("area_km2", s.areaKm2());
        body.put("bbox_km2", s.bboxKm2());
        body.put("mean", s.mean());
        body.put("median", s.median());
        body.put("p90", s.p90());
        body.put("max", s.max());
        body.put("max_at", Map.of(
                "lon", round(s.maxLon(), 6),
                "lat", round(s.maxLat(), 6)));

        List<Map<String, Object>> levels = new ArrayList<>();
        for (FloodGrid.LevelStat lv : s.levels()) {
            Map<String, Object> one = new LinkedHashMap<>();
            one.put("name", lv.name());
            one.put("color", lv.color());
            one.put("km2", round(lv.km2(), 2));
            one.put("pct", round(lv.pct(), 1));
            levels.add(one);
        }
        body.put("levels", levels);
        return ResponseEntity.ok(body);
    }

    /**
     * 双检锁的懒加载。
     * 之所以不在构造函数里加载：文件可能还没生成（先跑 Python 再开后端），
     * 那种情况应该是"这个接口不可用"，而不是"整个应用起不来"。
     */
    private FloodGrid grid() {
        FloodGrid local = grid;
        if (local != null) {
            return local;
        }
        synchronized (this) {
            if (grid != null) {
                return grid;
            }
            Path json = resolveJson();
            if (json == null) {
                loadError = "找不到 wuhan_flood_index_grid.json，请先在 pipeline 目录跑："
                        + "python process/flood_index.py";
                return null;
            }
            try {
                grid = FloodGrid.load(json, mapper);
                System.out.println("[flood-index] 已加载网格 " + json.toAbsolutePath()
                        + "  尺寸 " + grid.width() + "x" + grid.height()
                        + "  级别 " + grid.levels().stream().map(FloodGrid.Level::name).toList());
                return grid;
            } catch (Exception e) {
                loadError = "网格加载失败：" + e.getMessage();
                System.err.println("[flood-index] " + loadError);
                return null;
            }
        }
    }

    private static Path resolveJson() {
        String fromEnv = System.getenv("FLOOD_GRID_JSON");
        if (fromEnv != null && !fromEnv.isBlank()) {
            Path p = Path.of(fromEnv.trim());
            return Files.exists(p) ? p : null;
        }
        for (String c : CANDIDATES) {
            Path p = Path.of(c);
            if (Files.exists(p)) {
                return p.toAbsolutePath().normalize();
            }
        }
        return null;
    }

    /** 保留 n 位小数。double 直接返回给 JSON 会出现 0.48799999999999999 这种。 */
    private static double round(double v, int digits) {
        double f = Math.pow(10, digits);
        return Math.round(v * f) / f;
    }
}
